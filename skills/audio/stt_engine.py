"""
stt_engine.py — Speech-to-text backends.

Backend priority:
    1. OpenAI Whisper API  (OPENAI_API_KEY)
    2. Local whisper.cpp   (binary in PATH or WHISPER_BINARY env var)
    3. Google STT          (GOOGLE_STT_KEY)
    4. Mock                (always available, returns placeholder)

All backends implement the same interface:
    transcribe(file_path: str, language: str | None) -> dict
        Returns {"text": str, "backend": str, "language": str | None}
        On failure returns {"error": str, "backend": str}
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Backend 1: OpenAI Whisper API
# ---------------------------------------------------------------------------

class WhisperAPIBackend:
    """Transcription via OpenAI Whisper REST API."""

    name = "whisper_api"
    ENDPOINT = "https://api.openai.com/v1/audio/transcriptions"

    def is_available(self) -> bool:
        return bool(os.environ.get("OPENAI_API_KEY"))

    def transcribe(self, file_path: str, language: Optional[str] = None) -> dict:
        api_key = os.environ["OPENAI_API_KEY"]
        path = Path(file_path)

        if not path.exists():
            return {"error": f"File not found: {file_path}", "backend": self.name}

        # Build multipart/form-data manually (no external deps)
        boundary = "----AudioSkillBoundary7MA4YWxkTrZu0gW"
        file_bytes = path.read_bytes()
        mime_type = _mime_for_ext(path.suffix)

        parts: list[bytes] = []
        # model field
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\nwhisper-1\r\n".encode()
        )
        # language field (optional)
        if language:
            parts.append(
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"language\"\r\n\r\n{language}\r\n".encode()
            )
        # file field
        parts.append(
            (
                f"--{boundary}\r\n"
                f"Content-Disposition: form-data; name=\"file\"; filename=\"{path.name}\"\r\n"
                f"Content-Type: {mime_type}\r\n\r\n"
            ).encode() + file_bytes + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode())

        body = b"".join(parts)

        req = urllib.request.Request(
            self.ENDPOINT,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode())
                return {
                    "text": data.get("text", ""),
                    "backend": self.name,
                    "language": language,
                }
        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            return {"error": f"Whisper API HTTP {exc.code}: {body_text}", "backend": self.name}
        except Exception as exc:
            return {"error": str(exc), "backend": self.name}


# ---------------------------------------------------------------------------
# Backend 2: Local whisper.cpp binary
# ---------------------------------------------------------------------------

class WhisperCppBackend:
    """Transcription via a local whisper.cpp binary."""

    name = "whisper_cpp"

    def _find_binary(self) -> Optional[str]:
        # Check env var first, then PATH
        env_path = os.environ.get("WHISPER_BINARY")
        if env_path and Path(env_path).is_file():
            return env_path
        return shutil.which("whisper") or shutil.which("main")  # whisper.cpp builds as 'main'

    def is_available(self) -> bool:
        return self._find_binary() is not None

    def transcribe(self, file_path: str, language: Optional[str] = None) -> dict:
        binary = self._find_binary()
        path = Path(file_path)

        if not path.exists():
            return {"error": f"File not found: {file_path}", "backend": self.name}

        # whisper.cpp requires WAV 16kHz mono; convert if needed
        wav_path, tmp_created = _ensure_wav_16k(path)

        try:
            cmd = [binary, "-f", str(wav_path), "--output-txt", "--no-prints"]
            if language:
                cmd += ["-l", language]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode != 0:
                return {
                    "error": f"whisper.cpp exited {result.returncode}: {result.stderr.strip()}",
                    "backend": self.name,
                }

            # whisper.cpp writes output to <file>.txt when --output-txt is used
            txt_file = wav_path.with_suffix(".txt")
            if txt_file.exists():
                text = txt_file.read_text(encoding="utf-8").strip()
                txt_file.unlink(missing_ok=True)
            else:
                # Fall back to stdout
                text = result.stdout.strip()

            return {"text": text, "backend": self.name, "language": language}

        except subprocess.TimeoutExpired:
            return {"error": "whisper.cpp timed out after 300s", "backend": self.name}
        except Exception as exc:
            return {"error": str(exc), "backend": self.name}
        finally:
            if tmp_created and wav_path.exists():
                wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Backend 3: Google Speech-to-Text REST API
# ---------------------------------------------------------------------------

class GoogleSTTBackend:
    """Transcription via Google Cloud Speech-to-Text v1 REST API."""

    name = "google"
    ENDPOINT = "https://speech.googleapis.com/v1/speech:recognize"

    def is_available(self) -> bool:
        return bool(os.environ.get("GOOGLE_STT_KEY"))

    def transcribe(self, file_path: str, language: Optional[str] = None) -> dict:
        api_key = os.environ["GOOGLE_STT_KEY"]
        path = Path(file_path)

        if not path.exists():
            return {"error": f"File not found: {file_path}", "backend": self.name}

        # Google STT works best with LINEAR16 WAV
        wav_path, tmp_created = _ensure_wav_16k(path)

        try:
            import base64
            audio_b64 = base64.b64encode(wav_path.read_bytes()).decode()

            lang_code = language or "en-US"
            payload = {
                "config": {
                    "encoding": "LINEAR16",
                    "sampleRateHertz": 16000,
                    "languageCode": lang_code,
                },
                "audio": {"content": audio_b64},
            }

            url = f"{self.ENDPOINT}?key={api_key}"
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"},
            )

            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode())

            results = data.get("results", [])
            text = " ".join(
                r.get("alternatives", [{}])[0].get("transcript", "")
                for r in results
            ).strip()

            return {"text": text, "backend": self.name, "language": lang_code}

        except urllib.error.HTTPError as exc:
            body_text = exc.read().decode(errors="replace")
            return {"error": f"Google STT HTTP {exc.code}: {body_text}", "backend": self.name}
        except Exception as exc:
            return {"error": str(exc), "backend": self.name}
        finally:
            if tmp_created and wav_path.exists():
                wav_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Backend 4: Mock
# ---------------------------------------------------------------------------

class MockSTTBackend:
    """Mock backend — always available, returns placeholder for testing."""

    name = "mock"

    def is_available(self) -> bool:
        return True

    def transcribe(self, file_path: str, language: Optional[str] = None) -> dict:
        path = Path(file_path)
        if not path.exists():
            return {"error": f"File not found: {file_path}", "backend": self.name}
        return {
            "text": f"[MOCK TRANSCRIPTION] Audio file: {path.name}",
            "backend": self.name,
            "language": language or "en",
        }


# ---------------------------------------------------------------------------
# Backend registry & selector
# ---------------------------------------------------------------------------

_ALL_STT_BACKENDS = [
    WhisperAPIBackend(),
    WhisperCppBackend(),
    GoogleSTTBackend(),
    MockSTTBackend(),
]


def get_stt_backend(name: Optional[str] = None):
    """
    Return the first available STT backend.
    If `name` is specified, return that backend (or raise ValueError).
    """
    if name:
        for b in _ALL_STT_BACKENDS:
            if b.name == name:
                if not b.is_available():
                    raise ValueError(f"STT backend '{name}' is not available (missing config/binary).")
                return b
        raise ValueError(
            f"Unknown STT backend '{name}'. Valid: {[b.name for b in _ALL_STT_BACKENDS]}"
        )

    for b in _ALL_STT_BACKENDS:
        if b.is_available():
            return b

    # MockSTTBackend is always available so this should never happen
    raise RuntimeError("No STT backend available — this should not happen.")


# ---------------------------------------------------------------------------
# Audio helpers
# ---------------------------------------------------------------------------

def _mime_for_ext(ext: str) -> str:
    return {
        ".wav": "audio/wav",
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".webm": "audio/webm",
    }.get(ext.lower(), "audio/octet-stream")


def _ensure_wav_16k(path: Path) -> tuple[Path, bool]:
    """
    Convert audio to 16kHz mono WAV if it is not already WAV.
    Returns (wav_path, was_converted).
    Uses ffmpeg if available, otherwise returns original path with no conversion.
    """
    if path.suffix.lower() == ".wav":
        return path, False

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        # Can't convert — return original and hope the backend handles it
        return path, False

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    tmp_path = Path(tmp.name)

    try:
        subprocess.run(
            [ffmpeg, "-y", "-i", str(path), "-ar", "16000", "-ac", "1", str(tmp_path)],
            capture_output=True,
            check=True,
            timeout=120,
        )
        return tmp_path, True
    except Exception:
        tmp_path.unlink(missing_ok=True)
        return path, False
