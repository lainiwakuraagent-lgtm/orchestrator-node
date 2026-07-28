#!/usr/bin/env python3
"""
recap_generator.py — Generate catch-up recap for the agent's conversational sessions.

When a conversational session starts after a meaningful gap, this script assembles
structured facts (Loom task events, Nexus messages, sessions ran, reports) and
narrates them via a Haiku LLM call into a standalone-readable recap message.

Usage:
    python3 tools/recap_generator.py check            # prints RECAP or NO_RECAP
    python3 tools/recap_generator.py generate          # gate + gather + narrate + write
    python3 tools/recap_generator.py generate --force   # skip gate, always generate

Part of Communication Layer v2, Phase 3.
Written: 2026-07-16, @Lain
"""

import csv
import json
import os
import re
import sqlite3
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
LOOM_DB = Path.home() / ".local" / "share" / "loom" / "loom.db"


def _load_agent_config() -> tuple[str, str]:
    """Read AGENT_NAME/OWNER_NAME from state/agent_config.env, defaults 'agent'/'owner'."""
    config_path = PROJECT_DIR / "state" / "agent_config.env"
    name, owner = "agent", "owner"
    if config_path.exists():
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("AGENT_NAME="):
                name = line.split("=", 1)[1].strip()
            elif line.startswith("OWNER_NAME="):
                owner = line.split("=", 1)[1].strip()
    return name, owner


_AGENT_NAME, _OWNER_NAME = _load_agent_config()
AGENT_TAG = "@" + _AGENT_NAME[:1].upper() + _AGENT_NAME[1:]

STATE_DIR = PROJECT_DIR / "state"
CONV_STATE_DIR = STATE_DIR / "conversation"
REPORTS_DIR = STATE_DIR / "reports"
LOGS_DIR = PROJECT_DIR / "logs"

RECAP_TS_FILE = CONV_STATE_DIR / "last_recap_ts.txt"
RECAP_JSON_FILE = CONV_STATE_DIR / "last_recap.json"
RECAP_MD_FILE = REPORTS_DIR / "recap.md"

SESSION_LOG_CSV = LOGS_DIR / "session_log.csv"
NEXUS_STATE_FILE = STATE_DIR / "nexus_conversation_state.json"
CHECKPOINT_FILE = CONV_STATE_DIR / "checkpoint.json"

MIN_GAP = 86400   # 24 hours — recap only after owner has been away a full day
MAX_GAP = 172800  # 48 hours

LLM_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = (
    f"You are {AGENT_TAG}, an autonomous AI agent. Write a brief recap of what happened "
    f"during a gap when the owner ({_OWNER_NAME.capitalize()}) was away. The recap must be "
    "standalone-readable -- the reader has NO other context. Be concise (3-8 sentences). "
    "Include: what sessions ran, what tasks progressed, any notable events. If nothing "
    "happened, say so directly. Do not pad. Use a casual but precise tone."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_ts():
    return time.time()


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _loom_query(sql, params=()):
    if not LOOM_DB.exists():
        return []
    try:
        conn = sqlite3.connect(str(LOOM_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _read_last_checkpoint_ts():
    """Read the last recap timestamp. Fallback chain: ts file -> checkpoint mtime -> now-1h."""
    if RECAP_TS_FILE.exists():
        try:
            return float(RECAP_TS_FILE.read_text().strip())
        except (ValueError, OSError):
            pass
    if CHECKPOINT_FILE.exists():
        try:
            return CHECKPOINT_FILE.stat().st_mtime
        except OSError:
            pass
    return _now_ts() - 3600  # fallback: 1 hour ago


def _count_session_log_since(ts):
    """Count lines in session_log.csv where timestamp > ts."""
    if not SESSION_LOG_CSV.exists():
        return [], 0
    sessions = []
    try:
        with open(SESSION_LOG_CSV, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                row_ts_str = row.get("timestamp", "")
                try:
                    # Parse various timestamp formats
                    # Strip timezone names like 'CEST'
                    clean = re.sub(r'[A-Z]{3,4}$', '', row_ts_str).strip()
                    # Try ISO format first
                    if '+' in clean or clean.endswith('Z'):
                        dt = datetime.fromisoformat(clean.replace('Z', '+00:00'))
                    else:
                        dt = datetime.fromisoformat(clean)
                    row_ts = dt.timestamp()
                except (ValueError, TypeError):
                    continue
                if row_ts > ts:
                    sessions.append({
                        "timestamp": row_ts_str,
                        "type": row.get("session_type", "unknown"),
                        "duration_min": row.get("duration_minutes", "?"),
                        "summary": (row.get("one_line_summary") or "")[:120],
                    })
    except (OSError, csv.Error):
        pass
    return sessions, len(sessions)


def _count_reports_since(ts):
    """Count report files in state/reports/ with mtime > ts."""
    if not REPORTS_DIR.exists():
        return []
    updated = []
    for p in REPORTS_DIR.iterdir():
        if p.is_file() and p.suffix == ".md":
            try:
                if p.stat().st_mtime > ts:
                    updated.append(p.name)
            except OSError:
                pass
    return updated


def _check_nexus_activity(ts):
    """Check nexus_conversation_state.json for new message indicators since ts."""
    if not NEXUS_STATE_FILE.exists():
        return None
    try:
        data = json.loads(NEXUS_STATE_FILE.read_text())
        threads = data.get("active_threads", {})
        new_msgs = 0
        active_convs = 0
        for name, thread in threads.items():
            msgs = thread.get("last_5_messages", [])
            for msg in msgs:
                msg_time_str = msg.get("time") or msg.get("ts")
                if not msg_time_str:
                    continue
                try:
                    clean = msg_time_str.replace('Z', '+00:00')
                    dt = datetime.fromisoformat(clean)
                    if dt.timestamp() > ts:
                        new_msgs += 1
                except (ValueError, TypeError):
                    pass
            if new_msgs > 0:
                active_convs += 1
        if new_msgs > 0:
            return f"{new_msgs} new messages across {active_convs} conversations"
        return None
    except (json.JSONDecodeError, OSError):
        return None


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------

def gate_check():
    """
    Determine whether a recap is warranted.
    Returns (should_recap: bool, elapsed: float, last_ts: float, signals: dict)
    """
    last_ts = _read_last_checkpoint_ts()
    now = _now_ts()
    elapsed = now - last_ts

    if elapsed < MIN_GAP:
        return False, elapsed, last_ts, {}

    # Gather signal counts
    task_events = _loom_query(
        "SELECT COUNT(*) as cnt FROM task_events WHERE changed_at > ?",
        (_iso(last_ts),)
    )
    task_event_count = task_events[0]["cnt"] if task_events else 0

    _, session_count = _count_session_log_since(last_ts)
    reports_updated = _count_reports_since(last_ts)
    nexus_activity = _check_nexus_activity(last_ts)

    signals = {
        "task_events": task_event_count,
        "sessions_ran": session_count,
        "reports_new": len(reports_updated),
        "nexus_new": nexus_activity,
    }

    # Always recap after MAX_GAP
    if elapsed >= MAX_GAP:
        return True, elapsed, last_ts, signals

    # Recap if any signal is positive
    if task_event_count > 0 or session_count > 0 or len(reports_updated) > 0 or nexus_activity:
        return True, elapsed, last_ts, signals

    return False, elapsed, last_ts, signals


# ---------------------------------------------------------------------------
# Gather
# ---------------------------------------------------------------------------

def gather(last_ts, elapsed):
    """Assemble structured gap_package dict. No LLM calls."""
    now = _now_ts()

    # Task events by type
    task_event_rows = _loom_query(
        "SELECT event_type, COUNT(*) as cnt FROM task_events "
        "WHERE changed_at > ? GROUP BY event_type",
        (_iso(last_ts),)
    )
    task_total = sum(r["cnt"] for r in task_event_rows)
    by_type = {r["event_type"]: r["cnt"] for r in task_event_rows}

    # Sessions
    sessions, _ = _count_session_log_since(last_ts)

    # Reports
    reports_updated = _count_reports_since(last_ts)

    # Nexus
    nexus_activity = _check_nexus_activity(last_ts)

    gap_package = {
        "elapsed_minutes": round(elapsed / 60),
        "window_start": _iso(last_ts),
        "window_end": _iso(now),
        "task_events": {
            "total": task_total,
            "by_type": by_type,
        },
        "sessions": sessions,
        "reports_updated": reports_updated,
        "nexus_activity": nexus_activity,
    }

    return gap_package


# ---------------------------------------------------------------------------
# Narrate (LLM call — Haiku)
# ---------------------------------------------------------------------------

def narrate(gap_package):
    """Call Anthropic API with gap_package to produce a human-readable recap."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        # Fallback: produce a simple text recap without LLM
        return _narrate_fallback(gap_package)

    user_content = json.dumps(gap_package, indent=2, default=str)

    payload = {
        "model": LLM_MODEL,
        "max_tokens": 512,
        "system": SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": user_content}],
    }

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=data,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            text = body["content"][0]["text"].strip()
        return text
    except (urllib.error.HTTPError, urllib.error.URLError, KeyError) as e:
        print(f"recap: LLM call failed ({e}), using fallback narration", file=sys.stderr)
        return _narrate_fallback(gap_package)


def _narrate_fallback(gap_package):
    """Simple template-based recap when LLM is unavailable."""
    elapsed = gap_package["elapsed_minutes"]
    parts = [f"Recap: {elapsed} minutes since last sync."]

    sessions = gap_package.get("sessions", [])
    if sessions:
        parts.append(f"{len(sessions)} session(s) ran:")
        for s in sessions[-5:]:  # cap at 5 most recent
            parts.append(f"  - [{s['type']}] {s['duration_min']}min: {s['summary']}")
    else:
        parts.append("No sessions ran during this window.")

    te = gap_package.get("task_events", {})
    if te.get("total", 0) > 0:
        by_type_str = ", ".join(f"{k}: {v}" for k, v in te.get("by_type", {}).items())
        parts.append(f"Task events: {te['total']} ({by_type_str})")

    reports = gap_package.get("reports_updated", [])
    if reports:
        parts.append(f"Reports updated: {', '.join(reports[:5])}")

    nexus = gap_package.get("nexus_activity")
    if nexus:
        parts.append(f"Nexus: {nexus}")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def write_outputs(recap_text, gap_package):
    """Write recap.md, last_recap.json, and update last_recap_ts.txt."""
    # Ensure directories exist
    CONV_STATE_DIR.mkdir(parents=True, exist_ok=True)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # Write narrated recap
    RECAP_MD_FILE.write_text(recap_text, encoding="utf-8")
    print(f"recap: wrote {RECAP_MD_FILE}")

    # Write structured data
    RECAP_JSON_FILE.write_text(
        json.dumps(gap_package, indent=2, default=str), encoding="utf-8"
    )
    print(f"recap: wrote {RECAP_JSON_FILE}")

    # Update timestamp
    RECAP_TS_FILE.write_text(str(time.time()), encoding="utf-8")
    print(f"recap: updated {RECAP_TS_FILE}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("check", "generate"):
        print("Usage: recap_generator.py check | generate [--force]", file=sys.stderr)
        sys.exit(1)

    mode = sys.argv[1]
    force = "--force" in sys.argv

    if mode == "check":
        should_recap, elapsed, _, signals = gate_check()
        if should_recap:
            print("RECAP")
        else:
            print("NO_RECAP")
        return

    if mode == "generate":
        if force:
            # Skip gate, always generate
            last_ts = _read_last_checkpoint_ts()
            elapsed = _now_ts() - last_ts
        else:
            should_recap, elapsed, last_ts, signals = gate_check()
            if not should_recap:
                print("NO_RECAP")
                return

        print(f"recap: generating (elapsed={round(elapsed/60)}min, force={force})")

        gap_package = gather(last_ts, elapsed)
        recap_text = narrate(gap_package)
        write_outputs(recap_text, gap_package)

        print("recap: done")
        return


if __name__ == "__main__":
    main()
