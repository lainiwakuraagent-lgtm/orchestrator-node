#!/usr/bin/env python3
"""
request_replan.py — Escape hatch for execution sessions.

When an execution session hits trouble it cannot resolve, it calls this script
to transition a task to needs_plan status with a handoff note explaining why.
The next session's dispatcher will then naturally select a planning session.

This is a script-enforced transition, not a self-granted session type change.
The agent does NOT switch its own session type mid-session; it marks the task
and exits, letting the dispatcher handle the rest.

Usage:
  python3 scripts/request_replan.py --task-id 42 --reason "dependency X broke, need new approach"
  python3 scripts/request_replan.py --task-id 42 --reason "blocked on missing API" --loom-db /path/to/loom.db
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

LOOM_DB_PATH = (Path(os.environ["LOOM_DB"]) if "LOOM_DB" in os.environ
                else Path.home() / ".local" / "share" / "loom" / "loom.db")


def parse_args():
    p = argparse.ArgumentParser(
        description="Transition a Loom task to needs_plan status (replan escape hatch)"
    )
    p.add_argument("--task-id", required=True, type=int,
                   help="Loom task ID to transition")
    p.add_argument("--reason", required=True,
                   help="Why replanning is needed (stored as handoff_note)")
    p.add_argument("--loom-db", default=None,
                   help="Override Loom DB path (default: ~/.local/share/loom/loom.db)")
    return p.parse_args()


def main():
    args = parse_args()
    db_path = Path(args.loom_db) if args.loom_db else LOOM_DB_PATH

    if not db_path.exists():
        print(f"ERROR: Loom DB not found at {db_path}", file=sys.stderr)
        sys.exit(1)

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as e:
        print(f"ERROR: Cannot open Loom DB: {e}", file=sys.stderr)
        sys.exit(1)

    # Verify the task exists and get current status
    row = conn.execute("SELECT id, name, status FROM tasks WHERE id = ?",
                       (args.task_id,)).fetchone()
    if not row:
        print(f"ERROR: Task {args.task_id} not found in Loom DB", file=sys.stderr)
        conn.close()
        sys.exit(1)

    old_status = row["status"]
    task_name = row["name"]
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Transition to needs_plan
    conn.execute(
        "UPDATE tasks SET status = 'needs_plan', handoff_note = ?, updated_at = ? "
        "WHERE id = ?",
        (args.reason, now_iso, args.task_id)
    )

    # Record the status change in task_events if the table exists
    try:
        conn.execute(
            "INSERT INTO task_events (task_id, event_type, field_name, old_value, new_value, changed_at) "
            "VALUES (?, 'updated', 'status', ?, 'needs_plan', ?)",
            (args.task_id, old_status, now_iso)
        )
    except sqlite3.OperationalError:
        pass  # task_events table may not exist

    conn.commit()
    conn.close()

    print(
        f"Task {args.task_id} ({task_name}): {old_status} -> needs_plan | "
        f"reason: {args.reason}",
        file=sys.stderr,
    )
    print(f"OK: Task {args.task_id} transitioned to needs_plan")


if __name__ == "__main__":
    main()
