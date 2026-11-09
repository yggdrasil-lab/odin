#!/usr/bin/env python3
"""test_mnemosyne_consolidate_daemon.py - Unit tests for Autonomous Memory Consolidation & Dreaming Daemon."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Ensure hermes-agent scripts directory is on sys.path
SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "scripts"))
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import mnemosyne_consolidate_daemon as daemon  # noqa: E402


class TestMnemosyneConsolidateDaemon(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.db_path = self.temp_db.name
        self.temp_db.close()

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE working_memory (
                id TEXT PRIMARY KEY,
                content TEXT NOT NULL,
                source TEXT,
                timestamp TEXT,
                session_id TEXT DEFAULT 'default',
                importance REAL DEFAULT 0.5,
                metadata_json TEXT,
                veracity TEXT DEFAULT 'unknown',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                memory_type TEXT DEFAULT 'unknown',
                consolidated_at TEXT,
                recall_count INTEGER DEFAULT 0,
                last_recalled TIMESTAMP DEFAULT NULL,
                valid_until TIMESTAMP DEFAULT NULL,
                superseded_by TEXT DEFAULT NULL,
                scope TEXT DEFAULT 'global'
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE consolidated_facts (
                id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                confidence REAL DEFAULT 0.5,
                mention_count INTEGER DEFAULT 1,
                first_seen TEXT,
                last_seen TEXT,
                sources_json TEXT,
                veracity TEXT DEFAULT 'unknown',
                superseded_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        cur.execute(
            """
            CREATE TABLE graph_edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                weight REAL DEFAULT 1.0,
                timestamp TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.commit()
        conn.close()

    def tearDown(self) -> None:
        if os.path.isfile(self.db_path):
            try:
                os.remove(self.db_path)
            except OSError:
                pass

    def test_clean_llm_json_response(self) -> None:
        # 1. Clean markdown code fence
        raw_fence = "```json\n[{\"subject\": \"Hermes\", \"predicate\": \"uses\", \"object\": \"Odin\"}]\n```"
        parsed = daemon.clean_llm_json_response(raw_fence)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["subject"], "Hermes")

        # 2. Surrounding text with array
        raw_text = "Here are the extracted facts:\n[{\"subject\": \"A\", \"predicate\": \"B\", \"object\": \"C\"}]\nDone."
        parsed2 = daemon.clean_llm_json_response(raw_text)
        self.assertEqual(len(parsed2), 1)
        self.assertEqual(parsed2[0]["subject"], "A")

        # 3. Malformed JSON with regex fallback
        raw_malformed = "Subject: {\"subject\": \"Muninn\", \"predicate\": \"proxies\", \"object\": \"LiteLLM\"}"
        parsed3 = daemon.clean_llm_json_response(raw_malformed)
        self.assertEqual(len(parsed3), 1)
        self.assertEqual(parsed3[0]["subject"], "Muninn")

    def test_filter_valid_triple(self) -> None:
        # Valid triple
        valid = {"subject": "Kratos", "predicate": "tracks", "object": "workouts", "confidence": 0.9, "memory_id": "m1"}
        res = daemon.filter_valid_triple(valid)
        self.assertIsNotNone(res)
        self.assertEqual(res[0], "Kratos")
        self.assertEqual(res[3], 0.9)

        # Noise subjects
        self.assertIsNone(daemon.filter_valid_triple({"subject": "this", "predicate": "is", "object": "good"}))
        self.assertIsNone(daemon.filter_valid_triple({"subject": "everything", "predicate": "is", "object": "working"}))
        self.assertIsNone(daemon.filter_valid_triple({"subject": "it", "predicate": "fails", "object": "silently"}))

        # Self-referential
        self.assertIsNone(daemon.filter_valid_triple({"subject": "Kratos", "predicate": "is", "object": "kratos"}))

        # Empty fields
        self.assertIsNone(daemon.filter_valid_triple({"subject": "", "predicate": "uses", "object": "test"}))

    def test_generate_fact_id(self) -> None:
        id1 = daemon.generate_fact_id("Raymund", "prefers", "dark mode")
        id2 = daemon.generate_fact_id("raymund", "PREFERS", "Dark Mode")
        self.assertEqual(id1, id2)
        self.assertTrue(id1.startswith("cf_"))

    def test_batch_consolidation_pipeline(self) -> None:
        # Insert 3 test working memories
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO working_memory (id, content, timestamp, consolidated_at)
            VALUES
                ('wm_101', 'Raymund configured Prometheus and Grafana for homelab monitoring.', '2026-11-09T02:00:00Z', NULL),
                ('wm_102', 'Poseidon Browserless runs on port 3000 over aether-net overlay.', '2026-11-09T02:30:00Z', NULL),
                ('wm_103', 'Already consolidated memory.', '2026-11-08T12:00:00Z', '2026-11-08T15:00:00Z')
            """
        )
        conn.commit()
        conn.close()

        mock_llm_response = json.dumps([
            {
                "memory_id": "wm_101",
                "subject": "Raymund",
                "predicate": "configured",
                "object": "Prometheus",
                "confidence": 0.95,
            },
            {
                "memory_id": "wm_102",
                "subject": "Poseidon Browserless",
                "predicate": "runs_on",
                "object": "port 3000",
                "confidence": 0.9,
            },
        ])

        with patch.object(daemon.LLMCaller, "complete", return_value=mock_llm_response):
            report = daemon.consolidate_memory_batch(
                db_path=self.db_path,
                batch_size=10,
                chunk_size=5,
                dry_run=False,
            )

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["items_processed"], 2)
        self.assertEqual(report["triples_extracted"], 2)
        self.assertEqual(report["facts_created"], 2)
        self.assertEqual(report["facts_updated"], 0)
        self.assertGreaterEqual(report["edges_created"], 4)

        # Verify DB updates
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()

        # Memories wm_101 and wm_102 should have consolidated_at set
        cur.execute("SELECT id, consolidated_at FROM working_memory WHERE id IN ('wm_101', 'wm_102')")
        for row in cur.fetchall():
            self.assertIsNotNone(row[1], f"Memory {row[0]} was not marked consolidated_at")

        # Consolidated facts verification
        cur.execute("SELECT id, subject, predicate, object, confidence, sources_json FROM consolidated_facts")
        facts = cur.fetchall()
        self.assertEqual(len(facts), 2)
        subjects = {f[1] for f in facts}
        self.assertIn("Raymund", subjects)
        self.assertIn("Poseidon Browserless", subjects)

        # Graph edges verification (forward + inverse + mentions)
        cur.execute("SELECT source, target, edge_type, weight FROM graph_edges")
        edges = cur.fetchall()
        self.assertGreaterEqual(len(edges), 4)

        conn.close()

        # Now run a second pass with a recurring mention to test fact updating and mention_count
        mock_llm_response_update = json.dumps([
            {
                "memory_id": "wm_104",
                "subject": "Raymund",
                "predicate": "configured",
                "object": "Prometheus",
                "confidence": 0.98,
            }
        ])

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO working_memory (id, content, timestamp, consolidated_at) VALUES ('wm_104', 'Raymund re-verified Prometheus alerts.', '2026-11-09T03:00:00Z', NULL)"
        )
        conn.commit()
        conn.close()

        with patch.object(daemon.LLMCaller, "complete", return_value=mock_llm_response_update):
            report2 = daemon.consolidate_memory_batch(
                db_path=self.db_path,
                batch_size=10,
                chunk_size=5,
                dry_run=False,
            )

        self.assertEqual(report2["facts_created"], 0)
        self.assertEqual(report2["facts_updated"], 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("SELECT mention_count, confidence, sources_json FROM consolidated_facts WHERE subject = 'Raymund'")
        fact_row = cur.fetchone()
        self.assertEqual(fact_row[0], 2)  # Mention count incremented
        self.assertEqual(fact_row[1], 0.98)  # Confidence updated to max
        sources = json.loads(fact_row[2])
        self.assertIn("wm_101", sources)
        self.assertIn("wm_104", sources)
        conn.close()

    def test_dry_run_mode(self) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO working_memory (id, content, timestamp, consolidated_at) VALUES ('wm_dry', 'Dry run test memory', '2026-11-09T01:00:00Z', NULL)"
        )
        conn.commit()
        conn.close()

        mock_resp = json.dumps([{"subject": "DryRun", "predicate": "tests", "object": "Flag", "confidence": 0.9}])

        with patch.object(daemon.LLMCaller, "complete", return_value=mock_resp):
            report = daemon.consolidate_memory_batch(
                db_path=self.db_path,
                batch_size=5,
                dry_run=True,
            )

        self.assertEqual(report["mode"], "dry_run")
        self.assertEqual(report["triples_extracted"], 1)

        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        # Ensure consolidated_at is still NULL
        cur.execute("SELECT consolidated_at FROM working_memory WHERE id = 'wm_dry'")
        self.assertIsNone(cur.fetchone()[0])

        # Ensure no facts or edges committed
        cur.execute("SELECT count(*) FROM consolidated_facts")
        self.assertEqual(cur.fetchone()[0], 0)
        cur.execute("SELECT count(*) FROM graph_edges")
        self.assertEqual(cur.fetchone()[0], 0)
        conn.close()


if __name__ == "__main__":
    unittest.main()
