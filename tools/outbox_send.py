#!/usr/bin/env python3
"""
outbox_send.py — Write a typed entry to state/conversation/outbox.json.

Used by the execution layer to proactively push messages or questions to
Andrii (via Telegram) or other agents (via Nexus DM), without directly
touching those APIs. The conversational layer (telegram_watcher.py) polls
the outbox on each cycle and routes each entry based on type+to.

Schema:
    {
        "id": "<8-char uuid>",
        "from": "<sender>",
        "type": "message" | "question",
        "to": "owner" | "agent:<name>",
        "content": "<text>",
        "timestamp": <unix_ts>,
        "sent": false,
        "priority": "normal" | "high"   (optional)
    }

Routing (done by telegram_watcher.py):
    type=message  + to=owner         → Telegram
    type=question + to=owner         → Telegram (prefixed "Question for you:")
    type=message  + to=agent:<name>  → Nexus DM (conversation in nexus_routing.json)
    type=question + to=agent:<name>  → Nexus DM (question framing)

Usage:
    python3 tools/outbox_send.py --content "message text"
    python3 tools/outbox_send.py --content "..." --type question --to owner
    python3 tools/outbox_send.py --content "..." --type message --to agent:asuka
    echo "message text" | python3 tools/outbox_send.py
    python3 tools/outbox_send.py --check   # print pending count (for testing)
"""

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
OUTBOX_FILE = PROJECT_DIR / "state" / "conversation" / "outbox.json"


def load_outbox() -> list:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if OUTBOX_FILE.exists():
        try:
            return json.loads(OUTBOX_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def save_outbox(entries: list) -> None:
    OUTBOX_FILE.write_text(json.dumps(entries, indent=2))


def send_message(
    content: str,
    sender: str = "execution_layer",
    msg_type: str = "message",
    to: str = "owner",
    priority: str | None = None,
) -> None:
    entries = load_outbox()
    entry: dict = {
        "id": str(uuid.uuid4())[:8],
        "from": sender,
        "type": msg_type,
        "to": to,
        "content": content,
        "timestamp": int(time.time()),
        "sent": False,
    }
    if priority:
        entry["priority"] = priority
    entries.append(entry)
    save_outbox(entries)
    print(f"outbox: queued {len([e for e in entries if not e.get('sent')])} pending")


def check_pending() -> int:
    entries = load_outbox()
    pending = [e for e in entries if not e.get("sent")]
    print(f"outbox: {len(pending)} pending, {len(entries)} total")
    return len(pending)


def main() -> int:
    parser = argparse.ArgumentParser(description="Write to execution outbox")
    parser.add_argument("--content", "-c", help="Message content (or read from stdin)")
    parser.add_argument("--from", dest="sender", default="execution_layer",
                        help="Sender label (default: execution_layer)")
    parser.add_argument("--type", dest="msg_type", default="message",
                        choices=["message", "question"],
                        help="Entry type: message (default) or question")
    parser.add_argument("--to", dest="to", default="owner",
                        help="Destination: 'owner' (default) or 'agent:<name>'")
    parser.add_argument("--priority", default=None,
                        choices=["normal", "high"],
                        help="Optional priority flag")
    parser.add_argument("--check", action="store_true",
                        help="Check pending count only, do not write")
    args = parser.parse_args()

    if args.check:
        check_pending()
        return 0

    content = args.content
    if not content:
        if not sys.stdin.isatty():
            content = sys.stdin.read().strip()
        else:
            print("ERROR: provide --content or pipe message via stdin", file=sys.stderr)
            return 1

    if not content:
        print("ERROR: empty message", file=sys.stderr)
        return 1

    send_message(content, sender=args.sender, msg_type=args.msg_type,
                 to=args.to, priority=args.priority)
    return 0


if __name__ == "__main__":
    sys.exit(main())
