#!/usr/bin/env python3
"""
notify_task_complete.py — Notify owner via outbox when high-priority Loom tasks complete.

Called at session shutdown (after analytics_write.py). Checks task_events for tasks
marked 'done' during this session, filters by priority threshold, and writes an outbox
entry if CONV_ACTIVE=0 (no live conversational session).

Usage:
  python3 tools/notify_task_complete.py [--min-priority N] [--dry-run]

  --min-priority N    Minimum Loom task priority to notify on (default: 7)
  --dry-run           Print what would be sent without writing to outbox
  --force             Send even if CONV_ACTIVE=1
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
LOOM_DB = Path.home() / ".local" / "share" / "loom" / "loom.db"
OUTBOX_FILE = PROJECT_DIR / "state" / "conversation" / "outbox.json"
SESSION_START_FILE = PROJECT_DIR / "state" / "session_start_epoch"
CONV_LOCK = PROJECT_DIR / "state" / "conversation.lock"
EXIT_REASON_FILE = PROJECT_DIR / "state" / "conversation" / "exit_reason.txt"


def _load_agent_name() -> str:
    """Read AGENT_NAME from state/agent_config.env, default 'agent'."""
    config_path = PROJECT_DIR / "state" / "agent_config.env"
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_NAME="):
                return line.split("=", 1)[1].strip()
    return "agent"


AGENT_NAME = _load_agent_name()


def is_conv_active() -> bool:
    """Return True if a conversational session is live (and not idle-closing)."""
    if not CONV_LOCK.exists():
        return False
    try:
        pid = int(CONV_LOCK.read_text().strip())
        os.kill(pid, 0)  # signal 0 = just check existence
    except (ValueError, ProcessLookupError, PermissionError):
        return False
    # PID alive — check if it's idle-closing
    if EXIT_REASON_FILE.exists():
        reason = EXIT_REASON_FILE.read_text().strip()
        if "idle_close" in reason:
            return False  # slow shutdown in progress — treat as inactive
    return True


def get_session_start_epoch() -> float:
    """Return session start as Unix timestamp, or 0 if unknown."""
    if SESSION_START_FILE.exists():
        try:
            return float(SESSION_START_FILE.read_text().strip())
        except ValueError:
            pass
    return 0.0


def get_completed_tasks(since_epoch: float, min_priority: int) -> list[dict]:
    """Return tasks marked done after since_epoch with priority >= min_priority."""
    if not LOOM_DB.exists():
        return []

    since_iso = datetime.fromtimestamp(since_epoch, tz=timezone.utc).isoformat()

    try:
        conn = sqlite3.connect(str(LOOM_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT t.id, t.name, t.priority, t.tags, te.changed_at
            FROM task_events te
            JOIN tasks t ON t.id = te.task_id
            WHERE te.event_type = 'status_change'
              AND te.new_value = 'done'
              AND te.changed_at > ?
            ORDER BY te.changed_at DESC
            """,
            (since_iso,)
        ).fetchall()
        conn.close()
    except sqlite3.Error as e:
        print(f"[notify] DB error: {e}", file=sys.stderr)
        return []

    results = []
    for r in rows:
        # Priority is stored as integer in DB (or None/"none" for unset)
        try:
            p = int(r["priority"]) if r["priority"] not in (None, "none", "") else 0
        except (ValueError, TypeError):
            p = 0
        if p >= min_priority:
            results.append({
                "id": r["id"],
                "name": r["name"],
                "priority": p,
                "tags": r["tags"] or "",
                "completed_at": r["changed_at"],
            })
    return results


def load_outbox() -> list:
    OUTBOX_FILE.parent.mkdir(parents=True, exist_ok=True)
    if OUTBOX_FILE.exists():
        try:
            return json.loads(OUTBOX_FILE.read_text())
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def already_notified(outbox: list, task_id: int) -> bool:
    """Return True if we already have an unsent notification for this task."""
    marker = f"task_complete:T{task_id}"
    for entry in outbox:
        if not entry.get("sent") and marker in entry.get("content", ""):
            return True
    return False


def write_notification(outbox: list, tasks: list[dict], dry_run: bool) -> int:
    """Write outbox entries for each task. Returns count written."""
    written = 0
    for t in tasks:
        if already_notified(outbox, t["id"]):
            print(f"[notify] T{t['id']} already in outbox, skipping")
            continue

        name_short = t["name"][:80] + ("…" if len(t["name"]) > 80 else "")
        content = (
            f"✓ Task complete [P{t['priority']}]: T{t['id']} — {name_short}  "
            f"[task_complete:T{t['id']}]"
        )
        entry = {
            "id": str(uuid.uuid4())[:8],
            "from": AGENT_NAME,
            "type": "message",
            "to": "owner",
            "content": content,
            "timestamp": int(time.time()),
            "sent": False,
            "priority": "high",
        }
        if dry_run:
            print(f"[notify] DRY RUN — would send: {content}")
        else:
            outbox.append(entry)
            print(f"[notify] Queued: T{t['id']} (P{t['priority']})")
            written += 1

    if not dry_run and written:
        OUTBOX_FILE.write_text(json.dumps(outbox, indent=2))

    return written


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--min-priority", type=int, default=7,
                        help="Minimum task priority to notify on (default: 7)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print what would be sent without writing")
    parser.add_argument("--force", action="store_true",
                        help="Send even if conversational session is active")
    args = parser.parse_args()

    # Check CONV_ACTIVE
    conv_active = is_conv_active()
    if conv_active and not args.force:
        print("[notify] CONV_ACTIVE=1 — skipping notification (conversational layer handles comms)")
        return 0

    since_epoch = get_session_start_epoch()
    if since_epoch == 0:
        print("[notify] No session_start_epoch found — skipping", file=sys.stderr)
        return 1

    tasks = get_completed_tasks(since_epoch, args.min_priority)
    if not tasks:
        print(f"[notify] No tasks with priority>={args.min_priority} completed this session")
        return 0

    print(f"[notify] {len(tasks)} task(s) qualify for notification:")
    for t in tasks:
        print(f"  T{t['id']} [P{t['priority']}] {t['name'][:60]}")

    outbox = load_outbox()
    written = write_notification(outbox, tasks, args.dry_run)
    print(f"[notify] Done — {written} notification(s) queued")
    return 0


if __name__ == "__main__":
    sys.exit(main())
