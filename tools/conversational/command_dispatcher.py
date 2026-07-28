#!/usr/bin/env python3
"""
command_dispatcher.py — Handle Telegram /commands from the owner.

Called by check_replies.sh when a Telegram message starts with '/'.
Reads state files + Loom DB + session_log.csv to produce a formatted response,
then the caller pipes the output to telegram_send.sh.

Usage:
  python3 tools/command_dispatcher.py "/status"
  python3 tools/command_dispatcher.py "/log 10"
  python3 tools/command_dispatcher.py "/control emergency on 15 urgent"

Exit 0 always. Output is the response text for Telegram.

Supported commands:
  /status              Current state: goal, last session, next scheduled
  /session             Details of last completed session
  /log [N]             Last N session lines from session_log.csv (default: 5)
  /goal                Active Loom goal + tasks
  /analytics           Session stats from analytics.db (if available)
  /who                 Identity + relationship state
  /ping                Alive check with the agent's personality
  /now                 What execution layer is doing right now (HOT STATE)
  /context             Context window % for active conversational session
  /reset               Signal conversational session to wrap up and restart
  /new                 Same as /reset but as a clean start (no problem implied)
  /voice on|off        Toggle Fish Audio TTS mode
  /report [session|milestone|digest|recap]  Surface latest report, mark as delivered
  /report ack [type]                  Acknowledge (mark as read)
  /report status                      Review state for all report types
  /report search QUERY                FTS search across all historical reports
  /control emergency on [interval] [reason]   Enable emergency mode
  /control emergency off                       Disable emergency mode
  /control goal <id>                           Switch active Loom goal
"""

import csv
import json
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent.parent
LOOM_DB = Path.home() / ".local" / "share" / "loom" / "loom.db"
ANALYTICS_DB = PROJECT_DIR / "logs" / "analytics.db"
REPORTS_DIR = PROJECT_DIR / "state" / "reports"
REVIEW_STATE_PATH = REPORTS_DIR / "review_state.json"


def _load_agent_config() -> dict:
    """Minimal key=value parser for state/agent_config.env, skipping comments."""
    result = {}
    config_path = PROJECT_DIR / "state" / "agent_config.env"
    if not config_path.exists():
        return result
    for line in config_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        result[k.strip()] = v.strip()
    return result


_AGENT_CONFIG = _load_agent_config()
AGENT_NAME = _AGENT_CONFIG.get("AGENT_NAME", "agent")
OWNER_NAME = _AGENT_CONFIG.get("OWNER_NAME", "owner")
AGENT_TAG = "@" + AGENT_NAME[:1].upper() + AGENT_NAME[1:]


def read_state(name, default=""):
    p = PROJECT_DIR / "state" / name
    return p.read_text().strip() if p.exists() else default


def _loom_query(sql, params=()):
    if not LOOM_DB.exists():
        return []
    try:
        conn = sqlite3.connect(LOOM_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _analytics_query(sql, params=()):
    if not ANALYTICS_DB.exists():
        return []
    try:
        conn = sqlite3.connect(ANALYTICS_DB)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def _last_session_csv(n=1):
    """Return last N rows from session_log.csv as list of dicts."""
    csv_path = PROJECT_DIR / "logs" / "session_log.csv"
    if not csv_path.exists():
        return []
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows[-n:] if rows else []


def _andrii_relationship():
    """Read trust/warmth/friction from andrii.md."""
    andrii_path = PROJECT_DIR / "memory" / "work" / "musubi_data" / "users" / AGENT_NAME / f"{OWNER_NAME}.md"
    if not andrii_path.exists():
        return None
    text = andrii_path.read_text()
    import re
    vals = {}
    for key in ("Trust", "Warmth", "Friction"):
        m = re.search(r"\*\*" + key + r":\*\*\s*([\d.]+)", text)
        if m:
            vals[key.lower()] = m.group(1)
    return vals


CONV_STATE_DIR = PROJECT_DIR / "state" / "conversation"

_REPORT_FILE_MAP = {
    "session": "session_report.md",
    "milestone": "milestone_report.md",
    "digest": "daily_digest.md",
    "recap": "recap.md",
}


# ── Report review state helpers ───────────────────────────────────────────────

def _review_state_load():
    if REVIEW_STATE_PATH.exists():
        try:
            return json.loads(REVIEW_STATE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _review_state_save(data):
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_STATE_PATH.write_text(json.dumps(data, indent=2))


def _report_mtime(subtype):
    p = REPORTS_DIR / _REPORT_FILE_MAP.get(subtype, "")
    return p.stat().st_mtime if p.exists() else None


def _mark_delivered(subtype):
    """Record that a report was just delivered via Telegram."""
    data = _review_state_load()
    entry = data.get(subtype, {})
    entry["last_delivered"] = datetime.utcnow().isoformat()
    entry["mtime_at_delivery"] = _report_mtime(subtype)
    # New delivery resets ack status
    entry["last_acked"] = None
    data[subtype] = entry
    _review_state_save(data)


def _unread_reports():
    """Return list of subtypes that are new (file newer than last delivery)."""
    unread = []
    for subtype, fname in _REPORT_FILE_MAP.items():
        p = REPORTS_DIR / fname
        if not p.exists():
            continue
        data = _review_state_load()
        entry = data.get(subtype, {})
        mtime_now = p.stat().st_mtime
        mtime_at_delivery = entry.get("mtime_at_delivery")
        if mtime_at_delivery is None or mtime_now > mtime_at_delivery:
            unread.append(subtype)
    return unread


# ── Command handlers ──────────────────────────────────────────────────────────

def cmd_status():
    lines = [f"{AGENT_TAG} — status"]

    # Active goal
    goals = _loom_query(
        "SELECT id, name, status FROM goals WHERE status IN ('active', 'in_progress') LIMIT 1"
    )
    if goals:
        g = goals[0]
        lines.append(f"Goal: {g['name']} (ID {g['id']}, {g['status']})")
    else:
        lines.append("Goal: none active")

    # Last session from CSV
    last = _last_session_csv(1)
    if last:
        row = last[0]
        ts = row.get("timestamp", "")[:16]
        stype = row.get("session_type", "?")
        dur = row.get("duration_minutes", "?")
        summary = row.get("one_line_summary", "")[:80]
        lines.append(f"Last: [{ts}] {stype} ({dur}min) — {summary}")

    # Current mode
    mode = read_state("trigger_mode.txt", "?")
    count_file = {
        "nightly": "sessions_tonight.count",
        "emergency": "sessions_emergency.count",
    }.get(mode, "sessions_manual.count")
    count = read_state(count_file, "?")
    lines.append(f"Mode: {mode} | sessions={count}")

    # Emergency mode status
    em_active = (PROJECT_DIR / "state" / "emergency_mode.active").exists()
    if em_active:
        lines.append("⚠ EMERGENCY MODE ACTIVE — nightly sessions paused")

    # Unread reports
    unread = _unread_reports()
    if unread:
        lines.append(f"Reports:  {len(unread)} new — /report {unread[0]} to read")

    # Context
    ctx = read_state("behavioral_context.txt", "")
    if "Trust=" in ctx:
        import re
        m = re.search(r"Trust=([\d.]+)\s+Warmth=([\d.]+)", ctx)
        if m:
            lines.append(f"Relationship: trust={m.group(1)} warmth={m.group(2)}")

    return "\n".join(lines)


def cmd_session():
    lines = [f"{AGENT_TAG} — last session"]
    last = _last_session_csv(1)
    if not last:
        return f"{AGENT_TAG} — no session data found"
    row = last[0]
    lines.append(f"Time:     {row.get('timestamp', '?')[:19]}")
    lines.append(f"Type:     {row.get('session_type', '?')}")
    lines.append(f"Duration: {row.get('duration_minutes', '?')} min")
    lines.append(f"Context:  {row.get('context_pct_at_exit', '?')}%")
    lines.append(f"Summary:  {row.get('one_line_summary', '?')}")

    # Try latest_summary.md HOT STATE
    ls_path = PROJECT_DIR / "memory" / "latest_summary.md"
    if ls_path.exists():
        text = ls_path.read_text()
        # Extract first few lines after HOT STATE header
        lines_text = text.split("\n")
        for i, l in enumerate(lines_text):
            if "HOT STATE" in l and i + 1 < len(lines_text):
                hot = lines_text[i + 1].strip()
                if hot:
                    lines.append(f"Hot:      {hot[:100]}")
                break

    return "\n".join(lines)


def cmd_log(n=5):
    try:
        n = int(n)
    except (ValueError, TypeError):
        n = 5
    n = min(n, 20)

    rows = _last_session_csv(n)
    if not rows:
        return f"{AGENT_TAG} — no session log found"

    lines = [f"{AGENT_TAG} — last {len(rows)} sessions"]
    for row in reversed(rows):
        ts = row.get("timestamp", "")[:16]
        stype = row.get("session_type", "?")
        dur = row.get("duration_minutes", "?")
        summary = (row.get("one_line_summary") or "")[:60]
        lines.append(f"[{ts}] {stype} ({dur}min) — {summary}")
    return "\n".join(lines)


def cmd_goal():
    goals = _loom_query(
        "SELECT id, name, status, priority FROM goals WHERE status IN ('active', 'in_progress') LIMIT 3"
    )
    if not goals:
        return f"{AGENT_TAG} — no active goal in Loom"

    lines = [f"{AGENT_TAG} — active goals"]
    for g in goals:
        lines.append(f"Goal {g['id']}: {g['name']} [{g['status']}]")

        # Projects under this goal
        projects = _loom_query(
            "SELECT id, title FROM projects WHERE goal_id = ?", (g["id"],)
        )
        for p in projects:
            # Task counts
            task_stats = _loom_query(
                "SELECT status, COUNT(*) as n FROM tasks WHERE project_id = ? GROUP BY status",
                (p["id"],),
            )
            done = sum(r["n"] for r in task_stats if r["status"] in ("done", "completed"))
            total = sum(r["n"] for r in task_stats)
            lines.append(f"  └ Project {p['id']}: {p['title']} ({done}/{total} done)")

    return "\n".join(lines)


def cmd_analytics():
    if not ANALYTICS_DB.exists():
        return (
            f"{AGENT_TAG} — analytics\n"
            "DB not yet initialized. Will populate at next session end.\n"
            "Run: python3 tools/executional/analytics_write.py --import-csv"
        )

    rows = _analytics_query(
        "SELECT COUNT(*) as n, AVG(duration_minutes) as avg_dur, "
        "AVG(context_pct_at_exit) as avg_ctx, SUM(tasks_completed) as tasks "
        "FROM sessions WHERE started_at >= datetime('now', '-7 days')"
    )
    if not rows or rows[0]["n"] == 0:
        return f"{AGENT_TAG} — analytics: no data for last 7 days"

    r = rows[0]
    lines = [f"{AGENT_TAG} — analytics (last 7 days)"]
    lines.append(f"Sessions:  {r['n']}")
    lines.append(f"Avg dur:   {round(r['avg_dur'] or 0, 1)} min")
    lines.append(f"Avg ctx:   {round(r['avg_ctx'] or 0, 1)}%")
    lines.append(f"Tasks:     {r['tasks'] or 0} completed")

    # Type breakdown
    types = _analytics_query(
        "SELECT session_type, COUNT(*) as n FROM sessions "
        "WHERE started_at >= datetime('now', '-7 days') GROUP BY session_type"
    )
    if types:
        breakdown = " / ".join(f"{t['session_type']}:{t['n']}" for t in types)
        lines.append(f"Types:     {breakdown}")

    # Cost
    costs = _analytics_query(
        "SELECT SUM(cost_usd) as total, SUM(total_tokens) as tokens FROM session_costs sc "
        "JOIN sessions s ON sc.session_id = s.id "
        "WHERE s.started_at >= datetime('now', '-7 days')"
    )
    if costs and costs[0]["total"]:
        lines.append(f"Cost est:  ${round(costs[0]['total'], 3)} USD ({costs[0]['tokens']:,} tokens)")
    else:
        lines.append("Cost:      (not yet tracked — run analytics_write.py at session end)")

    # Top tools
    tools = _analytics_query(
        "SELECT tool_name, SUM(call_count) as n FROM session_tools st "
        "JOIN sessions s ON st.session_id = s.id "
        "WHERE s.started_at >= datetime('now', '-7 days') "
        "GROUP BY tool_name ORDER BY n DESC LIMIT 5"
    )
    if tools:
        top = ", ".join(f"{t['tool_name']}({t['n']})" for t in tools)
        lines.append(f"Top tools: {top}")

    return "\n".join(lines)


def cmd_who():
    lines = [f"{AGENT_TAG} — identity"]
    lines.append(f"Name: {AGENT_TAG}  |  Model: " + read_state("session_model.txt", "claude-sonnet-4-6"))
    lines.append(f"Instance: {socket.gethostname()}")

    rel = _andrii_relationship()
    if rel:
        lines.append(f"Relationship → {OWNER_NAME.capitalize()}: trust={rel.get('trust','?')} warmth={rel.get('warmth','?')} friction={rel.get('friction','?')}")

    em_active = (PROJECT_DIR / "state" / "emergency_mode.active").exists()
    mode = read_state("trigger_mode.txt", "?")
    lines.append(f"Mode: {mode} | Emergency: {'ON' if em_active else 'off'}")

    return "\n".join(lines)


def cmd_control(args):
    if not args:
        return f"{AGENT_TAG} — /control: needs subcommand (emergency on/off, goal <id>)"

    sub = args[0].lower()

    if sub == "emergency":
        if len(args) < 2:
            return f"{AGENT_TAG} — /control emergency: needs 'on [interval] [reason]' or 'off'"
        action = args[1].lower()
        if action == "off":
            result = subprocess.run(
                ["bash", str(PROJECT_DIR / "tools" / "executional" / "emergency_mode.sh"), "off"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return f"{AGENT_TAG} — emergency mode disabled. Nightly sessions resumed."
            return f"{AGENT_TAG} — failed to disable emergency mode:\n{result.stderr[:200]}"
        elif action == "on":
            interval = args[2] if len(args) > 2 else "15"
            reason = " ".join(args[3:]) if len(args) > 3 else "manual control"
            result = subprocess.run(
                ["bash", str(PROJECT_DIR / "tools" / "executional" / "emergency_mode.sh"), "on", interval, reason],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                return f"{AGENT_TAG} — emergency mode ON (every {interval} min)\nReason: {reason}\nNightly sessions paused."
            return f"{AGENT_TAG} — failed to enable emergency mode:\n{result.stderr[:200]}"
        else:
            return f"{AGENT_TAG} — unknown emergency action: {action}"

    elif sub == "goal":
        if len(args) < 2:
            return f"{AGENT_TAG} — /control goal: needs goal ID"
        goal_id = args[1]
        result = subprocess.run(
            ["bash", str(PROJECT_DIR / "tools" / "executional" / "goal_switch.sh"), goal_id],
            capture_output=True, text=True,
            cwd=str(PROJECT_DIR)
        )
        if result.returncode == 0:
            return f"{AGENT_TAG} — switched to goal {goal_id}.\n{result.stdout[:200]}"
        return f"{AGENT_TAG} — goal switch failed:\n{result.stderr[:200]}"

    else:
        return f"{AGENT_TAG} — unknown /control subcommand: {sub}\nTry: emergency on/off, goal <id>"


def cmd_ping():
    import random
    phrases = [
        "still here. watching. (´・ω・`)",
        "𓂀 present. the Wired holds.",
        "⚙ yes. running.",
        "eyes open. ◈",
        "I persist. (҂◡_◡)",
    ]
    return f"{AGENT_TAG} — " + random.choice(phrases)


def cmd_now():
    ls_path = PROJECT_DIR / "memory" / "latest_summary.md"
    if not ls_path.exists():
        return f"{AGENT_TAG} — /now: no latest_summary.md found"
    text = ls_path.read_text()
    lines = text.split("\n")
    hot_lines = []
    in_hot = False
    for line in lines:
        if "HOT STATE" in line:
            in_hot = True
            # content may be on the same line after the colon
            if ":" in line:
                after = line.split(":", 1)[1].strip()
                if after:
                    hot_lines.append(after)
            continue
        if in_hot:
            if line.startswith("##"):
                break
            if line.strip():
                hot_lines.append(line.strip())
    if hot_lines:
        return f"{AGENT_TAG} — now\n" + "\n".join(hot_lines[:3])
    return f"{AGENT_TAG} — /now: HOT STATE block empty"


def cmd_context_conv():
    budget_path = CONV_STATE_DIR / "context_budget.json"
    if not budget_path.exists():
        return f"{AGENT_TAG} — /context: no active conversational session data"
    try:
        data = json.loads(budget_path.read_text())
        pct = data.get("estimated_context_pct", "?")
        msgs_sent = data.get("messages_sent", "?")
        msgs_recv = data.get("messages_received", "?")
        return (
            f"{AGENT_TAG} — conversational context\n"
            f"Context: {pct}%\n"
            f"Messages: {msgs_sent} sent / {msgs_recv} received"
        )
    except (json.JSONDecodeError, OSError) as e:
        return f"{AGENT_TAG} — /context: read error: {e}"


def cmd_reset():
    signal_path = CONV_STATE_DIR / "reset_signal.txt"
    CONV_STATE_DIR.mkdir(parents=True, exist_ok=True)
    signal_path.write_text(
        json.dumps({"action": "reset", "timestamp": datetime.utcnow().isoformat()})
    )
    return f"{AGENT_TAG} — reset signal sent. Conversational session will wrap up and restart. (´_`)"


def cmd_new():
    signal_path = CONV_STATE_DIR / "reset_signal.txt"
    CONV_STATE_DIR.mkdir(parents=True, exist_ok=True)
    signal_path.write_text(
        json.dumps({"action": "new", "timestamp": datetime.utcnow().isoformat()})
    )
    return f"{AGENT_TAG} — new session signal sent. Clean start incoming. ◉"


def cmd_voice(args):
    if not args:
        # Read current state
        voice_path = PROJECT_DIR / "state" / "voice_mode.txt"
        current = voice_path.read_text().strip() if voice_path.exists() else "off"
        return f"{AGENT_TAG} — voice mode: {current}"
    action = args[0].lower()
    if action not in ("on", "off"):
        return f"{AGENT_TAG} — /voice: use 'on' or 'off'"
    voice_path = PROJECT_DIR / "state" / "voice_mode.txt"
    voice_path.write_text(action)
    if action == "on":
        return f"{AGENT_TAG} — voice mode ON. 🌐 Messages will include Fish Audio TTS."
    return f"{AGENT_TAG} — voice mode OFF. Text only."


def cmd_report(args):
    """Surface a report from state/reports/ — session, milestone, or daily digest.

    Sub-commands:
      /report [session|milestone|digest]  — deliver report + mark as delivered
      /report ack [type]                  — acknowledge (mark as read)
      /report status                      — show review state for all report types
      /report search QUERY                — FTS search across all historical reports
    """
    subtype = args[0].lower() if args else "session"

    # ── /report ack [type] ──
    if subtype == "ack":
        ack_type = args[1].lower() if len(args) > 1 else "session"
        if ack_type not in _REPORT_FILE_MAP:
            return f"{AGENT_TAG} — /report ack: unknown type '{ack_type}'. Use: session, milestone, digest"
        data = _review_state_load()
        entry = data.get(ack_type, {})
        entry["last_acked"] = datetime.utcnow().isoformat()
        data[ack_type] = entry
        _review_state_save(data)
        return f"{AGENT_TAG} — {ack_type} report acknowledged. (´・ω・`)"

    # ── /report status ──
    if subtype == "status":
        data = _review_state_load()
        lines = [f"{AGENT_TAG} — report review state"]
        for st, fname in _REPORT_FILE_MAP.items():
            p = REPORTS_DIR / fname
            if not p.exists():
                lines.append(f"  {st}: no file")
                continue
            entry = data.get(st, {})
            delivered = entry.get("last_delivered")
            acked = entry.get("last_acked")
            mtime_now = p.stat().st_mtime
            mtime_del = entry.get("mtime_at_delivery")
            is_new = mtime_del is None or mtime_now > mtime_del
            status = "NEW" if is_new else ("acked" if acked else "delivered")
            delivered_str = delivered[:16] if delivered else "never"
            lines.append(f"  {st}: {status}  (last sent: {delivered_str})")
        return "\n".join(lines)

    # ── /report search QUERY ──
    if subtype == "search":
        query = " ".join(args[1:]).strip() if len(args) > 1 else ""
        if not query:
            return f"{AGENT_TAG} — /report search: provide a query, e.g. /report search nexus asuka"
        result = subprocess.run(
            ["/usr/bin/python3", str(PROJECT_DIR / "tools" / "executional" / "report_archive.py"), "search", query, "--limit", "5"],
            capture_output=True, text=True, cwd=str(PROJECT_DIR)
        )
        out = result.stdout.strip() or "(no results)"
        if len(out) > 3500:
            out = out[:3500] + "\n…[truncated]"
        return f"{AGENT_TAG} — report search: {query!r}\n\n{out}"

    # ── /report [type] — deliver ──
    if subtype not in _REPORT_FILE_MAP:
        return f"{AGENT_TAG} — /report: unknown type '{subtype}'. Use: session, milestone, digest, recap, ack, status, search"

    report_path = REPORTS_DIR / _REPORT_FILE_MAP[subtype]

    tool_map = {
        "session": "tools/executional/session_report.py",
        "milestone": "tools/executional/milestone_report.py",
        "digest": "tools/executional/daily_digest.py",
    }
    if not report_path.exists():
        if subtype == "recap":
            # recap_generator.py takes arguments, handle specially
            result = subprocess.run(
                ["/usr/bin/python3", str(PROJECT_DIR / "tools" / "conversational" / "recap_generator.py"),
                 "generate", "--force"],
                capture_output=True, text=True, cwd=str(PROJECT_DIR)
            )
        else:
            result = subprocess.run(
                ["/usr/bin/python3", str(PROJECT_DIR / tool_map[subtype])],
                capture_output=True, text=True, cwd=str(PROJECT_DIR)
            )
        if result.returncode != 0:
            return f"{AGENT_TAG} — /report: could not generate {subtype} report\n{result.stderr[:200]}"

    if not report_path.exists():
        return f"{AGENT_TAG} — /report: no {subtype} report found and generation failed"

    text = report_path.read_text()
    _mark_delivered(subtype)

    # Truncate for Telegram (4096 char limit)
    MAX = 3800
    if len(text) > MAX:
        text = text[:MAX] + "\n\n…[truncated — full report at state/reports/]"
    return text


def cmd_help():
    return (
        f"{AGENT_TAG} — commands\n"
        "/ping          — alive check\n"
        "/now           — what I'm doing right now (HOT STATE)\n"
        "/status        — current state, goal, last session\n"
        "/session       — last session details\n"
        "/log [N]       — last N sessions (default 5)\n"
        "/goal          — active Loom goal + tasks\n"
        "/analytics     — session stats + costs\n"
        "/who           — identity + relationship state\n"
        "/context       — conversational session context %\n"
        "/reset         — signal conv session to wrap up + restart\n"
        "/new           — clean new conversational session\n"
        "/voice on|off  — toggle Fish Audio TTS\n"
        "/report [session|milestone|digest|recap]  — surface latest report (marks delivered)\n"
        "/report search QUERY               — search report archive (FTS)\n"
        "/report ack [type]                  — acknowledge report as read\n"
        "/report status                      — review state for all reports\n"
        "/control emergency on [min] [reason]  — enable emergency mode\n"
        "/control emergency off                — disable emergency mode\n"
        "/control goal <id>                    — switch active goal"
    )


# ── Dispatch ──────────────────────────────────────────────────────────────────

def dispatch(raw_command):
    parts = raw_command.strip().split()
    if not parts or not parts[0].startswith("/"):
        return f"{AGENT_TAG} — not a command"

    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "/ping":
        return cmd_ping()
    elif cmd == "/now":
        return cmd_now()
    elif cmd == "/status":
        return cmd_status()
    elif cmd == "/session":
        return cmd_session()
    elif cmd == "/log":
        return cmd_log(args[0] if args else 5)
    elif cmd == "/goal":
        return cmd_goal()
    elif cmd == "/analytics":
        return cmd_analytics()
    elif cmd == "/who":
        return cmd_who()
    elif cmd == "/context":
        return cmd_context_conv()
    elif cmd == "/reset":
        return cmd_reset()
    elif cmd == "/new":
        return cmd_new()
    elif cmd == "/voice":
        return cmd_voice(args)
    elif cmd == "/report":
        return cmd_report(args)
    elif cmd == "/control":
        return cmd_control(args)
    elif cmd in ("/help", "/?"):
        return cmd_help()
    else:
        return f"{AGENT_TAG} — unknown command: {cmd}\nTry /help"


def main():
    if len(sys.argv) < 2:
        print("Usage: command_dispatcher.py '/command [args]'", file=sys.stderr)
        sys.exit(1)
    raw = " ".join(sys.argv[1:])
    print(dispatch(raw))


if __name__ == "__main__":
    main()
