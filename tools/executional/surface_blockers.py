#!/usr/bin/env python3
"""
surface_blockers.py — Digest all blocked_owner tasks and write to outbox.

Runs after planning sessions. Queries Loom DB directly (cross-goal),
excludes abandoned/suspended goals. Rate-limited by content hash.

Usage:
    python3 tools/surface_blockers.py [--dry-run] [--force]
"""

import argparse
import hashlib
import json
import sqlite3
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
LOOM_DB = Path.home() / ".local/share/loom/loom.db"
HASH_FILE = PROJECT_DIR / "state" / "blocker_digest_hash.txt"
LAST_SENT_FILE = PROJECT_DIR / "state" / "blocker_digest_last_sent.txt"

sys.path.insert(0, str(PROJECT_DIR / "tools"))
from outbox import send_message


def _load_agent_tag() -> str:
    """Read AGENT_NAME from state/agent_config.env, default 'agent'."""
    config_path = PROJECT_DIR / "state" / "agent_config.env"
    name = "agent"
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_NAME="):
                name = line.split("=", 1)[1].strip()
                break
    return "@" + name[:1].upper() + name[1:]


AGENT_TAG = _load_agent_tag()


def query_blockers(db_path: Path) -> list:
    """Return all blocked_owner tasks whose goal is not abandoned/suspended."""
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("""
            SELECT t.id, t.name, t.description, t.blocked_note, t.blocked_reason
            FROM tasks t
            LEFT JOIN goals g ON t.goal_id = g.id
            WHERE t.status = 'blocked_owner'
              AND (g.status IS NULL OR g.status NOT IN ('abandoned', 'suspended'))
            ORDER BY t.id
        """).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def format_entry(task: dict) -> str:
    name = (task["name"] or "")[:80]
    what = task["blocked_note"] or (task["description"] or "")[:200] or "(no detail)"
    return (
        f"[T{task['id']}] {name}\n"
        f"What's needed: {what}\n"
        'How to answer: reply with the answer or "defer/skip/won\'t do"'
    )


def build_digest(tasks: list) -> str:
    n = len(tasks)
    entries = "\n\n".join(format_entry(t) for t in tasks)
    return (
        f"{AGENT_TAG} — Blocker Digest ({n} task{'s' if n != 1 else ''} awaiting your input)\n\n"
        f"{entries}\n\n"
        "Reply inline or in Telegram. Any decision moves the task forward."
    )


def compute_hash(tasks: list) -> str:
    key = json.dumps(
        [{"id": t["id"], "blocked_note": t["blocked_note"]} for t in tasks],
        sort_keys=True,
    )
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def main() -> int:
    parser = argparse.ArgumentParser(description="Surface blocked_owner tasks to outbox")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print digest without writing to outbox")
    parser.add_argument("--force", action="store_true",
                        help="Bypass rate-limit hash check")
    args = parser.parse_args()

    if not LOOM_DB.exists():
        print(f"ERROR: Loom DB not found at {LOOM_DB}", file=sys.stderr)
        return 1

    tasks = query_blockers(LOOM_DB)

    if not tasks:
        print("surface_blockers: 0 blocked_owner tasks — no digest sent")
        return 0

    current_hash = compute_hash(tasks)

    if not args.force and HASH_FILE.exists():
        stored = HASH_FILE.read_text().strip()
        if stored == current_hash:
            print(f"surface_blockers: blockers unchanged ({len(tasks)} tasks) — no digest sent")
            return 0

    digest = build_digest(tasks)

    if args.dry_run:
        print("=== DRY RUN ===")
        print(digest)
        print(f"=== {len(tasks)} tasks, hash {current_hash} ===")
        return 0

    send_message(digest, sender="surface_blockers", msg_type="message",
                 to="owner", priority="normal")

    HASH_FILE.write_text(current_hash)
    LAST_SENT_FILE.write_text(str(int(time.time())))

    print(f"surface_blockers: queued digest for {len(tasks)} blocked tasks")
    return 0


if __name__ == "__main__":
    sys.exit(main())
