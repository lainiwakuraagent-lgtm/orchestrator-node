#!/usr/bin/env python3
"""
test_session_type.py
Task 44 — Integration test: session type flows end to end

Tests the current Phase-4 queue-state-driven resolver (scripts/resolve_session_type.py).

Rewritten 2026-07-23: the previous version tested a "recurring slot" + explicit
per-slot session_type mechanism that resolve_session_type.py no longer implements
at all (superseded by Loom-queue-state-driven resolution). 11 of its 17 checks
failed against current code — it had been silently broken since that rewrite.
This version drives the resolver through --loom-db against a throwaway sqlite
fixture, matching resolve_session_type.py's actual priority order:
  1. SESSION_TYPE env var
  2. WINDOW_TYPE env var (maintenance / reflection / work-or-unset)
  3. Loom queue state (evaluation / planning / audit / execution rules)
  4. inbox/pending.json task_request/bug_report/task_comment -> execution
  5. default -> philosophy (tiered by consecutive_philosophy.count)

Usage:
  python3 tools/test_session_type.py [--project-dir DIR]

Returns exit code 0 on all pass, 1 if any test fails.
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# --- Setup ---
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_PROJECT_DIR = SCRIPT_DIR.parent.parent


def project_dir_from_args() -> Path:
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == "--project-dir" and i < len(sys.argv):
            return Path(sys.argv[i + 1]).resolve()
    return DEFAULT_PROJECT_DIR


PROJECT_DIR = project_dir_from_args()
RESOLVER = PROJECT_DIR / "scripts" / "executional" / "resolve_session_type.py"

PASS = "\033[32mPASS\033[0m"
FAIL = "\033[31mFAIL\033[0m"
failures = []


def check(name: str, condition: bool, detail: str = ""):
    if condition:
        print(f"  {PASS}  {name}")
    else:
        print(f"  {FAIL}  {name}{f' — {detail}' if detail else ''}")
        failures.append(name)


def run_resolver(project_dir: Path, trigger_mode: str, loom_db: Path = None,
                  extra_env: dict = None) -> dict:
    """Run resolve_session_type.py and return parsed JSON output."""
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        out_path = f.name
    env = os.environ.copy()
    env.pop("SESSION_TYPE", None)
    env.pop("WINDOW_TYPE", None)
    if extra_env:
        env.update(extra_env)
    cmd = [
        sys.executable, str(RESOLVER),
        "--project-dir", str(project_dir),
        "--trigger-mode", trigger_mode,
        "--output", out_path,
    ]
    if loom_db is not None:
        cmd += ["--loom-db", str(loom_db)]
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.returncode != 0:
        return {"_error": result.stderr}
    try:
        return json.loads(Path(out_path).read_text())
    except Exception as e:
        return {"_error": str(e)}
    finally:
        Path(out_path).unlink(missing_ok=True)


def make_loom_db(path: Path, goals: list = None, tasks: list = None) -> None:
    """Create a minimal Loom-schema sqlite DB for queue-state tests."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'desire', priority INTEGER DEFAULT 0,
            blocked_reason TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'triage', tags TEXT,
            depends TEXT, blocked_reason TEXT, wait_until TEXT,
            urgency_score REAL DEFAULT 0, priority TEXT DEFAULT 'none'
        )
    """)
    for g in (goals or []):
        conn.execute(
            "INSERT INTO goals (name, status, priority, blocked_reason) VALUES (?,?,?,?)",
            (g.get("name", "goal"), g.get("status", "desire"), g.get("priority", 0),
             g.get("blocked_reason")),
        )
    for t in (tasks or []):
        conn.execute(
            "INSERT INTO tasks (name, status, tags, depends, blocked_reason, wait_until, "
            "urgency_score, priority) VALUES (?,?,?,?,?,?,?,?)",
            (t.get("name", "task"), t.get("status", "triage"), t.get("tags"),
             t.get("depends"), t.get("blocked_reason"), t.get("wait_until"),
             t.get("urgency_score", 0), t.get("priority", "none")),
        )
    conn.commit()
    conn.close()


def make_bare_project(tmp: Path) -> None:
    (tmp / "config" / "session_types").mkdir(parents=True, exist_ok=True)


# ===========================================================================
# Test 1: SESSION_TYPE env var override (always wins, priority 1)
# ===========================================================================
print("\n[1] SESSION_TYPE env var override")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    result = run_resolver(tmp, "nightly", extra_env={"SESSION_TYPE": "philosophy"})
    check("session_type=philosophy", result.get("session_type") == "philosophy")
    check("resolution_source=env_var", result.get("resolution_source") == "env_var")

# ===========================================================================
# Test 2: Truly empty environment -> default is philosophy, not execution
# ===========================================================================
print("\n[2] Default fallback (empty queue, no Loom DB, no inbox)")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    missing_db = tmp / "nonexistent_loom.db"
    result = run_resolver(tmp, "nightly", loom_db=missing_db)
    check("session_type=philosophy (default)", result.get("session_type") == "philosophy",
          f"got {result.get('session_type')!r}")
    check("resolution_source=default", result.get("resolution_source") == "default")

# ===========================================================================
# Test 3: WINDOW_TYPE=maintenance always wins over queue state
# ===========================================================================
print("\n[3] WINDOW_TYPE=maintenance overrides queue state")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    db = tmp / "loom.db"
    make_loom_db(db, tasks=[{"name": "ready task", "status": "scheduled"}])
    result = run_resolver(tmp, "nightly", loom_db=db, extra_env={"WINDOW_TYPE": "maintenance"})
    check("session_type=maintenance", result.get("session_type") == "maintenance",
          f"got {result.get('session_type')!r}")
    check("resolution_source=window_type", result.get("resolution_source") == "window_type")

# ===========================================================================
# Test 4: WINDOW_TYPE=reflection, recent philosophy session -> reflection
# (regression test for the T251-pattern bug fixed 2026-07-23: this used to
#  query loom_sessions.type='philosophy', which never matches since that
#  column holds trigger mode, not session type -- always fell through to
#  'philosophy'. Now reads logs/session_log.csv instead.)
# ===========================================================================
print("\n[4] WINDOW_TYPE=reflection — recent philosophy session (<3 days)")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    (tmp / "logs").mkdir()
    recent_ts = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp / "logs" / "session_log.csv").write_text(
        "timestamp,session_type,duration_minutes,context_pct_at_exit,one_line_summary\n"
        f"{recent_ts},philosophy,15,10,test entry\n"
    )
    result = run_resolver(tmp, "nightly", extra_env={"WINDOW_TYPE": "reflection"})
    check("session_type=reflection", result.get("session_type") == "reflection",
          f"got {result.get('session_type')!r}")

print("\n[5] WINDOW_TYPE=reflection — only stale philosophy session (>3 days)")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    (tmp / "logs").mkdir()
    old_ts = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%SZ")
    (tmp / "logs" / "session_log.csv").write_text(
        "timestamp,session_type,duration_minutes,context_pct_at_exit,one_line_summary\n"
        f"{old_ts},philosophy,15,10,test entry\n"
    )
    result = run_resolver(tmp, "nightly", extra_env={"WINDOW_TYPE": "reflection"})
    check("session_type=philosophy (stale gap)", result.get("session_type") == "philosophy",
          f"got {result.get('session_type')!r}")

# ===========================================================================
# Test 6: Queue-state Rule 1 — desire-status goal not blocked -> evaluation
# ===========================================================================
print("\n[6] Queue-state: unblocked desire-status goal -> evaluation")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    db = tmp / "loom.db"
    make_loom_db(db, goals=[{"name": "new idea", "status": "desire"}])
    result = run_resolver(tmp, "nightly", loom_db=db)
    check("session_type=evaluation", result.get("session_type") == "evaluation",
          f"got {result.get('session_type')!r}")
    check("resolution_source=queue_state", result.get("resolution_source") == "queue_state")

# ===========================================================================
# Test 7: Queue-state Rule 2 — needs_plan task, no deps -> planning
# ===========================================================================
print("\n[7] Queue-state: needs_plan task with no deps -> planning")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    db = tmp / "loom.db"
    make_loom_db(db, tasks=[{"name": "needs a plan", "status": "needs_plan"}])
    result = run_resolver(tmp, "nightly", loom_db=db)
    check("session_type=planning", result.get("session_type") == "planning",
          f"got {result.get('session_type')!r}")

# ===========================================================================
# Test 8: Queue-state Rule 2 — needs_plan task with an UNMET dependency
# is skipped (falls through, since nothing else is actionable, to philosophy)
# ===========================================================================
print("\n[8] Queue-state: needs_plan task with unmet dependency is skipped")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    db = tmp / "loom.db"
    make_loom_db(
        db,
        tasks=[
            {"name": "blocker task", "status": "triage"},  # id=1, not done
            {"name": "needs a plan", "status": "needs_plan", "depends": "[1]"},  # id=2
        ],
    )
    result = run_resolver(tmp, "nightly", loom_db=db)
    check("session_type=philosophy (no actionable plan)", result.get("session_type") == "philosophy",
          f"got {result.get('session_type')!r}")

# ===========================================================================
# Test 9: Queue-state Rule 4 — scheduled, unblocked, wait_until in past -> execution
# ===========================================================================
print("\n[9] Queue-state: ready scheduled task -> execution")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    db = tmp / "loom.db"
    make_loom_db(db, tasks=[{"name": "ready to go", "status": "scheduled"}])
    result = run_resolver(tmp, "nightly", loom_db=db)
    check("session_type=execution", result.get("session_type") == "execution",
          f"got {result.get('session_type')!r}")

# ===========================================================================
# Test 10: Queue-state Rule 4 — scheduled but blocked_reason set -> not counted
# ===========================================================================
print("\n[10] Queue-state: blocked scheduled task is not picked up")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    db = tmp / "loom.db"
    make_loom_db(db, tasks=[{"name": "blocked", "status": "scheduled",
                             "blocked_reason": "waiting on owner"}])
    result = run_resolver(tmp, "nightly", loom_db=db)
    check("session_type=philosophy (nothing actionable)", result.get("session_type") == "philosophy",
          f"got {result.get('session_type')!r}")

# ===========================================================================
# Test 11: Inbox pending task_request forces execution even with empty queue
# ===========================================================================
print("\n[11] Inbox pending task_request -> execution")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    (tmp / "inbox").mkdir()
    (tmp / "inbox" / "pending.json").write_text(json.dumps([
        {"type": "task_request", "content": "do the thing", "processed": False}
    ]))
    missing_db = tmp / "nonexistent_loom.db"
    result = run_resolver(tmp, "nightly", loom_db=missing_db)
    check("session_type=execution", result.get("session_type") == "execution",
          f"got {result.get('session_type')!r}")
    check("resolution_source=inbox_pending", result.get("resolution_source") == "inbox_pending")

# ===========================================================================
# Test 12: Philosophy tier escalation via consecutive_philosophy.count
# ===========================================================================
print("\n[12] Philosophy tier escalation")
for count, expected in [(0, "philosophy"), (1, "creative"),
                         (2, "blocker_resolver"), (3, "philosophy_cap"), (5, "philosophy_cap")]:
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        make_bare_project(tmp)
        (tmp / "state").mkdir()
        (tmp / "state" / "consecutive_philosophy.count").write_text(str(count))
        missing_db = tmp / "nonexistent_loom.db"
        result = run_resolver(tmp, "nightly", loom_db=missing_db)
        check(f"count={count} -> {expected}", result.get("session_type") == expected,
              f"got {result.get('session_type')!r}")

# ===========================================================================
# Test 13: YAML type config loading — correct type config returned
# ===========================================================================
print("\n[13] YAML type config loading")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    (tmp / "config" / "session_types" / "maintenance.yaml").write_text(
        'id: maintenance\nname: "Maintenance Session"\nfocus_hint: "maintain the system"\n'
    )
    result = run_resolver(tmp, "nightly", extra_env={"SESSION_TYPE": "maintenance"})
    check("session_type=maintenance loaded", result.get("session_type") == "maintenance")
    check("focus_hint extracted",
          "maintain" in (result.get("focus_hint") or ""),
          f"got {result.get('focus_hint')!r}")

# ===========================================================================
# Test 14: Context assembly — files assembled correctly
# ===========================================================================
print("\n[14] Context assembly")
with tempfile.TemporaryDirectory() as tmpdir:
    tmp = Path(tmpdir)
    make_bare_project(tmp)
    (tmp / "memory").mkdir()
    (tmp / "memory" / "notes.md").write_text("hello from context")
    (tmp / "config" / "session_types" / "planning.yaml").write_text(
        'id: planning\nname: "Planning Session"\n'
        'context_files:\n  - memory/notes.md\n'
    )
    result = run_resolver(tmp, "nightly", extra_env={"SESSION_TYPE": "planning"})
    check("context assembled",
          "hello from context" in (result.get("assembled_context") or ""),
          f"got assembled_context={result.get('assembled_context', '')[:60]!r}")

# ===========================================================================
# Test 15: analytics_write.py reads CURRENT_SESSION_TYPE env var
# ===========================================================================
print("\n[15] analytics_write.py reads CURRENT_SESSION_TYPE env var")
analytics_script = PROJECT_DIR / "tools" / "analytics_write.py"
if not analytics_script.exists():
    print("  SKIP  analytics_write.py not found in this project")
else:
    source = analytics_script.read_text()
    check("CURRENT_SESSION_TYPE referenced in source",
          "CURRENT_SESSION_TYPE" in source)
    check("maintenance in choices", "maintenance" in source)
    check("philosophy in choices", "philosophy" in source)

# ===========================================================================
# Summary
# ===========================================================================
print(f"\n{'=' * 50}")
if failures:
    print(f"RESULT: {len(failures)} test(s) FAILED — {', '.join(failures)}")
    sys.exit(1)
else:
    print(f"RESULT: All checks passed (҂◡_◡)")
    sys.exit(0)
