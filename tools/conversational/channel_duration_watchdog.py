#!/usr/bin/env python3
"""
channel_duration_watchdog.py — Force-closes agent-channel sessions that exceed max duration.

Runs every 5 minutes via systemd timer. Iterates ALL active agent-channel@* instances,
checks each session's active duration via lock file mtime, and force-closes any that
exceed the threshold.

Default threshold: 20 minutes (CHANNEL_MAX_DURATION_MINUTES env var or --threshold flag).
Threshold is provisional — tune once real session-duration data from T372 is available.

Usage:
    python3 tools/conversational/channel_duration_watchdog.py [--dry-run] [--threshold N]

Flags:
    --dry-run     Log what would happen but do not stop any service.
    --threshold N Duration ceiling in minutes (default: 20).
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
STATE_DIR = PROJECT_DIR / "state"
CHANNELS_DIR = STATE_DIR / "agent_channels"
LOG_DIR = PROJECT_DIR / "logs"
WAKE_LOG = LOG_DIR / "wake.log"

DEFAULT_THRESHOLD_MINUTES = int(os.environ.get("CHANNEL_MAX_DURATION_MINUTES", "20"))
_UNIT_RE = re.compile(r"^agent-channel@(.+)\.service$")


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg: str) -> None:
    line = f"[{ts()}] CHANNEL-WATCHDOG: {msg}"
    print(line, flush=True)
    try:
        with WAKE_LOG.open("a") as f:
            f.write(line + "\n")
    except OSError:
        pass


def get_active_channel_units() -> list[str]:
    """Return list of active agent-channel@*.service unit names."""
    try:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        result = subprocess.run(
            ["systemctl", "--user", "list-units", "agent-channel@*.service",
             "--state=active", "--output=json", "--no-pager"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        if result.returncode != 0:
            return []
        data = json.loads(result.stdout or "[]")
        return [u["unit"] for u in data if u.get("active") == "active"]
    except Exception as e:
        log(f"ERROR listing agent-channel units: {e}")
        return []


def get_session_age_minutes(channel_id: str) -> float | None:
    """Return session duration in minutes via lock file mtime, or None if not found."""
    lock_file = CHANNELS_DIR / channel_id / "session.lock"
    if not lock_file.exists():
        return None
    try:
        age_seconds = time.time() - lock_file.stat().st_mtime
        return age_seconds / 60.0
    except OSError:
        return None


def force_close_channel(channel_id: str, dry_run: bool) -> None:
    """Write force_close exit reason and stop the service via systemctl."""
    exit_reason_file = CHANNELS_DIR / channel_id / "exit_reason.txt"
    unit = f"agent-channel@{channel_id}.service"
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}

    if dry_run:
        log(f"DRY-RUN: would write force_close to {exit_reason_file}")
        log(f"DRY-RUN: would stop {unit}")
        return

    # Write exit reason before stopping — serves as audit trail in the channel dir.
    # The running agent_channel.sh may not read it (systemctl stop kills the process),
    # but nexus_watcher.py or future tooling can inspect it post-facto.
    try:
        exit_reason_file.parent.mkdir(parents=True, exist_ok=True)
        exit_reason_file.write_text("force_close\n")
        log(f"Wrote force_close exit reason: {exit_reason_file}")
    except OSError as e:
        log(f"WARN: could not write exit reason for {channel_id}: {e}")

    # systemctl stop (not kill) so Restart=on-failure does not trigger.
    try:
        subprocess.run(
            ["systemctl", "--user", "stop", unit],
            env=env, timeout=30, check=True,
        )
        log(f"Stopped {unit}.")
    except subprocess.CalledProcessError as e:
        log(f"ERROR stopping {unit}: {e}")
    except Exception as e:
        log(f"ERROR stopping {unit}: {e}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Force-close agent-channel sessions that exceed max active duration"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Log what would happen but do not stop any service")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD_MINUTES,
                        metavar="N",
                        help=f"Duration ceiling in minutes (default: {DEFAULT_THRESHOLD_MINUTES})")
    args = parser.parse_args()

    threshold = args.threshold
    log(f"Started. Threshold: {threshold} min. dry_run={args.dry_run}")

    units = get_active_channel_units()
    if not units:
        log("No active agent-channel instances. Done.")
        return 0

    log(f"Active channel(s): {', '.join(units)}")

    closed = 0
    for unit_name in units:
        m = _UNIT_RE.match(unit_name)
        if not m:
            continue
        channel_id = m.group(1)

        age = get_session_age_minutes(channel_id)
        if age is None:
            log(f"Channel {channel_id}: no lock file — skipping (service may be starting).")
            continue

        if age < threshold:
            log(f"Channel {channel_id}: {age:.1f}min < {threshold}min — OK.")
            continue

        log(f"Channel {channel_id}: {age:.1f}min >= {threshold}min — force-closing.")
        force_close_channel(channel_id, args.dry_run)
        closed += 1

    log(f"Done. Checked {len(units)} channel(s), closed {closed}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
