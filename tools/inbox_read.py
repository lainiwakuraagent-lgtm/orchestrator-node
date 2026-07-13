#!/usr/bin/env python3
"""
inbox_read.py — Read and process unread inbox entries.

Called from execution session startup to surface messages from conversational
sessions: task requests, ideas, context updates, agent messages.

Usage:
    python3 tools/inbox_read.py               # print unprocessed entries as JSON
    python3 tools/inbox_read.py --mark-read   # mark all as processed after reading
    python3 tools/inbox_read.py --summary     # print human-readable summary

Exit 0 always. Prints to stdout.

Inbox file: inbox/pending.json
"""

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
INBOX_FILE = PROJECT_DIR / "inbox" / "pending.json"


def load_inbox() -> list:
    if not INBOX_FILE.exists():
        return []
    try:
        return json.loads(INBOX_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return []


def save_inbox(entries: list) -> None:
    tmp = INBOX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(INBOX_FILE)


def format_summary(entries: list) -> str:
    if not entries:
        return "inbox: empty"
    lines = [f"inbox: {len(entries)} unprocessed entries"]
    for i, e in enumerate(entries, 1):
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(e.get("timestamp", 0)))
        lines.append(f"  [{i}] [{e.get('type','?')}] from={e.get('from','?')} at={ts}")
        lines.append(f"      {e.get('content','')[:120]}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Read inbox/pending.json")
    parser.add_argument("--mark-read", action="store_true", help="Mark all entries as processed")
    parser.add_argument("--summary", action="store_true", help="Print human-readable summary")
    args = parser.parse_args()

    entries = load_inbox()
    unprocessed = [e for e in entries if not e.get("processed", False)]

    if args.summary:
        print(format_summary(unprocessed))
    else:
        print(json.dumps(unprocessed, indent=2))

    if args.mark_read and unprocessed:
        for e in entries:
            if not e.get("processed", False):
                e["processed"] = True
        save_inbox(entries)

    return 0


if __name__ == "__main__":
    sys.exit(main())
