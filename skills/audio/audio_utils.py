"""
audio_utils.py — Audio format conversion, chunking, and silence detection.

All functions are pure Python with optional ffmpeg for format conversion.
No external Python dependencies are required for the core utilities.
"""

from __future__ import annotations

import io
import os
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path
from typing import Iterator, Optional


SUPPORTED_INPUT_FORMATS = {".wav", ".mp3", ".ogg", ".flac", ".m4a", ".webm"}
SUPPORTED_OUTPUT_FORMATS = {".wav", ".mp3", ".ogg", ".flac"}


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def detect_format(file_path: str) -> str:
    """
    Return the audio format of the file based on its extension.
    Returns a lowercase string like 'wav', 'mp3', etc.
    Returns 'unknown' when the file has no extension.
    """
    ext = Path(file_path).suffix.lower().lstrip(".")
    return ext if ext else "unknown"


def is_supported_format(file_path: str) -> bool:
    """Return True if the file extension is in SUPPORTED_INPUT_FORMATS."""
    ext = Path(file_path).suffix.lower()
    return ext in SUPPORTED_INPUT_FORMATS


# ---------------------------------------------------------------------------
# WAV introspection (pure Python, no deps)
# ---------------------------------------------------------------------------

def get_wav_info(file_path: str) -> dict:
    """
    Read basic properties from a WAV file without external dependencies.
    Returns a dict with keys: channels, sample_rate, sample_width, n_frames, duration_seconds.
    Returns {"error": ...} if the file cannot be read.
    """
    try:
        with wave.open(file_path, "rb") as wf:
            channels = wf.getnchannels()
            sample_rate = wf.getframerate()
            sample_width = wf.getsampwidth()
            n_frames = wf.getnframes()
            duration = n_frames / sample_rate if sample_rate > 0 else 0.0
            return {
                "channels": channels,
                "sample_rate": sample_rate,
                "sample_width_bytes": sample_width,
                "bits_per_sample": sample_width * 8,
                "n_frames": n_frames,
                "duration_seconds": round(duration, 3),
            }
    except Exception as exc:
        return {"error": str(exc)}


# ---------------------------------------------------------------------------
# Format conversion (requires ffmpeg)
# ---------------------------------------------------------------------------

def convert_audio(
    input_path: str,
    output_path: str,
    sample_rate: Optional[int] = None,
    channels: Optional[int] = None,
) -> dict:
    """
    Convert audio from one format to another using ffmpeg.

    Args:
        input_path:  Source file path.
        output_path: Destination file path (format inferred from extension).
        sample_rate: Optional target sample rate (e.g. 16000 for Whisper).
        channels:    Optional number of output channels (1=mono, 2=stereo).

    Returns:
        {"output_path": str} on success or {"error": str} on failure.
    """
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return {"error": "ffmpeg not found in PATH. Install ffmpeg to enable format conversion."}

    if not Path(input_path).exists():
        return {"error": f"Input file not found: {input_path}"}

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    cmd = [ffmpeg, "-y", "-i", input_path]
    if sample_rate:
        cmd += ["-ar", str(sample_rate)]
    if channels:
        cmd += ["-ac", str(channels)]
    cmd.append(output_path)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"error": f"ffmpeg failed (exit {result.returncode}): {result.stderr.strip()}"}
        return {"output_path": output_path}
    except subprocess.TimeoutExpired:
        return {"error": "ffmpeg timed out after 120s"}
    except Exception as exc:
        return {"error": str(exc)}


def to_wav_16k_mono(input_path: str, output_path: Optional[str] = None) -> dict:
    """
    Convert any supported audio file to 16kHz mono WAV.
    Convenience wrapper for convert_audio — the format required by most STT backends.

    If output_path is None, a temp file is created.
    Returns {"output_path": str} or {"error": str}.
    """
    if output_path is None:
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp.close()
        output_path = tmp.name

    return convert_audio(input_path, output_path, sample_rate=16000, channels=1)


# ---------------------------------------------------------------------------
# Audio chunking
# ---------------------------------------------------------------------------

def chunk_wav(
    file_path: str,
    chunk_duration_seconds: float = 30.0,
    output_dir: Optional[str] = None,
) -> Iterator[str]:
    """
    Split a WAV file into fixed-duration chunks for streaming transcription.

    Yields absolute paths to chunk files (temporary unless output_dir is given).
    The caller is responsible for deleting yielded files after use.

    Args:
        file_path:               Input WAV path.
        chunk_duration_seconds:  Max duration per chunk (default 30s for Whisper).
        output_dir:              Directory for chunk files; uses /tmp if None.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {file_path}")

    out_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)

    with wave.open(str(path), "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        frames_per_chunk = int(sample_rate * chunk_duration_seconds)

        chunk_idx = 0
        while True:
            frames = wf.readframes(frames_per_chunk)
            if not frames:
                break

            chunk_path = out_dir / f"{path.stem}_chunk{chunk_idx:04d}.wav"
            with wave.open(str(chunk_path), "wb") as cw:
                cw.setnchannels(channels)
                cw.setsampwidth(sample_width)
                cw.setframerate(sample_rate)
                cw.writeframes(frames)

            yield str(chunk_path)
            chunk_idx += 1


# ---------------------------------------------------------------------------
# Silence detection (pure Python, WAV only)
# ---------------------------------------------------------------------------

def detect_silence(
    file_path: str,
    threshold_db: float = -40.0,
    min_silence_ms: int = 500,
) -> list[dict]:
    """
    Detect silent regions in a WAV file without external dependencies.

    Uses simple RMS energy per frame window.

    Args:
        file_path:       WAV file path.
        threshold_db:    dB level below which audio is considered silent (default -40 dB).
        min_silence_ms:  Minimum duration (ms) to classify as a silence region.

    Returns:
        List of {"start_ms": int, "end_ms": int, "duration_ms": int} dicts.
        Returns [{"error": str}] on failure.
    """
    import math

    path = Path(file_path)
    if not path.exists():
        return [{"error": f"File not found: {file_path}"}]

    try:
        with wave.open(str(path), "rb") as wf:
            sample_rate = wf.getframerate()
            sample_width = wf.getsampwidth()
            channels = wf.getnchannels()
            n_frames = wf.getnframes()
            raw = wf.readframes(n_frames)
    except Exception as exc:
        return [{"error": str(exc)}]

    # Parse samples (16-bit signed only; other widths skipped gracefully)
    if sample_width == 2:
        fmt = f"<{len(raw) // 2}h"
        samples = struct.unpack(fmt, raw)
        # Mix down to mono by averaging channels
        if channels > 1:
            mono = [
                sum(samples[i : i + channels]) / channels
                for i in range(0, len(samples), channels)
            ]
        else:
            mono = list(samples)
        max_amplitude = 32768.0
    elif sample_width == 1:
        mono = [b - 128 for b in raw[::channels]]
        max_amplitude = 128.0
    else:
        return [{"error": f"Unsupported sample width: {sample_width} bytes. Only 8-bit and 16-bit WAV supported."}]

    threshold_linear = max_amplitude * (10 ** (threshold_db / 20.0))

    # Window size: 20ms
    window_size = max(1, int(sample_rate * 0.02))
    silence_regions: list[dict] = []

    in_silence = False
    silence_start_ms = 0

    for idx in range(0, len(mono), window_size):
        window = mono[idx : idx + window_size]
        rms = math.sqrt(sum(s * s for s in window) / len(window))
        ts_ms = int(idx / sample_rate * 1000)

        if rms < threshold_linear:
            if not in_silence:
                in_silence = True
                silence_start_ms = ts_ms
        else:
            if in_silence:
                in_silence = False
                duration_ms = ts_ms - silence_start_ms
                if duration_ms >= min_silence_ms:
                    silence_regions.append({
                        "start_ms": silence_start_ms,
                        "end_ms": ts_ms,
                        "duration_ms": duration_ms,
                    })

    # Close any trailing silence
    if in_silence:
        end_ms = int(len(mono) / sample_rate * 1000)
        duration_ms = end_ms - silence_start_ms
        if duration_ms >= min_silence_ms:
            silence_regions.append({
                "start_ms": silence_start_ms,
                "end_ms": end_ms,
                "duration_ms": duration_ms,
            })

    return silence_regions


# ---------------------------------------------------------------------------
# Streaming transcription helper
# ---------------------------------------------------------------------------

def iter_transcript_chunks(
    file_path: str,
    chunk_duration_seconds: float = 30.0,
) -> Iterator[dict]:
    """
    Yield {"chunk_index": int, "chunk_path": str} for each audio chunk
    produced by splitting file_path.

    Designed to be consumed by the transcribe_audio streaming path.
    Caller must delete chunk_path after processing.
    """
    for idx, chunk_path in enumerate(
        chunk_wav(file_path, chunk_duration_seconds=chunk_duration_seconds)
    ):
        yield {"chunk_index": idx, "chunk_path": chunk_path}
