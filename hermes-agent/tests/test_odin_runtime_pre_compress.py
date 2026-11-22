#!/usr/bin/env python3
"""Direct interface unit tests for odin_runtime.pre_compress_handover."""

from __future__ import annotations

from datetime import datetime, timezone
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
import urllib.error
from unittest.mock import MagicMock, patch

HERMES_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

from odin_runtime.pre_compress_handover import (  # noqa: E402
    FakeDiscordAdapter,
    HandoverConfig,
    UrllibDiscordAdapter,
    auckland_journal_date,
    next_part_title,
    resolve_parent_channel_id,
    run_pre_compress_handover,
    should_suppress_inflight_replay,
    should_suppress_reappend_text,
)


def _seed_sessions_db(db_path: Path, row: tuple[str, str, str, str, str, str]) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, chat_id TEXT, thread_id TEXT, title TEXT, origin_json TEXT)"
    )
    conn.execute("INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?)", row)
    conn.commit()
    conn.close()


class TestOdinRuntimePreCompressHandover(unittest.TestCase):
    """Tests for journal checkpointing, DiscordThreadPort handover, and replay guards."""

    def test_config_reads_info_channel_and_auckland_rollover(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            vault = Path(tmpdir)
            cfg = HandoverConfig.from_env(
                {"DISCORD_CHANNEL_INFO_ID": "chan-info-1", "DISCORD_USER_ID": "user-42"},
                vault_candidates=(vault,),
                state_db=vault / "state.db",
            )
            self.assertEqual(cfg.vault_root, vault)
            self.assertEqual(cfg.info_channel_id, "chan-info-1")
            self.assertEqual(cfg.user_id, "user-42")

        # 2026-11-22 16:30 UTC == 2026-11-23 05:30 NZDT (<06:00 rollover -> 2026-11-22)
        early_date, label = auckland_journal_date(datetime(2026, 11, 22, 16, 30, tzinfo=timezone.utc))
        self.assertEqual(early_date.isoformat(), "2026-11-22")
        self.assertIn("05:30", label)

        # 2026-11-22 18:05 UTC == 2026-11-23 07:05 NZDT (>=06:00 -> 2026-11-23)
        morning_date, _ = auckland_journal_date(datetime(2026, 11, 22, 18, 5, tzinfo=timezone.utc))
        self.assertEqual(morning_date.isoformat(), "2026-11-23")

    def test_handover_writes_dash_free_journal_and_migrates_thread(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            vault = root / "vault"
            vault.mkdir()
            db_path = root / "state.db"
            _seed_sessions_db(
                db_path,
                (
                    "sess-1",
                    "discord",
                    "thread-old-9",
                    "thread-old-9",
                    "Mutated title",
                    json.dumps({"chat_name": "Guild / #ops / Refactor \u2014 Cleanup", "parent_chat_id": "thread-old-9"}),
                ),
            )
            fake = FakeDiscordAdapter(next_thread_id="thread-new-2")
            cfg = HandoverConfig(
                vault_root=vault,
                state_db=db_path,
                info_channel_id="chan-info-fallback",
                user_id="user-101",
            )
            result = run_pre_compress_handover(
                turns_to_summarize=[{"role": "user", "content": "Clean up runtime \u2014 slice 2"}],
                summary="[CONTEXT COMPACTION]\n## Summary\nDone \u2014 part 1\n## Anchor Index\nignored",
                display_tokens=162000,
                session_id="sess-1",
                config=cfg,
                discord_port=fake,
                now_utc=datetime(2026, 11, 22, 20, 0, tzinfo=timezone.utc),
            )
            self.assertEqual(result.new_thread_id, "thread-new-2")
            self.assertIsNone(result.skipped_reason)
            self.assertIsNotNone(result.journal_file)
            assert result.journal_file is not None
            journal_text = result.journal_file.read_text(encoding="utf-8")
            self.assertNotIn("\u2014", journal_text)
            self.assertNotIn("\u2013", journal_text)
            self.assertNotIn("Anchor Index", journal_text)
            self.assertIn("Done - part 1", journal_text)

            self.assertEqual(len(fake.created_threads), 1)
            parent_id, new_title, starter = fake.created_threads[0]
            self.assertEqual(parent_id, "chan-info-fallback")
            self.assertEqual(new_title, "Refactor - Cleanup - Part 2")
            self.assertIn("<@user-101>", starter)
            self.assertEqual(fake.notices[0][0], "thread-old-9")
            self.assertIn("<#thread-new-2>", fake.notices[0][1])
            self.assertEqual(fake.archived, [("thread-old-9", 120.0)])

    def test_handover_skips_health_thread_and_missing_info_channel(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            db_path = root / "state.db"
            _seed_sessions_db(
                db_path,
                (
                    "sess-health",
                    "discord",
                    "thread-h",
                    "thread-h",
                    "Daily Workout Log",
                    json.dumps({"chat_name": "Guild / health / Daily Workout Log", "parent_chat_id": "chan-health"}),
                ),
            )
            fake = FakeDiscordAdapter()
            cfg = HandoverConfig(vault_root=None, state_db=db_path, info_channel_id="", user_id="")
            res = run_pre_compress_handover(
                turns_to_summarize=[], summary="ok", session_id="sess-health", config=cfg, discord_port=fake
            )
            self.assertEqual(res.skipped_reason, "entity_thread_skipped")
            self.assertEqual(fake.created_threads, [])

            # No parent channel and unset DISCORD_CHANNEL_INFO_ID fails cleanly without hardcoded ID.
            self.assertIsNone(resolve_parent_channel_id({"parent_chat_id": "t1"}, "t1", "t1", ""))
            self.assertEqual(next_part_title("Build - Part 2"), "Build - Part 3")

    def test_urllib_adapter_forum_retry_and_custom_user_agent(self) -> None:
        with self.assertRaises(ValueError):
            UrllibDiscordAdapter("")

        adapter = UrllibDiscordAdapter("bot-tok")
        seen_headers: list[dict[str, str]] = []
        seen_bodies: list[dict[str, object]] = []
        http_err = urllib.error.HTTPError(
            "https://discord.com/api/v10/channels/c1/threads",
            400,
            "Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"message": "Forum channels require no type field"}'),
        )
        ok_resp = MagicMock()
        ok_resp.__enter__.return_value = ok_resp
        ok_resp.read.return_value = b'{"id": "new-forum-thread"}'

        def fake_urlopen(req, timeout=15):
            seen_headers.append(dict(req.header_items()))
            seen_bodies.append(json.loads(req.data.decode("utf-8")))
            if len(seen_bodies) == 1:
                raise http_err
            return ok_resp

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            thread_id = adapter.create_thread("c1", "Topic - Part 2", "hello")

        self.assertEqual(thread_id, "new-forum-thread")
        self.assertIn("type", seen_bodies[0])
        self.assertNotIn("type", seen_bodies[1])
        self.assertEqual(seen_headers[0]["User-agent"], "Yggdrasil-Odin/1.0 (DiscordBot)")

    def test_inflight_replay_suppression_rules(self) -> None:
        self.assertTrue(should_suppress_reappend_text("Please wrap up this session"))
        self.assertTrue(should_suppress_reappend_text("[STILL IN PROGRESS - resume]"))
        self.assertFalse(should_suppress_reappend_text("Implement Slice 02"))

        answered_with_trailing_tool = [
            {"role": "user", "content": "Check status"},
            {"role": "assistant", "content": "All services are healthy."},
            {"role": "tool", "content": "background cron note"},
        ]
        self.assertTrue(should_suppress_inflight_replay(answered_with_trailing_tool, 0))

        mid_tool_call = [
            {"role": "user", "content": "Check status"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        ]
        self.assertFalse(should_suppress_inflight_replay(mid_tool_call, 0))


if __name__ == "__main__":
    unittest.main()
