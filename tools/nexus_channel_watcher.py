#!/usr/bin/env python3
"""
nexus_channel_watcher.py — Per-channel Nexus message watcher for agent-channel sessions.

Backgrounded by agent-channel@<channel_id>.service (same relationship as
telegram_watcher.py has to conversation.service). Monitors ONE Nexus channel.
Blocks until an event is available, then prints the event as JSON to stdout
and exits with code 0.

Events emitted (JSON to stdout):
  {event: peer_message, peer: str, text: str, msg_id: str, ts: str}
  {event: system, kind: outbox_intent, content: str, expects_reply: bool}
  {event: system, kind: context_soft, pct: int}
  {event: system, kind: context_hard, pct: int}

Event priority (checked in order each poll cycle):
  1. outbox_intent — pending nexus_send entries in nexus_watcher_queue.json for this channel
  2. context threshold — soft @ 50%, hard @ 70% (emitted once each per session)
  3. peer_message — new messages from the other agent

Usage:
    python3 tools/nexus_channel_watcher.py --channel-id <uuid>

State files:
  state/agent_channels/<id>/last_read.txt      — ISO timestamp of last consumed message
  state/agent_channels/<id>/thresholds.json    — which thresholds have been emitted this session

Token source: state/nexus_<AGENT_NAME>_token.txt → NEXUS_PASSWORD env → identity/nexus_seed_passwords.txt
"""

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent

_AGENT_NAME = os.environ.get("AGENT_NAME", "orchestrator")
TOKEN_FILE = PROJECT_DIR / "state" / f"nexus_{_AGENT_NAME}_token.txt"
PASS_FILE = PROJECT_DIR / "identity" / "nexus_seed_passwords.txt"
WATCHER_QUEUE_FILE = PROJECT_DIR / "state" / "nexus_watcher_queue.json"
CHECK_CONTEXT_SCRIPT = PROJECT_DIR / "tools" / "check_context.sh"
CHANNELS_DIR = PROJECT_DIR / "state" / "agent_channels"

NEXUS_URL = os.environ.get("NEXUS_URL", "http://100.110.36.84:8900")
NEXUS_USERNAME = os.environ.get("NEXUS_USERNAME", os.environ.get("AGENT_NAME", "orchestrator"))

POLL_INTERVAL = 30  # seconds between poll cycles when idle
CONTEXT_SOFT_PCT = 50
CONTEXT_HARD_PCT = 70


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def _load_token() -> str:
    if TOKEN_FILE.exists():
        tok = TOKEN_FILE.read_text().strip()
        if tok:
            return tok
    return _authenticate()


def _authenticate() -> str:
    password = os.environ.get("NEXUS_PASSWORD", "")
    if not password and PASS_FILE.exists():
        for line in PASS_FILE.read_text().splitlines():
            if line.startswith(f"# {NEXUS_USERNAME}"):
                password = line.split()[-1]
                break
    if not password:
        # Try identity/agent.env
        agent_env = PROJECT_DIR / "identity" / "agent.env"
        if agent_env.exists():
            for line in agent_env.read_text().splitlines():
                if "NEXUS_PASSWORD" in line and "=" in line:
                    password = line.split("=", 1)[1].strip()
                    break
    if not password:
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
        print(f"nexus auth error: {e}", file=sys.stderr)
        return ""


def _nexus_get(path: str, token: str):
    """GET from Nexus. Returns parsed JSON or None on error/auth failure."""
    try:
        req = urllib.request.Request(
            f"{NEXUS_URL}{path}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None  # caller should refresh token
        print(f"nexus GET {path} → {e.code}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"nexus GET {path} error: {e}", file=sys.stderr)
        return None


def _nexus_post(path: str, body: dict, token: str) -> bool:
    """POST to Nexus. Returns True on success."""
    try:
        data = json.dumps(body).encode()
        req = urllib.request.Request(
            f"{NEXUS_URL}{path}", data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as e:
        print(f"nexus POST {path} error: {e}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------------
# Per-channel state
# ---------------------------------------------------------------------------

def _channel_dir(channel_id: str) -> Path:
    d = CHANNELS_DIR / channel_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_last_read(channel_id: str) -> str | None:
    """Returns ISO timestamp string or None (first time / no cursor)."""
    p = _channel_dir(channel_id) / "last_read.txt"
    if p.exists():
        v = p.read_text().strip()
        return v if v else None
    return None


def save_last_read(channel_id: str, iso_ts: str) -> None:
    p = _channel_dir(channel_id) / "last_read.txt"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(iso_ts)
    tmp.rename(p)


def load_thresholds(channel_id: str) -> dict:
    p = _channel_dir(channel_id) / "thresholds.json"
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"soft_emitted": False, "hard_emitted": False}


def save_thresholds(channel_id: str, state: dict) -> None:
    p = _channel_dir(channel_id) / "thresholds.json"
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(state))
    tmp.rename(p)


# ---------------------------------------------------------------------------
# Context percentage
# ---------------------------------------------------------------------------

def get_context_pct() -> int:
    """Run check_context.sh and parse context_pct_estimate. Returns 0 on failure."""
    if not CHECK_CONTEXT_SCRIPT.exists():
        return 0
    try:
        result = subprocess.run(
            ["bash", str(CHECK_CONTEXT_SCRIPT)],
            capture_output=True, text=True, timeout=20,
        )
        m = re.search(r"context_pct_estimate:\s*(\d+)%", result.stdout)
        return int(m.group(1)) if m else 0
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Outbox scan
# ---------------------------------------------------------------------------

def check_outbox_intent(channel_id: str) -> dict | None:
    """
    Scan nexus_watcher_queue.json for an unsent entry targeting this channel.
    If found: mark it sent, return the outbox_intent event dict.
    """
    if not WATCHER_QUEUE_FILE.exists():
        return None
    try:
        entries: list = json.loads(WATCHER_QUEUE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None

    found = None
    for entry in entries:
        if not entry.get("sent") and entry.get("channel_id") == channel_id:
            found = entry
            break

    if found is None:
        return None

    # Mark as sent atomically
    found["sent"] = True
    found["sent_at"] = datetime.now(timezone.utc).isoformat()
    tmp = WATCHER_QUEUE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(WATCHER_QUEUE_FILE)

    return {
        "event": "system",
        "kind": "outbox_intent",
        "content": found.get("content", ""),
        "expects_reply": bool(found.get("expects_reply", False)),
        "entry_id": found.get("id", ""),
    }


# ---------------------------------------------------------------------------
# Peer message poll
# ---------------------------------------------------------------------------

def poll_peer_message(channel_id: str, token: str, my_username: str) -> tuple[dict | None, str | None]:
    """
    Poll for new messages from the peer agent on this channel.
    Returns (event_dict_or_None, new_token_if_refreshed).
    Advances the last_read cursor on success.
    """
    last_read = load_last_read(channel_id)
    path = f"/conversations/{channel_id}/messages?limit=50"
    if last_read:
        # URL-encode the since param (colons → %3A etc.)
        path += f"&since={urllib.parse.quote(last_read)}"

    result = _nexus_get(path, token)
    new_token = None
    if result is None:
        # Possible 401 — refresh once
        refreshed = _authenticate()
        if refreshed:
            new_token = refreshed
            result = _nexus_get(path, refreshed)

    if not result:
        return None, new_token

    messages = result if isinstance(result, list) else result.get("messages", [])
    if not messages:
        return None, new_token

    # Messages are newest-first from the API. Find messages NOT from self.
    peer_msgs = [m for m in messages if m.get("sender_id") != my_username]
    if not peer_msgs:
        # No messages from peer — still update cursor to avoid re-reading own messages
        newest = messages[0]
        if newest.get("created_at"):
            save_last_read(channel_id, newest["created_at"])
        return None, new_token

    # Take the oldest unread peer message (last in newest-first list)
    msg = peer_msgs[-1]
    sender = msg.get("sender_id", "unknown")
    text = msg.get("content", "")
    msg_id = msg.get("id", "")
    ts = msg.get("created_at", "")

    # Advance cursor to newest message overall (not just peer message)
    save_last_read(channel_id, messages[0]["created_at"])

    # Post read receipt (non-fatal)
    _nexus_post(f"/conversations/{channel_id}/read", {"message_id": messages[0]["id"]}, token)

    return {
        "event": "peer_message",
        "peer": sender,
        "text": text,
        "msg_id": msg_id,
        "ts": ts,
    }, new_token


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Per-channel Nexus watcher")
    parser.add_argument("--channel-id", required=True, help="Nexus conversation UUID to monitor")
    args = parser.parse_args()

    channel_id = args.channel_id

    # Ensure state dir exists
    _channel_dir(channel_id)

    token = _load_token()
    if not token:
        token = _authenticate()
    if not token:
        print("ERROR: Nexus token unavailable", file=sys.stderr)
        return 2

    def _cleanup(signum, frame):  # noqa: ANN001
        sys.exit(1)

    signal.signal(signal.SIGTERM, _cleanup)
    signal.signal(signal.SIGINT, _cleanup)

    thresholds = load_thresholds(channel_id)

    while True:
        # --- 1. Outbox intent (highest priority) ---
        intent = check_outbox_intent(channel_id)
        if intent:
            print(json.dumps(intent))
            return 0

        # --- 2. Context threshold events ---
        pct = get_context_pct()
        if pct >= CONTEXT_HARD_PCT and not thresholds["hard_emitted"]:
            thresholds["hard_emitted"] = True
            save_thresholds(channel_id, thresholds)
            print(json.dumps({"event": "system", "kind": "context_hard", "pct": pct}))
            return 0
        if pct >= CONTEXT_SOFT_PCT and not thresholds["soft_emitted"]:
            thresholds["soft_emitted"] = True
            save_thresholds(channel_id, thresholds)
            print(json.dumps({"event": "system", "kind": "context_soft", "pct": pct}))
            return 0

        # --- 3. Poll Nexus for peer messages ---
        event, new_token = poll_peer_message(channel_id, token, NEXUS_USERNAME)
        if new_token:
            token = new_token
        if event:
            print(json.dumps(event))
            return 0

        # Nothing ready — wait and retry
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    sys.exit(main())
