#!/usr/bin/env python3
"""owner_brief.py — Generate an owner catch-up brief.

Reads: Loom DB (active goals), logs/session_log.csv (recent sessions),
       memory/latest_summary.md (HOT STATE),
       memory/work/pending_decisions.md (HIGH PRIORITY pending items).

Usage:
  python3 tools/owner_brief.py [--send] [--recent N]

  --send      Also send the brief to Telegram
  --recent N  Show last N sessions (default 7)
"""

import argparse
import csv
import os
import sqlite3
import subprocess
import sys
from datetime import datetime

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOOM_DB = os.path.expanduser("~/.local/share/loom/loom.db")
LOG_FILE = os.path.join(PROJECT_DIR, "logs", "session_log.csv")
SUMMARY_FILE = os.path.join(PROJECT_DIR, "memory", "latest_summary.md")
DECISIONS_FILE = os.path.join(PROJECT_DIR, "memory", "work", "pending_decisions.md")

STATUS_ICON = {
    "in_progress": "▶",
    "active": "◈",
    "desire": "○",
    "review": "⟁",
    "blocked": "✗",
    "completed": "✓",
}


def _load_agent_tag():
    """Read AGENT_NAME from state/agent_config.env, default 'agent'."""
    config_path = os.path.join(PROJECT_DIR, "state", "agent_config.env")
    name = "agent"
    if os.path.exists(config_path):
        with open(config_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("AGENT_NAME="):
                    name = line.split("=", 1)[1].strip()
                    break
    return "@" + name[:1].upper() + name[1:]


AGENT_TAG = _load_agent_tag()


def get_goals():
    """Return active (non-abandoned, non-completed) goals from Loom DB."""
    if not os.path.exists(LOOM_DB):
        return None
    try:
        con = sqlite3.connect(LOOM_DB)
        rows = con.execute(
            "SELECT id, status, priority, name FROM goals "
            "WHERE status NOT IN ('abandoned', 'completed') "
            "ORDER BY priority DESC"
        ).fetchall()
        con.close()
        return rows
    except Exception as e:
        return None


def get_hot_state():
    """Return the HOT STATE lines from latest_summary.md."""
    if not os.path.exists(SUMMARY_FILE):
        return ["(no latest_summary.md found)"]
    try:
        result, in_hot = [], False
        with open(SUMMARY_FILE, encoding="utf-8") as f:
            for line in f:
                if "HOT STATE" in line:
                    in_hot = True
                    continue
                if in_hot:
                    if line.startswith("##"):
                        break
                    stripped = line.rstrip()
                    if stripped:
                        result.append(stripped)
        return result or ["(empty)"]
    except Exception as e:
        return [f"(error: {e})"]


def get_recent_sessions(n):
    """Return last N rows from session_log.csv."""
    try:
        rows = []
        with open(LOG_FILE, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(row)
        return rows[-n:]
    except Exception:
        return []


def get_recent_commits(n):
    """Return last N git log --oneline entries from this project's own repo."""
    try:
        result = subprocess.run(
            ["git", "-C", PROJECT_DIR, "log", "--oneline", f"-{n}"],
            capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split("\n")
        return []
    except Exception:
        return []


def get_pending_high():
    """Extract HIGH PRIORITY items from pending_decisions.md.
    Returns list of {'title': str, 'needed': str|None}.
    """
    if not os.path.exists(DECISIONS_FILE):
        return None
    try:
        items, current, in_high = [], None, False
        with open(DECISIONS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.rstrip()
                if "## HIGH PRIORITY" in line:
                    in_high = True
                    continue
                if in_high and line.startswith("## ") and "HIGH" not in line:
                    break
                if not in_high:
                    continue
                if line.startswith("### "):
                    if current:
                        items.append(current)
                    current = {"title": line[4:].strip(), "needed": None}
                elif current and line.startswith("- What's needed:"):
                    current["needed"] = line[len("- What's needed:"):].strip()
        if current:
            items.append(current)
        return items
    except Exception:
        return None


def main():
    parser = argparse.ArgumentParser(description="Owner catch-up brief")
    parser.add_argument("--send", action="store_true", help="Send to Telegram")
    parser.add_argument("--recent", type=int, default=7, help="Recent sessions to show")
    args = parser.parse_args()

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    out = []

    out.append(f"{AGENT_TAG} STATUS BRIEF — {now_str}")
    out.append("=" * 50)

    # Goals
    goals = get_goals()
    out.append("\nACTIVE GOALS (Loom):")
    if goals is None:
        out.append("  (could not read Loom DB)")
    elif not goals:
        out.append("  (no active goals)")
    else:
        for gid, status, pri, name in goals:
            icon = STATUS_ICON.get(status, "?")
            out.append(f"  [{gid}] {icon} {status:<12}  pri={pri}  {name[:42]}")

    # Hot state
    out.append("\nHOT STATE:")
    for line in get_hot_state():
        out.append(f"  {line}")

    # Recent sessions
    sessions = get_recent_sessions(args.recent)
    out.append(f"\nRECENT {len(sessions)} SESSIONS:")
    for s in sessions:
        ts = (s.get("timestamp") or "?")[:16]
        stype = (s.get("session_type") or "?")[:10]
        raw_dur = s.get("duration_minutes") or "?"
        try:
            dur = f"{int(float(raw_dur)):>3}m"
        except (ValueError, TypeError):
            dur = "  ?m"
        summary = (s.get("one_line_summary") or "")[:58]
        out.append(f"  {ts}  {stype:<10}  {dur}  {summary}")

    # Recent commits
    commits = get_recent_commits(6)
    out.append("\nRECENT COMMITS (this repo):")
    if commits:
        for c in commits:
            out.append(f"  {c[:72]}")
    else:
        out.append("  (could not read git log)")

    # Pending decisions
    decisions = get_pending_high()
    out.append("\nPENDING (HIGH PRIORITY):")
    if decisions is None:
        out.append("  (pending_decisions.md not found)")
    elif not decisions:
        out.append("  (none)")
    else:
        for d in decisions:
            out.append(f"  • {d['title']}")
            if d["needed"]:
                out.append(f"    -> {d['needed'][:72]}")

    brief = "\n".join(out)
    print(brief)

    if args.send:
        tool = os.path.join(PROJECT_DIR, "tools", "telegram_send.sh")
        try:
            result = subprocess.run(
                ["bash", tool], input=brief, text=True, capture_output=True
            )
            if result.returncode == 0:
                print(f"[Telegram: {result.stdout.strip()}]", file=sys.stderr)
            else:
                print(f"[Telegram send failed: {result.stderr.strip()}]", file=sys.stderr)
        except Exception as e:
            print(f"[Telegram error: {e}]", file=sys.stderr)


if __name__ == "__main__":
    main()
