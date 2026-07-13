#!/usr/bin/env python3
"""
schedule_one_off.py
Task 40 — One-off Session Scheduler

Reads config/session_schedule.json, finds one_off entries with fired=false,
and creates a transient systemd --user timer unit for each that hasn't been
scheduled yet. When the timer fires, it calls wake.sh with the correct trigger
mode, which then matches the one_off entry by datetime proximity.

Usage:
  python3 tools/schedule_one_off.py [--project-dir DIR] [--dry-run] [--list]

Arguments:
  --project-dir DIR   Agent project root (default: directory containing this script's parent)
  --dry-run           Show what would be scheduled without creating timers
  --list              List active one_off entries and their scheduling status
  --clear-expired     Remove transient timers for entries that have fired=true

How timers are named:
  agent-one-off-{sanitized_label}.timer
  where sanitized_label is derived from entry["label"] or datetime.

How timers are created:
  systemd-run --user --unit="agent-one-off-{label}" --on-calendar="{datetime}" \\
              --property="WorkingDirectory={project_dir}" \\
              bash scripts/wake.sh prompts/goal.txt prompts/persona.txt

The unit fires once and is automatically removed by systemd after completion.
resolve_session_type.py in wake.sh picks up the one_off entry by datetime match
and marks it fired=true in the JSON.

Notes:
  - Requires systemd --user (available on systemd-based Linux desktops/servers)
  - datetime must be in the future; past entries are skipped with a warning
  - If a timer with the same name already exists, it is skipped (idempotent)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def parse_args():
    p = argparse.ArgumentParser(description="Schedule one-off agent sessions via systemd")
    p.add_argument("--project-dir", default=None,
                   help="Agent project root (default: auto-detect from script location)")
    p.add_argument("--dry-run", action="store_true",
                   help="Show what would be done without creating any timers")
    p.add_argument("--list", action="store_true",
                   help="List one_off entries and scheduling status, then exit")
    p.add_argument("--clear-expired", action="store_true",
                   help="Stop and remove timers for fired=true entries")
    return p.parse_args()


def find_project_dir(hint: str | None) -> Path:
    if hint:
        return Path(hint).resolve()
    # Assume script lives in tools/ and project root is one level up
    return Path(__file__).resolve().parent.parent


def load_schedule(project_dir: Path) -> dict:
    schedule_file = project_dir / "config" / "session_schedule.json"
    if not schedule_file.exists():
        return {}
    try:
        return json.loads(schedule_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        print(f"ERROR: could not read {schedule_file}: {e}", file=sys.stderr)
        return {}


def sanitize_label(label: str) -> str:
    """Convert arbitrary text to a valid systemd unit name fragment."""
    s = label.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    return s[:48] or "unnamed"


def timer_unit_name(entry: dict) -> str:
    label = entry.get("label") or entry.get("datetime", "unknown")
    return f"agent-one-off-{sanitize_label(label)}"


def is_timer_active(unit_name: str) -> bool:
    """Check if a transient systemd user timer exists."""
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", f"{unit_name}.timer"],
            capture_output=True, text=True
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def list_entries(schedule: dict, project_dir: Path):
    """Print status of all one_off entries."""
    entries = schedule.get("one_off", [])
    if not entries:
        print("No one_off entries in session_schedule.json.")
        return
    now = datetime.now()
    print(f"{'#':<3} {'datetime':<28} {'trigger':<10} {'type':<14} {'fired':<6} {'timer':<8} label")
    print("-" * 90)
    for i, entry in enumerate(entries):
        fired = entry.get("fired", False)
        unit = timer_unit_name(entry)
        active = "active" if is_timer_active(unit) else "none"
        dt_str = entry.get("datetime", "?")
        try:
            dt = datetime.fromisoformat(dt_str).replace(tzinfo=None)
            past = " (PAST)" if dt < now else ""
            dt_display = dt.strftime("%Y-%m-%d %H:%M") + past
        except ValueError:
            dt_display = dt_str
        print(f"{i:<3} {dt_display:<28} {entry.get('trigger','?'):<10} "
              f"{entry.get('session_type','?'):<14} {str(fired):<6} {active:<8} "
              f"{entry.get('label','(no label)')}")


def schedule_entry(entry: dict, project_dir: Path, dry_run: bool) -> bool:
    """
    Create a transient systemd timer for a one_off entry.
    Returns True if scheduled (or would be in dry-run), False if skipped.
    """
    if entry.get("fired", False):
        return False

    dt_str = entry.get("datetime", "")
    if not dt_str:
        print(f"  SKIP: missing datetime in entry: {entry}", file=sys.stderr)
        return False

    try:
        dt = datetime.fromisoformat(dt_str).replace(tzinfo=None)
    except ValueError as e:
        print(f"  SKIP: unparseable datetime '{dt_str}': {e}", file=sys.stderr)
        return False

    now = datetime.now()
    if dt < now:
        print(f"  SKIP: datetime {dt_str} is in the past ({entry.get('label', '')})")
        return False

    unit_name = timer_unit_name(entry)
    if is_timer_active(unit_name):
        print(f"  SKIP: timer '{unit_name}' already active")
        return False

    # Format datetime for systemd OnCalendar: YYYY-MM-DD HH:MM:SS
    calendar_str = dt.strftime("%Y-%m-%d %H:%M:%S")
    trigger_mode = entry.get("trigger", "manual")
    goal_file = project_dir / "prompts" / "goal.txt"
    persona_file = project_dir / "prompts" / "persona.txt"

    cmd = [
        "systemd-run",
        "--user",
        f"--unit={unit_name}",
        f"--on-calendar={calendar_str}",
        f"--property=WorkingDirectory={project_dir}",
        f"--setenv=TRIGGER_MODE={trigger_mode}",
        "--",
        "bash", str(project_dir / "scripts" / "wake.sh"),
        str(goal_file),
        str(persona_file) if persona_file.exists() else "",
    ]
    # Remove empty trailing args
    cmd = [c for c in cmd if c]

    label = entry.get("label", calendar_str)
    if dry_run:
        print(f"  DRY-RUN: would schedule '{unit_name}' at {calendar_str} "
              f"(trigger={trigger_mode}, label={label!r})")
        print(f"    cmd: {' '.join(cmd)}")
        return True

    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  SCHEDULED: '{unit_name}' → {calendar_str} ({label})")
            return True
        else:
            print(f"  ERROR: systemd-run failed for '{unit_name}': {result.stderr.strip()}",
                  file=sys.stderr)
            return False
    except FileNotFoundError:
        print("  ERROR: systemd-run not found — is systemd running in user mode?",
              file=sys.stderr)
        return False


def clear_expired(schedule: dict, project_dir: Path, dry_run: bool):
    """Stop and remove timers for entries with fired=true."""
    for entry in schedule.get("one_off", []):
        if not entry.get("fired", False):
            continue
        unit_name = timer_unit_name(entry)
        if not is_timer_active(unit_name):
            continue
        label = entry.get("label", entry.get("datetime", unit_name))
        if dry_run:
            print(f"  DRY-RUN: would stop timer '{unit_name}' ({label})")
            continue
        try:
            subprocess.run(
                ["systemctl", "--user", "stop", f"{unit_name}.timer"],
                capture_output=True
            )
            print(f"  STOPPED: '{unit_name}' ({label})")
        except FileNotFoundError:
            pass


def main():
    args = parse_args()
    project_dir = find_project_dir(args.project_dir)
    schedule = load_schedule(project_dir)

    if not schedule:
        print("No session_schedule.json found or it is empty.")
        if not args.list:
            sys.exit(0)

    if args.list:
        list_entries(schedule, project_dir)
        return

    if args.clear_expired:
        print("Clearing expired one-off timers...")
        clear_expired(schedule, project_dir, args.dry_run)
        return

    entries = schedule.get("one_off", [])
    pending = [e for e in entries if not e.get("fired", False)]

    if not pending:
        print("No pending one_off entries to schedule.")
        return

    print(f"Scheduling {len(pending)} pending one_off entry/entries...")
    scheduled = 0
    for entry in pending:
        if schedule_entry(entry, project_dir, args.dry_run):
            scheduled += 1

    print(f"\nDone: {scheduled}/{len(pending)} entries scheduled.")
    if args.dry_run:
        print("(dry-run mode — no actual timers created)")


if __name__ == "__main__":
    main()
