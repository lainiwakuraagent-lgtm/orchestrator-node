#!/usr/bin/env python3
"""
schedule_lain.py — Issue a schedule directive to an agent's inbox.

Reads @Lain's schedule_analytics.json (if available), constructs a
schedule_directive inbox entry, and writes it to the target agent's
inbox/pending.json. Called by Ting during its weekly schedule review.

Usage:
    python3 tools/schedule_lain.py \\
        --action adjust_triggers \\
        --window-label gcal-nightly-sessions \\
        --triggers "23:15,01:00,02:45" \\
        --reason "23-01 bucket 50%% more productive than 01-03" \\
        [--target-agent lain] \\
        [--dry-run]

Supported actions:
    adjust_triggers    -- replace trigger list in a named window
    set_window_enabled -- enable or disable a window
    add_window         -- add a new window entry
    remove_window      -- remove a window by label
    set_type_hint      -- set session_type_hint on a window

Target agent paths (same-machine, fokacco-hp):
    lain  → /home/andrii/lain/agent_project/inbox/pending.json

Exit 0 on success, 1 on error.
"""

import argparse
import json
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
ORCHESTRATOR_DIR = SCRIPT_DIR.parent

# Analytics report written by @Lain's maintenance sessions
LAIN_ANALYTICS = Path("/home/andrii/lain/agent_project/state/reports/schedule_analytics.json")

# Decision log for Ting
DECISION_LOG = ORCHESTRATOR_DIR / "memory" / "work" / "schedule_decisions.md"

# Agent inbox registry (same machine)
AGENT_INBOXES = {
    "lain": Path("/home/andrii/lain/agent_project/inbox/pending.json"),
}


def load_analytics(analytics_path: Path) -> dict | None:
    """Load schedule_analytics.json if it exists and is fresh (< 7 days old)."""
    if not analytics_path.exists():
        return None
    try:
        data = json.loads(analytics_path.read_text())
        generated_at = data.get("generated_at", "")
        if generated_at:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
            if age_days > 7:
                print(f"warning: schedule_analytics.json is {age_days:.1f} days old", file=sys.stderr)
        return data
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"warning: could not load analytics: {e}", file=sys.stderr)
        return None


def build_payload(args: argparse.Namespace) -> dict:
    """Build the action payload from CLI args."""
    payload: dict = {}

    if args.action in ("adjust_triggers", "set_window_enabled", "set_type_hint",
                        "remove_window", "add_window"):
        if args.window_label:
            payload["window_label"] = args.window_label

    if args.action == "adjust_triggers":
        if not args.triggers:
            print("error: --triggers required for adjust_triggers", file=sys.stderr)
            sys.exit(1)
        payload["new_triggers"] = [t.strip() for t in args.triggers.split(",")]

    elif args.action == "set_window_enabled":
        payload["enabled"] = args.enabled.lower() in ("true", "1", "yes")

    elif args.action == "set_type_hint":
        if not args.type_hint:
            print("error: --type-hint required for set_type_hint", file=sys.stderr)
            sys.exit(1)
        payload["session_type_hint"] = args.type_hint

    elif args.action == "add_window":
        payload["start"] = args.window_start or "00:00"
        payload["end"] = args.window_end or "08:00"
        payload["triggers"] = [t.strip() for t in args.triggers.split(",")] if args.triggers else []
        if args.type_hint:
            payload["session_type_hint"] = args.type_hint

    return payload


def write_to_inbox(inbox_path: Path, entry: dict) -> None:
    """Atomically append entry to inbox/pending.json."""
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    entries = []
    if inbox_path.exists():
        try:
            entries = json.loads(inbox_path.read_text())
        except (json.JSONDecodeError, OSError):
            entries = []
    entries.append(entry)
    tmp = inbox_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(entries, indent=2))
    tmp.rename(inbox_path)


def log_decision(analytics: dict | None, entry: dict) -> None:
    """Append decision to orchestrator memory/work/schedule_decisions.md."""
    DECISION_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts_now = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
    week = entry.get("week_starting", "?")

    analytics_summary = "no analytics available"
    if analytics:
        overall = analytics.get("overall", {})
        buckets = analytics.get("window_analysis", [])
        best = max(buckets, key=lambda b: b.get("avg_tasks_completed", 0), default=None)
        worst = min(buckets, key=lambda b: b.get("avg_tasks_completed", 0), default=None)
        analytics_summary = (
            f"{overall.get('total_sessions', '?')} sessions, "
            f"{overall.get('total_tasks_completed', '?')} tasks total | "
            f"best bucket: {best['hour_bucket']} ({best['avg_tasks_completed']} t/s)" if best else ""
        )

    payload = entry.get("payload", {})
    log_entry = (
        f"\n## {ts_now} — directive issued (week={week})\n"
        f"Analytics: {analytics_summary}\n"
        f"Action: {entry.get('action')} | payload: {json.dumps(payload)}\n"
        f"Reason: {entry.get('reason', '?')}\n"
    )
    try:
        with DECISION_LOG.open("a") as f:
            f.write(log_entry)
    except OSError as e:
        print(f"warning: could not write decision log: {e}", file=sys.stderr)


def main() -> int:
    parser = argparse.ArgumentParser(description="Issue a schedule directive to an agent's inbox")
    parser.add_argument("--action", required=True,
                        choices=["adjust_triggers", "set_window_enabled", "add_window",
                                 "remove_window", "set_type_hint"],
                        help="Directive action type")
    parser.add_argument("--window-label", default="", help="Target window label")
    parser.add_argument("--triggers", default="", help="Comma-separated trigger times (HH:MM)")
    parser.add_argument("--enabled", default="true", help="Enabled flag for set_window_enabled")
    parser.add_argument("--type-hint", default="", help="Session type hint for set_type_hint/add_window")
    parser.add_argument("--window-start", default="", help="Window start time for add_window")
    parser.add_argument("--window-end", default="", help="Window end time for add_window")
    parser.add_argument("--reason", default="", help="Human-readable reason for the directive")
    parser.add_argument("--target-agent", default="lain",
                        choices=list(AGENT_INBOXES.keys()),
                        help="Target agent (default: lain)")
    parser.add_argument("--week-starting", default="",
                        help="ISO date of week being reviewed (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Print entry without writing")
    args = parser.parse_args()

    inbox_path = AGENT_INBOXES[args.target_agent]
    analytics = load_analytics(LAIN_ANALYTICS)

    week_starting = args.week_starting
    if not week_starting:
        from datetime import date, timedelta
        week_starting = (date.today() - timedelta(days=7)).isoformat()

    payload = build_payload(args)

    entry: dict = {
        "type": "schedule_directive",
        "timestamp": int(time.time()),
        "source": "orchestrator",
        "processed": False,
        "action": args.action,
        "week_starting": week_starting,
        "payload": payload,
        "reason": args.reason,
    }

    if args.dry_run:
        print("[DRY RUN] would write to", inbox_path)
        print(json.dumps(entry, indent=2))
        return 0

    write_to_inbox(inbox_path, entry)
    log_decision(analytics, entry)

    print(f"schedule_directive written → {inbox_path}")
    print(f"  action: {args.action} | payload: {json.dumps(payload)}")
    if args.reason:
        print(f"  reason: {args.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
