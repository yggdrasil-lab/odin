#!/usr/bin/env python3
"""Temp-DB unit tests for Kanban atomic dependencies, linking, handoff gating, and wrap-up idempotency."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

import kanban_gateway.config as kg_config  # noqa: E402
import kanban_gateway.engine as kg_engine  # noqa: E402
import kanban_jev_gateway as kg_cli  # noqa: E402
from kanban_gateway.server import KanbanGatewayHTTPHandler  # noqa: E402


def _init_kanban_schema(db_path: str) -> None:
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
            consecutive_failures INTEGER DEFAULT 0,
            last_failure_error TEXT,
            goal_mode INTEGER DEFAULT 0,
            block_recurrences INTEGER DEFAULT 0,
            model_override TEXT,
            provider_override TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE task_links (
            parent_id TEXT NOT NULL,
            child_id TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
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
            task_id TEXT PRIMARY KEY,
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


class _FakeHTTPRequestHandler(KanbanGatewayHTTPHandler):
    def __init__(self, path: str, body: dict) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.path = path
        self.headers = {"Content-Length": str(len(raw))}
        self.rfile = io.BytesIO(raw)
        self.wfile = io.BytesIO()
        self.client_address = ("127.0.0.1", 0)
        self.status_code = 0

    def send_response(self, code: int, message: str | None = None) -> None:
        self.status_code = code

    def send_header(self, keyword: str, value: str) -> None:
        pass

    def end_headers(self) -> None:
        pass

    def json_body(self) -> dict:
        return json.loads(self.wfile.getvalue().decode("utf-8"))


class TestKanbanDependenciesAndWrapup(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = tmp.name
        tmp.close()
        _init_kanban_schema(self.db_path)

        self.orig_engine_db = kg_engine.KANBAN_DB_PATH
        self.orig_config_db = kg_config.KANBAN_DB_PATH
        self.orig_bot_token = kg_engine.DISCORD_BOT_TOKEN
        self.orig_channel_id = kg_engine.DISCORD_CHANNEL_ID
        kg_engine.KANBAN_DB_PATH = self.db_path
        kg_config.KANBAN_DB_PATH = self.db_path
        kg_engine.DISCORD_BOT_TOKEN = ""
        kg_engine.DISCORD_CHANNEL_ID = ""

    def tearDown(self) -> None:
        kg_engine.KANBAN_DB_PATH = self.orig_engine_db
        kg_config.KANBAN_DB_PATH = self.orig_config_db
        kg_engine.DISCORD_BOT_TOKEN = self.orig_bot_token
        kg_engine.DISCORD_CHANNEL_ID = self.orig_channel_id
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _query_all(self, sql: str, params: tuple = ()) -> list[tuple]:
        conn = sqlite3.connect(self.db_path)
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return rows

    def test_atomic_create_with_dependencies_and_fail_loud_rollback(self) -> None:
        open_parent, _ = kg_engine.create_kanban_task(
            title="Parent open", body="Open work", assignee="sindri", notify_discord=False
        )
        done_parent, _ = kg_engine.create_kanban_task(
            title="Parent done", body="Done work", assignee="sindri", notify_discord=False
        )
        kg_engine.update_task_lifecycle(done_parent, "done", "Finished parent")

        # Open parent forces todo even if status='ready' is requested
        child_open, _ = kg_engine.create_kanban_task(
            title="Child blocked",
            body="Waits for parent",
            assignee="sindri",
            depends_on=[open_parent],
            status="ready",
            notify_discord=False,
        )
        info_open = kg_engine.show_kanban_task(child_open, as_json=True)
        self.assertEqual(info_open["status"], "todo")
        self.assertEqual(
            self._query_all("SELECT parent_id, child_id FROM task_links WHERE child_id = ?", (child_open,)),
            [(open_parent, child_open)],
        )
        kinds_open = [ev["kind"] for ev in info_open["events"]]
        self.assertIn("linked", kinds_open)
        self.assertIn("dependency_wait", kinds_open)

        # Completed parent allows ready (and honors explicit status='todo' if desired)
        child_ready, _ = kg_engine.create_kanban_task(
            title="Child ready",
            body="Unblocked",
            assignee="sindri",
            depends_on=[done_parent],
            notify_discord=False,
        )
        info_ready = kg_engine.show_kanban_task(child_ready, as_json=True)
        self.assertEqual(info_ready["status"], "ready")
        self.assertNotIn("dependency_wait", [ev["kind"] for ev in info_ready["events"]])

        # Missing parent fails loudly with zero partial rows written
        before_tasks = self._query_all("SELECT COUNT(*) FROM tasks")[0][0]
        before_links = self._query_all("SELECT COUNT(*) FROM task_links")[0][0]
        before_events = self._query_all("SELECT COUNT(*) FROM task_events")[0][0]
        with self.assertRaises(ValueError):
            kg_engine.create_kanban_task(
                title="Should rollback",
                body="Bad dependency",
                assignee="sindri",
                depends_on=[done_parent, "kb-doesnotexist"],
                notify_discord=False,
            )
        self.assertEqual(self._query_all("SELECT COUNT(*) FROM tasks")[0][0], before_tasks)
        self.assertEqual(self._query_all("SELECT COUNT(*) FROM task_links")[0][0], before_links)
        self.assertEqual(self._query_all("SELECT COUNT(*) FROM task_events")[0][0], before_events)

    def test_link_validation_demotion_cycles_and_idempotency(self) -> None:
        parent_open, _ = kg_engine.create_kanban_task(
            title="Open parent", body="Body", assignee="sindri", notify_discord=False
        )
        parent_done, _ = kg_engine.create_kanban_task(
            title="Done parent", body="Body", assignee="sindri", notify_discord=False
        )
        kg_engine.update_task_lifecycle(parent_done, "done", "Done")
        child_a, _ = kg_engine.create_kanban_task(
            title="Child A", body="Body", assignee="sindri", notify_discord=False
        )
        child_b, _ = kg_engine.create_kanban_task(
            title="Child B", body="Body", assignee="sindri", notify_discord=False
        )
        running_child, _ = kg_engine.create_kanban_task(
            title="Running child", body="Body", assignee="sindri", notify_discord=False
        )
        conn = sqlite3.connect(self.db_path)
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (running_child,))
        conn.commit()
        conn.close()

        # Happy path with done parent keeps child ready
        res_done = kg_engine.link_kanban_tasks(parent_done, child_b)
        self.assertTrue(res_done["linked"])
        self.assertFalse(res_done["demoted"])
        self.assertEqual(res_done["child_status"], "ready")

        # Open parent demotes ready -> todo and logs dependency_wait
        res_demote = kg_engine.link_kanban_tasks(parent_open, child_a)
        self.assertTrue(res_demote["linked"])
        self.assertTrue(res_demote["demoted"])
        self.assertEqual(res_demote["child_status"], "todo")
        self.assertEqual(kg_engine.show_kanban_task(child_a, as_json=True)["status"], "todo")
        self.assertTrue(
            self._query_all("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'dependency_wait'", (child_a,))
        )

        # Idempotent duplicate link succeeds without duplicating linked event
        linked_before = self._query_all(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'linked'", (child_a,)
        )[0][0]
        res_dup = kg_engine.link_kanban_tasks(parent_open, child_a)
        self.assertFalse(res_dup["linked"])
        self.assertTrue(res_dup["already_linked"])
        linked_after = self._query_all(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'linked'", (child_a,)
        )[0][0]
        self.assertEqual(linked_before, linked_after)

        # Self-link, cycle (parent_open -> child_a -> child_b => child_b -> parent_open), and running child rejected
        kg_engine.link_kanban_tasks(child_a, child_b)
        with self.assertRaises(ValueError):
            kg_engine.link_kanban_tasks(child_a, child_a)
        with self.assertRaises(ValueError):
            kg_engine.link_kanban_tasks(child_b, parent_open)
        with self.assertRaises(ValueError):
            kg_engine.link_kanban_tasks(parent_done, running_child)

    def test_handoff_respects_parent_completion_status(self) -> None:
        parent, _ = kg_engine.create_kanban_task(
            title="Parent task", body="Work", assignee="huginn", notify_discord=False
        )
        child, _ = kg_engine.create_kanban_task(
            title="Child task", body="Work", assignee="huginn", depends_on=[parent], notify_discord=False
        )
        res_waiting = kg_engine.handoff_kanban_task(child, "sindri", "Waiting on parent")
        self.assertEqual(res_waiting["status"], "todo")
        self.assertEqual(kg_engine.show_kanban_task(child, as_json=True)["status"], "todo")

        kg_engine.update_task_lifecycle(parent, "done", "Parent finished")
        res_ready = kg_engine.handoff_kanban_task(child, "forseti", "Parent finished, ready for review")
        self.assertEqual(res_ready["status"], "ready")
        self.assertEqual(kg_engine.show_kanban_task(child, as_json=True)["status"], "ready")

    def test_wrapup_idempotency_kb_prefix_recursion_and_regression_guard(self) -> None:
        task_id, _ = kg_engine.create_kanban_task(
            title="Odin feature task", body="Deliver slice", assignee="sindri", notify_discord=False
        )
        with patch.object(kg_engine, "DISCORD_BOT_TOKEN", ""), patch.object(kg_engine, "DISCORD_CHANNEL_ID", ""):
            self.assertTrue(kg_engine.update_task_lifecycle(task_id, "done", "Merged PR"))
            wrapups_first = self._query_all(
                "SELECT id FROM tasks WHERE title LIKE '%Wrap-up:%' AND id != ?", (task_id,)
            )
            self.assertEqual(len(wrapups_first), 1)
            self.assertEqual(
                self._query_all("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'wrapup_triggered'", (task_id,))[0][0],
                1,
            )

            # Non-forced done -> in_review is rejected
            self.assertFalse(kg_engine.update_task_lifecycle(task_id, "in_review", "Late debrief"))
            self.assertEqual(kg_engine.show_kanban_task(task_id, as_json=True)["status"], "done")

            # Forced done -> in_review -> done must not spawn duplicate wrap-up
            self.assertTrue(kg_engine.update_task_lifecycle(task_id, "in_review", "Force reopen", force=True))
            self.assertTrue(kg_engine.update_task_lifecycle(task_id, "done", "Re-closed"))
            wrapups_second = self._query_all(
                "SELECT id FROM tasks WHERE title LIKE '%Wrap-up:%' AND id != ?", (task_id,)
            )
            self.assertEqual(len(wrapups_second), 1)

            # Recursion guard strips [KB-xxxxxxxx] prefix before checking wrap-up / auto-sync
            self.assertIsNone(kg_engine.trigger_session_wrapup_chore("kb-deadbeef", "[KB-deadbeef] Wrap-up: PR #99"))
            self.assertIsNone(kg_engine.trigger_session_wrapup_chore("kb-cafebabe", "[kb-cafebabe] Auto-sync docs"))

    def test_cli_and_http_create_link_and_update_guards(self) -> None:
        with patch.object(kg_engine, "DISCORD_BOT_TOKEN", ""), patch.object(kg_engine, "DISCORD_CHANNEL_ID", ""):
            parent_handler = _FakeHTTPRequestHandler("/create", {"title": "HTTP Parent", "body": "Parent", "assignee": "sindri"})
            parent_handler.do_POST()
            self.assertEqual(parent_handler.status_code, 200)
            parent_id = parent_handler.json_body()["task_id"]

            rc = kg_cli.main(
                [
                    "create",
                    "--title", "CLI Child",
                    "--body", "Blocked child",
                    "--assignee", "sindri",
                    "--depends-on", parent_id,
                    "--status", "ready",
                ]
            )
            self.assertEqual(rc, 0)
            child_rows = self._query_all("SELECT id, status FROM tasks WHERE title LIKE '%CLI Child'")
            self.assertEqual(len(child_rows), 1)
            self.assertEqual(child_rows[0][1], "todo")

            # HTTP /link attaches extra dependency and rejects cycle with HTTP 400
            standalone_handler = _FakeHTTPRequestHandler("/create", {"title": "HTTP Child", "body": "Child", "assignee": "sindri"})
            standalone_handler.do_POST()
            child2_id = standalone_handler.json_body()["task_id"]

            link_handler = _FakeHTTPRequestHandler("/link", {"parent_id": parent_id, "child_id": child2_id})
            link_handler.do_POST()
            self.assertEqual(link_handler.status_code, 200)
            self.assertTrue(link_handler.json_body()["demoted"])
            self.assertEqual(link_handler.json_body()["child_status"], "todo")

            bad_link = _FakeHTTPRequestHandler("/link", {"parent_id": child2_id, "child_id": parent_id})
            bad_link.do_POST()
            self.assertEqual(bad_link.status_code, 400)


if __name__ == "__main__":
    unittest.main()
