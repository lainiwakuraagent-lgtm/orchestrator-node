#!/usr/bin/env python3
"""
conv_watchdog.py — Monitors the conversational layer and restarts it if stuck.

Runs every 5 minutes via systemd timer. Detects two failure modes:

  PRIMARY: conversation.service is not active → restart immediately.

  SECONDARY: service is active but watcher appears dead:
    - watcher.pid missing or process dead
    - last_update_id.txt unchanged for > STALE_THRESHOLD_MINUTES
    → restart conversation.service

State: state/conversation/watchdog_state.json

Usage:
    python3 tools/conv_watchdog.py [--dry-run]

Flags:
    --dry-run   Log what would happen but do not restart service or send alerts.
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
STATE_DIR = PROJECT_DIR / "state"
CONV_DIR = STATE_DIR / "conversation"
LOG_DIR = PROJECT_DIR / "logs"

LAST_UPDATE_FILE = CONV_DIR / "last_update_id.txt"
WATCHER_PID_FILE = CONV_DIR / "watcher.pid"
WATCHDOG_STATE_FILE = CONV_DIR / "watchdog_state.json"
WAKE_LOG = LOG_DIR / "wake.log"

STALE_THRESHOLD_MINUTES = 15
SERVICE_NAME = "conversation.service"


def ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S %Z")


def log(msg: str) -> None:
    line = f"[{ts()}] WATCHDOG: {msg}"
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


def restart_service(dry_run: bool) -> None:
    env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
    if dry_run:
        log(f"DRY-RUN: would restart {SERVICE_NAME}")
        return
    log(f"Restarting {SERVICE_NAME} ...")
    try:
        subprocess.run(
            ["systemctl", "--user", "restart", SERVICE_NAME],
            env=env, timeout=30, check=True,
        )
        log(f"Restart issued for {SERVICE_NAME}.")
    except subprocess.CalledProcessError as e:
        log(f"ERROR restarting service: {e}")
    except Exception as e:
        log(f"ERROR restarting service: {e}")


def send_alert(msg: str, dry_run: bool) -> None:
    if dry_run:
        log(f"DRY-RUN: would send alert: {msg}")
        return
    try:
        send_sh = PROJECT_DIR / "tools" / "telegram_send.sh"
        env = {**os.environ, "SKIP_TTS": "1"}
        subprocess.run(
            ["bash", str(send_sh)],
            input=msg, text=True, env=env,
            capture_output=True, timeout=15,
        )
    except Exception as e:
        log(f"Alert send failed (non-fatal): {e}")


def watcher_alive() -> bool:
    """Return True if the watcher process in watcher.pid is running."""
    if not WATCHER_PID_FILE.exists():
        return False
    try:
        pid = int(WATCHER_PID_FILE.read_text().strip())
        os.kill(pid, 0)  # signal 0 = existence check
        return True
    except (ValueError, ProcessLookupError, PermissionError):
        return False


def load_watchdog_state() -> dict:
    if WATCHDOG_STATE_FILE.exists():
        try:
            return json.loads(WATCHDOG_STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "last_seen_update_id": None,
        "last_update_id_changed_at": time.time(),
        "restart_count": 0,
        "last_restart_at": None,
    }


def save_watchdog_state(state: dict) -> None:
    CONV_DIR.mkdir(parents=True, exist_ok=True)
    WATCHDOG_STATE_FILE.write_text(json.dumps(state, indent=2))


def read_current_update_id() -> str | None:
    if not LAST_UPDATE_FILE.exists():
        return None
    try:
        return LAST_UPDATE_FILE.read_text().strip()
    except OSError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description="Conversation layer watchdog")
    parser.add_argument("--dry-run", action="store_true", help="Log only, do not restart")
    args = parser.parse_args()

    log("Watchdog check started.")

    state = load_watchdog_state()
    current_update_id = read_current_update_id()
    now = time.time()

    # --- PRIMARY CHECK: is conversation.service active? ---
    if not service_is_active():
        state["restart_count"] = state.get("restart_count", 0) + 1
        state["last_restart_at"] = now
        save_watchdog_state(state)
        log(f"PRIMARY: {SERVICE_NAME} is NOT active. Restarting (restart #{state['restart_count']}).")
        restart_service(args.dry_run)
        alert = (
            f"@Lain watchdog: conversation.service was inactive — restarted. "
            f"(restart #{state['restart_count']}) (눈_눈)"
        )
        send_alert(alert, args.dry_run)
        return 0

    # Service is active. Check if update_id has changed since last watchdog run.
    last_seen = state.get("last_seen_update_id")
    last_changed_at = state.get("last_update_id_changed_at", now)

    if current_update_id != last_seen:
        # Activity detected — update state and exit healthy.
        state["last_seen_update_id"] = current_update_id
        state["last_update_id_changed_at"] = now
        save_watchdog_state(state)
        log(f"Healthy: update_id changed ({last_seen} -> {current_update_id}).")
        return 0

    # update_id unchanged since last check. How long?
    stale_minutes = (now - last_changed_at) / 60.0

    if stale_minutes < STALE_THRESHOLD_MINUTES:
        log(f"Healthy: update_id unchanged for {stale_minutes:.1f}min (< {STALE_THRESHOLD_MINUTES}min threshold). Quiet period.")
        save_watchdog_state(state)
        return 0

    # --- SECONDARY CHECK: stale + watcher dead? ---
    alive = watcher_alive()
    if alive:
        log(f"update_id stale for {stale_minutes:.1f}min but watcher PID is alive. Likely long-poll idle. OK.")
        save_watchdog_state(state)
        return 0

    # Watcher dead AND update_id stale for >15min → conversation layer stuck.
    state["restart_count"] = state.get("restart_count", 0) + 1
    state["last_restart_at"] = now
    state["last_update_id_changed_at"] = now  # reset so we don't re-trigger immediately
    save_watchdog_state(state)
    log(
        f"SECONDARY: watcher dead AND update_id stale for {stale_minutes:.1f}min. "
        f"Restarting {SERVICE_NAME} (restart #{state['restart_count']})."
    )
    restart_service(args.dry_run)
    alert = (
        f"@Lain watchdog: conversation watcher was dead ({stale_minutes:.0f}min stale) — "
        f"restarted conversation.service. (restart #{state['restart_count']}) (╥_╥)"
    )
    send_alert(alert, args.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
