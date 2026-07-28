#!/usr/bin/env python3
"""
home_record.py — Record microphone audio until silence.

Records in 100ms chunks at 16kHz mono. Stops when the RMS level stays
below SILENCE_RMS_THRESHOLD for silence_timeout seconds. Saves as WAV.

Install: pip install sounddevice numpy scipy

Usage:
  python3 tools/home_record.py --output /tmp/recording.wav
  python3 tools/home_record.py --output /tmp/clip.wav --silence-timeout 2.0 --max-duration 15

Exit codes:
  0 — audio captured and saved
  1 — error (missing deps, device failure)
  2 — no meaningful audio (too quiet or empty)
"""
import argparse
import sys
import time

SAMPLE_RATE = 16000
SILENCE_RMS_THRESHOLD = 500  # RMS below this counts as silence
CHUNK_DURATION = 0.1  # 100ms per chunk


def record_until_silence(
    output_path: str,
    silence_timeout: float = 2.0,
    max_duration: float = 15.0,
) -> bool:
    """
    Record from the default microphone until silence is sustained.

    Returns True and writes WAV if non-trivial audio was captured.
    Returns False if the recording was empty or entirely silent.
    """
    try:
        import numpy as np
        import sounddevice as sd
        import scipy.io.wavfile
    except ImportError as exc:
        raise RuntimeError(
            "Missing deps. Run: pip install sounddevice numpy scipy"
        ) from exc

    chunk_samples = int(SAMPLE_RATE * CHUNK_DURATION)
    chunks: list = []
    silence_start: float | None = None
    start_time = time.monotonic()

    with sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=chunk_samples,
    ) as stream:
        while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= max_duration:
                break

            chunk, _overflowed = stream.read(chunk_samples)
            flat = chunk.flatten()
            chunks.append(flat)

            rms = float(np.sqrt(np.mean(flat.astype(np.float64) ** 2)))

            if rms < SILENCE_RMS_THRESHOLD:
                if silence_start is None:
                    silence_start = time.monotonic()
                elif time.monotonic() - silence_start >= silence_timeout:
                    break
            else:
                silence_start = None  # voice detected — reset silence timer

    if not chunks:
        return False

    import numpy as np
    import scipy.io.wavfile

    audio = np.concatenate(chunks)

    # Reject recordings that are entirely silent
    rms_overall = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms_overall < SILENCE_RMS_THRESHOLD:
        return False

    scipy.io.wavfile.write(output_path, SAMPLE_RATE, audio)
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record audio until silence")
    parser.add_argument("--output", required=True, help="Output WAV file path")
    parser.add_argument(
        "--silence-timeout", type=float, default=2.0,
        help="Seconds of silence before stopping (default: 2.0)",
    )
    parser.add_argument(
        "--max-duration", type=float, default=15.0,
        help="Maximum recording length in seconds (default: 15.0)",
    )
    args = parser.parse_args()

    print(
        f"[home_record] Recording (silence={args.silence_timeout}s, "
        f"max={args.max_duration}s)...",
        file=sys.stderr,
        flush=True,
    )

    try:
        ok = record_until_silence(
            args.output,
            silence_timeout=args.silence_timeout,
            max_duration=args.max_duration,
        )
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if ok:
        print(f"[home_record] Saved to {args.output}", file=sys.stderr)
        sys.exit(0)
    else:
        print("[home_record] No meaningful audio captured", file=sys.stderr)
        sys.exit(2)
