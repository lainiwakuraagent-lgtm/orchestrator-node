#!/usr/bin/env python3
"""
inbox_append.py — Append a message to the conversational inbox.

Called from conversational sessions when Andrii mentions something that
needs follow-up in an execution session (task request, idea, context update).

Usage:
    python3 tools/inbox_append.py --type task_request --content "message text" [--from andrii] [--source telegram]

Output: prints the appended entry as JSON.
Exit 0 on success, 1 on error.

Inbox file: inbox/pending.json
Entry types: task_request | idea | agent_message | context_update
"""

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
INBOX_FILE = PROJECT_DIR / "inbox" / "pending.json"

VALID_TYPES = {"task_request", "idea", "agent_message", "context_update"}


def load_inbox() -> list:
    if INBOX_FILE.exists():
        try:
            return json.loads(INBOX_FILE.read_text())
        except (json.JSONDecodeError, OSError):
            return []
    return []


def save_inbox(entries: list) -> None:
    INBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = INBOX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(INBOX_FILE)


def main() -> int:
    parser = argparse.ArgumentParser(description="Append to inbox/pending.json")
    parser.add_argument("--type", required=True, choices=sorted(VALID_TYPES), help="Entry type")
    parser.add_argument("--content", required=True, help="Message content")
    parser.add_argument("--from", dest="from_", default="andrii", help="Source agent/user")
    parser.add_argument("--source", default="telegram", help="Channel (telegram, nexus, internal)")
    args = parser.parse_args()

    entry = {
        "source": args.source,
        "from": args.from_,
        "content": args.content,
        "timestamp": int(time.time()),
        "type": args.type,
        "processed": False,
    }

    entries = load_inbox()
    entries.append(entry)
    save_inbox(entries)

    print(json.dumps(entry))
    return 0


if __name__ == "__main__":
    sys.exit(main())
