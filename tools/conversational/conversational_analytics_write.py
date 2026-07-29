#!/usr/bin/env python3
"""conversational_analytics_write.py — Write conversational session analytics.

Handles BOTH Telegram sessions (channel=telegram) and Nexus agent-channel
sessions (channel=<nexus_channel_uuid>). Writes to the conversational_sessions
table in logs/analytics.db (schema created by analytics_write.py's init_schema).

Called at session close by conversation.sh or agent_channel.sh.

Usage:
  python3 tools/conversational/conversational_analytics_write.py \\
    --channel telegram \\
    --exit-reason natural_close|context_full|context_hard|fallback_timeout|crash|maintenance_close \\
    [--context-pct N]

  python3 tools/conversational/conversational_analytics_write.py \\
    --channel <nexus_channel_uuid> \\
    --exit-reason closed_by_agent|context_full|crash \\
    [--context-pct N]
"""

import argparse
import glob
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
DB_PATH = PROJECT_DIR / "logs" / "analytics.db"

PRICE_TABLE = {
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00,
                                   "cache_create": 3.75, "cache_read": 0.30},
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00,
                                   "cache_create": 1.00, "cache_read": 0.08},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00,
                                   "cache_create": 18.75, "cache_read": 1.50},
}
DEFAULT_PRICE = {"input": 3.00, "output": 15.00, "cache_create": 3.75, "cache_read": 0.30}


def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_schema(conn):
    """Create conversational_sessions table and daily_cost view if absent."""
    conn.execute("""
    CREATE TABLE IF NOT EXISTS conversational_sessions (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key             TEXT UNIQUE NOT NULL,
        channel                 TEXT NOT NULL,
        started_at              TEXT,
        ended_at                TEXT,
        total_duration_minutes  INTEGER,
        active_duration_minutes INTEGER,
        turn_count              INTEGER,
        input_tokens            INTEGER,
        output_tokens           INTEGER,
        total_tokens            INTEGER,
        cost_usd                REAL,
        model                   TEXT,
        context_pct_at_close    REAL,
        context_soft_fired      INTEGER DEFAULT 0,
        context_hard_fired      INTEGER DEFAULT 0,
        exit_reason             TEXT,
        avg_wait_time_seconds   REAL,
        spawn_sequence_number   INTEGER
    )
    """)
    # Create daily_cost view only when sessions table also exists (it's created by
    # analytics_write.py's init_schema; on blank nodes both run eventually).
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "sessions" in tables:
        conn.execute("DROP VIEW IF EXISTS daily_cost")
        conn.execute("""
        CREATE VIEW daily_cost AS
        SELECT
            date(s.started_at) AS day,
            'executional' AS layer,
            COUNT(*) AS session_count,
            ROUND(SUM(COALESCE(c.cost_usd, 0)), 6) AS cost_usd,
            SUM(COALESCE(c.total_tokens, 0)) AS total_tokens
        FROM sessions s
        LEFT JOIN session_costs c ON c.session_id = s.id
        WHERE s.started_at IS NOT NULL
        GROUP BY date(s.started_at)
        UNION ALL
        SELECT
            date(started_at) AS day,
            'conversational' AS layer,
            COUNT(*) AS session_count,
            ROUND(SUM(COALESCE(cost_usd, 0)), 6) AS cost_usd,
            SUM(COALESCE(total_tokens, 0)) AS total_tokens
        FROM conversational_sessions
        WHERE started_at IS NOT NULL
        GROUP BY date(started_at)
        ORDER BY day DESC, layer
        """)
    conn.commit()


def channel_state_dir(channel: str) -> Path:
    """Return the per-channel state directory."""
    if channel == "telegram":
        return PROJECT_DIR / "state" / "conversation"
    return PROJECT_DIR / "state" / "agent_channels" / channel


def load_budget(channel: str) -> dict:
    """Load context_budget.json for this channel (Telegram) or nexus_session_context.json."""
    state_dir = channel_state_dir(channel)
    # Telegram sessions
    budget_file = state_dir / "context_budget.json"
    if budget_file.exists():
        try:
            return json.loads(budget_file.read_text())
        except Exception:
            pass
    # Nexus/agent-channel sessions
    ctx_file = state_dir / "nexus_session_context.json"
    if ctx_file.exists():
        try:
            raw = json.loads(ctx_file.read_text())
            started_at_str = raw.get("started_at", "")
            session_start = 0
            if started_at_str:
                try:
                    dt = datetime.fromisoformat(started_at_str.replace("Z", "+00:00"))
                    session_start = int(dt.timestamp())
                except Exception:
                    pass
            return {"session_start": session_start, "messages_sent": 0, "messages_received": 0}
        except Exception:
            pass
    return {"session_start": int(time.time()) - 300, "messages_sent": 0, "messages_received": 0}


def load_thread(channel: str) -> list:
    """Load thread.json entries for this channel. Returns list of dicts."""
    state_dir = channel_state_dir(channel)
    thread_file = state_dir / "thread.json"
    if not thread_file.exists():
        return []
    try:
        data = json.loads(thread_file.read_text())
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def compute_thread_metrics(thread: list) -> dict:
    """Compute turn_count, active_duration_seconds, avg_wait_time from thread entries.

    Supports both Telegram format:
      {"role": "user"/"lain", "timestamp": <epoch>, ...}
    and agent-channel format:
      {"role": "peer"/"agent", "ts": "<ISO>", ...}  (agent writes this ad-hoc)

    Active duration = sum of (agent_response_ts - user_message_ts) per exchange.
    Wait time per message = same per-exchange value, averaged.
    """
    if not thread:
        return {"turn_count": 0, "active_duration_seconds": 0, "avg_wait_seconds": None}

    OWNER_ROLES = {"user", "peer", "andrii", "owner"}
    AGENT_ROLES = {"lain", "agent", "assistant"}

    def get_ts(entry: dict) -> float | None:
        if "timestamp" in entry:
            v = entry["timestamp"]
            return float(v) if v else None
        if "ts" in entry:
            v = entry["ts"]
            if isinstance(v, (int, float)):
                return float(v)
            if isinstance(v, str):
                try:
                    dt = datetime.fromisoformat(v.replace("Z", "+00:00"))
                    return dt.timestamp()
                except Exception:
                    pass
        return None

    user_turns = 0
    wait_times: list[float] = []

    i = 0
    while i < len(thread):
        entry = thread[i]
        role = str(entry.get("role", "")).lower()
        if role in OWNER_ROLES:
            user_ts = get_ts(entry)
            # Find next agent response
            j = i + 1
            while j < len(thread):
                next_role = str(thread[j].get("role", "")).lower()
                if next_role in AGENT_ROLES:
                    agent_ts = get_ts(thread[j])
                    if user_ts is not None and agent_ts is not None and agent_ts > user_ts:
                        wait_times.append(agent_ts - user_ts)
                    user_turns += 1
                    i = j
                    break
                j += 1
        i += 1

    active_secs = sum(wait_times)
    avg_wait = sum(wait_times) / len(wait_times) if wait_times else None

    return {
        "turn_count": user_turns,
        "active_duration_seconds": active_secs,
        "avg_wait_seconds": avg_wait,
    }


def find_current_transcript(channel: str) -> Path | None:
    """Find the most-recently-modified .jsonl transcript for this project."""
    projects_dir = Path.home() / ".claude" / "projects"
    if not projects_dir.exists():
        return None
    files = sorted(
        projects_dir.glob("*/*.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True
    )
    return files[0] if files else None


def parse_transcript_tokens(jsonl_path: Path, model: str) -> tuple[int, int, float]:
    """Parse transcript for tokens and cost. Returns (input, output, cost_usd)."""
    prices = PRICE_TABLE.get(model, DEFAULT_PRICE)

    total_input = total_output = total_cache_create = total_cache_read = 0

    try:
        with open(jsonl_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if entry.get("type") != "assistant":
                    continue
                usage = entry.get("message", {}).get("usage") or {}
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)
                total_cache_create += usage.get("cache_creation_input_tokens", 0)
                total_cache_read += usage.get("cache_read_input_tokens", 0)
    except OSError:
        pass

    cost = (
        total_input * prices["input"]
        + total_output * prices["output"]
        + total_cache_create * prices["cache_create"]
        + total_cache_read * prices["cache_read"]
    ) / 1_000_000

    return total_input, total_output, cost


def count_prior_sessions(conn, channel: str) -> int:
    """Count existing conversational_sessions for this channel."""
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM conversational_sessions WHERE channel = ?", (channel,)
        ).fetchone()
        return row[0] if row else 0
    except Exception:
        return 0


def write_session(
    channel: str,
    exit_reason: str,
    context_pct: float | None,
) -> str:
    """Write one row to conversational_sessions. Returns session_key."""
    now = datetime.now(tz=timezone.utc)
    session_key = f"conv_{channel[:8]}_{now.strftime('%Y%m%d_%H%M%S_%f')[:25]}"

    budget = load_budget(channel)
    session_start = budget.get("session_start", int(now.timestamp()) - 60)
    started_at = datetime.fromtimestamp(session_start, tz=timezone.utc).isoformat()
    ended_at = now.isoformat()
    total_seconds = int(now.timestamp()) - session_start
    total_duration_minutes = max(1, total_seconds // 60)

    thread = load_thread(channel)
    metrics = compute_thread_metrics(thread)
    turn_count = metrics["turn_count"]
    active_secs = metrics["active_duration_seconds"]
    active_duration_minutes = max(0, int(active_secs // 60))
    avg_wait_seconds = metrics["avg_wait_seconds"]

    model_file = PROJECT_DIR / "state" / "session_model.txt"
    model = model_file.read_text().strip() if model_file.exists() else "claude-sonnet-4-6"

    transcript = find_current_transcript(channel)
    if transcript:
        input_tokens, output_tokens, cost_usd = parse_transcript_tokens(transcript, model)
    else:
        input_tokens = output_tokens = 0
        cost_usd = 0.0
    total_tokens = input_tokens + output_tokens

    if context_pct is None:
        context_pct = float(budget.get("estimated_context_pct", 0))

    context_soft_fired = 1 if context_pct >= 50 else 0
    context_hard_fired = 1 if (
        context_pct >= 70
        or exit_reason in ("context_full", "context_hard", "context_limit")
    ) else 0

    conn = get_db()
    ensure_schema(conn)
    spawn_seq = count_prior_sessions(conn, channel) + 1

    conn.execute("""
        INSERT INTO conversational_sessions (
            session_key, channel, started_at, ended_at,
            total_duration_minutes, active_duration_minutes,
            turn_count, input_tokens, output_tokens, total_tokens, cost_usd, model,
            context_pct_at_close, context_soft_fired, context_hard_fired,
            exit_reason, avg_wait_time_seconds, spawn_sequence_number
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_key) DO UPDATE SET
            ended_at               = excluded.ended_at,
            total_duration_minutes = excluded.total_duration_minutes,
            active_duration_minutes = excluded.active_duration_minutes,
            turn_count             = excluded.turn_count,
            input_tokens           = excluded.input_tokens,
            output_tokens          = excluded.output_tokens,
            total_tokens           = excluded.total_tokens,
            cost_usd               = excluded.cost_usd,
            context_pct_at_close   = excluded.context_pct_at_close,
            context_soft_fired     = excluded.context_soft_fired,
            context_hard_fired     = excluded.context_hard_fired,
            exit_reason            = excluded.exit_reason,
            avg_wait_time_seconds  = excluded.avg_wait_time_seconds
    """, (
        session_key, channel, started_at, ended_at,
        total_duration_minutes, active_duration_minutes,
        turn_count, input_tokens, output_tokens, total_tokens, cost_usd, model,
        context_pct, context_soft_fired, context_hard_fired,
        exit_reason,
        round(avg_wait_seconds, 1) if avg_wait_seconds is not None else None,
        spawn_seq,
    ))
    conn.commit()
    conn.close()
    return session_key


def main():
    parser = argparse.ArgumentParser(description="Write conversational session analytics")
    parser.add_argument("--channel", default="telegram",
                        help="Channel: 'telegram' or nexus channel UUID")
    parser.add_argument("--exit-reason", default="unknown",
                        help="Exit reason: natural_close|context_full|context_hard|"
                             "fallback_timeout|crash|maintenance_close|closed_by_agent")
    parser.add_argument("--context-pct", type=float, default=None,
                        help="Context % at close (falls back to context_budget.json)")
    args = parser.parse_args()

    try:
        key = write_session(args.channel, args.exit_reason, args.context_pct)
        print(f"conv_analytics: channel={args.channel} reason={args.exit_reason} key={key}")
        sys.exit(0)
    except Exception as e:
        print(f"conv_analytics ERROR: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
