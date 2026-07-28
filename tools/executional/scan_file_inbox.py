#!/usr/bin/env python3
"""
scan_file_inbox.py — Scan ~/lain/file_inbox/ for new files.

Run during maintenance sessions to surface files dropped into the inbox
by Andrii or other machines. Logs findings to logs/maintenance_decisions.md.

Usage:
    /usr/bin/python3 tools/scan_file_inbox.py [--project-dir DIR]
    Returns exit code 0 always (non-fatal).
"""

import os
import sys
import json
import argparse
from datetime import datetime
from pathlib import Path

FILE_INBOX = Path("/home/andrii/lain/file_inbox")
STATE_FILE = Path("/home/andrii/lain/agent_project/state/file_inbox_seen.json")
MAINTENANCE_LOG = Path("/home/andrii/lain/agent_project/logs/maintenance_decisions.md")


def load_seen() -> set:
    if STATE_FILE.exists():
        try:
            return set(json.loads(STATE_FILE.read_text()))
        except Exception:
            pass
    return set()


def save_seen(seen: set) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(sorted(seen), indent=2))


def log_findings(new_files: list) -> None:
    if not new_files:
        return
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"\n## File Inbox Scan — {ts}\n"]
    for f in new_files:
        size = f.stat().st_size
        mtime = datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        lines.append(f"- NEW: `{f.name}` ({size:,} bytes, modified {mtime})")
    lines.append(f"  Total: {len(new_files)} new file(s). Review and process manually.\n")
    MAINTENANCE_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(MAINTENANCE_LOG, "a") as fh:
        fh.write("\n".join(lines) + "\n")


def scan() -> int:
    if not FILE_INBOX.exists():
        print(f"file_inbox not found: {FILE_INBOX}")
        return 0

    seen = load_seen()
    all_files = [f for f in FILE_INBOX.rglob("*") if f.is_file() and f.name != "README.md"]
    new_files = [f for f in all_files if str(f) not in seen]

    if not new_files:
        print(f"file_inbox: {len(all_files)} file(s), 0 new.")
        return 0

    print(f"file_inbox: {len(all_files)} file(s), {len(new_files)} NEW:")
    for f in new_files:
        print(f"  + {f.name} ({f.stat().st_size:,} bytes)")
        seen.add(str(f))

    log_findings(new_files)
    save_seen(seen)
    return len(new_files)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", default=None)
    args = parser.parse_args()
    # project-dir arg is accepted for consistency but scan_file_inbox uses fixed path
    sys.exit(0) if scan() >= 0 else sys.exit(0)
