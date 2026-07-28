#!/usr/bin/env python3
"""
wake_word_listener.py — Wake word detection for the home agent.

Streams microphone audio in 80ms chunks and fires a callback when the
configured wake word is detected. Uses openWakeWord library.

Config (via agent_config.env or env vars):
  WAKE_WORD       - model name. Default: "hey_jarvis" (off-the-shelf placeholder).
                    After T264 name decision: replace with custom-trained model.
  WAKE_THRESHOLD  - detection threshold 0.0–1.0. Default: 0.5.

Install:
  pip install openwakeword sounddevice numpy

Usage:
  python3 tools/wake_word_listener.py          # listen and print detections
  WAKE_WORD=alexa python3 tools/wake_word_listener.py

  # As a module:
  from tools.wake_word_listener import listen_for_wake_word
  listen_for_wake_word(callback=lambda: do_something())
"""
import os
import sys
import signal
import time

_CONFIG_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "state", "agent_config.env",
)


def _load_env() -> None:
    """Pull WAKE_WORD / WAKE_THRESHOLD from agent_config.env (non-overriding)."""
    if not os.path.exists(_CONFIG_FILE):
        return
    with open(_CONFIG_FILE) as fh:
        for line in fh:
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key in ("WAKE_WORD", "WAKE_THRESHOLD") and key not in os.environ:
                os.environ[key] = val.strip()


_load_env()

WAKE_WORD = os.environ.get("WAKE_WORD", "hey_jarvis")
WAKE_THRESHOLD = float(os.environ.get("WAKE_THRESHOLD", "0.5"))

# openWakeWord expects 16kHz mono int16 audio. 80ms chunks = 1280 samples.
# This matches the library's internal processing window for real-time detection.
_SAMPLE_RATE = 16000
_CHUNK_SAMPLES = 1280  # 80ms at 16kHz — openWakeWord's native chunk size


def listen_for_wake_word(
    callback=None,
    timeout_seconds: float | None = None,
    max_detections: int | None = None,
) -> int:
    """
    Stream mic audio and fire callback on each wake word detection.

    Args:
        callback: Called with no arguments on each detection.
                  Defaults to printing a message.
        timeout_seconds: Stop after this many seconds. None = run forever.
        max_detections: Stop after this many detections. None = no limit.

    Returns:
        Number of detections made.

    Raises:
        RuntimeError: if required packages are not installed.
    """
    try:
        import numpy as np
        import sounddevice as sd
        from openwakeword.model import Model as OwwModel
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependencies. Run: pip install openwakeword sounddevice numpy"
        ) from exc

    oww = OwwModel(wakeword_models=[WAKE_WORD], inference_framework="onnx")

    if callback is None:
        def callback() -> None:
            ts = time.strftime("%H:%M:%S")
            print(f"[wake_word] {ts} — detected '{WAKE_WORD}'", flush=True)

    detections = 0
    keep_running = True
    start_time = time.monotonic()

    def _stop(_sig, _frame) -> None:
        nonlocal keep_running
        keep_running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    print(
        f"[wake_word] Listening for '{WAKE_WORD}' "
        f"(threshold={WAKE_THRESHOLD}, chunk=80ms)",
        file=sys.stderr,
        flush=True,
    )

    with sd.InputStream(
        samplerate=_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=_CHUNK_SAMPLES,
    ) as stream:
        while keep_running:
            if timeout_seconds is not None:
                if time.monotonic() - start_time >= timeout_seconds:
                    break

            chunk, _overflowed = stream.read(_CHUNK_SAMPLES)
            audio_data = chunk.flatten()

            prediction = oww.predict(audio_data)

            # Check all model scores — the key might be model name or file path
            triggered = any(
                score >= WAKE_THRESHOLD
                for score in prediction.values()
                if isinstance(score, (int, float))
            )

            if triggered:
                detections += 1
                callback()
                if max_detections is not None and detections >= max_detections:
                    break

    return detections


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Listen for a wake word")
    parser.add_argument(
        "--max-detections", type=int, default=None,
        help="Stop after N detections (default: run until SIGTERM/Ctrl-C)",
    )
    parser.add_argument(
        "--timeout", type=float, default=None,
        help="Stop after N seconds (default: no timeout)",
    )
    args = parser.parse_args()

    print(
        f"[wake_word] WAKE_WORD={WAKE_WORD}  WAKE_THRESHOLD={WAKE_THRESHOLD}",
        file=sys.stderr,
    )
    try:
        total = listen_for_wake_word(
            timeout_seconds=args.timeout,
            max_detections=args.max_detections,
        )
        print(f"[wake_word] Total detections: {total}")
        sys.exit(0 if total > 0 else 1)
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\n[wake_word] Stopped.", file=sys.stderr)
