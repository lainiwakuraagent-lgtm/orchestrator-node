#!/usr/bin/env python3
"""
home_stt.py — Speech-to-text module for the home agent.

Dual modes controlled by STT_MODE env var:
  local  — uses openai-whisper (offline, base model ~140MB). Default.
  api    — uses OpenAI Whisper API (requires OPENAI_API_KEY).

Main interface:
  from tools.home_stt import transcribe
  text = transcribe("/path/to/audio.wav")

CLI:
  python3 tools/home_stt.py /path/to/audio.wav
  STT_MODE=api python3 tools/home_stt.py /path/to/audio.wav
"""
import os
import sys
from pathlib import Path

STT_MODE = os.environ.get("STT_MODE", "local")

_local_model_cache: object = None


def transcribe(wav_path: str) -> str:
    """Transcribe audio file to text. Returns stripped transcription string."""
    path = Path(wav_path)
    if not path.exists():
        raise FileNotFoundError(f"Audio file not found: {wav_path}")
    if path.stat().st_size == 0:
        raise ValueError(f"Audio file is empty: {wav_path}")

    if STT_MODE == "api":
        return _transcribe_api(str(path))
    return _transcribe_local(str(path))


def _transcribe_local(wav_path: str) -> str:
    """Transcribe with local openai-whisper base model (offline)."""
    global _local_model_cache
    try:
        import whisper  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "openai-whisper not installed. Run: pip install openai-whisper"
        ) from exc

    if _local_model_cache is None:
        _local_model_cache = whisper.load_model("base")

    result = _local_model_cache.transcribe(wav_path)
    return result["text"].strip()


def _transcribe_api(wav_path: str) -> str:
    """Transcribe via OpenAI Whisper API."""
    try:
        from openai import OpenAI  # type: ignore[import]
    except ImportError as exc:
        raise RuntimeError(
            "openai package not installed. Run: pip install openai"
        ) from exc

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY not set — required for STT_MODE=api"
        )

    client = OpenAI(api_key=api_key)
    with open(wav_path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model="whisper-1",
            file=audio_file,
        )
    return response.text.strip()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <audio_file.wav>", file=sys.stderr)
        print(f"  STT_MODE={STT_MODE}  (set env var to 'local' or 'api')", file=sys.stderr)
        sys.exit(1)

    audio_path = sys.argv[1]
    print(f"[home_stt] mode={STT_MODE} file={audio_path}", file=sys.stderr)
    try:
        text = transcribe(audio_path)
        print(text)
    except (RuntimeError, FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
