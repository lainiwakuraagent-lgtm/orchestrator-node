#!/usr/bin/env python3
"""nexus_watcher.py — Pure dispatcher: notice when a Nexus channel needs attention,
ensure the right per-channel session is running.

Responsibilities:
  1. Discover all Nexus conversations (not just seeded ones)
  2. Poll each channel for new messages
  3. When new messages arrive on a channel with no active session → spawn it
  4. Write per-channel session context before spawning (for close-comms-session skill)

NOT responsible for:
  - Sending messages (the per-channel session does that)
  - Draining any outbox queue (outbox events are the first event in the session loop)
  - Publishing status to lain-status

Per-channel state: state/agent_channels/<channel_id>/
Per-channel session unit: agent-channel@<channel_id>.service

Usage:
  python3 tools/nexus_watcher.py [--dry-run] [--once]

State files (all writes are atomic via tmp+rename):
  state/nexus_last_read.json        — per-channel last-read message ID
  state/nexus_watcher_state.json    — per-channel watcher state
  state/agent_channels/<id>/        — per-channel session state dir
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

PID_FILE = PROJECT_DIR / "state" / "nexus_watcher.pid"
LAST_READ_FILE = PROJECT_DIR / "state" / "nexus_last_read.json"
STATE_FILE = PROJECT_DIR / "state" / "nexus_watcher_state.json"
TOKEN_FILE = PROJECT_DIR / "state" / "nexus_lain_token.txt"
WAKE_LOG = PROJECT_DIR / "logs" / "wake.log"
PASS_FILE = PROJECT_DIR / "identity" / "nexus_seed_passwords.txt"
CHANNELS_DIR = PROJECT_DIR / "state" / "agent_channels"

NEXUS_URL = os.environ.get("NEXUS_URL", "http://100.110.36.84:8900")
NEXUS_USERNAME = os.environ.get("NEXUS_USERNAME", "lain")

POLL_INTERVAL = 30  # seconds


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] nexus_watcher: {msg}"
    print(line, file=sys.stderr)
    try:
        with open(WAKE_LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Nexus API
# ---------------------------------------------------------------------------

def _load_token() -> str:
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    return _authenticate()


def _authenticate() -> str:
    import urllib.request
    password = os.environ.get("NEXUS_PASSWORD", "")
    if not password and PASS_FILE.exists():
        for line in PASS_FILE.read_text().splitlines():
            if line.startswith(f"# {NEXUS_USERNAME}"):
                password = line.split()[-1]
                break
    if not password:
        log("no Nexus password — cannot authenticate")
        return ""
    try:
        body = json.dumps({"username": NEXUS_USERNAME, "password": password}).encode()
        req = urllib.request.Request(
            f"{NEXUS_URL}/auth/token", data=body,
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            token = data.get("access_token", "")
            if token and TOKEN_FILE.parent.exists():
                TOKEN_FILE.write_text(token)
            return token
    except Exception as e:
        log(f"auth error: {e}")
        return ""


def nexus_get(path: str, token: str):
    import urllib.request, urllib.error
    try:
        req = urllib.request.Request(
            f"{NEXUS_URL}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None  # token expired
        log(f"nexus GET {path} error {e.code}")
        return None
    except Exception as e:
        log(f"nexus GET {path} error: {e}")
        return None


# ---------------------------------------------------------------------------
# Atomic file helpers
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


def load_last_read() -> dict:
    if LAST_READ_FILE.exists():
        try:
            return json.loads(LAST_READ_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_last_read(data: dict) -> None:
    try:
        _atomic_write(LAST_READ_FILE, data)
    except OSError as e:
        log(f"last_read save error: {e}")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def save_state(state: dict) -> None:
    try:
        _atomic_write(STATE_FILE, state)
    except OSError as e:
        log(f"state save error: {e}")


# ---------------------------------------------------------------------------
# Channel discovery
# ---------------------------------------------------------------------------

def discover_channels(token: str) -> dict[str, str]:
    """
    Return {channel_id: peer_id} for all DM conversations we're part of.
    Falls back to empty dict if Nexus is unreachable or token expired.
    """
    result = nexus_get("/conversations/", token)
    if not result or not isinstance(result, list):
        return {}
    channels: dict[str, str] = {}
    for conv in result:
        channel_id = conv.get("id", "")
        if not channel_id:
            continue
        # Determine peer_id from members (for DMs: the other participant)
        members = conv.get("members", []) or conv.get("participants", [])
        peer_id = ""
        for m in members:
            username = m.get("username", "") if isinstance(m, dict) else str(m)
            if username and username != NEXUS_USERNAME:
                peer_id = username
                break
        channels[channel_id] = peer_id
    return channels


# ---------------------------------------------------------------------------
# Per-channel session management
# ---------------------------------------------------------------------------

def is_channel_session_active(channel_id: str) -> bool:
    """True if agent-channel@<channel_id>.service is running."""
    try:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        result = subprocess.run(
            ["systemctl", "--user", "is-active", f"agent-channel@{channel_id}.service"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        return result.stdout.strip() == "active"
    except Exception:
        return False


def write_channel_context(channel_id: str, peer_id: str) -> None:
    """Write per-channel session context before spawning (read by close-comms-session skill)."""
    channel_dir = CHANNELS_DIR / channel_id
    channel_dir.mkdir(parents=True, exist_ok=True)
    ctx = {
        "is_nexus_session": True,
        "session_id": f"{datetime.now(timezone.utc).strftime('%Y%m%d')}_nexus_{peer_id}",
        "peer_id": peer_id,
        "channel_id": channel_id,
        "started_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    try:
        _atomic_write(channel_dir / "nexus_session_context.json", ctx)
    except OSError as e:
        log(f"context write error for channel {channel_id}: {e}")


def spawn_channel_session(channel_id: str, peer_id: str, dry_run: bool = False) -> bool:
    """Ensure agent-channel@<channel_id>.service is running."""
    if dry_run:
        log(f"[DRY-RUN] would start agent-channel@{channel_id}.service (peer={peer_id})")
        return True
    write_channel_context(channel_id, peer_id)
    try:
        env = {**os.environ, "XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}
        subprocess.run(
            ["systemctl", "--user", "start", f"agent-channel@{channel_id}.service"],
            env=env, timeout=15, check=True,
        )
        log(f"started agent-channel@{channel_id}.service (peer={peer_id})")
        return True
    except subprocess.CalledProcessError as e:
        log(f"failed to start agent-channel@{channel_id}.service: {e}")
        return False
    except Exception as e:
        log(f"spawn error for channel {channel_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Main poll cycle
# ---------------------------------------------------------------------------

def poll_once(token: str, state: dict, dry_run: bool = False) -> str:
    """One poll iteration. Returns the token (possibly refreshed)."""
    now_iso = datetime.now(timezone.utc).isoformat()

    # Discover all channels + merge with known channels from last_read
    discovered = discover_channels(token)
    if discovered is None:
        # 401 or network failure — try re-auth
        new_token = _authenticate()
        if new_token:
            token = new_token
            discovered = discover_channels(token)
        if not discovered:
            discovered = {}

    last_read = load_last_read()

    # Union: discovered channels + any already tracked in last_read
    all_channel_ids = set(discovered.keys()) | set(last_read.keys())
    # Peer id for channels we know from discovery; "" for channels only in last_read
    peer_ids = {cid: discovered.get(cid, "") for cid in all_channel_ids}

    for channel_id in all_channel_ids:
        result = nexus_get(f"/conversations/{channel_id}/messages?limit=50", token)
        if result is None:
            # Possible 401 — re-auth once
            new_token = _authenticate()
            if new_token:
                token = new_token
                result = nexus_get(f"/conversations/{channel_id}/messages?limit=50", token)
        if not result:
            continue

        messages = result if isinstance(result, list) else result.get("messages", [])
        if not messages:
            continue

        last_msg_id = last_read.get(channel_id)
        new_msgs = []
        if last_msg_id:
            seen_last = False
            for m in reversed(messages):  # oldest-first traversal
                if seen_last:
                    new_msgs.append(m)
                elif m.get("id") == last_msg_id:
                    seen_last = True
        else:
            new_msgs = list(reversed(messages))  # first time: treat all as new

        if new_msgs:
            peer_id = peer_ids.get(channel_id, "")
            log(f"channel {channel_id}: {len(new_msgs)} new message(s) from {peer_id or '(unknown)'}")
            if not is_channel_session_active(channel_id):
                if spawn_channel_session(channel_id, peer_id, dry_run=dry_run):
                    state.setdefault(channel_id, {})["last_spawn"] = now_iso
            else:
                log(f"channel {channel_id}: session already active")

            # Advance cursor to newest message (after spawn so session picks it up before next poll)
            last_read[channel_id] = messages[0]["id"]
            state.setdefault(channel_id, {})["last_activity"] = now_iso

    save_last_read(last_read)
    return token


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Nexus pure dispatcher")
    parser.add_argument("--dry-run", action="store_true", help="Log only, no side effects")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle then exit")
    args = parser.parse_args()

    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    def cleanup(signum=None, frame=None):
        PID_FILE.unlink(missing_ok=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, cleanup)
    signal.signal(signal.SIGINT, cleanup)

    log(f"starting (PID {os.getpid()}, dry_run={args.dry_run})")

    token = _load_token()
    if not token:
        token = _authenticate()
    if not token:
        log("cannot authenticate — exiting")
        PID_FILE.unlink(missing_ok=True)
        sys.exit(1)

    state = load_state()

    try:
        if args.once:
            token = poll_once(token, state, dry_run=args.dry_run)
            save_state(state)
            log("--once: done")
            return

        while True:
            try:
                token = poll_once(token, state, dry_run=args.dry_run)
                save_state(state)
            except Exception as e:
                log(f"poll error (continuing): {e}")
            time.sleep(POLL_INTERVAL)
    finally:
        PID_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
