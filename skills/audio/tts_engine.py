"""
tts_engine.py — Text-to-speech backends.

Backend priority:
    1. ElevenLabs API  (ELEVENLABS_API_KEY)
    2. Edge TTS        (edge-tts python package, no API key needed)
    3. pyttsx3         (local system TTS, if installed)
    4. Mock            (always available, writes a placeholder file)

All backends implement the same interface:
    synthesize(text, voice_id, output_path) -> dict
        Returns {"audio_path": str, "backend": str, "voice_id": str | None}
        On failure returns {"error": str, "backend": str}
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Backend 1: ElevenLabs API
# ---------------------------------------------------------------------------

class ElevenLabsBackend:
    """TTS via ElevenLabs REST API."""

    name = "elevenlabs"
    DEFAULT_VOICE = "21m00Tcm4TlvDq8ikWAM"  # Rachel
    ENDPOINT_TMPL = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

    def is_available(self) -> bool:
        return bool(os.environ.get("ELEVENLABS_API_KEY"))

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> dict:
        api_key = os.environ["ELEVENLABS_API_KEY"]
        vid = (
            voice_id
            or os.environ.get("ELEVENLABS_VOICE_ID")
            or self.DEFAULT_VOICE
        )
        url = self.ENDPOINT_TMPL.format(voice_id=vid)

        payload = {
            "text": text,
            "model_id": "eleven_monolingual_v1",
            "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode(),
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                audio_bytes = resp.read()

            out = _resolve_output_path(output_path, suffix=".mp3")
            out.write_bytes(audio_bytes)
            return {"audio_path": str(out), "backend": self.name, "voice_id": vid}

        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            return {"error": f"ElevenLabs HTTP {exc.code}: {body_text}", "backend": self.name}
        except Exception as exc:
            return {"error": str(exc), "backend": self.name}


# ---------------------------------------------------------------------------
# Backend 2: Edge TTS (Microsoft neural voices, free)
# ---------------------------------------------------------------------------

class EdgeTTSBackend:
    """TTS via the edge-tts Python package (no API key required)."""

    name = "edge_tts"
    DEFAULT_VOICE = "en-US-AriaNeural"

    def is_available(self) -> bool:
        return importlib.util.find_spec("edge_tts") is not None

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> dict:
        import asyncio
        try:
            import edge_tts  # noqa: F401
        except ImportError:
            return {"error": "edge-tts not installed. Run: pip install edge-tts", "backend": self.name}

        voice = voice_id or self.DEFAULT_VOICE
        out = _resolve_output_path(output_path, suffix=".mp3")

        async def _run():
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(out))

        try:
            asyncio.run(_run())
            return {"audio_path": str(out), "backend": self.name, "voice_id": voice}
        except Exception as exc:
            return {"error": str(exc), "backend": self.name}


# ---------------------------------------------------------------------------
# Backend 3: pyttsx3 (offline system TTS)
# ---------------------------------------------------------------------------

class Pyttsx3Backend:
    """TTS via pyttsx3 (offline, uses OS speech engine)."""

    name = "pyttsx3"

    def is_available(self) -> bool:
        return importlib.util.find_spec("pyttsx3") is not None

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> dict:
        try:
            import pyttsx3
        except ImportError:
            return {"error": "pyttsx3 not installed. Run: pip install pyttsx3", "backend": self.name}

        out = _resolve_output_path(output_path, suffix=".wav")

        try:
            engine = pyttsx3.init()

            # Set voice if specified (voice_id is the pyttsx3 voice ID string)
            if voice_id:
                engine.setProperty("voice", voice_id)

            effective_voice = engine.getProperty("voice")
            engine.save_to_file(text, str(out))
            engine.runAndWait()
            engine.stop()

            if not out.exists() or out.stat().st_size == 0:
                return {"error": "pyttsx3 produced no output file.", "backend": self.name}

            return {"audio_path": str(out), "backend": self.name, "voice_id": effective_voice}
        except Exception as exc:
            return {"error": str(exc), "backend": self.name}


# ---------------------------------------------------------------------------
# Backend 4: Mock
# ---------------------------------------------------------------------------

class MockTTSBackend:
    """Mock backend — always available, writes a minimal silent WAV for testing."""

    name = "mock"

    # Minimal valid WAV: 44-byte header, 0 data bytes (44100 Hz, 16-bit, mono, 0 samples)
    _SILENT_WAV = (
        b"RIFF$\x00\x00\x00WAVEfmt \x10\x00\x00\x00"
        b"\x01\x00\x01\x00\x44\xac\x00\x00\x88X\x01\x00\x02\x00\x10\x00"
        b"data\x00\x00\x00\x00"
    )

    def is_available(self) -> bool:
        return True

    def synthesize(
        self,
        text: str,
        voice_id: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> dict:
        out = _resolve_output_path(output_path, suffix=".wav")
        out.write_bytes(self._SILENT_WAV)
        return {
            "audio_path": str(out),
            "backend": self.name,
            "voice_id": voice_id or "mock-voice",
            "note": f"[MOCK] Would speak: {text!r}",
        }


# ---------------------------------------------------------------------------
# Backend registry & selector
# ---------------------------------------------------------------------------

_ALL_TTS_BACKENDS = [
    ElevenLabsBackend(),
    EdgeTTSBackend(),
    Pyttsx3Backend(),
    MockTTSBackend(),
]


def get_tts_backend(name: Optional[str] = None):
    """
    Return the first available TTS backend.
    If `name` is specified, return that backend (or raise ValueError).
    """
    if name:
        for b in _ALL_TTS_BACKENDS:
            if b.name == name:
                if not b.is_available():
                    raise ValueError(
                        f"TTS backend '{name}' is not available (missing package/config)."
                    )
                return b
        raise ValueError(
            f"Unknown TTS backend '{name}'. Valid: {[b.name for b in _ALL_TTS_BACKENDS]}"
        )

    for b in _ALL_TTS_BACKENDS:
        if b.is_available():
            return b

    # MockTTSBackend is always available so this should never happen
    raise RuntimeError("No TTS backend available — this should not happen.")


def list_edge_tts_voices() -> list[dict]:
    """
    Return a list of available Edge TTS voices.
    Requires edge-tts to be installed.
    """
    try:
        import asyncio
        import edge_tts

        async def _get():
            voices = await edge_tts.list_voices()
            return voices

        return asyncio.run(_get())
    except Exception as exc:
        return [{"error": str(exc)}]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_output_path(output_path: Optional[str], suffix: str) -> Path:
    """Return a Path for the output file, creating a temp file if none provided."""
    if output_path:
        p = Path(output_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    output_dir = os.environ.get("AUDIO_OUTPUT_DIR")
    if output_dir:
        import uuid
        d = Path(output_dir)
        d.mkdir(parents=True, exist_ok=True)
        return d / f"speech_{uuid.uuid4().hex[:8]}{suffix}"

    tmp = tempfile.NamedTemporaryFile(suffix=suffix, delete=False)
    tmp.close()
    return Path(tmp.name)
