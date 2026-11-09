#!/usr/bin/env python3
"""test_kanban_verification_pipeline.py - Unit tests for Court of Odin state-machine verification transitions."""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

# Ensure kanban_gateway package is on path
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
sys.path.insert(0, BASE_DIR)

from kanban_gateway.engine import verify_pipeline_stage  # noqa: E402
import kanban_gateway.engine as kg_engine  # noqa: E402
import kanban_gateway.config as kg_config  # noqa: E402


class TestKanbanVerificationPipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        # Point config to temp DB
        self.orig_db_path = kg_engine.KANBAN_DB_PATH
        kg_engine.KANBAN_DB_PATH = self.db_path
        kg_config.KANBAN_DB_PATH = self.db_path

        # Setup schema
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT,
                body TEXT,
                assignee TEXT,
                status TEXT,
                priority INTEGER,
                created_by TEXT,
                created_at INTEGER,
                workspace_kind TEXT,
                consecutive_failures INTEGER DEFAULT 0,
                goal_mode INTEGER DEFAULT 0,
                block_recurrences INTEGER DEFAULT 0,
                model_override TEXT,
                provider_override TEXT,
                last_failure_error TEXT,
                started_at INTEGER,
                completed_at INTEGER
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE task_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id TEXT,
                kind TEXT,
                payload TEXT,
                created_at INTEGER
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE kanban_notify_subs (
                task_id TEXT PRIMARY KEY,
                platform TEXT,
                chat_id TEXT,
                thread_id TEXT,
                user_id TEXT,
                notifier_profile TEXT,
                created_at INTEGER,
                last_event_id INTEGER,
                delivery_mode TEXT
            );
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        kg_engine.KANBAN_DB_PATH = self.orig_db_path
        kg_config.KANBAN_DB_PATH = self.orig_db_path
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _insert_task(self, task_id: str, assignee: str, failures: int = 0) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO tasks (id, title, body, assignee, status, priority, consecutive_failures)
            VALUES (?, 'Test Task', 'Initial Body', ?, 'in_progress', 1, ?)
            """,
            (task_id, assignee, failures),
        )
        conn.commit()
        conn.close()

    def test_verify_passed_from_forseti_advances_to_vor(self) -> None:
        self._insert_task("kb-001", assignee="forseti", failures=0)
        res = verify_pipeline_stage(
            task_id="kb-001",
            passed=True,
            summary="All visual tests passed on iOS & Android",
            pr_url="https://github.com/yggdrasil-lab/kratos/pull/99",
        )
        self.assertTrue(res["passed"])
        self.assertEqual(res["next_assignee"], "vor")
        self.assertEqual(res["status"], "ready")
        self.assertEqual(res["consecutive_failures"], 0)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT assignee, status, consecutive_failures, body FROM tasks WHERE id = 'kb-001'")
        row = cur.fetchone()
        conn.close()

        self.assertEqual(row[0], "vor")
        self.assertEqual(row[1], "ready")
        self.assertEqual(row[2], 0)
        self.assertIn("Verification Gate Passed (forseti)", row[3])

    def test_verify_failed_retry_bounces_to_sindri(self) -> None:
        self._insert_task("kb-002", assignee="forseti", failures=0)
        res = verify_pipeline_stage(
            task_id="kb-002",
            passed=False,
            feedback="Playwright test failed: button overflow on Pixel 7",
            max_retries=3,
        )
        self.assertFalse(res["passed"])
        self.assertEqual(res["next_assignee"], "sindri")
        self.assertEqual(res["consecutive_failures"], 1)
        self.assertFalse(res["circuit_tripped"])

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT assignee, status, consecutive_failures, last_failure_error FROM tasks WHERE id = 'kb-002'")
        row = cur.fetchone()
        conn.close()

        self.assertEqual(row[0], "sindri")
        self.assertEqual(row[1], "ready")
        self.assertEqual(row[2], 1)
        self.assertIn("button overflow", row[3])

    def test_verify_failed_exceeding_max_retries_trips_circuit_breaker(self) -> None:
        # Task already had 3 failures
        self._insert_task("kb-003", assignee="forseti", failures=3)
        res = verify_pipeline_stage(
            task_id="kb-003",
            passed=False,
            feedback="Fatal build error persists after 3 retries",
            max_retries=3,
        )
        self.assertFalse(res["passed"])
        self.assertEqual(res["next_assignee"], "raymund")
        self.assertEqual(res["status"], "blocked")
        self.assertTrue(res["circuit_tripped"])
        self.assertEqual(res["consecutive_failures"], 4)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT assignee, status, consecutive_failures FROM tasks WHERE id = 'kb-003'")
        row = cur.fetchone()
        conn.close()

        self.assertEqual(row[0], "raymund")
        self.assertEqual(row[1], "blocked")
        self.assertEqual(row[2], 4)


if __name__ == "__main__":
    unittest.main()
