import json
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from kanban_gateway.jev import evaluate_task_complexity


class TestKanbanRoleRouting(unittest.TestCase):
    @patch("kanban_gateway.jev.call_jev_raw")
    def test_complexity_and_saga_routing(self, mock_jev):
        mock_jev.return_value = (
            '{"tier": "flash_fast", "role": "saga"}',
            0.12,
        )
        res = evaluate_task_complexity(
            "Analyze Eir sleep/HRV biometrics and log badminton journal story",
            "Check Eir readiness, scale Kratos workout, and update today's journal.",
        )
        self.assertEqual(res["tier"], "flash_fast")
        self.assertEqual(res["recommended_assignee"], "saga")
        self.assertEqual(res["recommended_model"], "gemini-3.8-flash-high")

    @patch("kanban_gateway.jev.call_jev_raw")
    def test_sindri_routing(self, mock_jev):
        mock_jev.return_value = (
            '{"tier": "frontier_deep", "role": "sindri"}',
            0.15,
        )
        res = evaluate_task_complexity(
            "Odin: Add healthcheck to muninn service in docker-compose.yml",
            "Update Forge/odin/docker-compose.yml and open a PR.",
        )
        self.assertEqual(res["tier"], "frontier_deep")
        self.assertEqual(res["recommended_assignee"], "sindri")
        self.assertEqual(res["recommended_model"], "gemini-4-argon-high")

    @patch("kanban_gateway.jev.call_jev_raw")
    def test_alias_and_keyword_fallback_routing(self, mock_jev):
        mock_jev.return_value = (
            '{"tier": "flash_fast", "role": "vault-curator"}',
            0.09,
        )
        res = evaluate_task_complexity(
            "Wrap-up: Session wrap-up and docs sync",
            "Update second-brain/Areas/90-Infrastructure/Odin/Odin Stack.md and skills.",
        )
        self.assertEqual(res["tier"], "flash_fast")
        self.assertEqual(res["recommended_assignee"], "vor")
        self.assertEqual(res["recommended_model"], "gemini-3.8-flash-high")

    def test_handoff_kanban_task(self):
        from kanban_gateway import engine
        from kanban_gateway.config import normalize_assignee

        self.assertEqual(normalize_assignee("forge-builder"), "sindri")
        self.assertEqual(normalize_assignee("qa-reviewer"), "forseti")
        self.assertEqual(normalize_assignee("scout"), "huginn")
        self.assertEqual(normalize_assignee("vault-curator"), "vor")
        self.assertEqual(normalize_assignee("journal-scribe"), "saga")

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "kanban.db")
            conn = sqlite3.connect(db_path)
            conn.execute(
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
                    consecutive_failures INTEGER,
                    last_failure_error TEXT,
                    goal_mode INTEGER,
                    block_recurrences INTEGER,
                    model_override TEXT,
                    provider_override TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT,
                    kind TEXT,
                    payload TEXT,
                    created_at INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE kanban_notify_subs (
                    task_id TEXT,
                    platform TEXT,
                    chat_id TEXT,
                    thread_id TEXT,
                    user_id TEXT,
                    notifier_profile TEXT,
                    created_at INTEGER,
                    last_event_id INTEGER,
                    delivery_mode TEXT
                )
                """
            )
            conn.commit()
            conn.close()

            with patch.object(engine, "KANBAN_DB_PATH", db_path), patch.object(
                engine, "DISCORD_WEBHOOK_URL", ""
            ), patch(
                "kanban_gateway.engine.evaluate_task_complexity",
                return_value={
                    "tier": "flash_fast",
                    "recommended_assignee": "huginn",
                    "recommended_model": "gemini-3.8-flash-high",
                },
            ):
                task_id, _ = engine.create_kanban_task(
                    title="Investigate Iris alert on poseidon",
                    body="Check docker service logs for poseidon.",
                    notify_discord=False,
                )

                # Simulate prior failure state on the task
                conn = sqlite3.connect(db_path)
                conn.execute(
                    "UPDATE tasks SET consecutive_failures = 2, last_failure_error = 'Playwright assertion failure' WHERE id = ?",
                    (task_id,),
                )
                conn.commit()
                conn.close()

                res = engine.handoff_kanban_task(
                    task_id=task_id,
                    to_assignee="sindri",
                    summary="Root cause: missing env var in poseidon compose. Ready for Forge fix.",
                )
                self.assertEqual(res["to_assignee"], "sindri")
                self.assertEqual(res["model_override"], "gemini-4-argon-high")

                conn = sqlite3.connect(db_path)
                row = conn.execute(
                    "SELECT assignee, status, model_override, body, consecutive_failures, last_failure_error FROM tasks WHERE id = ?",
                    (task_id,),
                ).fetchone()
                conn.close()
                self.assertEqual(row[0], "sindri")
                self.assertEqual(row[1], "ready")
                self.assertEqual(row[2], "gemini-4-argon-high")
                self.assertIn("Root cause: missing env var", row[3])
                self.assertEqual(row[4], 0)
                self.assertIsNone(row[5])


if __name__ == "__main__":
    unittest.main()
