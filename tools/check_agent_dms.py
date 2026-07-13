#!/usr/bin/env python3
"""
check_agent_dms.py — Check Nexus DMs from agents, output new messages.

Reads the Ting-Asuka DM conversation (and future agent DMs) using the
orchestrator's Nexus JWT token. Tracks last-read position per conversation
in state/nexus_agent_dms_last_read.json.

Exit codes:
  0 — no new messages (or on error, to be non-fatal)
  1 — new messages found

Output (stdout, JSON lines):
  {"agent": "asuka", "message_id": "...", "content": "...", "timestamp": "..."}
"""

import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
NEXUS_URL = "http://100.110.36.84:8900"
TOKEN_FILE = PROJECT_DIR / "state" / "nexus_orchestrator_token.txt"
ROUTING_FILE = PROJECT_DIR / "state" / "nexus_routing.json"
LAST_READ_FILE = PROJECT_DIR / "state" / "nexus_agent_dms_last_read.json"


def load_token() -> str:
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text(encoding="utf-8").strip()
    return ""


def nexus_get(path: str, token: str) -> list | dict | None:
    url = f"{NEXUS_URL}{path}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        resp = urllib.request.urlopen(req, timeout=8)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None  # Token expired — caller handles
        return None
    except Exception:
        return None


def load_routing() -> dict:
    if ROUTING_FILE.exists():
        try:
            return json.loads(ROUTING_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def load_last_read() -> dict:
    if LAST_READ_FILE.exists():
        try:
            return json.loads(LAST_READ_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_last_read(data: dict) -> None:
    try:
        tmp = LAST_READ_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(LAST_READ_FILE)
    except Exception:
        pass


def main() -> int:
    token = load_token()
    if not token:
        return 0

    routing = load_routing()
    agent_dms = routing.get("agent_dm_conversations", {})
    if not agent_dms:
        return 0

    last_read = load_last_read()
    found_any = False
    updated_last_read = dict(last_read)

    for agent_name, conv_id in agent_dms.items():
        messages = nexus_get(f"/conversations/{conv_id}/messages", token)
        if not messages:
            continue

        last_id = last_read.get(conv_id)

        # Messages are newest-first. Find new ones (before last_id in the list).
        new_msgs = []
        for msg in messages:
            if msg["id"] == last_id:
                break
            new_msgs.append(msg)

        # Process oldest-first
        for msg in reversed(new_msgs):
            sender = msg.get("sender", {}).get("username", "unknown")
            if sender == "orchestrator":
                continue  # Skip own messages
            out = {
                "agent": agent_name,
                "sender": sender,
                "message_id": msg["id"],
                "content": msg.get("content", ""),
                "timestamp": msg.get("created_at", ""),
            }
            print(json.dumps(out))
            found_any = True

        if new_msgs:
            updated_last_read[conv_id] = messages[0]["id"]

    save_last_read(updated_last_read)
    return 1 if found_any else 0


if __name__ == "__main__":
    sys.exit(main())
