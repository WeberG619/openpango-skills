"""
Audio skill — STT transcription and TTS speech generation.

Entry points:
    transcribe_audio(file_path, language=None, backend=None) -> dict
    generate_speech(text, voice_id=None, output_path=None, backend=None) -> dict
"""

from .voice_handler import transcribe_audio, generate_speech

__all__ = ["transcribe_audio", "generate_speech"]
__version__ = "1.0.0"
