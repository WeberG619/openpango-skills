---
name: audio
description: "Voice & audio interaction skill — STT transcription and TTS speech generation for agent communication."
version: "1.0.0"
user-invocable: true
metadata:
  capabilities:
    - audio/transcribe
    - audio/speak
    - audio/stream
  author: "WeberG619"
  license: "MIT"
---

# Audio Skill — Voice & Audio Interaction

Gives agents the ability to transcribe spoken audio (STT) and generate natural-sounding speech (TTS). Designed for multi-backend graceful fallback so it works with or without API keys.

## Quick Start

```bash
# Transcribe an audio file
python3 skills/audio/voice_handler.py transcribe /path/to/audio.wav

# Generate speech from text
python3 skills/audio/voice_handler.py speak "Hello, I am your agent." --voice en-US-AriaNeural

# Run the test suite
python3 skills/audio/test_voice.py
```

## Environment Variables

| Variable | Purpose | Required |
|----------|---------|----------|
| `OPENAI_API_KEY` | Whisper API for STT | No (falls back) |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS | No (falls back) |
| `ELEVENLABS_VOICE_ID` | Default ElevenLabs voice ID | No |
| `GOOGLE_STT_KEY` | Google Speech-to-Text API | No (falls back) |
| `WHISPER_BINARY` | Path to local whisper.cpp binary | No (auto-detected) |
| `AUDIO_OUTPUT_DIR` | Directory for generated audio files | No (uses /tmp) |

## STT Backends (priority order)

1. **OpenAI Whisper API** — Best accuracy, requires `OPENAI_API_KEY`. Supports all formats.
2. **Local whisper.cpp** — Offline, requires binary in PATH or `WHISPER_BINARY`. Converts audio then runs inference.
3. **Google Speech-to-Text** — Requires `GOOGLE_STT_KEY`. Good for short utterances.
4. **Mock mode** — Returns placeholder transcription for testing without any API keys.

## TTS Backends (priority order)

1. **ElevenLabs API** — Highest quality voices, requires `ELEVENLABS_API_KEY`.
2. **Edge TTS** — Free Microsoft neural voices via `edge-tts` package. No API key needed.
3. **pyttsx3** — Offline system TTS. Works everywhere, lower quality.
4. **Mock mode** — Returns a placeholder audio path for testing.

## Supported Audio Formats

Input: WAV, MP3, OGG, FLAC
Output: MP3 (ElevenLabs), MP3/WAV (Edge TTS), WAV (pyttsx3)

## Tools Exposed

### `transcribe_audio(file_path, language=None, backend=None)`
Transcribes an audio file to text.

- `file_path` — Path to audio file (WAV, MP3, OGG, FLAC)
- `language` — Optional BCP-47 language code (e.g. `"en"`, `"es"`)
- `backend` — Force a specific backend: `"whisper_api"`, `"whisper_cpp"`, `"google"`, `"mock"`

Returns `{"text": "...", "backend": "...", "language": "..."}`.

### `generate_speech(text, voice_id=None, output_path=None, backend=None)`
Synthesizes text to an audio file.

- `text` — The text to speak
- `voice_id` — Voice identifier (ElevenLabs voice ID, Edge TTS voice name, or pyttsx3 voice index)
- `output_path` — Where to save the audio file. Auto-generated if not provided.
- `backend` — Force a specific backend: `"elevenlabs"`, `"edge_tts"`, `"pyttsx3"`, `"mock"`

Returns `{"audio_path": "...", "backend": "...", "voice_id": "..."}`.

## Cross-Skill Integration

- **Orchestration**: Orchestration can delegate voice I/O to this skill for spoken agent responses.
- **Comms**: Can pair with the comms skill to read messages aloud or transcribe voice notes.
- **Memory**: Transcribed audio can be stored as memories via the memory skill.

## Error Handling

All functions return a dict. On failure, the `error` key is populated:
```json
{"error": "No STT backend available. Set OPENAI_API_KEY, install whisper.cpp, or set GOOGLE_STT_KEY."}
```
No exceptions are raised to callers — errors are always returned as structured dicts.
