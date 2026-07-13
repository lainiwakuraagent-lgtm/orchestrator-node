#!/usr/bin/env python3
"""
analytics_write.py — Write session analytics to logs/analytics.db (SQLite).

Run at session shutdown to record session metadata, costs (from transcript),
and tool usage (from transcript).

Usage:
  python3 tools/analytics_write.py [options]

  --session-key KEY       Session key, e.g. 2026-07-10_1 (auto-derived if omitted)
  --session-type TYPE     execution | planning | free (required)
  --exit-reason REASON    time_limit | context_limit | natural_stop | gate_abort
  --summary TEXT          One-line session summary
  --handoff TEXT          Next action note
  --tasks-completed N     Number of Loom tasks completed this session (default: 0)
  --import-csv            Import session_log.csv into sessions table (historical)
  --no-transcript         Skip transcript parsing (no cost/tool data)
"""

import argparse
import csv
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
DB_PATH = PROJECT_DIR / "logs" / "analytics.db"

PRICE_TABLE = {
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-opus-4-6":           {"input": 15.00, "output": 75.00},
}
DEFAULT_PRICE = {"input": 3.00, "output": 15.00}


def get_db(path=DB_PATH):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS sessions (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key         TEXT UNIQUE NOT NULL,
        trigger_mode        TEXT,
        session_type             TEXT,
        type_resolution_source   TEXT,
        started_at               TEXT,
        ended_at                 TEXT,
        duration_minutes         INTEGER,
        model                    TEXT,
        context_pct_at_exit      REAL,
        exit_reason              TEXT,
        goal_id                  INTEGER,
        loom_session_id          INTEGER,
        tasks_completed          INTEGER DEFAULT 0,
        summary                  TEXT,
        handoff                  TEXT
    );
    -- Migrate existing DBs: add type_resolution_source column if absent
    -- (SQLite does not support IF NOT EXISTS for columns; use try/ignore pattern in code)

    CREATE TABLE IF NOT EXISTS session_costs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id    INTEGER NOT NULL,
        input_tokens  INTEGER,
        output_tokens INTEGER,
        total_tokens  INTEGER,
        cost_usd      REAL,
        model         TEXT,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    CREATE TABLE IF NOT EXISTS session_tools (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id  INTEGER NOT NULL,
        tool_name   TEXT NOT NULL,
        call_count  INTEGER DEFAULT 1,
        FOREIGN KEY (session_id) REFERENCES sessions(id)
    );

    CREATE UNIQUE INDEX IF NOT EXISTS idx_session_tools
        ON session_tools(session_id, tool_name);

    CREATE VIEW IF NOT EXISTS weekly_summary AS
    SELECT
        strftime('%Y-W%W', started_at) AS week,
        trigger_mode,
        session_type,
        COUNT(*) AS session_count,
        ROUND(AVG(duration_minutes), 1) AS avg_duration,
        ROUND(AVG(context_pct_at_exit), 1) AS avg_context_pct,
        SUM(tasks_completed) AS total_tasks
    FROM sessions
    GROUP BY week, trigger_mode, session_type;

    CREATE VIEW IF NOT EXISTS weekly_cost AS
    SELECT
        strftime('%Y-W%W', s.started_at) AS week,
        SUM(c.total_tokens) AS total_tokens,
        ROUND(SUM(c.cost_usd), 4) AS total_cost_usd
    FROM sessions s
    JOIN session_costs c ON c.session_id = s.id
    GROUP BY week;

    CREATE VIEW IF NOT EXISTS top_tools AS
    SELECT
        tool_name,
        SUM(call_count) AS total_calls,
        COUNT(DISTINCT session_id) AS sessions_used_in
    FROM session_tools
    GROUP BY tool_name
    ORDER BY total_calls DESC;
    """)
    # Migrate existing DBs: add type_resolution_source if not present
    try:
        conn.execute("ALTER TABLE sessions ADD COLUMN type_resolution_source TEXT")
        conn.commit()
    except Exception:
        pass  # Column already exists — ignore
    conn.commit()


def read_state(filename, default=""):
    path = PROJECT_DIR / "state" / filename
    if path.exists():
        return path.read_text().strip()
    return default


def derive_session_key():
    """Derive session key from trigger_mode + counter files."""
    mode = read_state("trigger_mode.txt", "manual")
    today = datetime.now().strftime("%Y-%m-%d")
    hour = datetime.now().hour
    # Nightly sessions use yesterday's date before 06:00
    if mode == "nightly" and hour < 6:
        from datetime import timedelta
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = yesterday
    if mode == "nightly":
        n = read_state("sessions_tonight.count", "1")
    elif mode == "emergency":
        n = read_state("sessions_emergency.count", "1")
    else:
        n = read_state("sessions_manual.count", "1")
    return f"{today}_{n}"


def find_transcript():
    """Return the path to the current session's JSONL transcript."""
    # check_context.sh prints it; alternatively, glob for the newest file
    projects_dir = Path.home() / ".claude" / "projects"
    slug = "-home-andrii-lain-agent-project"
    target = projects_dir / slug
    if not target.exists():
        return None
    files = sorted(target.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    return files[0] if files else None


def parse_transcript(jsonl_path):
    """Extract token counts and tool call counts from a Claude JSONL transcript.

    Claude transcript format (as of 2026-07):
    - Each line is a JSON object with a 'type' field (user | assistant | etc.)
    - assistant entries: entry["message"]["usage"] for token counts
    - assistant entries: entry["message"]["content"] list for tool_use blocks
    - Token types: input_tokens, output_tokens, cache_creation_input_tokens,
      cache_read_input_tokens (we count cache_read as input for cost purposes)
    """
    total_input = total_output = 0
    tool_calls = {}

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

                msg = entry.get("message", {})

                # Token usage — nested inside message
                usage = msg.get("usage") or {}
                total_input += usage.get("input_tokens", 0)
                total_output += usage.get("output_tokens", 0)

                # Tool calls — in message.content blocks
                for block in msg.get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        name = block.get("name", "unknown")
                        tool_calls[name] = tool_calls.get(name, 0) + 1

    except OSError:
        pass

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "tool_calls": tool_calls,
    }


def estimate_cost_detailed(jsonl_path, model):
    """Estimate cost by summing per-turn usage with correct cache pricing.

    Claude cache pricing (Sonnet 4.6):
      input:            $3.00/M
      cache_creation:   $3.75/M (1.25x input)
      cache_read:       $0.30/M (0.10x input)
      output:          $15.00/M

    Returns cost_usd, input_tokens, output_tokens.
    """
    prices = PRICE_TABLE.get(model, DEFAULT_PRICE)
    inp_price = prices["input"]
    out_price = prices["output"]
    # Cache pricing: create = 1.25x input, read = 0.10x input
    cache_create_price = inp_price * 1.25
    cache_read_price = inp_price * 0.10

    total_input = total_output = 0
    total_cache_create = total_cache_read = 0

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
        total_input * inp_price
        + total_output * out_price
        + total_cache_create * cache_create_price
        + total_cache_read * cache_read_price
    ) / 1_000_000

    return cost, total_input, total_output


def estimate_cost(input_tokens, output_tokens, model):
    prices = PRICE_TABLE.get(model, DEFAULT_PRICE)
    return (input_tokens * prices["input"] + output_tokens * prices["output"]) / 1_000_000


def write_session(conn, args):
    mode = read_state("trigger_mode.txt", "manual")
    model = read_state("session_model.txt", "claude-sonnet-4-6")
    loom_id_str = read_state("current_loom_session_id.txt", "")
    loom_session_id = int(loom_id_str) if loom_id_str.isdigit() else None

    # Goal ID from loom_context.json
    goal_id = None
    loom_ctx_path = PROJECT_DIR / "state" / "loom_context.json"
    if loom_ctx_path.exists():
        try:
            loom_ctx = json.loads(loom_ctx_path.read_text())
            goal_id = loom_ctx.get("goal_id")
        except (json.JSONDecodeError, KeyError):
            pass

    # started_at from session_start_epoch
    started_at = None
    epoch_str = read_state("session_start_epoch", "")
    if epoch_str.isdigit():
        started_at = datetime.fromtimestamp(int(epoch_str), tz=timezone.utc).isoformat()

    ended_at = datetime.now(tz=timezone.utc).isoformat()

    # Duration
    duration_minutes = None
    if started_at and epoch_str.isdigit():
        elapsed = int(datetime.now().timestamp()) - int(epoch_str)
        duration_minutes = max(1, elapsed // 60)

    session_key = args.session_key or derive_session_key()

    conn.execute("""
        INSERT OR REPLACE INTO sessions
        (session_key, trigger_mode, session_type, type_resolution_source,
         started_at, ended_at,
         duration_minutes, model, context_pct_at_exit, exit_reason,
         goal_id, loom_session_id, tasks_completed, summary, handoff)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        session_key,
        mode,
        args.session_type,
        os.environ.get("CURRENT_SESSION_TYPE_SOURCE", ""),
        started_at,
        ended_at,
        duration_minutes,
        model,
        args.context_pct,
        args.exit_reason,
        goal_id,
        loom_session_id,
        args.tasks_completed,
        args.summary,
        args.handoff,
    ))
    conn.commit()

    session_id = conn.execute(
        "SELECT id FROM sessions WHERE session_key = ?", (session_key,)
    ).fetchone()["id"]
    print(f"analytics: session {session_key} → id={session_id}", flush=True)
    return session_id, model


def write_costs_and_tools(conn, session_id, model, no_transcript=False):
    if no_transcript:
        return

    transcript = find_transcript()
    if not transcript:
        print("analytics: transcript not found, skipping costs/tools", flush=True)
        return

    parsed = parse_transcript(transcript)
    cost, inp, out = estimate_cost_detailed(transcript, model)
    total = inp + out

    if total > 0 or cost > 0:
        conn.execute("""
            INSERT OR REPLACE INTO session_costs
            (session_id, input_tokens, output_tokens, total_tokens, cost_usd, model)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (session_id, inp, out, total, cost, model))
        print(f"analytics: tokens={total} (in={inp} out={out}) cost=${cost:.4f}", flush=True)

    for tool_name, count in parsed["tool_calls"].items():
        conn.execute("""
            INSERT INTO session_tools (session_id, tool_name, call_count)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id, tool_name)
            DO UPDATE SET call_count = call_count + excluded.call_count
        """, (session_id, tool_name, count))

    if parsed["tool_calls"]:
        top = sorted(parsed["tool_calls"].items(), key=lambda x: -x[1])[:5]
        print(f"analytics: top tools: {top}", flush=True)

    conn.commit()


def import_csv(conn):
    """Import session_log.csv into sessions table (historical, no costs/tools)."""
    csv_path = PROJECT_DIR / "logs" / "session_log.csv"
    if not csv_path.exists():
        print("analytics: session_log.csv not found", flush=True)
        return

    imported = 0
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            ts_raw = row.get("timestamp", "").strip()
            # Normalize timestamp → ISO 8601
            try:
                # Try various formats
                for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S+%f",
                            "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M"):
                    try:
                        ts = datetime.strptime(ts_raw[:19], fmt[:len(fmt)])
                        started_at = ts.isoformat()
                        break
                    except ValueError:
                        continue
                else:
                    started_at = ts_raw
            except Exception:
                started_at = ts_raw

            duration_raw = (row.get("duration_minutes") or "").strip()
            try:
                duration = int(float(duration_raw))
            except (ValueError, TypeError):
                duration = None

            ctx_raw = (row.get("context_pct_at_exit") or "").strip().rstrip("%")
            try:
                ctx_pct = float(ctx_raw)
            except (ValueError, TypeError):
                ctx_pct = None

            # Some rows have malformed CSV (all data jammed into timestamp field)
            if row.get("session_type") is None:
                continue  # skip unparseable rows

            session_type = (row.get("session_type") or "execution").strip()
            summary = (row.get("one_line_summary") or "").strip()

            # Derive session_key from timestamp + session_type (best effort)
            date_part = started_at[:10] if len(started_at) >= 10 else "unknown"
            # Use a placeholder key — CSV doesn't have session N
            # We'll use timestamp as key to avoid duplicates
            session_key = f"{ts_raw[:16].replace('T', '_').replace(':', '')}_csv"

            try:
                conn.execute("""
                    INSERT OR IGNORE INTO sessions
                    (session_key, session_type, started_at, duration_minutes,
                     context_pct_at_exit, summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (session_key, session_type, started_at, duration, ctx_pct, summary))
                imported += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    print(f"analytics: imported {imported} rows from session_log.csv", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Write session analytics to analytics.db")
    parser.add_argument("--session-key", help="Session key (YYYY-MM-DD_N)")
    _session_type_choices = ["execution", "planning", "maintenance", "philosophy", "free"]
    _default_session_type = os.environ.get("CURRENT_SESSION_TYPE", "execution")
    if _default_session_type not in _session_type_choices:
        _default_session_type = "execution"
    parser.add_argument("--session-type", default=_default_session_type,
                        choices=_session_type_choices,
                        help="Session type (default: $CURRENT_SESSION_TYPE env var or 'execution')")
    parser.add_argument("--exit-reason", default="natural_stop",
                        choices=["time_limit", "context_limit", "natural_stop", "gate_abort"],
                        help="Why the session ended")
    parser.add_argument("--summary", default="", help="One-line summary")
    parser.add_argument("--handoff", default="", help="Next action note")
    parser.add_argument("--tasks-completed", type=int, default=0,
                        help="Number of Loom tasks completed this session")
    parser.add_argument("--context-pct", type=float, default=None,
                        help="Context percentage at exit")
    parser.add_argument("--import-csv", action="store_true",
                        help="Import session_log.csv (historical)")
    parser.add_argument("--no-transcript", action="store_true",
                        help="Skip transcript parsing")
    parser.add_argument("--db", default=str(DB_PATH), help="DB path")
    args = parser.parse_args()

    db_path = Path(args.db)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    conn = get_db(db_path)
    init_schema(conn)

    if args.import_csv:
        import_csv(conn)
        conn.close()
        return

    session_id, model = write_session(conn, args)
    write_costs_and_tools(conn, session_id, model, no_transcript=args.no_transcript)
    conn.close()


if __name__ == "__main__":
    main()
