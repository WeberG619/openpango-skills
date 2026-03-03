#!/usr/bin/env python3
"""
test_voice.py — Tests for the audio skill.

Run from the project root:
    python3 -m pytest skills/audio/test_voice.py -v
    # or directly:
    python3 skills/audio/test_voice.py
"""

from __future__ import annotations

import json
import struct
import sys
import tempfile
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

# Allow running as a script from any directory
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from skills.audio.voice_handler import generate_speech, list_backends, transcribe_audio
from skills.audio.stt_engine import (
    MockSTTBackend,
    WhisperAPIBackend,
    WhisperCppBackend,
    get_stt_backend,
)
from skills.audio.tts_engine import (
    MockTTSBackend,
    EdgeTTSBackend,
    get_tts_backend,
)
from skills.audio.audio_utils import (
    chunk_wav,
    detect_format,
    detect_silence,
    get_wav_info,
    is_supported_format,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_wav(path: Path, duration_seconds: float = 1.0, sample_rate: int = 16000) -> Path:
    """Create a minimal sine-wave WAV file for testing."""
    import math

    n_frames = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        # Simple 440 Hz sine wave
        samples = [
            int(32767 * math.sin(2 * math.pi * 440 * i / sample_rate))
            for i in range(n_frames)
        ]
        wf.writeframes(struct.pack(f"<{n_frames}h", *samples))
    return path


def _make_silent_wav(path: Path, duration_seconds: float = 2.0, sample_rate: int = 16000) -> Path:
    """Create a silent WAV (all zeros)."""
    n_frames = int(sample_rate * duration_seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)
    return path


# ---------------------------------------------------------------------------
# Test class
# ---------------------------------------------------------------------------

class TestAudioUtils:
    def test_detect_format(self):
        assert detect_format("/tmp/audio.wav") == "wav"
        assert detect_format("/tmp/audio.MP3") == "mp3"
        assert detect_format("/tmp/audio.FLAC") == "flac"
        assert detect_format("/tmp/no_ext") == "unknown"

    def test_is_supported_format(self):
        assert is_supported_format("/tmp/test.wav") is True
        assert is_supported_format("/tmp/test.mp3") is True
        assert is_supported_format("/tmp/test.txt") is False
        assert is_supported_format("/tmp/test.pdf") is False

    def test_get_wav_info(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "test.wav", duration_seconds=0.5)
            info = get_wav_info(str(wav_path))
            assert "error" not in info, f"Unexpected error: {info}"
            assert info["channels"] == 1
            assert info["sample_rate"] == 16000
            assert info["bits_per_sample"] == 16
            assert info["duration_seconds"] == 0.5

    def test_get_wav_info_missing_file(self):
        info = get_wav_info("/nonexistent/file.wav")
        assert "error" in info

    def test_chunk_wav(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "long.wav", duration_seconds=3.0)
            chunks = list(chunk_wav(str(wav_path), chunk_duration_seconds=1.0, output_dir=tmpdir))
            assert len(chunks) == 3, f"Expected 3 chunks, got {len(chunks)}"
            for c in chunks:
                assert Path(c).exists()
                info = get_wav_info(c)
                assert "error" not in info
                # Each chunk should be ~1 second
                assert abs(info["duration_seconds"] - 1.0) < 0.05

    def test_chunk_wav_missing_file(self):
        try:
            list(chunk_wav("/nonexistent/file.wav"))
            assert False, "Should have raised FileNotFoundError"
        except FileNotFoundError:
            pass

    def test_detect_silence_silent_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_silent_wav(Path(tmpdir) / "silent.wav", duration_seconds=2.0)
            regions = detect_silence(str(wav_path), threshold_db=-40.0, min_silence_ms=500)
            assert isinstance(regions, list)
            # A fully silent 2s file should produce at least one silence region
            assert len(regions) >= 1, f"Expected silence regions, got: {regions}"
            assert "error" not in regions[0]

    def test_detect_silence_missing_file(self):
        regions = detect_silence("/nonexistent.wav")
        assert len(regions) == 1
        assert "error" in regions[0]


class TestSTTEngine:
    def test_mock_backend_available(self):
        mock = MockSTTBackend()
        assert mock.is_available() is True

    def test_mock_transcription(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "test.wav")
            mock = MockSTTBackend()
            result = mock.transcribe(str(wav_path))
            assert "error" not in result
            assert "text" in result
            assert "MOCK" in result["text"]
            assert result["backend"] == "mock"

    def test_mock_transcription_missing_file(self):
        mock = MockSTTBackend()
        result = mock.transcribe("/nonexistent/audio.wav")
        assert "error" in result

    def test_get_stt_backend_returns_mock_when_no_keys(self, monkeypatch=None):
        """When no API keys are set, get_stt_backend() should return MockSTTBackend."""
        import os
        saved = {k: os.environ.pop(k, None) for k in ["OPENAI_API_KEY", "GOOGLE_STT_KEY", "WHISPER_BINARY"]}
        # Also clear PATH-based whisper binary by temporarily patching shutil.which
        import shutil
        original_which = shutil.which

        def no_whisper(name, **kwargs):
            if name in ("whisper", "main"):
                return None
            return original_which(name, **kwargs)

        shutil.which = no_whisper
        try:
            backend = get_stt_backend()
            assert backend.name == "mock"
        finally:
            shutil.which = original_which
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v

    def test_get_stt_backend_by_name(self):
        backend = get_stt_backend("mock")
        assert backend.name == "mock"

    def test_get_stt_backend_invalid_name(self):
        try:
            get_stt_backend("nonexistent_backend")
            assert False, "Should have raised ValueError"
        except ValueError as exc:
            assert "nonexistent_backend" in str(exc)

    def test_whisper_api_unavailable_without_key(self, monkeypatch=None):
        import os
        key = os.environ.pop("OPENAI_API_KEY", None)
        try:
            backend = WhisperAPIBackend()
            assert backend.is_available() is False
        finally:
            if key is not None:
                os.environ["OPENAI_API_KEY"] = key

    def test_whisper_cpp_unavailable_without_binary(self):
        import shutil
        original = shutil.which

        def no_bin(name, **kwargs):
            return None

        shutil.which = no_bin
        try:
            backend = WhisperCppBackend()
            assert backend.is_available() is False
        finally:
            shutil.which = original


class TestTTSEngine:
    def test_mock_backend_available(self):
        mock = MockTTSBackend()
        assert mock.is_available() is True

    def test_mock_synthesis(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = str(Path(tmpdir) / "out.wav")
            mock = MockTTSBackend()
            result = mock.synthesize("Hello world", output_path=out)
            assert "error" not in result
            assert "audio_path" in result
            assert Path(result["audio_path"]).exists()
            # Should be a valid WAV
            info = get_wav_info(result["audio_path"])
            assert "error" not in info

    def test_get_tts_backend_by_name(self):
        backend = get_tts_backend("mock")
        assert backend.name == "mock"

    def test_get_tts_backend_invalid_name(self):
        try:
            get_tts_backend("nonexistent_backend")
            assert False, "Should have raised ValueError"
        except ValueError as exc:
            assert "nonexistent_backend" in str(exc)

    def test_edge_tts_availability_check(self):
        """EdgeTTSBackend.is_available() returns True iff edge-tts is installed."""
        import importlib.util
        backend = EdgeTTSBackend()
        expected = importlib.util.find_spec("edge_tts") is not None
        assert backend.is_available() == expected


class TestVoiceHandler:
    def test_transcribe_audio_mock(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "test.wav")
            result = transcribe_audio(str(wav_path), backend="mock")
            assert "error" not in result, f"Unexpected error: {result}"
            assert "text" in result
            assert result["backend"] == "mock"

    def test_transcribe_audio_missing_file(self):
        result = transcribe_audio("/nonexistent/file.wav", backend="mock")
        assert "error" in result

    def test_transcribe_audio_unsupported_format(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create an actual file with an unsupported extension
            bad_path = str(Path(tmpdir) / "test.pdf")
            Path(bad_path).write_text("not audio")
            result = transcribe_audio(bad_path, backend="mock")
            assert "error" in result
            assert "Unsupported format" in result["error"]

    def test_transcribe_audio_with_language(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "test.wav")
            result = transcribe_audio(str(wav_path), language="es", backend="mock")
            assert "error" not in result
            assert result["language"] == "es"

    def test_transcribe_audio_streaming(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "long.wav", duration_seconds=3.0)
            result = transcribe_audio(
                str(wav_path),
                backend="mock",
                stream=True,
                chunk_duration_seconds=1.0,
            )
            assert "error" not in result, f"Unexpected error: {result}"
            assert result["chunks_processed"] == 3

    def test_transcribe_audio_empty_text(self):
        result = transcribe_audio("")
        assert "error" in result

    def test_generate_speech_mock(self):
        result = generate_speech("Hello, world!", backend="mock")
        assert "error" not in result, f"Unexpected error: {result}"
        assert "audio_path" in result
        assert Path(result["audio_path"]).exists()
        assert result["backend"] == "mock"

    def test_generate_speech_with_output_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            out = str(Path(tmpdir) / "speech.wav")
            result = generate_speech("Test", output_path=out, backend="mock")
            assert "error" not in result
            assert result["audio_path"] == out
            assert Path(out).exists()

    def test_generate_speech_empty_text(self):
        result = generate_speech("", backend="mock")
        assert "error" in result
        assert "empty" in result["error"].lower()

    def test_generate_speech_whitespace_only(self):
        result = generate_speech("   ", backend="mock")
        assert "error" in result

    def test_list_backends(self):
        result = list_backends()
        assert "stt" in result
        assert "tts" in result
        assert isinstance(result["stt"], list)
        assert isinstance(result["tts"], list)
        # Mock backends should always be available
        stt_names = {b["name"] for b in result["stt"]}
        tts_names = {b["name"] for b in result["tts"]}
        assert "mock" in stt_names
        assert "mock" in tts_names

    def test_list_backends_structure(self):
        result = list_backends()
        for b in result["stt"] + result["tts"]:
            assert "name" in b
            assert "available" in b
            assert isinstance(b["available"], bool)

    def test_generate_speech_invalid_backend(self):
        result = generate_speech("Hello", backend="nonexistent")
        assert "error" in result

    def test_transcribe_audio_invalid_backend(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            wav_path = _make_wav(Path(tmpdir) / "test.wav")
            result = transcribe_audio(str(wav_path), backend="nonexistent")
            assert "error" in result


# ---------------------------------------------------------------------------
# Simple test runner (no pytest dependency required)
# ---------------------------------------------------------------------------

def _run_tests() -> None:
    """Run all test methods and report results."""
    test_classes = [TestAudioUtils, TestSTTEngine, TestTTSEngine, TestVoiceHandler]
    passed = 0
    failed = 0
    errors: list[str] = []

    for cls in test_classes:
        instance = cls()
        methods = [m for m in dir(cls) if m.startswith("test_")]
        for method_name in methods:
            try:
                getattr(instance, method_name)()
                print(f"  PASS  {cls.__name__}.{method_name}")
                passed += 1
            except Exception as exc:
                print(f"  FAIL  {cls.__name__}.{method_name}: {exc}")
                failed += 1
                errors.append(f"{cls.__name__}.{method_name}: {exc}")

    total = passed + failed
    print(f"\n{'='*60}")
    print(f"Results: {passed}/{total} passed", end="")
    if failed:
        print(f", {failed} failed")
        for e in errors:
            print(f"  - {e}")
        sys.exit(1)
    else:
        print()
        print("All tests passed.")


if __name__ == "__main__":
    _run_tests()
