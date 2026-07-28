#!/usr/bin/env python3
"""
conv_idle_check.py — Idle-close signal for the conversational layer.

Runs every 5 minutes (same timer as conv_watchdog.py). If conversation.service
is active and no real message has arrived for 30 minutes, writes reset_signal.txt
with action=idle_close so the agent can gracefully shut down.

Usage:
    python3 tools/conv_idle_check.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
CONV_DIR = PROJECT_DIR / "state" / "conversation"
LOG_DIR = PROJECT_DIR / "logs"
WAKE_LOG = LOG_DIR / "wake.log"

LAST_REAL_MSG_FILE = CONV_DIR / "last_real_message_at.txt"
RESET_SIGNAL_FILE = CONV_DIR / "reset_signal.txt"
SERVICE_NAME = f"conversation@{PROJECT_DIR.name}.service"

IDLE_THRESHOLD_SECONDS = 1800  # 30 minutes


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg: str) -> None:
    line = f"[{ts()}] IDLE-CHECK: {msg}"
    print(line, flush=True)
    try:
        with WAKE_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def service_is_active() -> bool:
    try:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        result = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE_NAME],
            capture_output=True, text=True, env=env, timeout=10,
        )
        return result.stdout.strip() == "active"
    except Exception as e:
        log(f"ERROR checking service status: {e}")
        return False


def main() -> int:
    now = time.time()

    # Skip if last_real_message_at.txt doesn't exist (no messages yet this session)
    if not LAST_REAL_MSG_FILE.exists():
        log("No last_real_message_at.txt — skipping.")
        return 0

    try:
        last_real = int(LAST_REAL_MSG_FILE.read_text().strip())
    except (ValueError, OSError) as e:
        log(f"Cannot read last_real_message_at.txt: {e}")
        return 0

    idle_seconds = now - last_real

    if not service_is_active():
        log(f"Service not active — skipping (idle {idle_seconds:.0f}s).")
        return 0

    if idle_seconds <= IDLE_THRESHOLD_SECONDS:
        log(f"Idle {idle_seconds:.0f}s (< {IDLE_THRESHOLD_SECONDS}s threshold). OK.")
        return 0

    # Idle threshold exceeded — write signal if not already present
    if RESET_SIGNAL_FILE.exists():
        log(f"Idle {idle_seconds:.0f}s but reset_signal.txt already exists. Skipping.")
        return 0

    signal_data = {
        "action": "idle_close",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    RESET_SIGNAL_FILE.write_text(json.dumps(signal_data))
    log(f"Idle {idle_seconds:.0f}s — wrote idle_close signal to reset_signal.txt.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
