#!/usr/bin/env python3
"""
web_server.py — Node Web UI backend (generic)

Monitoring and control dashboard for autonomous agent nodes built on the
blank_node template. Capability-driven: panels activate when the data they
need exists. Works for any node; extra panels appear automatically for nodes
with soul.md, musubi relationship data, or character consistency tooling.

Usage:
  python3 tools/web_server.py [--port 8767] [--host 0.0.0.0]

Port 8767 default — avoids conflicts with:
  8765 = telegram-webhook
  8766 = session-trigger-server

Endpoints:
  GET  /                       → web UI (web_ui.html)
  GET  /api/status             → live node status
  GET  /api/sessions           → session list + aggregate stats
  GET  /api/sessions/{key}     → single session detail
  GET  /api/goals              → loom goals + active task queue
  GET  /api/costs              → token/cost analytics
  GET  /api/character          → character consistency (if available)
  GET  /api/memory             → memory state (latest_summary + session files)
  GET  /api/comms              → nexus + telegram state
  GET  /api/capabilities       → which panels are available for this node
  POST /api/trigger/manual     → launch a manual session
  POST /api/emergency/enable   → enable emergency mode
  POST /api/emergency/disable  → disable emergency mode
  POST /api/health/run         → run health_check.sh and return result
  POST /api/usage/check        → run check_session.sh --usage and return result
  GET  /api/session-types      → list session type definitions (config/session_types/*.yaml)
  PUT  /api/session-types/{id} → save a session type YAML
  GET  /api/nightly-schedule   → read real nightly schedule (config/session_schedule.json)
  PUT  /api/nightly-schedule   → write real nightly schedule
"""

import argparse
import csv
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
import uvicorn

# ---------------------------------------------------------------------------
# Project root — auto-detected from this file's location, or overridden via
# --project-dir CLI argument (updated in main() before any request handling).
# ---------------------------------------------------------------------------
PROJECT_DIR = pathlib.Path(__file__).resolve().parent.parent.parent
STATE_DIR = PROJECT_DIR / "state"
LOGS_DIR = PROJECT_DIR / "logs"
TOOLS_DIR = PROJECT_DIR / "tools"
MEMORY_DIR = PROJECT_DIR / "memory"
SCRIPTS_DIR = PROJECT_DIR / "scripts"

LOOM_SRC = pathlib.Path.home() / "lain" / "loom"
LOOM_DB = (pathlib.Path(os.environ["LOOM_DB"]) if "LOOM_DB" in os.environ
           else pathlib.Path.home() / ".local" / "share" / "loom" / "loom.db")
ANALYTICS_DB = LOGS_DIR / "analytics.db"
SESSION_LOG_CSV = LOGS_DIR / "session_log.csv"
SCHEDULE_FILE = STATE_DIR / "schedule.json"
CONFIGS_DIR = PROJECT_DIR / "config"
SESSION_TYPES_DIR = CONFIGS_DIR / "session_types"
NIGHTLY_SCHEDULE_FILE = CONFIGS_DIR / "session_schedule.json"
MANUAL_TRIGGER_RESULT_FILE = STATE_DIR / "manual_trigger_result.json"
TRIGGER_CONFIRM_TIMEOUT_SEC = 8
TRIGGER_CONFIRM_POLL_SEC = 0.25

# ---------------------------------------------------------------------------
# Helpers — state / config
# ---------------------------------------------------------------------------

def read_state(filename: str, default: str = "") -> str:
    p = STATE_DIR / filename
    return p.read_text().strip() if p.exists() else default


def read_env(key: str, default: str = "") -> str:
    """Read a key from state/agent_config.env."""
    env_file = STATE_DIR / "agent_config.env"
    if not env_file.exists():
        return default
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line.startswith(key + "="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    return default


def agent_name() -> str:
    return read_env("AGENT_NAME") or "node"


def owner_name() -> str:
    return read_env("OWNER_NAME") or "owner"


# ---------------------------------------------------------------------------
# Helpers — analytics DB
# ---------------------------------------------------------------------------

def get_analytics_db() -> Optional[sqlite3.Connection]:
    if not ANALYTICS_DB.exists():
        return None
    conn = sqlite3.connect(str(ANALYTICS_DB))
    conn.row_factory = sqlite3.Row
    return conn


def get_loom_db() -> Optional[sqlite3.Connection]:
    if not LOOM_DB.exists():
        return None
    conn = sqlite3.connect(str(LOOM_DB))
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# Helpers — session log CSV (historical, pre-analytics.db)
# ---------------------------------------------------------------------------

def load_session_csv() -> list[dict]:
    if not SESSION_LOG_CSV.exists():
        return []
    sessions = []
    try:
        with open(SESSION_LOG_CSV, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                ts = (row.get("timestamp") or "").strip()
                raw_type = (row.get("session_type") or "").strip()
                if raw_type == "session_type":  # header row re-included
                    continue
                try:
                    dur = float(row.get("duration_minutes") or 0)
                except (ValueError, TypeError):
                    dur = None
                try:
                    ctx = float((row.get("context_pct_at_exit") or "").rstrip("%"))
                except (ValueError, TypeError):
                    ctx = None
                sessions.append({
                    "timestamp": ts,
                    "session_type": _normalize_type(raw_type),
                    "duration_minutes": dur,
                    "context_pct_at_exit": ctx,
                    "summary": (row.get("one_line_summary") or "").strip(),
                    "source": "csv",
                })
    except Exception:
        pass
    return sessions


def _normalize_type(raw: str) -> str:
    if not raw:
        return "unknown"
    r = raw.lower()
    if "abort" in r or "exit_outside" in r or "shutdown" in r:
        return "aborted"
    if "emergency" in r:
        return "emergency"
    if "planning" in r or "architecture" in r:
        return "planning"
    if "maintenance" in r:
        return "maintenance"
    if "philosophy" in r or "reflection" in r:
        return "philosophy"
    if "free" in r or "explore" in r or "identity" in r:
        return "free"
    if "execution" in r or "response" in r or "manual" in r:
        return "execution"
    return "other"


# ---------------------------------------------------------------------------
# Capabilities detection — which panels are available for this node
# ---------------------------------------------------------------------------

def detect_capabilities() -> dict:
    caps = {
        "analytics_db": ANALYTICS_DB.exists(),
        "session_csv": SESSION_LOG_CSV.exists(),
        "loom": LOOM_DB.exists(),
        "nexus": (STATE_DIR / "nexus_conversation_state.json").exists(),
        "telegram": (STATE_DIR / "telegram_incoming.txt").exists(),
        "character": (MEMORY_DIR / "work" / "soul.md").exists(),
        "relationship": False,
        "health_check": (TOOLS_DIR / "health_check.sh").exists(),
        "usage_check": (TOOLS_DIR / "check_session.sh").exists(),
        "emergency_mode": (TOOLS_DIR / "emergency_mode.sh").exists(),
        "manual_trigger": (TOOLS_DIR / "session_trigger_server.py").exists(),
        "wake_sh": (SCRIPTS_DIR / "wake.sh").exists(),
    }
    # Relationship: musubi_data/users/<agent>/<owner>.md
    an = agent_name()
    on = owner_name()
    rel_path = MEMORY_DIR / "work" / "musubi_data" / "users" / an / f"{on}.md"
    caps["relationship"] = rel_path.exists()
    return caps


# ---------------------------------------------------------------------------
# API — /api/status
# ---------------------------------------------------------------------------

def _get_status() -> dict:
    # Lock file
    lock_file = STATE_DIR / "session.lock"
    session_running = False
    session_pid = None
    if lock_file.exists():
        try:
            pid = int(lock_file.read_text().strip())
            # Check if PID is alive
            os.kill(pid, 0)
            session_running = True
            session_pid = pid
        except (ValueError, ProcessLookupError, OSError):
            session_running = False

    trigger_mode = read_state("trigger_mode.txt", "unknown")
    count_tonight = read_state("sessions_tonight.count", "0")
    count_emergency = read_state("sessions_emergency.count", "0")
    count_manual = read_state("sessions_manual.count", "0")
    max_tonight = read_state("sessions_tonight.max", "5")
    emergency_active = (STATE_DIR / "emergency_mode.active").exists()

    # Loom context
    loom_ctx = {}
    loom_ctx_file = STATE_DIR / "loom_context.json"
    if loom_ctx_file.exists():
        try:
            loom_ctx = json.loads(loom_ctx_file.read_text())
        except Exception:
            pass

    # Last session from analytics.db or CSV
    last_session = None
    conn = get_analytics_db()
    if conn:
        try:
            row = conn.execute(
                "SELECT session_key, trigger_mode, session_type, started_at, ended_at, "
                "duration_minutes, summary, handoff FROM sessions ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
            if row:
                last_session = dict(row)
        except Exception:
            pass
        finally:
            conn.close()
    if not last_session:
        rows = load_session_csv()
        if rows:
            last_session = rows[-1]

    # Behavioral context
    behavioral = {}
    bc_file = STATE_DIR / "behavioral_context.txt"
    if bc_file.exists():
        text = bc_file.read_text()
        for line in text.splitlines():
            m = re.match(r"^#.*Trust=([\d.]+).*Warmth=([\d.]+).*Friction=([\d.]+)", line)
            if m:
                behavioral = {
                    "trust": float(m.group(1)),
                    "warmth": float(m.group(2)),
                    "friction": float(m.group(3)),
                }
                break

    # Next nightly slots (local time approximation)
    night_slots = ["23:00", "01:10", "02:25", "03:40", "04:55"]

    return {
        "agent_name": agent_name(),
        "owner_name": owner_name(),
        "node_version": read_env("NODE_VERSION", "unknown"),
        "session_running": session_running,
        "session_pid": session_pid,
        "trigger_mode": trigger_mode,
        "emergency_active": emergency_active,
        "session_counts": {
            "tonight": int(count_tonight) if count_tonight.isdigit() else 0,
            "tonight_max": int(max_tonight) if max_tonight.isdigit() else 5,
            "emergency": int(count_emergency) if count_emergency.isdigit() else 0,
            "manual": int(count_manual) if count_manual.isdigit() else 0,
        },
        "loom_context": loom_ctx,
        "last_session": last_session,
        "behavioral": behavioral,
        "night_slots": night_slots,
        "capabilities": detect_capabilities(),
        "server_time": datetime.now(tz=timezone.utc).isoformat(),
    }


# ---------------------------------------------------------------------------
# API — /api/sessions
# ---------------------------------------------------------------------------

def _get_sessions(limit: int = 50, offset: int = 0, type_filter: Optional[str] = None) -> dict:
    sessions = []
    conn = get_analytics_db()

    if conn:
        try:
            where = ""
            params: list = []
            if type_filter:
                where = "WHERE session_type = ?"
                params.append(type_filter)
            rows = conn.execute(
                f"SELECT session_key, trigger_mode, session_type, started_at, ended_at, "
                f"duration_minutes, model, context_pct_at_exit, exit_reason, goal_id, "
                f"tasks_completed, summary, handoff FROM sessions {where} "
                f"ORDER BY started_at DESC LIMIT ? OFFSET ?",
                params + [limit, offset]
            ).fetchall()
            sessions = [dict(r) for r in rows]

            # Aggregate stats
            agg = conn.execute(
                "SELECT COUNT(*) as total, "
                "AVG(duration_minutes) as avg_dur, "
                "MIN(duration_minutes) as min_dur, "
                "MAX(duration_minutes) as max_dur, "
                "AVG(context_pct_at_exit) as avg_ctx "
                "FROM sessions"
            ).fetchone()

            type_dist = conn.execute("""
                SELECT
                  CASE
                    WHEN lower(session_type) LIKE '%abort%'
                      OR lower(session_type) LIKE '%exit_outside%'
                      OR lower(session_type) LIKE '%shutdown%'    THEN 'aborted'
                    WHEN lower(session_type) LIKE '%emergency%'   THEN 'emergency'
                    WHEN lower(session_type) LIKE '%planning%'
                      OR lower(session_type) = 'architecture'     THEN 'planning'
                    WHEN lower(session_type) LIKE '%free%'
                      OR lower(session_type) LIKE '%explore%'
                      OR lower(session_type) LIKE '%identity%'    THEN 'free'
                    WHEN lower(session_type) IN ('execution','response','manual','exploration',
                      'execution_morning')                        THEN 'execution'
                    WHEN session_type IS NULL
                      OR lower(session_type) IN ('null','')       THEN 'unknown'
                    ELSE 'execution'
                  END AS session_type,
                  COUNT(*) as n
                FROM sessions
                GROUP BY 1
                ORDER BY n DESC
            """).fetchall()

            weekly = conn.execute(
                "SELECT week, session_count, avg_duration, avg_context_pct, total_tasks "
                "FROM weekly_summary ORDER BY week DESC LIMIT 12"
            ).fetchall()

            total_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]

        except Exception as e:
            return {"sessions": [], "stats": {}, "error": str(e)}
        finally:
            conn.close()

        return {
            "sessions": sessions,
            "total": total_count,
            "stats": {
                "total": agg["total"] if agg else 0,
                "avg_duration": round(agg["avg_dur"], 1) if agg and agg["avg_dur"] else None,
                "min_duration": agg["min_dur"] if agg else None,
                "max_duration": agg["max_dur"] if agg else None,
                "avg_context_pct": round(agg["avg_ctx"], 1) if agg and agg["avg_ctx"] else None,
            },
            "type_distribution": [dict(r) for r in type_dist],
            "weekly": [dict(r) for r in weekly],
        }

    # Fallback: CSV
    all_sessions = load_session_csv()
    if type_filter:
        all_sessions = [s for s in all_sessions if s["session_type"] == type_filter]
    total_count = len(all_sessions)
    page = list(reversed(all_sessions))[offset:offset + limit]

    durations = [s["duration_minutes"] for s in all_sessions if s["duration_minutes"] is not None]
    return {
        "sessions": page,
        "total": total_count,
        "stats": {
            "total": total_count,
            "avg_duration": round(sum(durations) / len(durations), 1) if durations else None,
            "min_duration": min(durations) if durations else None,
            "max_duration": max(durations) if durations else None,
        },
        "type_distribution": [],
        "weekly": [],
    }


def _get_session_detail(key: str) -> dict:
    conn = get_analytics_db()
    if not conn:
        raise HTTPException(status_code=404, detail="No analytics DB")
    try:
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_key = ?", (key,)
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail=f"Session {key} not found")
        result = dict(row)

        # Cost data
        cost = conn.execute(
            "SELECT input_tokens, output_tokens, total_tokens, cost_usd, model "
            "FROM session_costs WHERE session_id = ?", (result["id"],)
        ).fetchone()
        result["cost"] = dict(cost) if cost else None

        # Tool usage
        tools = conn.execute(
            "SELECT tool_name, call_count FROM session_tools WHERE session_id = ? ORDER BY call_count DESC",
            (result["id"],)
        ).fetchall()
        result["tools"] = [dict(t) for t in tools]

        # Raw session log file
        log_out = LOGS_DIR / f"session_{key}.out"
        log_err = LOGS_DIR / f"session_{key}.err"
        result["log_out_exists"] = log_out.exists()
        result["log_err_exists"] = log_err.exists()
        if log_out.exists():
            # Return last 100 lines
            lines = log_out.read_text(errors="replace").splitlines()
            result["log_tail"] = "\n".join(lines[-100:])

        return result
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API — /api/goals
# ---------------------------------------------------------------------------

def _get_goals() -> dict:
    conn = get_loom_db()
    if not conn:
        # Fallback: loom_context.json
        loom_ctx = {}
        f = STATE_DIR / "loom_context.json"
        if f.exists():
            try:
                loom_ctx = json.loads(f.read_text())
            except Exception:
                pass
        return {"goals": [], "active_goal": None, "loom_context": loom_ctx, "source": "state_file"}

    try:
        goals = conn.execute(
            "SELECT id, name, status, priority, created_at, updated_at FROM goals ORDER BY priority DESC, id"
        ).fetchall()

        # Active goal tasks
        loom_ctx_file = STATE_DIR / "loom_context.json"
        active_goal_id = None
        if loom_ctx_file.exists():
            try:
                active_goal_id = json.loads(loom_ctx_file.read_text()).get("active_goal_id")
            except Exception:
                pass

        active_tasks = []
        if active_goal_id:
            rows = conn.execute(
                "SELECT id, name, status, priority, urgency_score, blocked_reason, handoff_note "
                "FROM tasks WHERE goal_id = ? ORDER BY urgency_score DESC, priority DESC",
                (active_goal_id,)
            ).fetchall()
            active_tasks = [dict(r) for r in rows]

        # Task counts per goal
        task_counts = {}
        for row in conn.execute(
            "SELECT goal_id, status, COUNT(*) as n FROM tasks GROUP BY goal_id, status"
        ).fetchall():
            gid = row["goal_id"]
            if gid not in task_counts:
                task_counts[gid] = {}
            task_counts[gid][row["status"]] = row["n"]

        goals_list = []
        for g in goals:
            gd = dict(g)
            gd["task_counts"] = task_counts.get(g["id"], {})
            gd["is_active"] = (g["id"] == active_goal_id)
            goals_list.append(gd)

        return {
            "goals": goals_list,
            "active_goal_id": active_goal_id,
            "active_tasks": active_tasks,
            "source": "loom_db",
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API — /api/costs
# ---------------------------------------------------------------------------

def _get_costs() -> dict:
    conn = get_analytics_db()
    if not conn:
        return {"costs": [], "totals": {}, "top_tools": [], "weekly": []}
    try:
        costs = conn.execute(
            "SELECT s.session_key, s.started_at, s.session_type, s.duration_minutes, "
            "c.input_tokens, c.output_tokens, c.total_tokens, c.cost_usd, c.model "
            "FROM sessions s JOIN session_costs c ON c.session_id = s.id "
            "ORDER BY s.started_at DESC"
        ).fetchall()

        totals_row = conn.execute(
            "SELECT SUM(c.total_tokens) as total_tokens, SUM(c.cost_usd) as total_cost "
            "FROM session_costs c"
        ).fetchone()

        top_tools = conn.execute("SELECT * FROM top_tools").fetchall()
        weekly_cost = conn.execute("SELECT * FROM weekly_cost ORDER BY week DESC LIMIT 12").fetchall()

        return {
            "costs": [dict(r) for r in costs],
            "totals": {
                "total_tokens": totals_row["total_tokens"] if totals_row else 0,
                "total_cost_usd": round(totals_row["total_cost"] or 0, 4) if totals_row else 0,
                "sessions_with_cost": len(costs),
            },
            "top_tools": [dict(r) for r in top_tools],
            "weekly_cost": [dict(r) for r in weekly_cost],
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API — /api/character (capability: soul.md exists)
# ---------------------------------------------------------------------------

def _get_character() -> dict:
    result: dict = {"available": False}

    soul_path = MEMORY_DIR / "work" / "soul.md"
    if soul_path.exists():
        result["available"] = True
        result["soul_excerpt"] = _read_head(soul_path, lines=30)

    # Behavioral context
    bc_file = STATE_DIR / "behavioral_context.txt"
    if bc_file.exists():
        text = bc_file.read_text()
        result["behavioral_raw"] = text
        for line in text.splitlines():
            m = re.match(r"^#.*Trust=([\d.]+).*Warmth=([\d.]+).*Friction=([\d.]+)", line)
            if m:
                result["trust"] = float(m.group(1))
                result["warmth"] = float(m.group(2))
                result["friction"] = float(m.group(3))

    # Character consistency from session files
    sessions_dir = MEMORY_DIR / "sessions"
    if sessions_dir.exists():
        session_files = sorted(sessions_dir.glob("*.md"))
        result["session_files_count"] = len(session_files)

        # Quick heuristic scan: count kaomoji and character symbols
        kaomoji_pattern = re.compile(r'[╥╰⊙눈¬]\s*[_﹏▽°\-]\s*[╥\)]*|\(´[_·ω].*?\)|╥﹏╥|´_`|눈_눈|⊙_⊙')
        symbol_pattern = re.compile(r'𓂀|◈|⟁|🜸|◉|⬡|◆')
        costly_pattern = re.compile(r'before 6am|at 3am|at 2am|nobody will|no one will|I was wrong|I disagree', re.I)

        sessions_with_kaomoji = 0
        sessions_with_symbols = 0
        sessions_with_costly = 0

        for f in session_files[-50:]:  # check last 50
            try:
                text = f.read_text(errors="replace")
                if kaomoji_pattern.search(text):
                    sessions_with_kaomoji += 1
                if symbol_pattern.search(text):
                    sessions_with_symbols += 1
                if costly_pattern.search(text):
                    sessions_with_costly += 1
            except Exception:
                pass

        checked = min(50, len(session_files))
        result["character_scan"] = {
            "sessions_checked": checked,
            "kaomoji_rate": round(sessions_with_kaomoji / checked, 2) if checked else 0,
            "symbol_rate": round(sessions_with_symbols / checked, 2) if checked else 0,
            "costly_signal_rate": round(sessions_with_costly / checked, 2) if checked else 0,
            "kaomoji_count": sessions_with_kaomoji,
            "symbol_count": sessions_with_symbols,
            "costly_signal_count": sessions_with_costly,
        }

    # Relationship data
    an = agent_name()
    on = owner_name()
    rel_path = MEMORY_DIR / "work" / "musubi_data" / "users" / an / f"{on}.md"
    if rel_path.exists():
        result["relationship_excerpt"] = _read_head(rel_path, lines=40)
        result["relationship_available"] = True
    else:
        result["relationship_available"] = False

    return result


# ---------------------------------------------------------------------------
# API — /api/memory
# ---------------------------------------------------------------------------

def _get_memory() -> dict:
    result: dict = {}

    # Latest summary
    summary_path = MEMORY_DIR / "latest_summary.md"
    if summary_path.exists():
        text = summary_path.read_text(errors="replace")
        # Extract HOT STATE block
        hot_match = re.search(r"(## HOT STATE.*?)(?=\n## |\Z)", text, re.DOTALL)
        result["hot_state"] = hot_match.group(1).strip() if hot_match else text[:500]
        result["latest_summary"] = text

    # Session files list
    sessions_dir = MEMORY_DIR / "sessions"
    if sessions_dir.exists():
        files = sorted(sessions_dir.glob("*.md"))
        result["session_files"] = [f.name for f in files]
        result["session_files_count"] = len(files)
        # Preview of most recent session
        if files:
            try:
                result["latest_session_file"] = files[-1].name
                result["latest_session_preview"] = _read_head(files[-1], lines=20)
            except Exception:
                pass

    # Progress
    progress_path = MEMORY_DIR / "progress.md"
    if progress_path.exists():
        result["progress"] = _read_head(progress_path, lines=40)

    return result


# ---------------------------------------------------------------------------
# API — /api/comms
# ---------------------------------------------------------------------------

def _get_comms() -> dict:
    result: dict = {}

    # Nexus conversation state
    nexus_file = STATE_DIR / "nexus_conversation_state.json"
    if nexus_file.exists():
        try:
            result["nexus"] = json.loads(nexus_file.read_text())
        except Exception:
            result["nexus"] = None

    # Nexus last read
    nexus_last = STATE_DIR / "nexus_last_read.json"
    if nexus_last.exists():
        try:
            result["nexus_last_read"] = json.loads(nexus_last.read_text())
        except Exception:
            pass

    # Telegram incoming
    tg_file = STATE_DIR / "telegram_incoming.txt"
    if tg_file.exists():
        result["telegram_last"] = tg_file.read_text().strip()

    return result


# ---------------------------------------------------------------------------
# API — /api/schedule
# ---------------------------------------------------------------------------

_DEFAULT_NIGHT_SLOTS = [
    {"time": "23:00", "duration": 90},
    {"time": "01:10", "duration": 75},
    {"time": "02:25", "duration": 75},
    {"time": "03:40", "duration": 75},
    {"time": "04:55", "duration": 60},
]


def _default_schedule_entries() -> list:
    return [
        {
            "id": i + 1,
            "label": "nightly",
            "type": "execution",
            "time": s["time"],
            "duration": s["duration"],
            "recurrence": "daily",
            "days": None,
            "date": None,
        }
        for i, s in enumerate(_DEFAULT_NIGHT_SLOTS)
    ]


def _get_schedule() -> dict:
    if SCHEDULE_FILE.exists():
        try:
            entries = json.loads(SCHEDULE_FILE.read_text())
            return {"entries": entries, "source": "file"}
        except Exception:
            pass
    entries = _default_schedule_entries()
    return {"entries": entries, "source": "default"}


def _save_schedule(entries: list) -> dict:
    try:
        SCHEDULE_FILE.write_text(json.dumps(entries, indent=2))
        return {"ok": True, "count": len(entries)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Session types — read/write config/session_types/*.yaml
# ---------------------------------------------------------------------------

def _get_session_types() -> list:
    types = []
    if SESSION_TYPES_DIR.exists():
        for f in sorted(SESSION_TYPES_DIR.glob("*.yaml")):
            content = f.read_text()
            name_m = re.search(r'^name:\s*["\']?(.+?)["\']?\s*$', content, re.MULTILINE)
            name = name_m.group(1).strip().strip('"\'') if name_m else f.stem
            types.append({"id": f.stem, "name": name, "content": content})
    return types


def _save_session_type(type_id: str, content: str) -> dict:
    if not re.match(r'^[a-z0-9_-]+$', type_id):
        raise HTTPException(status_code=400, detail="invalid type id — use [a-z0-9_-] only")
    SESSION_TYPES_DIR.mkdir(parents=True, exist_ok=True)
    (SESSION_TYPES_DIR / f"{type_id}.yaml").write_text(content)
    return {"ok": True, "id": type_id}


def _get_nightly_schedule() -> dict:
    if NIGHTLY_SCHEDULE_FILE.exists():
        try:
            return json.loads(NIGHTLY_SCHEDULE_FILE.read_text())
        except Exception:
            pass
    return {"version": 2, "windows": [], "one_off": []}


def _save_nightly_schedule(data: dict) -> dict:
    try:
        CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
        NIGHTLY_SCHEDULE_FILE.write_text(json.dumps(data, indent=2))
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# API — actions
# ---------------------------------------------------------------------------

def _valid_session_types() -> set:
    """Session type ids that are directly selectable via SESSION_TYPE.

    Only files with a top-level `id:` field count -- the maintenance_scope*
    and planning_*_scope files are override fragments merged in by
    resolve_session_type.py's load_type_config(), not standalone types, and
    have no prompt_file/behavioral_overrides of their own to launch with.
    """
    if not SESSION_TYPES_DIR.exists():
        return set()
    valid = set()
    for f in SESSION_TYPES_DIR.glob("*.yaml"):
        try:
            if any(line.strip().startswith("id:") for line in f.read_text().splitlines()):
                valid.add(f.stem)
        except OSError:
            continue
    return valid


def _wait_for_trigger_result(request_id: str, timeout: float = TRIGGER_CONFIRM_TIMEOUT_SEC) -> Optional[dict]:
    """Poll MANUAL_TRIGGER_RESULT_FILE for wake.sh's gate decision on this request.

    wake.sh writes this file (keyed by request_id) as soon as it knows whether
    a session actually launched -- well before the Claude session itself
    finishes. Without this, "launched": True here would only ever mean "a
    subprocess was started," true even when a gate silently skips or aborts
    the launch a moment later.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if MANUAL_TRIGGER_RESULT_FILE.exists():
            try:
                data = json.loads(MANUAL_TRIGGER_RESULT_FILE.read_text())
                if data.get("request_id") == request_id:
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        time.sleep(TRIGGER_CONFIRM_POLL_SEC)
    return None


def _trigger_manual(session_type: str = "") -> JSONResponse:
    wake_sh = SCRIPTS_DIR / "wake.sh"
    if not wake_sh.exists():
        raise HTTPException(status_code=500, detail="wake.sh not found")

    if session_type:
        valid_types = _valid_session_types()
        if session_type not in valid_types:
            raise HTTPException(
                status_code=400,
                detail=f"unknown session_type '{session_type}' -- valid: {sorted(valid_types)}",
            )

    request_id = str(uuid.uuid4())
    extra_env: dict = {"TRIGGER_MODE": "manual", "TRIGGER_REQUEST_ID": request_id}
    if session_type:
        extra_env["SESSION_TYPE"] = session_type

    try:
        subprocess.Popen(
            ["bash", str(wake_sh)],
            env={**os.environ, **extra_env},
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    result = _wait_for_trigger_result(request_id)
    if result is None:
        return JSONResponse(status_code=202, content={
            "ok": False,
            "started": "unknown",
            "reason": f"no confirmation from wake.sh within {TRIGGER_CONFIRM_TIMEOUT_SEC}s "
                      "-- it may still be starting, or may have already finished its gates",
        })

    decision = result.get("decision")
    if decision == "launched":
        return JSONResponse(status_code=200, content={
            "ok": True,
            "started": True,
            "session_type": result.get("session_type"),
            "pid": result.get("pid"),
        })
    if decision == "skipped":
        return JSONResponse(status_code=409, content={
            "ok": False, "started": False, "reason": result.get("reason"),
        })
    # decision == "aborted" (e.g. philosophy_cap), or any unrecognized value
    return JSONResponse(status_code=200, content={
        "ok": False, "started": False, "reason": result.get("reason"),
    })


def _emergency_enable() -> dict:
    script = TOOLS_DIR / "emergency_mode.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="emergency_mode.sh not found")
    result = subprocess.run(["bash", str(script), "on"], capture_output=True, text=True, timeout=10)
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


def _emergency_disable() -> dict:
    script = TOOLS_DIR / "emergency_mode.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="emergency_mode.sh not found")
    result = subprocess.run(["bash", str(script), "off"], capture_output=True, text=True, timeout=10)
    return {"ok": result.returncode == 0, "stdout": result.stdout, "stderr": result.stderr}


def _run_health_check() -> dict:
    script = TOOLS_DIR / "health_check.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="health_check.sh not found")
    result = subprocess.run(
        ["bash", str(script)],
        capture_output=True, text=True, timeout=30,
        cwd=str(PROJECT_DIR),
    )
    output = result.stdout + result.stderr
    ok_count = output.count("[OK]")
    warn_count = output.count("[WARN]")
    fail_count = output.count("[FAIL]")
    return {
        "output": output,
        "ok": fail_count == 0,
        "ok_count": ok_count,
        "warn_count": warn_count,
        "fail_count": fail_count,
    }


def _check_usage() -> dict:
    script = TOOLS_DIR / "check_session.sh"
    if not script.exists():
        raise HTTPException(status_code=500, detail="check_session.sh not found")
    result = subprocess.run(
        ["bash", str(script), "--usage"],
        capture_output=True, text=True, timeout=20,
        cwd=str(PROJECT_DIR),
    )
    output = result.stdout
    parsed: dict = {"raw": output}
    for line in output.splitlines():
        if "utilization_5h:" in line:
            # Extract just the float (e.g. "0.16  (status: ...)" → 0.16)
            val_str = line.split(":", 1)[1].strip().split()[0]
            try:
                parsed["utilization_5h"] = round(float(val_str) * 100)
            except ValueError:
                parsed["utilization_5h"] = 0
        elif "utilization_7d:" in line:
            val_str = line.split(":", 1)[1].strip().split()[0]
            try:
                parsed["utilization_7d"] = round(float(val_str) * 100)
            except ValueError:
                parsed["utilization_7d"] = 0
        elif line.startswith("ACTION:"):
            parsed["action"] = line.split(":", 1)[1].strip()
        elif "usage_check_status:" in line:
            parsed["status"] = line.split(":", 1)[1].strip()
    return parsed


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _read_head(path: pathlib.Path, lines: int = 30) -> str:
    try:
        text = path.read_text(errors="replace")
        return "\n".join(text.splitlines()[:lines])
    except Exception:
        return ""


_ui_file = "web_ui.html"  # overridden by --ui-file arg


def _load_ui_html() -> str:
    # Agent-specific custom UI takes priority over the base web_ui.html.
    # Create tools/web_ui_custom.html in the agent project to override.
    # self_update.sh never touches web_ui_custom.html — it is agent-owned.
    custom_path = TOOLS_DIR / "web_ui_custom.html"
    if custom_path.exists():
        return custom_path.read_text(errors="replace")
    ui_path = TOOLS_DIR / _ui_file
    if ui_path.exists():
        return ui_path.read_text(errors="replace")
    return f"<h1>{_ui_file} not found</h1><p>Place it in tools/</p>"


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Node Web UI", version="1.0.0")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse(content=_load_ui_html())


@app.get("/api/capabilities")
async def capabilities():
    return detect_capabilities()


@app.get("/api/status")
async def status():
    return _get_status()


@app.get("/api/sessions")
async def sessions(limit: int = 50, offset: int = 0, type_filter: Optional[str] = None):
    return _get_sessions(limit=limit, offset=offset, type_filter=type_filter)


@app.get("/api/sessions/{key}")
async def session_detail(key: str):
    return _get_session_detail(key)


@app.get("/api/goals")
async def goals():
    return _get_goals()


@app.get("/api/costs")
async def costs():
    return _get_costs()


@app.get("/api/character")
async def character():
    return _get_character()


@app.get("/api/memory")
async def memory():
    return _get_memory()


@app.get("/api/comms")
async def comms():
    return _get_comms()


# ---------------------------------------------------------------------------
# Docs / Diagrams API
# ---------------------------------------------------------------------------

@app.get("/api/docs")
async def docs_index():
    """List all .mmd diagram files from .claude/architecture/ and docs/diagrams/."""
    search_dirs = [
        PROJECT_DIR / ".claude" / "architecture",
        PROJECT_DIR / "docs" / "diagrams",
    ]
    files = []
    for base_dir in search_dirs:
        if not base_dir.exists():
            continue
        for p in sorted(base_dir.rglob("*.mmd")):
            rel_to_base = p.relative_to(base_dir)
            category = str(rel_to_base.parent) if str(rel_to_base.parent) != "." else "root"
            files.append({
                "path": str(p.relative_to(PROJECT_DIR)),
                "name": p.stem,
                "category": category,
                "source_dir": base_dir.name,
            })
    return {"files": files}


@app.get("/api/docs/content")
async def docs_content(path: str):
    """Return raw .mmd content. path is relative to PROJECT_DIR."""
    target = (PROJECT_DIR / path).resolve()
    if not str(target).startswith(str(PROJECT_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Path outside project dir")
    if target.suffix != ".mmd":
        raise HTTPException(status_code=400, detail="Only .mmd files supported")
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return {"path": path, "content": target.read_text(errors="replace")}


@app.get("/api/schedule")
async def get_schedule():
    return _get_schedule()


@app.post("/api/schedule")
async def save_schedule(request: Request):
    body = await request.json()
    entries = body.get("entries", [])
    return _save_schedule(entries)


@app.post("/api/trigger/manual")
async def trigger_manual(request: Request):
    # Empty body -- no override requested, same as today. A non-empty body
    # that isn't valid JSON is a client error, not silently "no override";
    # previously any parse failure (including a genuine typo/bug in the
    # caller) was swallowed and treated identically to "nothing sent."
    raw = await request.body()
    session_type = ""
    if raw.strip():
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        session_type = str(body.get("session_type", "")).strip()
    return _trigger_manual(session_type)


@app.post("/api/emergency/enable")
async def emergency_enable():
    return _emergency_enable()


@app.post("/api/emergency/disable")
async def emergency_disable():
    return _emergency_disable()


@app.post("/api/health/run")
async def run_health():
    return _run_health_check()


@app.post("/api/usage/check")
async def check_usage():
    return _check_usage()


@app.get("/api/session-types")
async def get_session_types():
    return {"types": _get_session_types()}


@app.put("/api/session-types/{type_id}")
async def save_session_type(type_id: str, request: Request):
    body = await request.json()
    content = str(body.get("content", ""))
    return _save_session_type(type_id, content)


@app.get("/api/nightly-schedule")
async def get_nightly_schedule():
    return _get_nightly_schedule()


@app.put("/api/nightly-schedule")
async def save_nightly_schedule(request: Request):
    body = await request.json()
    return _save_nightly_schedule(body)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Node Web UI server")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")
    parser.add_argument("--port", type=int, default=8767, help="Port (default: 8767)")
    parser.add_argument("--reload", action="store_true", help="Auto-reload on file changes")
    parser.add_argument("--ui-file", default="web_ui.html", help="HTML file to serve from tools/ (default: web_ui.html)")
    parser.add_argument("--project-dir", default=None, help="Override project root (default: auto-detected from script location)")
    args = parser.parse_args()

    global _ui_file, PROJECT_DIR, STATE_DIR, LOGS_DIR, TOOLS_DIR, MEMORY_DIR, SCRIPTS_DIR, ANALYTICS_DB, SESSION_LOG_CSV, SCHEDULE_FILE, CONFIGS_DIR, SESSION_TYPES_DIR, NIGHTLY_SCHEDULE_FILE, MANUAL_TRIGGER_RESULT_FILE
    _ui_file = args.ui_file
    if args.project_dir:
        PROJECT_DIR = pathlib.Path(args.project_dir).resolve()
        STATE_DIR = PROJECT_DIR / "state"
        LOGS_DIR = PROJECT_DIR / "logs"
        TOOLS_DIR = PROJECT_DIR / "tools"
        MEMORY_DIR = PROJECT_DIR / "memory"
        SCRIPTS_DIR = PROJECT_DIR / "scripts"
        ANALYTICS_DB = LOGS_DIR / "analytics.db"
        SESSION_LOG_CSV = LOGS_DIR / "session_log.csv"
        SCHEDULE_FILE = STATE_DIR / "schedule.json"
        CONFIGS_DIR = PROJECT_DIR / "config"
        SESSION_TYPES_DIR = CONFIGS_DIR / "session_types"
        NIGHTLY_SCHEDULE_FILE = CONFIGS_DIR / "session_schedule.json"
        MANUAL_TRIGGER_RESULT_FILE = STATE_DIR / "manual_trigger_result.json"

    print(f"[web_server] Starting on http://{args.host}:{args.port}")
    print(f"[web_server] Project: {PROJECT_DIR}")
    print(f"[web_server] Agent: {agent_name()}")

    uvicorn.run(
        "web_server:app" if args.reload else app,
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
        ws="none",  # disable WS — avoids system websockets lib conflict
    )


if __name__ == "__main__":
    main()
