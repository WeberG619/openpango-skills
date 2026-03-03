#!/usr/bin/env python3
"""
voice_handler.py — Main interface for the audio skill.

Provides two primary tools:
    transcribe_audio(file_path, language=None, backend=None) -> dict
    generate_speech(text, voice_id=None, output_path=None, backend=None) -> dict

Also exposes a CLI:
    python3 voice_handler.py transcribe /path/to/audio.wav
    python3 voice_handler.py speak "Hello world" --voice en-US-AriaNeural --out /tmp/hello.mp3
    python3 voice_handler.py backends
    python3 voice_handler.py info /path/to/audio.wav
    python3 voice_handler.py chunk /path/to/audio.wav --duration 30
    python3 voice_handler.py silence /path/to/audio.wav
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Iterator, Optional

from .stt_engine import get_stt_backend, _ALL_STT_BACKENDS
from .tts_engine import get_tts_backend, _ALL_TTS_BACKENDS
from .audio_utils import (
    chunk_wav,
    detect_silence,
    get_wav_info,
    is_supported_format,
    iter_transcript_chunks,
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def transcribe_audio(
    file_path: str,
    language: Optional[str] = None,
    backend: Optional[str] = None,
    stream: bool = False,
    chunk_duration_seconds: float = 30.0,
) -> dict:
    """
    Transcribe an audio file to text.

    Args:
        file_path:               Path to audio file (WAV, MP3, OGG, FLAC).
        language:                Optional BCP-47 language code (e.g. "en", "es").
        backend:                 Force a specific backend: "whisper_api", "whisper_cpp",
                                 "google", or "mock". Auto-selected if None.
        stream:                  If True, split file into chunks and transcribe each
                                 piece in sequence (useful for long recordings).
        chunk_duration_seconds:  Chunk length when streaming (default 30s).

    Returns:
        On success: {"text": str, "backend": str, "language": str | None}
        On failure: {"error": str, "backend": str}
    """
    path = Path(file_path)

    if not path.exists():
        return {"error": f"File not found: {file_path}", "backend": backend or "unknown"}

    if not is_supported_format(file_path):
        return {
            "error": (
                f"Unsupported format: {path.suffix}. "
                "Supported: .wav .mp3 .ogg .flac .m4a .webm"
            ),
            "backend": backend or "unknown",
        }

    try:
        stt = get_stt_backend(backend)
    except ValueError as exc:
        return {"error": str(exc), "backend": backend or "unknown"}

    if not stream:
        return stt.transcribe(file_path, language)

    # Streaming path: chunk the file, transcribe each chunk, combine
    if path.suffix.lower() != ".wav":
        return {
            "error": "Streaming (chunk-based) transcription requires a WAV input file. "
                     "Convert to WAV first using audio_utils.to_wav_16k_mono().",
            "backend": stt.name,
        }

    all_text: list[str] = []
    chunks_processed = 0
    last_error: Optional[str] = None

    for item in iter_transcript_chunks(file_path, chunk_duration_seconds):
        chunk_path = item["chunk_path"]
        try:
            result = stt.transcribe(chunk_path, language)
            if "error" in result:
                last_error = result["error"]
            else:
                all_text.append(result.get("text", ""))
                chunks_processed += 1
        finally:
            Path(chunk_path).unlink(missing_ok=True)

    if not all_text and last_error:
        return {"error": last_error, "backend": stt.name}

    return {
        "text": " ".join(all_text).strip(),
        "backend": stt.name,
        "language": language,
        "chunks_processed": chunks_processed,
    }


def generate_speech(
    text: str,
    voice_id: Optional[str] = None,
    output_path: Optional[str] = None,
    backend: Optional[str] = None,
) -> dict:
    """
    Synthesize text to an audio file.

    Args:
        text:         The text to convert to speech.
        voice_id:     Voice identifier. Meaning depends on backend:
                      - ElevenLabs: voice ID string
                      - Edge TTS: voice name like "en-US-AriaNeural"
                      - pyttsx3: voice ID string from engine.getProperty("voices")
        output_path:  Where to write the audio. Auto-generated temp file if None.
        backend:      Force a specific backend: "elevenlabs", "edge_tts", "pyttsx3", "mock".
                      Auto-selected if None.

    Returns:
        On success: {"audio_path": str, "backend": str, "voice_id": str | None}
        On failure: {"error": str, "backend": str}
    """
    if not text or not text.strip():
        return {"error": "text must not be empty.", "backend": backend or "unknown"}

    try:
        tts = get_tts_backend(backend)
    except ValueError as exc:
        return {"error": str(exc), "backend": backend or "unknown"}

    return tts.synthesize(text, voice_id=voice_id, output_path=output_path)


def list_backends() -> dict:
    """
    Return availability info for all STT and TTS backends.

    Returns:
        {
            "stt": [{"name": str, "available": bool}, ...],
            "tts": [{"name": str, "available": bool}, ...],
        }
    """
    return {
        "stt": [
            {"name": b.name, "available": b.is_available()}
            for b in _ALL_STT_BACKENDS
        ],
        "tts": [
            {"name": b.name, "available": b.is_available()}
            for b in _ALL_TTS_BACKENDS
        ],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Audio skill — transcribe audio and generate speech.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 voice_handler.py transcribe audio.wav
  python3 voice_handler.py transcribe audio.mp3 --language es --backend whisper_api
  python3 voice_handler.py transcribe long.wav --stream --chunk-duration 20
  python3 voice_handler.py speak "Hello, world!"
  python3 voice_handler.py speak "Hola" --voice es-ES-AlvaroNeural --out /tmp/hola.mp3
  python3 voice_handler.py backends
  python3 voice_handler.py info audio.wav
  python3 voice_handler.py silence audio.wav
  python3 voice_handler.py chunk audio.wav --duration 15 --out-dir /tmp/chunks/
        """,
    )
    sub = p.add_subparsers(dest="command", required=True)

    # transcribe
    t = sub.add_parser("transcribe", help="Transcribe an audio file to text.")
    t.add_argument("file", help="Path to the audio file.")
    t.add_argument("--language", "-l", default=None, help="BCP-47 language code (e.g. en, es).")
    t.add_argument(
        "--backend",
        choices=["whisper_api", "whisper_cpp", "google", "mock"],
        default=None,
        help="Force a specific STT backend.",
    )
    t.add_argument("--stream", action="store_true", help="Chunk and stream long WAV files.")
    t.add_argument(
        "--chunk-duration",
        type=float,
        default=30.0,
        help="Chunk duration in seconds for --stream mode (default 30).",
    )

    # speak
    s = sub.add_parser("speak", help="Generate speech from text.")
    s.add_argument("text", help="Text to synthesize.")
    s.add_argument("--voice", "-v", default=None, help="Voice ID or name.")
    s.add_argument("--out", "-o", default=None, help="Output audio file path.")
    s.add_argument(
        "--backend",
        choices=["elevenlabs", "edge_tts", "pyttsx3", "mock"],
        default=None,
        help="Force a specific TTS backend.",
    )

    # backends
    sub.add_parser("backends", help="List all STT/TTS backends and their availability.")

    # info
    i = sub.add_parser("info", help="Show WAV file properties.")
    i.add_argument("file", help="Path to a WAV file.")

    # silence
    sl = sub.add_parser("silence", help="Detect silent regions in a WAV file.")
    sl.add_argument("file", help="Path to a WAV file.")
    sl.add_argument("--threshold", type=float, default=-40.0, help="Silence threshold in dB (default -40).")
    sl.add_argument("--min-ms", type=int, default=500, help="Minimum silence duration in ms (default 500).")

    # chunk
    c = sub.add_parser("chunk", help="Split a WAV file into fixed-duration chunks.")
    c.add_argument("file", help="Path to a WAV file.")
    c.add_argument("--duration", type=float, default=30.0, help="Chunk duration in seconds (default 30).")
    c.add_argument("--out-dir", default=None, help="Output directory for chunks.")

    return p


def main(argv: Optional[list[str]] = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "transcribe":
        result = transcribe_audio(
            args.file,
            language=args.language,
            backend=args.backend,
            stream=args.stream,
            chunk_duration_seconds=args.chunk_duration,
        )
        _print_json(result)
        if "error" in result:
            sys.exit(1)

    elif args.command == "speak":
        result = generate_speech(
            args.text,
            voice_id=args.voice,
            output_path=args.out,
            backend=args.backend,
        )
        _print_json(result)
        if "error" in result:
            sys.exit(1)

    elif args.command == "backends":
        _print_json(list_backends())

    elif args.command == "info":
        _print_json(get_wav_info(args.file))

    elif args.command == "silence":
        regions = detect_silence(
            args.file,
            threshold_db=args.threshold,
            min_silence_ms=args.min_ms,
        )
        _print_json(regions)

    elif args.command == "chunk":
        chunks = list(chunk_wav(args.file, args.duration, output_dir=args.out_dir))
        _print_json({"chunks": chunks, "count": len(chunks)})


def _print_json(data) -> None:
    print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
