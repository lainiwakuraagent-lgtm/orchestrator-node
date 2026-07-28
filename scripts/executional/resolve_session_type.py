#!/usr/bin/env python3
"""
resolve_session_type.py
Phase 4 — Queue-State-Driven Session Type Dispatcher (v2 schedule)

Resolves the session type and assembles type-specific context (prompt injection
+ preloaded context files) for wake.sh to use.

Priority order:
  1. SESSION_TYPE env var (explicit override — always wins)
  2. WINDOW_TYPE env var (lane constraint from wake.sh):
     - work:        full queue-state logic (below)
     - maintenance: always return "maintenance"
     - reflection:  always return "reflection" (or "philosophy")
  3. Loom queue state (DB-driven algorithmic selection) — used when
     WINDOW_TYPE is "work" or unset (emergency/manual mode)
  4. default: "philosophy" (empty queue → identity/relationship session)

Queue-state rules (priority 3, within work windows):
  - desire-status goals/projects, not blocked -> evaluation
  - needs_plan-status goals/projects/tasks    -> planning
  - review-status goals, or scheduled/        -> audit
    in_progress tasks tagged milestone_review
    with all deps done
  - scheduled tasks ready to execute          -> execution
  - nothing actionable (empty queue)          -> philosophy

  Each rule widened from task-only to also cover goals and projects sharing
  the same status (Loom's Goal/Project/Task schema shares one status
  vocabulary). No new tiers, no reordering — the four-rule shape is
  unchanged. When a rule matches at multiple levels simultaneously, goal
  beats project beats task. Rule 4 (execution) stays task-only; there's no
  goal/project equivalent of "ready to execute."

Usage:
  python3 scripts/resolve_session_type.py \\
    --project-dir /path/to/agent_project \\
    --trigger-mode nightly \\
    --output /tmp/session_type_result.json

Output JSON:
  session_type:        resolved type id
  resolution_source:   env_var | queue_state | default
  queue_state_reason:  human-readable reason when source is queue_state (or "")
  target_level:        "goal" | "project" | "task" | null — which level the
                        matched dispatch rule actually fired on (queue_state only)
  target_id:            id of that specific matched record, or null
  target_context:       full detail of the matched goal/project record, formatted
                        for prompt injection (or "" for task-level/no match — task
                        detail is already covered by state/loom_context.json's
                        current_task/current_task_lineage, which reflects the
                        *active* goal/project, not necessarily whatever this rule
                        matched, since rules 1-3 are system-wide, not scoped to
                        the active goal)
  prompt_content:      contents of the type's prompt_file (or "")
  assembled_context:   concatenated context_files (or "")
  focus_hint:          type's focus_hint text (or "")
  behavioral_overrides: dict from type YAML
  memory_discipline:   strict | normal (from behavioral_overrides)
"""

import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

LOOM_DB_PATH = (Path(os.environ["LOOM_DB"]) if "LOOM_DB" in os.environ
                else Path.home() / ".local" / "share" / "loom" / "loom.db")


def parse_args():
    p = argparse.ArgumentParser(description="Resolve session type for wake.sh")
    p.add_argument("--project-dir", required=True, help="Agent project root directory")
    p.add_argument("--trigger-mode", default="nightly",
                   choices=["nightly", "emergency", "manual"],
                   help="Current trigger mode")
    p.add_argument("--output", required=True, help="Output JSON file path")
    p.add_argument("--loom-db", default=None,
                   help="Override Loom DB path (default: ~/.local/share/loom/loom.db)")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Queue-state resolution from Loom DB
# ---------------------------------------------------------------------------

def resolve_from_queue_state(db_path: Path) -> tuple:
    """
    Query the Loom DB and return (session_type, reason, target) based on queue state.
    target is {"level": "goal"|"project"|"task", "id": <int>} or None.
    Returns (None, None, None) if no queue-state rule matches (fall through).
    """
    if not db_path.exists():
        return None, None, None

    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return None, None, None

    try:
        result = _check_queue_state(conn)
        return result
    finally:
        conn.close()


def _check_queue_state(conn: sqlite3.Connection) -> tuple:
    """Run queue-state checks in priority order. Returns (type, reason, target) or (None, None, None).

    target identifies the specific record a rule matched — needed because
    rules 1-3 are system-wide (not scoped to whichever goal is active), so
    the matched goal/project may not be the one state/loom_context.json's
    active_goal/active_project already describe.
    """

    # Rule 1: desire-status goals/projects, not blocked -> evaluation (goal-first)
    try:
        rows = conn.execute(
            "SELECT id, name FROM goals "
            "WHERE status = 'desire' "
            "AND (blocked_reason IS NULL OR blocked_reason = '') "
            "ORDER BY priority DESC, id ASC "
            "LIMIT 5"
        ).fetchall()
        if rows:
            names = ", ".join(r["name"] for r in rows[:3])
            return ("evaluation", f"{len(rows)} desire-status goal(s) need evaluation: {names}",
                    {"level": "goal", "id": rows[0]["id"]})
    except sqlite3.Error:
        pass

    try:
        rows = conn.execute(
            "SELECT id, name FROM projects "
            "WHERE status = 'desire' "
            "AND (blocked_reason IS NULL OR blocked_reason = '') "
            "ORDER BY priority DESC, id ASC "
            "LIMIT 5"
        ).fetchall()
        if rows:
            names = ", ".join(r["name"] for r in rows[:3])
            return ("evaluation", f"{len(rows)} desire-status project(s) need evaluation: {names}",
                    {"level": "project", "id": rows[0]["id"]})
    except sqlite3.Error:
        pass

    # Rule 2: needs_plan-status goals/projects/tasks -> planning (goal-first, then project, then task)
    try:
        rows = conn.execute(
            "SELECT id, name FROM goals WHERE status = 'needs_plan' "
            "ORDER BY priority DESC, id ASC LIMIT 5"
        ).fetchall()
        if rows:
            names = ", ".join(r["name"] for r in rows[:3])
            return ("planning", f"{len(rows)} needs_plan-status goal(s) need planning: {names}",
                    {"level": "goal", "id": rows[0]["id"]})
    except sqlite3.Error:
        pass

    try:
        rows = conn.execute(
            "SELECT id, name FROM projects WHERE status = 'needs_plan' "
            "ORDER BY priority DESC, id ASC LIMIT 5"
        ).fetchall()
        if rows:
            names = ", ".join(r["name"] for r in rows[:3])
            return ("planning", f"{len(rows)} needs_plan-status project(s) need planning: {names}",
                    {"level": "project", "id": rows[0]["id"]})
    except sqlite3.Error:
        pass

    # needs_plan-status tasks with all deps done -> planning.
    # Tasks with unmet deps cannot be planned yet — skip them to avoid wasted planning sessions.
    # (Goals/projects have no `depends` field, so this gating is task-only.)
    try:
        rows = conn.execute(
            "SELECT id, name, depends FROM tasks "
            "WHERE status = 'needs_plan' "
            "ORDER BY priority DESC, id ASC "
            "LIMIT 20"
        ).fetchall()
        actionable = []
        for row in rows:
            deps_raw = row["depends"]
            if deps_raw:
                # depends column can be JSON array, bare int, or comma-separated ints
                try:
                    dep_ids = json.loads(deps_raw)
                    if isinstance(dep_ids, int):
                        dep_ids = [dep_ids]
                except (json.JSONDecodeError, TypeError):
                    try:
                        dep_ids = [int(x) for x in str(deps_raw).split(",") if x.strip()]
                    except ValueError:
                        dep_ids = []
                if dep_ids:
                    placeholders = ",".join("?" for _ in dep_ids)
                    undone = conn.execute(
                        f"SELECT COUNT(*) FROM tasks "
                        f"WHERE id IN ({placeholders}) AND status != 'done'",
                        dep_ids,
                    ).fetchone()[0]
                    if undone > 0:
                        continue  # deps not done yet — cannot plan this task
            actionable.append(row)
        if actionable:
            names = ", ".join(r["name"] for r in actionable[:3])
            return ("planning", f"{len(actionable)} task(s) need planning: {names}",
                    {"level": "task", "id": actionable[0]["id"]})
    except sqlite3.Error:
        pass

    # Rule 3: review-status goals -> audit (goal-level, checked before task milestone_review)
    try:
        rows = conn.execute(
            "SELECT id, name FROM goals WHERE status = 'review' "
            "ORDER BY priority DESC, id ASC LIMIT 5"
        ).fetchall()
        if rows:
            names = ", ".join(r["name"] for r in rows[:3])
            return ("audit", f"{len(rows)} review-status goal(s) ready for audit: {names}",
                    {"level": "goal", "id": rows[0]["id"]})
    except sqlite3.Error:
        pass

    # scheduled/in_progress tasks tagged milestone_review with all deps done -> audit
    try:
        rows = conn.execute(
            "SELECT id, name, depends FROM tasks "
            "WHERE status IN ('scheduled', 'in_progress') "
            "AND tags LIKE '%milestone_review%' "
            "ORDER BY priority DESC, id ASC "
            "LIMIT 10"
        ).fetchall()
        audit_candidates = []
        for row in rows:
            deps_raw = row["depends"]
            if deps_raw:
                try:
                    dep_ids = json.loads(deps_raw)
                    if isinstance(dep_ids, int):
                        dep_ids = [dep_ids]
                except (json.JSONDecodeError, TypeError):
                    dep_ids = []
                if dep_ids:
                    placeholders = ",".join("?" for _ in dep_ids)
                    undone = conn.execute(
                        f"SELECT COUNT(*) FROM tasks "
                        f"WHERE id IN ({placeholders}) AND status != 'done'",
                        dep_ids
                    ).fetchone()[0]
                    if undone > 0:
                        continue  # deps not all done, skip
            audit_candidates.append(row)

        if audit_candidates:
            names = ", ".join(r["name"] for r in audit_candidates[:3])
            return ("audit", f"{len(audit_candidates)} milestone_review task(s) ready for audit: {names}",
                    {"level": "task", "id": audit_candidates[0]["id"]})
    except sqlite3.Error:
        pass

    # Rule 4: scheduled tasks ready to execute (not blocked, wait_until not in future,
    # all deps done) -> execution. Fetch candidates in SQL, dep-check in Python to
    # handle both JSON-array ("depends":"[273,274]") and comma-separated ("depends":"273,274")
    # formats without relying on json_each() SQL extension.
    try:
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        candidates = conn.execute(
            "SELECT id, name, depends, project_id, goal_id FROM tasks "
            "WHERE status = 'scheduled' "
            "AND (blocked_reason IS NULL OR blocked_reason = '') "
            "AND (wait_until IS NULL OR wait_until <= ?) "
            "ORDER BY urgency_score DESC "
            "LIMIT 20",
            (now_iso,)
        ).fetchall()
        ready = []
        for row in candidates:
            deps_raw = row["depends"]
            if deps_raw:
                try:
                    dep_ids = json.loads(deps_raw)
                except (json.JSONDecodeError, TypeError):
                    try:
                        dep_ids = [int(x) for x in str(deps_raw).split(",") if x.strip()]
                    except ValueError:
                        dep_ids = []
                if dep_ids:
                    placeholders = ",".join("?" for _ in dep_ids)
                    undone = conn.execute(
                        f"SELECT COUNT(*) FROM tasks "
                        f"WHERE id IN ({placeholders}) AND status != 'done'",
                        dep_ids,
                    ).fetchone()[0]
                    if undone > 0:
                        continue
            ready.append(row)

        # A1: scope filter — limit to same project_id bucket as the top task.
        # If top task has project_id set: keep only matching project_id.
        # If top task has project_id=None: keep only project_id IS NULL under the same goal_id.
        if len(ready) > 1:
            top = ready[0]
            top_project = top["project_id"]
            top_goal = top["goal_id"]
            if top_project is not None:
                ready = [r for r in ready if r["project_id"] == top_project]
            else:
                ready = [r for r in ready if r["project_id"] is None and r["goal_id"] == top_goal]

        # A2: count cap — expose at most EXECUTION_TASK_CAP tasks per session.
        task_cap = int(os.environ.get("EXECUTION_TASK_CAP", "2"))
        ready = ready[:task_cap]

        if ready:
            names = ", ".join(r["name"] for r in ready)
            return ("execution", f"{len(ready)} scheduled task(s) ready: {names}",
                    {"level": "task", "id": ready[0]["id"]})
    except sqlite3.Error:
        pass

    # No queue-state rule matched — fall through (empty queue → philosophy)
    return None, None, None


# ---------------------------------------------------------------------------
# Combined resolution: env_var > queue_state > default
# ---------------------------------------------------------------------------

def resolve_type(project_dir: Path, trigger_mode: str, db_path: Path) -> tuple:
    """
    Returns (session_type, resolution_source, queue_state_reason, target).
    target is {"level": "goal"|"project"|"task", "id": <int>} or None.
    """

    # Priority 1: SESSION_TYPE env var (explicit override — always wins)
    env_type = os.environ.get("SESSION_TYPE", "").strip()
    if env_type:
        return env_type, "env_var", "", None

    # Priority 2: WINDOW_TYPE env var (lane constraint from wake.sh)
    window_type = os.environ.get("WINDOW_TYPE", "").strip().lower()
    if window_type == "maintenance":
        return "maintenance", "window_type", "", None
    if window_type == "reflection":
        # Reflection windows can also pick philosophy
        reflection_type = _pick_reflection_type(project_dir)
        return reflection_type, "window_type", "", None
    # window_type == "work" or unset: fall through to queue-state logic

    # Priority 2.5: Philosophy cap gate — must run BEFORE queue-state check.
    # See agent_project's resolve_session_type.py for detailed rationale.
    # Short version: philosophy_blocker-created tasks must not immediately
    # trigger execution and reset the consecutive counter, bypassing the cap.
    default_goal_id = _read_default_goal_id(project_dir)
    _cap_target = {"level": "goal", "id": default_goal_id} if default_goal_id else None
    consec = _read_consecutive_philosophy_count(project_dir)
    if consec >= 3:
        return "philosophy_cap", "default", f"consecutive_philosophy_count={consec} >= 3, cap reached", _cap_target

    # Priority 3: Queue state from Loom DB
    queue_type, queue_reason, target = resolve_from_queue_state(db_path)
    if queue_type:
        return queue_type, "queue_state", queue_reason, target

    # Priority 3b: Inbox has unprocessed task_requests/bug_reports/task_comments → execution needed
    # This handles the case where inbox.py startup hasn't run yet (it runs inside the
    # session, after session type is resolved). If inbox has actionable work, force
    # execution so inbox.py startup can convert entries to Loom tasks and work them.
    if _inbox_has_pending_tasks(project_dir):
        return "execution", "inbox_pending", \
            "inbox/pending.json has unprocessed task_request/bug_report/task_comment entries", None

    # Priority 4: default — nothing eligible anywhere (no goal, project, or task
    # matched any rule above) means philosophy session. Select sub-mode based on
    # consecutive philosophy session count.
    #
    # If a default goal is configured for this node (DEFAULT_GOAL_ID in
    # state/agent_config.env), attach it as target so main()'s existing
    # target_context machinery (built for rules 1-3) picks it up for free and
    # injects its content as framing — no new session type, no new injection
    # path. Looked up by id directly, not gated by status: the default goal is
    # a carve-out from the normal Goal status table (never evaluated, planned,
    # or audited), so it must resolve here even if its status is desire or
    # anything else. philosophy_cap aborts before consuming target_context
    # anyway, so attaching it there too is harmless.
    target = _cap_target  # reuse — already computed above

    # consec already read above; consec >= 3 case already returned.
    if consec == 2:
        return "blocker_resolver", "default", f"consecutive_philosophy_count={consec}, blocker review mode", target
    if consec == 1:
        return "creative", "default", f"consecutive_philosophy_count={consec}, creative mode", target
    return "philosophy", "default", "", target


def _read_default_goal_id(project_dir: Path) -> Optional[int]:
    """Read DEFAULT_GOAL_ID from state/agent_config.env. None if unset/unreadable.

    This is the per-node fallback goal invoked only when nothing else is
    eligible (no goal/project/task matched any dispatch rule) — the Loom-native
    replacement for the old default_goal.txt file. Content and identity of
    that goal is a per-node decision, not something this script decides.
    """
    config_path = project_dir / "state" / "agent_config.env"
    if not config_path.exists():
        return None
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            if key.strip() == "DEFAULT_GOAL_ID":
                value = value.strip().strip('"').strip("'")
                return int(value) if value else None
    except (OSError, ValueError):
        return None
    return None


def _read_consecutive_philosophy_count(project_dir: Path) -> int:
    """Read state/consecutive_philosophy.count. Returns 0 if missing or unreadable."""
    count_file = project_dir / "state" / "consecutive_philosophy.count"
    try:
        return max(0, int(count_file.read_text(encoding="utf-8").strip()))
    except (FileNotFoundError, ValueError, OSError):
        return 0


def _inbox_has_pending_tasks(project_dir: Path) -> bool:
    """
    Return True if inbox/pending.json contains unprocessed entries that require
    an execution session to handle: task_request, bug_report, or task_comment.

    task_comment entries carry owner feedback on existing Loom tasks. Without this
    check, an empty Loom queue causes philosophy sessions to loop while task_comments
    sit unread in the inbox.
    """
    inbox_path = project_dir / "inbox" / "pending.json"
    if not inbox_path.exists():
        return False
    try:
        data = json.loads(inbox_path.read_text(encoding="utf-8"))
        entries = data if isinstance(data, list) else data.get("entries", [])
        for e in entries:
            if e.get("processed", False):
                continue
            if e.get("type") in ("task_request", "bug_report", "task_comment"):
                return True
        return False
    except Exception:
        return False


def _pick_reflection_type(project_dir: Path) -> str:
    """
    For reflection windows, decide between 'reflection' and 'philosophy'.
    Uses a simple heuristic: if there is a recent philosophy session (last 3 days),
    return 'reflection'; otherwise pick philosophy this time.
    Falls back to 'reflection' on any error.

    Reads logs/session_log.csv rather than the Loom DB's loom_sessions.type column.
    That column holds the trigger mode (nightly/emergency/manual), not the session
    type — the same mismatch T251 fixed for the philosophy_gap trigger. Querying
    it for type='philosophy' here would always return zero rows, making this
    heuristic always resolve to 'philosophy' and never 'reflection'.
    """
    session_log = project_dir / "logs" / "session_log.csv"
    if not session_log.exists():
        return "reflection"

    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=3)
        with open(session_log, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("session_type") != "philosophy":
                    continue
                ts_raw = row.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_raw)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts > cutoff:
                        return "reflection"
                except ValueError:
                    continue
        # No recent philosophy — pick philosophy this time
        return "philosophy"
    except Exception:
        return "reflection"


# ---------------------------------------------------------------------------
# YAML loader + config assembly
# ---------------------------------------------------------------------------

def load_yaml_simple(path: Path) -> dict:
    """
    Minimal YAML loader for the simple key-value + list structure used in
    session type configs. Handles: str values, block scalars (>), lists (- items),
    and nested dicts (2-space indent). Does not handle anchors, multi-doc, etc.
    Falls back to {} on parse error.
    """
    try:
        import yaml
        with open(path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except ImportError:
        pass
    except Exception:
        return {}

    # Fallback: hand-rolled minimal parser
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        result = {}
        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.rstrip()
            if not stripped or stripped.lstrip().startswith("#"):
                i += 1
                continue
            if ":" in stripped and not stripped.startswith(" "):
                key, _, rest = stripped.partition(":")
                key = key.strip()
                rest = rest.strip()
                if rest in (">", "|", ""):
                    j = i + 1
                    sub_lines = []
                    while j < len(lines):
                        sub = lines[j]
                        if not sub.strip() and sub_lines:
                            sub_lines.append("")
                            j += 1
                            continue
                        if sub and not sub[0].isspace():
                            break
                        sub_lines.append(sub.strip())
                        j += 1
                    if sub_lines and sub_lines[0].startswith("- "):
                        result[key] = [s[2:].strip() for s in sub_lines if s.startswith("- ")]
                    elif sub_lines and sub_lines[0].startswith("#"):
                        result[key] = {}
                    elif sub_lines:
                        result[key] = " ".join(s for s in sub_lines if s)
                    i = j
                elif rest.startswith("["):
                    result[key] = []
                    i += 1
                else:
                    result[key] = rest.strip('"').strip("'")
                    i += 1
            else:
                i += 1
        return result
    except Exception:
        return {}


def load_type_config(project_dir: Path, session_type: str, target: Optional[dict] = None) -> dict:
    """Load and return the session type YAML config dict.

    target ({"level": ..., "id": ...}, from resolve_type()) selects which
    scope variant to merge in, where applicable.
    """
    type_file = project_dir / "config" / "session_types" / f"{session_type}.yaml"
    if not type_file.exists():
        return {}
    config = load_yaml_simple(type_file)

    # Scope rotation for maintenance sessions
    if session_type == "maintenance" and config.get("scope_rotation"):
        scope_state_file = project_dir / config["scope_state_file"]
        try:
            idx = int(scope_state_file.read_text(encoding="utf-8").strip())
        except (FileNotFoundError, ValueError, OSError):
            idx = 0

        scope_num = idx + 1  # 1-indexed for YAML file naming
        scope_file = project_dir / "config" / "session_types" / f"maintenance_scope{scope_num}.yaml"
        scope_config = load_yaml_simple(scope_file)

        # Merge: scope overrides base context and focus_hint
        config["context_files"] = scope_config.get("context_files", config.get("context_files", []))
        config["focus_hint"] = scope_config.get("focus_hint", config.get("focus_hint", ""))
        config["scope_id"] = scope_num
        config["scope_name"] = scope_config.get("scope_name", f"Scope {scope_num}")
        config["scope_slug"] = scope_config.get("scope_slug", f"scope{scope_num}")

        # Rotate for next maintenance session
        next_idx = (idx + 1) % 3
        try:
            scope_state_file.write_text(str(next_idx), encoding="utf-8")
        except OSError:
            pass

    # Planning has goal-scoped ("decompose into projects") and project-scoped
    # (≈ the original planning content) variants, selected by which level
    # resolve_type()'s dispatch rule actually matched — a direct selection,
    # not a rotation. Falls back to plain planning.yaml when target is
    # task-level or unset, matching planning's original behavior.
    if session_type == "planning" and target and target.get("level") in ("goal", "project"):
        scope_file = project_dir / "config" / "session_types" / f"planning_{target['level']}_scope.yaml"
        scope_config = load_yaml_simple(scope_file)
        if scope_config:
            config["context_files"] = scope_config.get("context_files", config.get("context_files", []))
            config["focus_hint"] = scope_config.get("focus_hint", config.get("focus_hint", ""))
            if scope_config.get("prompt_file"):
                config["prompt_file"] = scope_config["prompt_file"]

    return config


def _fetch_target_context(db_path: Path, target: Optional[dict]) -> str:
    """Fetch and format the full detail of whatever record resolve_type()'s dispatch
    rule actually matched (a goal or project awaiting evaluation/planning/audit).

    Task-level matches (and no match) return "" — task detail is already
    covered by state/loom_context.json's current_task/current_task_lineage,
    which loom's context.py resolves separately. That reflects the *active*
    goal/project; this function exists because rules 1-3 are system-wide, not
    scoped to the active goal, so the matched record may be a different one.
    """
    if not target or target.get("level") not in ("goal", "project"):
        return ""
    if not db_path.exists():
        return ""
    try:
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return ""
    try:
        table = "goals" if target["level"] == "goal" else "projects"
        row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (target["id"],)).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        conn.close()
    if row is None:
        return ""
    d = dict(row)
    lines = [f"{target['level'].capitalize()} {d.get('id')}: {d.get('name')}"]
    for key in ("status", "priority", "description", "blocked_reason", "blocked_note"):
        val = d.get(key)
        if val:
            lines.append(f"  {key}: {val}")
    return "\n".join(lines)


def load_agent_identity(project_dir: Path) -> dict:
    """
    Read AGENT_NAME/OWNER_NAME from state/agent_config.env, falling back to
    the same defaults wake.sh uses when the file is absent. Used to substitute
    ${AGENT_NAME}/${OWNER_NAME} tokens in context_files paths and prompt
    content, so a differently-named clone doesn't silently lose relationship
    context (identity paths used to be hardcoded to lain/andrii everywhere).
    """
    identity = {"AGENT_NAME": "UNCONFIGURED_AGENT", "OWNER_NAME": "UNCONFIGURED_OWNER"}
    config_path = project_dir / "state" / "agent_config.env"
    if not config_path.exists():
        import sys
        print(f"WARNING: {config_path} missing — AGENT_NAME/OWNER_NAME unset. Copy state/agent_config.env.example.", file=sys.stderr)
        return identity
    try:
        for line in config_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in identity and value:
                identity[key] = value
    except OSError:
        pass
    return identity


def _substitute_identity(text: str, identity: dict) -> str:
    for key, value in identity.items():
        text = text.replace("${" + key + "}", value)
    return text


def assemble_context(project_dir: Path, context_files: list, identity: dict = None) -> str:
    """
    Read context files and concatenate them with headers.
    ${AGENT_NAME}/${OWNER_NAME} tokens in a path are substituted before
    resolution. Files that don't exist (after substitution) are skipped,
    with a warning to stderr -- silent skipping previously made a missing
    identity/relationship file indistinguishable from "nothing to preload."
    """
    if identity is None:
        identity = load_agent_identity(project_dir)

    parts = []
    for rel_path in context_files:
        if not isinstance(rel_path, str):
            continue
        resolved_rel_path = _substitute_identity(rel_path, identity)
        abs_path = project_dir / resolved_rel_path
        if not abs_path.exists():
            print(f"WARNING: context file not found, skipping: {resolved_rel_path}", file=sys.stderr)
            continue
        try:
            content = abs_path.read_text(encoding="utf-8").strip()
            if content:
                parts.append(f"### {resolved_rel_path}\n\n{content}")
        except OSError:
            continue

    if not parts:
        return ""

    return "\n\n---\n\n".join(parts)


def load_prompt_content(project_dir: Path, prompt_file: str, identity: dict = None) -> str:
    """Load the type-specific prompt file contents, substituting identity tokens."""
    if not prompt_file:
        return ""
    prompt_path = project_dir / prompt_file
    if not prompt_path.exists():
        return ""
    if identity is None:
        identity = load_agent_identity(project_dir)
    try:
        content = prompt_path.read_text(encoding="utf-8").strip()
        return _substitute_identity(content, identity)
    except OSError:
        return ""


def main():
    args = parse_args()
    project_dir = Path(args.project_dir).resolve()
    db_path = Path(args.loom_db) if args.loom_db else LOOM_DB_PATH

    session_type, resolution_source, queue_reason, target = resolve_type(
        project_dir, args.trigger_mode, db_path
    )
    config = load_type_config(project_dir, session_type, target)
    identity = load_agent_identity(project_dir)

    context_files = config.get("context_files") or []
    if not isinstance(context_files, list):
        context_files = []

    assembled_context = assemble_context(project_dir, context_files, identity)
    target_context = _fetch_target_context(db_path, target)

    prompt_file = config.get("prompt_file") or ""
    if not isinstance(prompt_file, str):
        prompt_file = ""
    prompt_content = load_prompt_content(project_dir, prompt_file, identity)

    behavioral_overrides = config.get("behavioral_overrides") or {}
    if not isinstance(behavioral_overrides, dict):
        behavioral_overrides = {}

    result = {
        "session_type": session_type,
        "resolution_source": resolution_source,
        "queue_state_reason": queue_reason,
        "target_level": target.get("level") if target else None,
        "target_id": target.get("id") if target else None,
        "target_context": target_context,
        "prompt_content": prompt_content,
        "assembled_context": assembled_context,
        "focus_hint": (config.get("focus_hint") or "").strip(),
        "behavioral_overrides": behavioral_overrides,
        "memory_discipline": behavioral_overrides.get("memory_discipline", "normal"),
        "scope_id": config.get("scope_id"),
        "scope_name": config.get("scope_name"),
        "scope_slug": config.get("scope_slug"),
        "consecutive_philosophy_count": _read_consecutive_philosophy_count(project_dir),
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # Print summary to stderr for wake.log capture
    scope_suffix = f" scope={result['scope_id']}({result['scope_name']})" if result.get("scope_id") else ""
    target_suffix = f" target={target['level']}:{target['id']}" if target else ""
    print(
        f"session_type={session_type} source={resolution_source} "
        f"queue_reason={queue_reason!r} "
        f"prompt={'yes' if prompt_content else 'no'} "
        f"context_files={len(context_files)} "
        f"memory_discipline={result['memory_discipline']}"
        f"{scope_suffix}{target_suffix}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
