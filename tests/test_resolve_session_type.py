#!/usr/bin/env python3
"""
Unit tests for resolve_session_type.py inbox trigger logic and the
goal/project/task dispatch rules in _check_queue_state().

Run: python3 tests/test_resolve_session_type.py
"""

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

# Add scripts/ to path so we can import the module directly
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from resolve_session_type import (  # noqa: E402
    _check_queue_state,
    _fetch_target_context,
    _inbox_has_pending_tasks,
    _read_default_goal_id,
    resolve_type,
)


def _make_scratch_db(db_path: Path) -> sqlite3.Connection:
    """Minimal goals/projects/tasks schema covering exactly the columns
    _check_queue_state()'s queries reference. Self-contained (no dependency
    on the loom package), matching this script's own self-contained style.
    """
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE goals (
            id INTEGER PRIMARY KEY,
            name TEXT, status TEXT, priority INTEGER DEFAULT 0,
            blocked_reason TEXT, description TEXT
        );
        CREATE TABLE projects (
            id INTEGER PRIMARY KEY,
            name TEXT, status TEXT, priority INTEGER DEFAULT 0,
            blocked_reason TEXT, description TEXT, goal_id INTEGER
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY,
            name TEXT, status TEXT, depends TEXT, tags TEXT,
            goal_id INTEGER, project_id INTEGER,
            blocked_reason TEXT, wait_until TEXT, urgency_score REAL DEFAULT 0,
            priority TEXT DEFAULT 'none'
        );
        """
    )
    conn.commit()
    return conn


class TestCheckQueueStateWidenedRules(unittest.TestCase):
    """Rules 1-3 widened to cover goals/projects alongside tasks, with
    goal-before-project-before-task ordering when a rule matches at
    multiple levels simultaneously.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "scratch.db"
        self.conn = _make_scratch_db(self.db_path)

    def tearDown(self):
        self.conn.close()
        self.tmpdir.cleanup()

    def test_rule1_goal_desire_beats_project_desire(self):
        self.conn.execute("INSERT INTO goals (id, name, status, priority) VALUES (1, 'G', 'desire', 1)")
        self.conn.execute("INSERT INTO projects (id, name, status, priority) VALUES (1, 'P', 'desire', 99)")
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertEqual(session_type, "evaluation")
        self.assertEqual(target, {"level": "goal", "id": 1})

    def test_rule1_project_desire_matches_when_no_goal_desire(self):
        self.conn.execute("INSERT INTO projects (id, name, status, priority) VALUES (1, 'P', 'desire', 1)")
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertEqual(session_type, "evaluation")
        self.assertEqual(target, {"level": "project", "id": 1})

    def test_rule1_blocked_goal_desire_skipped(self):
        self.conn.execute(
            "INSERT INTO goals (id, name, status, priority, blocked_reason) "
            "VALUES (1, 'G', 'desire', 1, 'blocked_owner')"
        )
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertNotEqual(session_type, "evaluation")

    def test_rule2_goal_needs_plan_beats_project_and_task(self):
        self.conn.execute("INSERT INTO goals (id, name, status, priority) VALUES (1, 'G', 'needs_plan', 1)")
        self.conn.execute("INSERT INTO projects (id, name, status, priority) VALUES (1, 'P', 'needs_plan', 1)")
        self.conn.execute("INSERT INTO tasks (id, name, status) VALUES (1, 'T', 'needs_plan')")
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertEqual(session_type, "planning")
        self.assertEqual(target, {"level": "goal", "id": 1})

    def test_rule2_task_needs_plan_still_respects_dep_gating(self):
        """Pre-existing behavior preserved: a task with an undone dependency is skipped."""
        self.conn.execute("INSERT INTO tasks (id, name, status) VALUES (1, 'Blocker', 'in_progress')")
        self.conn.execute(
            "INSERT INTO tasks (id, name, status, depends) VALUES (2, 'Blocked', 'needs_plan', '[1]')"
        )
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertNotEqual(session_type, "planning")

    def test_rule3_goal_review_beats_task_milestone_review(self):
        self.conn.execute("INSERT INTO goals (id, name, status, priority) VALUES (1, 'G', 'review', 1)")
        self.conn.execute(
            "INSERT INTO tasks (id, name, status, tags) VALUES (1, 'T', 'scheduled', 'milestone_review')"
        )
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertEqual(session_type, "audit")
        self.assertEqual(target, {"level": "goal", "id": 1})

    def test_rule4_execution_stays_task_only(self):
        """A goal/project sitting in scheduled/in_progress must not trigger
        anything by itself — Rule 4 has no goal/project equivalent."""
        self.conn.execute("INSERT INTO goals (id, name, status, priority) VALUES (1, 'G', 'scheduled', 1)")
        self.conn.execute(
            "INSERT INTO tasks (id, name, status) VALUES (1, 'T', 'scheduled')"
        )
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertEqual(session_type, "execution")
        self.assertEqual(target, {"level": "task", "id": 1})

    def test_priority_tiebreak_lower_id_wins_within_a_level(self):
        self.conn.execute("INSERT INTO goals (id, name, status, priority) VALUES (2, 'Second', 'desire', 5)")
        self.conn.execute("INSERT INTO goals (id, name, status, priority) VALUES (1, 'First', 'desire', 5)")
        self.conn.commit()
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertEqual(target, {"level": "goal", "id": 1})

    def test_empty_db_falls_through(self):
        session_type, reason, target = _check_queue_state(self.conn)
        self.assertIsNone(session_type)
        self.assertIsNone(target)


class TestFetchTargetContext(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmpdir.name) / "scratch.db"
        conn = _make_scratch_db(self.db_path)
        conn.execute(
            "INSERT INTO goals (id, name, status, priority, description) "
            "VALUES (1, 'Goal A', 'desire', 3, 'Why this matters')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_goal_target_returns_formatted_detail(self):
        text = _fetch_target_context(self.db_path, {"level": "goal", "id": 1})
        self.assertIn("Goal A", text)
        self.assertIn("Why this matters", text)

    def test_task_level_target_returns_empty(self):
        text = _fetch_target_context(self.db_path, {"level": "task", "id": 1})
        self.assertEqual(text, "")

    def test_no_target_returns_empty(self):
        self.assertEqual(_fetch_target_context(self.db_path, None), "")

    def test_unknown_id_returns_empty(self):
        text = _fetch_target_context(self.db_path, {"level": "goal", "id": 999})
        self.assertEqual(text, "")


class TestReadDefaultGoalId(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.tmpdir.name)
        (self.project_dir / "state").mkdir(parents=True)
        self.config_path = self.project_dir / "state" / "agent_config.env"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_no_config_file_returns_none(self):
        self.assertIsNone(_read_default_goal_id(self.project_dir))

    def test_unset_returns_none(self):
        self.config_path.write_text("AGENT_NAME=lain\n", encoding="utf-8")
        self.assertIsNone(_read_default_goal_id(self.project_dir))

    def test_reads_configured_value(self):
        self.config_path.write_text("AGENT_NAME=lain\nDEFAULT_GOAL_ID=7\n", encoding="utf-8")
        self.assertEqual(_read_default_goal_id(self.project_dir), 7)

    def test_ignores_commented_line(self):
        self.config_path.write_text("# DEFAULT_GOAL_ID=7\n", encoding="utf-8")
        self.assertIsNone(_read_default_goal_id(self.project_dir))

    def test_empty_value_returns_none(self):
        self.config_path.write_text("DEFAULT_GOAL_ID=\n", encoding="utf-8")
        self.assertIsNone(_read_default_goal_id(self.project_dir))

    def test_non_integer_value_returns_none(self):
        self.config_path.write_text("DEFAULT_GOAL_ID=notanumber\n", encoding="utf-8")
        self.assertIsNone(_read_default_goal_id(self.project_dir))


class TestResolveTypeDefaultGoalFallback(unittest.TestCase):
    """Priority 4 (empty queue) attaches the configured default goal as target,
    on top of the existing philosophy sub-mode selection — no new session type.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.tmpdir.name)
        (self.project_dir / "state").mkdir(parents=True)
        (self.project_dir / "inbox").mkdir(parents=True)
        (self.project_dir / "inbox" / "pending.json").write_text("[]", encoding="utf-8")
        self.fake_db = self.project_dir / "nonexistent.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_default_goal_attached_when_configured(self):
        (self.project_dir / "state" / "agent_config.env").write_text(
            "DEFAULT_GOAL_ID=3\n", encoding="utf-8"
        )
        session_type, source, reason, target = resolve_type(self.project_dir, "nightly", self.fake_db)
        self.assertEqual(session_type, "philosophy")
        self.assertEqual(target, {"level": "goal", "id": 3})

    def test_no_target_when_not_configured(self):
        session_type, source, reason, target = resolve_type(self.project_dir, "nightly", self.fake_db)
        self.assertEqual(session_type, "philosophy")
        self.assertIsNone(target)

    def test_target_still_attached_at_philosophy_cap(self):
        (self.project_dir / "state" / "agent_config.env").write_text(
            "DEFAULT_GOAL_ID=3\n", encoding="utf-8"
        )
        (self.project_dir / "state" / "consecutive_philosophy.count").write_text("3", encoding="utf-8")
        session_type, source, reason, target = resolve_type(self.project_dir, "nightly", self.fake_db)
        self.assertEqual(session_type, "philosophy_cap")
        self.assertEqual(target, {"level": "goal", "id": 3})


class TestInboxHasPendingTasks(unittest.TestCase):
    """Unit tests for _inbox_has_pending_tasks()."""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.tmpdir.name)
        self.inbox_dir = self.project_dir / "inbox"
        self.inbox_dir.mkdir(parents=True)
        self.inbox_path = self.inbox_dir / "pending.json"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_inbox(self, entries):
        self.inbox_path.write_text(json.dumps(entries), encoding="utf-8")

    def test_empty_inbox_returns_false(self):
        self._write_inbox([])
        self.assertFalse(_inbox_has_pending_tasks(self.project_dir))

    def test_no_inbox_file_returns_false(self):
        self.assertFalse(_inbox_has_pending_tasks(self.project_dir))

    def test_task_request_unprocessed_returns_true(self):
        self._write_inbox([
            {"type": "task_request", "processed": False, "content": "do X"}
        ])
        self.assertTrue(_inbox_has_pending_tasks(self.project_dir))

    def test_bug_report_unprocessed_returns_true(self):
        self._write_inbox([
            {"type": "bug_report", "processed": False, "content": "Y is broken"}
        ])
        self.assertTrue(_inbox_has_pending_tasks(self.project_dir))

    def test_task_comment_unprocessed_returns_true(self):
        """KEY TEST: task_comment must now trigger execution (T233 fix)."""
        self._write_inbox([
            {"type": "task_comment", "task_id": 231, "processed": False,
             "text": "dig deeper into scope design"}
        ])
        self.assertTrue(_inbox_has_pending_tasks(self.project_dir))

    def test_task_comment_already_processed_returns_false(self):
        """Processed task_comment must not trigger re-execution."""
        self._write_inbox([
            {"type": "task_comment", "task_id": 231, "processed": True,
             "text": "dig deeper into scope design"}
        ])
        self.assertFalse(_inbox_has_pending_tasks(self.project_dir))

    def test_context_update_does_not_trigger_execution(self):
        """context_update is informational — should not force execution."""
        self._write_inbox([
            {"type": "context_update", "processed": False, "content": "repo link: ..."}
        ])
        self.assertFalse(_inbox_has_pending_tasks(self.project_dir))

    def test_idea_does_not_trigger_execution(self):
        """idea entries are optional — should not force execution."""
        self._write_inbox([
            {"type": "idea", "processed": False, "content": "what if we..."}
        ])
        self.assertFalse(_inbox_has_pending_tasks(self.project_dir))

    def test_mixed_all_processed_returns_false(self):
        """All processed entries — should return false."""
        self._write_inbox([
            {"type": "task_comment", "processed": True},
            {"type": "task_request", "processed": True},
            {"type": "bug_report", "processed": True},
        ])
        self.assertFalse(_inbox_has_pending_tasks(self.project_dir))

    def test_mixed_one_unprocessed_task_comment_returns_true(self):
        """One unprocessed task_comment among processed entries — must trigger."""
        self._write_inbox([
            {"type": "context_update", "processed": False},
            {"type": "task_request", "processed": True},
            {"type": "task_comment", "task_id": 231, "processed": False},
        ])
        self.assertTrue(_inbox_has_pending_tasks(self.project_dir))

    def test_entry_missing_processed_field_treated_as_unprocessed(self):
        """Entries without 'processed' field default to False (unprocessed)."""
        self._write_inbox([
            {"type": "task_comment", "task_id": 231}
        ])
        self.assertTrue(_inbox_has_pending_tasks(self.project_dir))


class TestResolveTypeInboxFallback(unittest.TestCase):
    """
    Regression tests for resolve_type() inbox fallback (Priority 3b).
    Uses a non-existent Loom DB to ensure queue_state returns None,
    so the inbox check is the deciding factor.
    """

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.project_dir = Path(self.tmpdir.name)
        self.inbox_dir = self.project_dir / "inbox"
        self.inbox_dir.mkdir(parents=True)
        self.inbox_path = self.inbox_dir / "pending.json"
        # Non-existent DB → queue_state returns (None, None) → falls through to inbox check
        self.fake_db = Path(self.tmpdir.name) / "nonexistent.db"

    def tearDown(self):
        self.tmpdir.cleanup()

    def _write_inbox(self, entries):
        self.inbox_path.write_text(json.dumps(entries), encoding="utf-8")

    def test_empty_queue_and_empty_inbox_resolves_to_philosophy(self):
        """Regression: empty inbox + empty Loom queue → philosophy."""
        self._write_inbox([])
        session_type, source, reason, target = resolve_type(self.project_dir, "nightly", self.fake_db)
        self.assertEqual(session_type, "philosophy")
        self.assertEqual(source, "default")

    def test_empty_queue_with_task_comment_resolves_to_execution(self):
        """KEY TEST: task_comment in inbox overrides philosophy default."""
        self._write_inbox([
            {"type": "task_comment", "task_id": 231, "processed": False,
             "text": "expand the maintenance plan"}
        ])
        session_type, source, reason, target = resolve_type(self.project_dir, "nightly", self.fake_db)
        self.assertEqual(session_type, "execution")
        self.assertEqual(source, "inbox_pending")

    def test_processed_task_comment_still_resolves_to_philosophy(self):
        """Processed comments must not loop-trigger execution."""
        self._write_inbox([
            {"type": "task_comment", "task_id": 231, "processed": True}
        ])
        session_type, source, reason, target = resolve_type(self.project_dir, "nightly", self.fake_db)
        self.assertEqual(session_type, "philosophy")

    def test_session_type_env_var_wins_over_inbox(self):
        """Priority 1: SESSION_TYPE env var must override inbox trigger."""
        import os
        self._write_inbox([
            {"type": "task_comment", "task_id": 231, "processed": False}
        ])
        orig = os.environ.get("SESSION_TYPE")
        try:
            os.environ["SESSION_TYPE"] = "maintenance"
            session_type, source, _, target = resolve_type(self.project_dir, "nightly", self.fake_db)
            self.assertEqual(session_type, "maintenance")
            self.assertEqual(source, "env_var")
        finally:
            if orig is None:
                os.environ.pop("SESSION_TYPE", None)
            else:
                os.environ["SESSION_TYPE"] = orig


if __name__ == "__main__":
    result = unittest.main(verbosity=2, exit=False)
    sys.exit(0 if result.result.wasSuccessful() else 1)
