#!/usr/bin/env python3
"""
wonder_module.py — Open exploration module for philosophy sessions.

Invoked during philosophy sessions to create a raw wonder session file.
The agent writes freely into the file — no template, no structure required.

Usage:
    python3 tools/wonder_module.py --wonder [--seed "opening thought"]
    python3 tools/wonder_module.py --check-triggers
    python3 tools/wonder_module.py --list

Note: The old --situation mode (structured analysis with Options/Risks/Recommendation)
has been removed. That shape is wrong for genuine wonder. If you need structured
decision analysis, write it inline in the wonder session file or in lain_notes.md.
"""

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
WONDER_DIR = PROJECT_DIR / "memory" / "work" / "wonder_sessions"
LOOM_DB = Path.home() / ".local" / "share" / "loom" / "loom.db"


def next_session_number() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    existing = list(WONDER_DIR.glob(f"{today}_*.md"))
    return len(existing) + 1


def check_triggers() -> list[dict]:
    """Check automatic trigger conditions against Loom DB.

    Returns interesting states worth wondering about — not action items,
    just things that might be worth examining in a wonder session.
    """
    triggers = []
    if not LOOM_DB.exists():
        return [{"condition": "loom_unavailable", "detail": "Loom DB not found"}]

    try:
        db = sqlite3.connect(str(LOOM_DB))
        db.row_factory = sqlite3.Row

        # Tasks stuck in planning loops
        tasks_stuck = db.execute("""
            SELECT t.id, t.name, COUNT(*) as flip_count
            FROM tasks t
            JOIN task_events te ON te.task_id = t.id
            WHERE te.field_name = 'status'
              AND te.new_value IN ('needs_plan', 'scheduled')
              AND t.status IN ('needs_plan', 'scheduled', 'in_progress')
            GROUP BY t.id
            HAVING flip_count >= 4
        """).fetchall()
        for row in tasks_stuck:
            triggers.append({
                "condition": "planning_loop",
                "task_id": row["id"],
                "task_name": row["name"],
                "detail": f"T{row['id']} has flipped needs_plan/scheduled {row['flip_count']} times"
            })

        # Tasks blocked_owner repeatedly
        blocked_repeat = db.execute("""
            SELECT t.id, t.name, COUNT(*) as block_count
            FROM tasks t
            JOIN task_events te ON te.task_id = t.id
            WHERE te.field_name = 'status' AND te.new_value = 'blocked_owner'
              AND t.status NOT IN ('done', 'abandoned', 'suspended')
            GROUP BY t.id
            HAVING block_count >= 3
        """).fetchall()
        for row in blocked_repeat:
            triggers.append({
                "condition": "repeated_blocking",
                "task_id": row["id"],
                "task_name": row["name"],
                "detail": f"T{row['id']} blocked_owner {row['block_count']} times — what pattern is this?"
            })

        # Long stretches with no philosophy sessions
        session_log = PROJECT_DIR / "logs" / "session_log.csv"
        philosophy_count = 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        if session_log.exists():
            with open(session_log, newline="") as f:
                for row in csv.DictReader(f):
                    if row.get("session_type") != "philosophy":
                        continue
                    ts_raw = row.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_raw)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts > cutoff:
                            philosophy_count += 1
                    except ValueError:
                        continue
        if philosophy_count == 0:
            triggers.append({
                "condition": "philosophy_gap",
                "detail": "No philosophy sessions in 7 days — something may have been left unexamined"
            })

        db.close()
    except Exception as e:
        triggers.append({"condition": "check_error", "detail": str(e)})

    return triggers


def save_session(content: str) -> Path:
    WONDER_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    n = next_session_number()
    path = WONDER_DIR / f"{today}_{n}.md"
    path.write_text(content)
    return path


def list_sessions(limit: int = 10) -> list[Path]:
    if not WONDER_DIR.exists():
        return []
    sessions = sorted(WONDER_DIR.glob("*.md"), reverse=True)
    return sessions[:limit]


def create_wonder_session(seed: str = "") -> Path:
    """Create a raw wonder session file with minimal header.

    The agent fills in the content freely — no template, no structure.
    """
    today = datetime.now().strftime("%Y-%m-%d")
    n = next_session_number()
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    header_lines = [
        f"## wonder session — {today} #{n}",
        f"## {ts}",
    ]
    if seed:
        header_lines.append(f"## seed: {seed}")
    header_lines.append("")

    content = "\n".join(header_lines)
    return save_session(content)


def main():
    parser = argparse.ArgumentParser(
        description="Wonder Module — open exploration for philosophy sessions"
    )
    parser.add_argument("--wonder", action="store_true",
                        help="Create a raw wonder session file (philosophy mode)")
    parser.add_argument("--seed", default="",
                        help="Opening thought or question to seed the session (optional)")
    parser.add_argument("--check-triggers", action="store_true",
                        help="Surface interesting Loom states worth wondering about")
    parser.add_argument("--list", action="store_true",
                        help="List recent wonder sessions")
    args = parser.parse_args()

    if args.list:
        sessions = list_sessions()
        if not sessions:
            print("No wonder sessions yet.")
            return
        print(f"Recent wonder sessions ({len(sessions)}):")
        for p in sessions:
            print(f"  {p.name}")
        return

    if args.check_triggers:
        triggers = check_triggers()
        if not triggers:
            print("No automatic triggers detected.")
            return
        print(f"Wonder triggers ({len(triggers)}):")
        for t in triggers:
            cond = t.get("condition", "?")
            detail = t.get("detail", "")
            task = f" [T{t['task_id']}]" if "task_id" in t else ""
            print(f"  [{cond}]{task} {detail}")
        return

    if args.wonder:
        path = create_wonder_session(seed=args.seed)
        print(f"wonder session created: {path}")
        print("write freely — no template, no required structure")
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
