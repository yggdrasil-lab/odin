#!/usr/bin/env python3
"""test_kanban_ide_ownership.py - Unit tests for IDE task ownership and initial status flags."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import patch

SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import kanban_jev_gateway  # noqa: E402
from kanban_gateway import config as kg_config  # noqa: E402
from kanban_gateway import engine as kg_engine  # noqa: E402
from kanban_gateway.server import KanbanGatewayHTTPHandler  # noqa: E402


def _create_kanban_schema(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL,
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


class TestKanbanIdeOwnership(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "kanban.db")
        _create_kanban_schema(self.db_path)
        self.orig_engine_db = kg_engine.KANBAN_DB_PATH
        self.orig_config_db = kg_config.KANBAN_DB_PATH
        kg_engine.KANBAN_DB_PATH = self.db_path
        kg_config.KANBAN_DB_PATH = self.db_path
        self.sample_complexity = {
            "tier": "frontier_deep",
            "recommended_assignee": "sindri",
            "recommended_model": "gemini-4-argon-high",
            "raw_verdict": "test_precomputed",
            "latency_seconds": 0.0,
        }

    def tearDown(self) -> None:
        kg_engine.KANBAN_DB_PATH = self.orig_engine_db
        kg_config.KANBAN_DB_PATH = self.orig_config_db
        self.tmpdir.cleanup()

    def _fetch_task_row(self, task_id: str) -> sqlite3.Row:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        return row

    def _fetch_created_event(self, task_id: str) -> dict:
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'created' ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(row)
        return json.loads(row[0])

    def test_cli_parser_owner_and_status_flags(self) -> None:
        parser = kanban_jev_gateway.build_parser()

        default_args = parser.parse_args(["create", "--title", "T", "--body", "B"])
        self.assertEqual(default_args.owner, "hermes")
        self.assertIsNone(default_args.status)

        ide_args = parser.parse_args(
            ["create", "--title", "T", "--body", "B", "--owner", "ide", "--status", "in_review"]
        )
        self.assertEqual(ide_args.owner, "ide")
        self.assertEqual(ide_args.status, "in_review")

        with self.assertRaises(SystemExit):
            parser.parse_args(["create", "--title", "T", "--body", "B", "--owner", "invalid"])
        with self.assertRaises(SystemExit):
            parser.parse_args(["create", "--title", "T", "--body", "B", "--status", "done"])

    def test_cmd_create_forwards_owner_and_status(self) -> None:
        with patch.object(kg_engine, "DISCORD_BOT_TOKEN", ""), patch.object(
            kg_engine, "DISCORD_CHANNEL_ID", ""
        ), patch(
            "kanban_gateway.engine.evaluate_task_complexity",
            return_value=self.sample_complexity,
        ):
            rc = kanban_jev_gateway.main(
                ["create", "--title", "IDE CLI", "--body", "Created via CLI", "--owner", "ide"]
            )
            self.assertEqual(rc, 0)

        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT status, created_by, started_at FROM tasks LIMIT 1").fetchone()
        conn.close()
        self.assertEqual(row[0], "in_progress")
        self.assertEqual(row[1], "ide")
        self.assertIsNotNone(row[2])

    def test_create_kanban_task_owner_ide_and_hermes_defaults(self) -> None:
        ide_id, _ = kg_engine.create_kanban_task(
            title="IDE Feature",
            body="Implemented inside IDE",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            owner="  IDE  ",
        )
        ide_row = self._fetch_task_row(ide_id)
        self.assertEqual(ide_row["status"], "in_progress")
        self.assertEqual(ide_row["created_by"], "ide")
        self.assertIsNotNone(ide_row["started_at"])
        ide_event = self._fetch_created_event(ide_id)
        self.assertEqual(ide_event["owner"], "ide")
        self.assertEqual(ide_event["initial_status"], "in_progress")

        hermes_id, _ = kg_engine.create_kanban_task(
            title="Hermes Feature",
            body="Dispatched to Hermes worker",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            owner="hermes",
        )
        hermes_row = self._fetch_task_row(hermes_id)
        self.assertEqual(hermes_row["status"], "ready")
        self.assertEqual(hermes_row["created_by"], "jev-gateway")
        self.assertIsNone(hermes_row["started_at"])
        hermes_event = self._fetch_created_event(hermes_id)
        self.assertEqual(hermes_event["owner"], "hermes")
        self.assertEqual(hermes_event["initial_status"], "ready")

    def test_create_kanban_task_status_and_await_approval_precedence(self) -> None:
        review_id, _ = kg_engine.create_kanban_task(
            title="IDE Parked Review",
            body="Explicit in_review status",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            owner="ide",
            status="in_review",
        )
        review_row = self._fetch_task_row(review_id)
        self.assertEqual(review_row["status"], "in_review")
        self.assertEqual(review_row["created_by"], "ide")
        self.assertIsNone(review_row["started_at"])

        hitl_id, _ = kg_engine.create_kanban_task(
            title="HITL Overrides Status",
            body="Approval wins over explicit status",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            await_approval=True,
            owner="ide",
            status="in_progress",
        )
        hitl_row = self._fetch_task_row(hitl_id)
        self.assertEqual(hitl_row["status"], "in_review")
        self.assertEqual(hitl_row["created_by"], "ide")
        self.assertIsNone(hitl_row["started_at"])

    def test_create_kanban_task_discord_status_badges(self) -> None:
        with patch.object(kg_engine, "DISCORD_BOT_TOKEN", "bot-token"), patch.object(
            kg_engine, "DISCORD_CHANNEL_ID", "chan-1"
        ), patch("kanban_gateway.engine.post_discord_thread", return_value="thread-1") as mock_post:
            kg_engine.create_kanban_task(
                title="IDE Badge",
                body="Check IDE badge",
                notify_discord=True,
                precomputed_complexity=self.sample_complexity,
                owner="ide",
            )
            ide_fields = mock_post.call_args.kwargs["fields"]
            self.assertEqual(ide_fields[0]["value"], "`IN_PROGRESS` (IDE-Owned)")

            kg_engine.create_kanban_task(
                title="Hermes In Progress Badge",
                body="Check Hermes in_progress badge",
                notify_discord=True,
                precomputed_complexity=self.sample_complexity,
                owner="hermes",
                status="in_progress",
            )
            hermes_ip_fields = mock_post.call_args.kwargs["fields"]
            self.assertEqual(hermes_ip_fields[0]["value"], "`IN_PROGRESS`")

            kg_engine.create_kanban_task(
                title="IDE Review Badge",
                body="Check in_review badge",
                notify_discord=True,
                precomputed_complexity=self.sample_complexity,
                owner="ide",
                status="in_review",
            )
            review_fields = mock_post.call_args.kwargs["fields"]
            self.assertEqual(review_fields[0]["value"], "`IN_REVIEW`")

            kg_engine.create_kanban_task(
                title="HITL Badge",
                body="Check HITL badge",
                notify_discord=True,
                precomputed_complexity=self.sample_complexity,
                owner="ide",
                await_approval=True,
            )
            hitl_fields = mock_post.call_args.kwargs["fields"]
            self.assertEqual(hitl_fields[0]["value"], "`IN_REVIEW (AWAITING APPROVAL)`")

    def test_create_kanban_task_invalid_owner_or_status_fails_loudly(self) -> None:
        with patch("kanban_gateway.engine.evaluate_task_complexity") as mock_eval:
            for bad_owner in ("", "cursor", "antigravity"):
                with self.assertRaises(ValueError):
                    kg_engine.create_kanban_task(
                        title="Bad Owner",
                        body="Should fail before Jev/DB",
                        notify_discord=False,
                        owner=bad_owner,
                    )
            for bad_status in ("", "bogus", "done", "running"):
                with self.assertRaises(ValueError):
                    kg_engine.create_kanban_task(
                        title="Bad Status",
                        body="Should fail before Jev/DB",
                        notify_discord=False,
                        status=bad_status,
                    )
            mock_eval.assert_not_called()

    def test_http_post_create_forwards_and_validates(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), KanbanGatewayHTTPHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            def post_create(payload: dict) -> tuple[int, dict]:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{port}/create",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        return resp.status, json.loads(resp.read().decode("utf-8"))
                except urllib.error.HTTPError as err:
                    return err.code, json.loads(err.read().decode("utf-8"))

            with patch.object(kg_engine, "DISCORD_BOT_TOKEN", ""), patch.object(
                kg_engine, "DISCORD_CHANNEL_ID", ""
            ), patch(
                "kanban_gateway.engine.evaluate_task_complexity",
                return_value=self.sample_complexity,
            ):
                code, body = post_create({"title": "HTTP IDE", "body": "Body", "owner": "ide"})
                self.assertEqual(code, 200)
                row = self._fetch_task_row(body["task_id"])
                self.assertEqual(row["status"], "in_progress")
                self.assertEqual(row["created_by"], "ide")
                self.assertIsNotNone(row["started_at"])

                code_rev, body_rev = post_create(
                    {"title": "HTTP Review", "body": "Body", "owner": "ide", "status": "in_review"}
                )
                self.assertEqual(code_rev, 200)
                rev_row = self._fetch_task_row(body_rev["task_id"])
                self.assertEqual(rev_row["status"], "in_review")
                self.assertEqual(rev_row["created_by"], "ide")

                bad_owner_code, bad_owner_body = post_create(
                    {"title": "Bad", "body": "Body", "owner": "vscode"}
                )
                self.assertEqual(bad_owner_code, 400)
                self.assertIn("error", bad_owner_body)

                empty_owner_code, _ = post_create({"title": "Bad", "body": "Body", "owner": ""})
                self.assertEqual(empty_owner_code, 400)

                int_owner_code, _ = post_create({"title": "Bad", "body": "Body", "owner": 123})
                self.assertEqual(int_owner_code, 400)

                bad_status_code, bad_status_body = post_create(
                    {"title": "Bad", "body": "Body", "owner": "ide", "status": "bogus"}
                )
                self.assertEqual(bad_status_code, 400)
                self.assertIn("error", bad_status_body)
        finally:
            server.shutdown()
            server.server_close()

    def test_update_task_lifecycle_done_skips_wrapup_only_for_ide(self) -> None:
        ide_id, _ = kg_engine.create_kanban_task(
            title="IDE Close",
            body="Closes via deploy.sh",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            owner="ide",
        )
        ide_review_id, _ = kg_engine.create_kanban_task(
            title="IDE Review Close",
            body="Closes from in_review via deploy.sh",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            owner="ide",
            status="in_review",
        )
        hermes_id, _ = kg_engine.create_kanban_task(
            title="Hermes Close",
            body="Triggers wrap-up chore",
            notify_discord=False,
            precomputed_complexity=self.sample_complexity,
            owner="hermes",
        )

        with patch("kanban_gateway.engine.trigger_session_wrapup_chore") as mock_wrapup:
            ok_ide = kg_engine.update_task_lifecycle(ide_id, "done", "PR merged to main.")
            self.assertTrue(ok_ide)
            ok_ide_review = kg_engine.update_task_lifecycle(ide_review_id, "done", "PR merged to main.")
            self.assertTrue(ok_ide_review)
            mock_wrapup.assert_not_called()

            ide_row = self._fetch_task_row(ide_id)
            self.assertEqual(ide_row["status"], "done")
            self.assertIsNotNone(ide_row["completed_at"])
            ide_review_row = self._fetch_task_row(ide_review_id)
            self.assertEqual(ide_review_row["status"], "done")
            self.assertIsNotNone(ide_review_row["completed_at"])

            ok_hermes = kg_engine.update_task_lifecycle(hermes_id, "done", "PR merged to main.")
            self.assertTrue(ok_hermes)
            mock_wrapup.assert_called_once_with(hermes_id, f"[KB-{hermes_id[3:]}] Hermes Close", None)

            hermes_row = self._fetch_task_row(hermes_id)
            self.assertEqual(hermes_row["status"], "done")
            self.assertIsNotNone(hermes_row["completed_at"])


if __name__ == "__main__":
    unittest.main()
