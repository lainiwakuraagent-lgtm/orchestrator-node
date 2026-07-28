#!/usr/bin/env python3
"""session_stats.py — Session quality metrics from session_log.csv

Usage:
  python3 tools/session_stats.py [--recent N] [--csv]

Outputs:
  - Session count by type
  - Duration stats (min/avg/max)
  - Context at exit stats
  - Recent N session summaries (default 10)
"""

import csv
import sys
import os
import argparse
from collections import defaultdict, Counter
from datetime import datetime, timezone

PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
LOG_FILE = os.path.join(PROJECT_DIR, "logs", "session_log.csv")


def normalize_type(raw) -> str:
    """Normalize session type strings to canonical form."""
    if not raw:
        return "unknown"
    raw = str(raw).strip().lower()
    if raw in ("session_type", "", "null", "none"):
        return "unknown"
    if raw.startswith("abort") or raw.startswith("exit_outside") or raw.startswith("emergency-abort"):
        return "aborted"
    if "emergency" in raw:
        return "emergency"
    if "planning" in raw:
        return "planning"
    if "free" in raw:
        return "free"
    if "execution" in raw:
        return "execution"
    if raw in ("response", "exploration"):
        return "execution"
    if raw.startswith("shutdown"):
        return "aborted"
    return "other"


def parse_context_pct(raw) -> float | None:
    """Parse context percentage, handle % suffix and 'null'."""
    if not raw:
        return None
    raw = str(raw).strip().rstrip("%")
    try:
        val = float(raw)
        if val < 0 or val > 100:
            return None
        return val
    except (ValueError, TypeError):
        return None


def parse_duration(raw) -> float | None:
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        val = float(raw)
        if val < 0 or val > 600:
            return None
        return val
    except (ValueError, TypeError):
        return None


def load_sessions(log_file: str) -> list[dict]:
    sessions = []
    with open(log_file, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            # Some rows have extra fields (5-col format with extra commas in summary)
            raw_type = row.get("session_type", "")
            raw_dur = row.get("duration_minutes", "")
            raw_ctx = row.get("context_pct_at_exit", "")
            summary = row.get("one_line_summary", "")
            timestamp = row.get("timestamp", "")

            sessions.append({
                "timestamp": (timestamp or "").strip(),
                "session_type": normalize_type(raw_type),
                "raw_type": (raw_type or "").strip(),
                "duration": parse_duration(raw_dur),
                "context_pct": parse_context_pct(raw_ctx),
                "summary": (summary or "").strip(),
            })
    return sessions


def stats_of(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "avg": None, "max": None}
    return {
        "count": len(values),
        "min": min(values),
        "avg": sum(values) / len(values),
        "max": max(values),
    }


def fmt_stat(s: dict, unit: str = "") -> str:
    if s["count"] == 0:
        return "n/a"
    u = unit
    return f"min={s['min']:.1f}{u}  avg={s['avg']:.1f}{u}  max={s['max']:.1f}{u}  (n={s['count']})"


def bar(count: int, total: int, width: int = 20) -> str:
    filled = round((count / total) * width) if total else 0
    return "[" + "#" * filled + "." * (width - filled) + "]"


def main():
    parser = argparse.ArgumentParser(description="Session quality stats")
    parser.add_argument("--recent", type=int, default=10,
                        help="Number of recent sessions to display (default: 10)")
    parser.add_argument("--csv", action="store_true",
                        help="Emit raw CSV rows for the recent sessions")
    args = parser.parse_args()

    if not os.path.exists(LOG_FILE):
        print(f"ERROR: {LOG_FILE} not found", file=sys.stderr)
        sys.exit(1)

    sessions = load_sessions(LOG_FILE)
    # Filter out the header row that gets included when csv.DictReader misparses
    sessions = [s for s in sessions if s["raw_type"] not in ("session_type",)]

    total = len(sessions)
    if total == 0:
        print("No sessions found.")
        return

    # --- Type distribution ---
    type_counts = Counter(s["session_type"] for s in sessions)
    type_order = ["execution", "free", "planning", "emergency", "aborted", "other", "unknown"]

    print(f"\n{'=' * 56}")
    print(f"  SESSION STATS  —  {total} sessions total")
    print(f"{'=' * 56}")
    print("\n  TYPE DISTRIBUTION")
    print(f"  {'Type':<14}  {'Count':>6}  {'%':>5}  {'Bar'}")
    print(f"  {'-' * 52}")
    for t in type_order:
        c = type_counts.get(t, 0)
        if c == 0:
            continue
        pct = 100.0 * c / total
        print(f"  {t:<14}  {c:>6}  {pct:>4.1f}%  {bar(c, total)}")

    # --- Duration stats ---
    durations = [s["duration"] for s in sessions if s["duration"] is not None]
    ctx_pcts = [s["context_pct"] for s in sessions if s["context_pct"] is not None]

    dur_by_type: dict[str, list[float]] = defaultdict(list)
    ctx_by_type: dict[str, list[float]] = defaultdict(list)
    for s in sessions:
        if s["duration"] is not None:
            dur_by_type[s["session_type"]].append(s["duration"])
        if s["context_pct"] is not None:
            ctx_by_type[s["session_type"]].append(s["context_pct"])

    print("\n  DURATION (minutes)")
    overall_dur = stats_of(durations)
    print(f"  overall:        {fmt_stat(overall_dur, 'min')}")
    for t in type_order:
        vals = dur_by_type.get(t, [])
        if vals:
            print(f"  {t:<14}  {fmt_stat(stats_of(vals), 'min')}")

    print("\n  CONTEXT AT EXIT (%)")
    overall_ctx = stats_of(ctx_pcts)
    print(f"  overall:        {fmt_stat(overall_ctx, '%')}")
    for t in type_order:
        vals = ctx_by_type.get(t, [])
        if vals:
            print(f"  {t:<14}  {fmt_stat(stats_of(vals), '%')}")

    # --- Aborted / short session analysis ---
    aborted = [s for s in sessions if s["session_type"] == "aborted"]
    short = [s for s in sessions if s["duration"] is not None and s["duration"] < 5 and s["session_type"] != "aborted"]
    if aborted or short:
        print(f"\n  PROBLEM SESSIONS")
        if aborted:
            print(f"  Aborted/outside-window: {len(aborted)}")
        if short:
            print(f"  Very short (<5 min, not aborted): {len(short)}")

    # --- Recent sessions ---
    recent = sessions[-args.recent:]
    print(f"\n  RECENT {min(args.recent, len(sessions))} SESSIONS")
    print(f"  {'Timestamp':<24}  {'Type':<12}  {'Dur':>5}  {'Ctx':>4}  Summary")
    print(f"  {'-' * 80}")
    if args.csv:
        for s in recent:
            print(f"  {s['timestamp']},{s['raw_type']},{s['duration']},{s['context_pct']},{s['summary']}")
    else:
        for s in recent:
            ts = s["timestamp"][:19] if s["timestamp"] else "?"
            dur = f"{s['duration']:.0f}m" if s["duration"] is not None else "  ?"
            ctx = f"{s['context_pct']:.0f}%" if s["context_pct"] is not None else "  ?"
            typ = s["session_type"][:12]
            summary = s["summary"][:55] + ("…" if len(s["summary"]) > 55 else "")
            print(f"  {ts:<24}  {typ:<12}  {dur:>5}  {ctx:>4}  {summary}")

    print(f"\n{'=' * 56}\n")


if __name__ == "__main__":
    main()
