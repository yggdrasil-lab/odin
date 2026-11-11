import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../scripts")))

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
    def test_sindri_flash_fast_routing(self, mock_jev):
        mock_jev.return_value = (
            '{"tier": "flash_fast", "role": "sindri"}',
            0.10,
        )
        res = evaluate_task_complexity(
            "Kratos: Per-Dumbbell Weight Basis Formatting",
            "Add per_hand label display to workout item.",
        )
        self.assertEqual(res["tier"], "flash_fast")
        self.assertEqual(res["recommended_assignee"], "sindri")
        self.assertEqual(res["recommended_model"], "gemini-3.8-flash-high")

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
                    started_at INTEGER,
                    completed_at INTEGER,
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

            with patch.object(engine, "KANBAN_DB_PATH", db_path), patch(
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

                # Test Tier 3 improvement proposal creation (in_review assigned to raymund) & show_kanban_task
                prop_id, prop_thread = engine.create_improvement_proposal(
                    title="Add preflight skill validator to wrap-up",
                    friction="Tail-of-session edits exceeded 4,000B body cap without failing check_wrapup_readiness.py.",
                    proposal="Run validate_skill.py --all inside check_wrapup_readiness.py.",
                    target_specialist="vor",
                    origin_task_id=task_id,
                    notify_discord=False,
                )
                self.assertIsNone(prop_thread)
                info = engine.show_kanban_task(prop_id, as_json=True)
                self.assertEqual(info["status"], "in_review")
                self.assertEqual(info["assignee"], "raymund")
                self.assertIn("💡 Proposal: Add preflight skill validator to wrap-up", info["title"])
                self.assertIn("kanban-gateway handoff", info["body"])

                # Verify recursion guard skips wrap-up trigger for Proposal cards
                wrapup_id = engine.trigger_session_wrapup_chore(prop_id, info["title"])
                self.assertIsNone(wrapup_id)

                # Test Tier 2 failing script friction in record_task_debrief -> spawns 'ready' [Auto-Fix] card assigned to vor
                with patch.object(engine, "DISCORD_BOT_TOKEN", ""), patch.object(engine, "DISCORD_CHANNEL_ID", ""), patch(
                    "kanban_gateway.engine.evaluate_debrief_gates_batch",
                    return_value={
                        "acceptance_gate": {"accepted": True, "raw_verdict": "accepted"},
                        "auto_merge_gate": {"auto_merge_eligible": False, "raw_verdict": "no_pr"},
                        "friction_gate": {"action": "auto_fix_script", "assignee": "vor", "raw_verdict": "auto_fix_script"},
                        "friction_assignee": "vor",
                    },
                ):
                    debrief_res = engine.record_task_debrief(
                        task_id=task_id,
                        summary="Completed poseidon diagnostic.",
                        frictions="check_poseidon.py failed with FileNotFoundError on line 42.",
                        chore_title="Fix FileNotFoundError in check_poseidon.py",
                    )
                    autofix_id = debrief_res["chore_id"]
                    self.assertIsNotNone(autofix_id)
                    autofix_info = engine.show_kanban_task(autofix_id, as_json=True)
                    self.assertEqual(autofix_info["status"], "ready")
                    self.assertEqual(autofix_info["assignee"], "vor")
                    self.assertIn("[Auto-Fix] Fix FileNotFoundError in check_poseidon.py", autofix_info["title"])
                    self.assertIsNone(engine.trigger_session_wrapup_chore(autofix_id, autofix_info["title"]))


if __name__ == "__main__":
    unittest.main()
