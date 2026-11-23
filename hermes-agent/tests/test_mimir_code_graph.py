#!/usr/bin/env python3
"""test_mimir_code_graph.py - Comprehensive Unit Tests for Mimir Cross-Repo Code Graph & Symbol Navigator."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

# Add scripts directory to path
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from mimir_code_graph import (
    extract_compose_symbols,
    extract_markdown_symbols,
    extract_python_symbols,
    extract_typescript_symbols,
    get_index_stats,
    init_mimir_db,
    index_all,
    index_repository,
    main,
    refresh_index_if_stale,
    resolve_max_index_age_seconds,
    resolve_mimir_db_path,
    resolve_vault_path,
    search_symbols,
)


class TestMimirCodeGraph(unittest.TestCase):
    """Test suite for Mimir Cross-Repo Code Graph (Pillar 4)."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.temp_dir, ignore_errors=True))

    def test_extract_python_symbols(self) -> None:
        """Verify AST extraction of functions, classes, and FastAPI route decorators."""
        sample_code = '''
from fastapi import APIRouter

router = APIRouter()

class WorkoutEngine:
    """Workout engine for prescribing progressive volume."""
    def __init__(self, db):
        self.db = db

@router.post("/v1/sessions")
def record_workout_session(user_id: str, routine_id: str):
    """Log a completed athletic workout session."""
    return {"status": "ok"}
'''
        symbols = extract_python_symbols(sample_code, "api/app/routers/log.py", "kratos")
        names = {s.name: s for s in symbols}

        self.assertIn("WorkoutEngine", names)
        self.assertEqual(names["WorkoutEngine"].kind, "class")
        self.assertIn("progressive volume", names["WorkoutEngine"].docstring.lower())

        self.assertIn("record_workout_session", names)
        self.assertEqual(names["record_workout_session"].kind, "function")
        self.assertIn("def record_workout_session(user_id, routine_id)", names["record_workout_session"].signature)

        self.assertIn("POST /v1/sessions", names)
        self.assertEqual(names["POST /v1/sessions"].kind, "route")

    def test_extract_typescript_symbols(self) -> None:
        """Verify syntactic extraction of React components, interfaces, and types."""
        sample_ts = '''
import React from 'react';

export interface TactileKeypadProps {
    value: string;
    onChange: (val: string) => void;
}

export type ThemeVariant = 'blue' | 'amber' | 'emerald';

export const TactileKeypad: React.FC<TactileKeypadProps> = ({ value, onChange }) => {
    return <div className="keypad" />;
};

export function formatCurrency(amount: number): string {
    return `$${amount.toFixed(2)}`;
}
'''
        symbols = extract_typescript_symbols(sample_ts, "src/components/TactileKeypad.tsx", "ui")
        names = {s.name: s for s in symbols}

        self.assertIn("TactileKeypadProps", names)
        self.assertEqual(names["TactileKeypadProps"].kind, "interface")

        self.assertIn("ThemeVariant", names)
        self.assertEqual(names["ThemeVariant"].kind, "type")

        self.assertIn("TactileKeypad", names)
        self.assertEqual(names["TactileKeypad"].kind, "component")

        self.assertIn("formatCurrency", names)
        self.assertEqual(names["formatCurrency"].kind, "function")

    def test_extract_markdown_symbols(self) -> None:
        """Verify extraction of Obsidian note titles and headers."""
        sample_md = """# Odin Stack Architecture

Authoritative documentation for Odin LLM stack and services.

## 1. Executive Summary
Overview of services.
"""
        symbols = extract_markdown_symbols(sample_md, "Areas/90-Infrastructure/Odin/Odin Stack.md", "vault-areas")
        self.assertEqual(len(symbols), 1)
        self.assertEqual(symbols[0].name, "Odin Stack Architecture")
        self.assertEqual(symbols[0].kind, "note")

    def test_extract_compose_symbols(self) -> None:
        """Verify extraction of Swarm services from docker-compose.yml."""
        sample_compose = """
services:
  huginn-gateway:
    image: hermes-agent:odin
    networks:
      - aether-net

  muninn:
    image: litellm:latest
"""
        symbols = extract_compose_symbols(sample_compose, "Forge/odin/docker-compose.yml", "odin")
        names = [s.name for s in symbols]
        self.assertIn("huginn-gateway", names)
        self.assertIn("muninn", names)
        self.assertEqual(symbols[0].kind, "service")

    def test_index_and_incremental_search(self) -> None:
        """Verify full repository indexing, incremental change detection, and FTS5 search."""
        vault = Path(self.temp_dir)
        repo_dir = vault / "Forge" / "plutus" / "api"
        repo_dir.mkdir(parents=True)

        parser_py = repo_dir / "statement_parser.py"
        parser_py.write_text(
            """
class DBS_Statement_Parser:
    \"\"\"Parser for DBS Bank statements.\"\"\"
    def parse(self, text):
        return []
""",
            encoding="utf-8",
        )

        db_path = vault / "mimir_test.db"
        conn = init_mimir_db(db_path)

        # 1. Initial indexing
        f_count, s_count = index_repository(conn, repo_dir, "plutus", vault)
        self.assertEqual(f_count, 1)
        self.assertEqual(s_count, 2)  # 1 class + 1 method

        # 2. Search exact and full-text
        matches = search_symbols(db_path, "DBS_Statement_Parser", exact=True)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["name"], "DBS_Statement_Parser")
        self.assertEqual(matches[0]["repo"], "plutus")

        matches_fts = search_symbols(db_path, "DBS Bank")
        self.assertTrue(len(matches_fts) >= 1)
        self.assertEqual(matches_fts[0]["name"], "DBS_Statement_Parser")

        # 3. Incremental test: file unchanged -> should index 0 files
        f_count_inc, s_count_inc = index_repository(conn, repo_dir, "plutus", vault, force=False)
        self.assertEqual(f_count_inc, 0)
        self.assertEqual(s_count_inc, 0)

        # 4. Modify file -> should detect change and update index
        parser_py.write_text(
            """
class DBS_Statement_Parser:
    \"\"\"Updated parser for DBS and OCBC.\"\"\"
    def parse(self, text):
        return []
    def parse_csv(self, content):
        return []
""",
            encoding="utf-8",
        )
        f_count_mod, s_count_mod = index_repository(conn, repo_dir, "plutus", vault, force=False)
        self.assertEqual(f_count_mod, 1)
        self.assertEqual(s_count_mod, 3)

        conn.close()

    def test_get_index_stats(self) -> None:
        """Verify index statistics aggregation by repository and kind."""
        vault = Path(self.temp_dir)
        repo_dir = vault / "Forge" / "midas"
        repo_dir.mkdir(parents=True)
        (repo_dir / "modal.tsx").write_text("export const QuickLogModal = () => null;", encoding="utf-8")

        db_path = vault / "stats_test.db"
        conn = init_mimir_db(db_path)
        index_repository(conn, repo_dir, "midas", vault)
        conn.close()

        stats = get_index_stats(db_path)
        self.assertEqual(stats["status"], "ready")
        self.assertEqual(stats["total_symbols"], 1)
        self.assertEqual(stats["total_files"], 1)
        self.assertIn("midas", stats["by_repo"])
        self.assertIn("component", stats["by_kind"])

    def test_refresh_index_if_stale_triggers_over_24h_and_skips_when_fresh(self) -> None:
        """Verify stale DB (>24h) auto-refreshes incrementally and fresh DB (<24h) skips."""
        vault = Path(self.temp_dir)
        repo_dir = vault / "Forge" / "kratos"
        repo_dir.mkdir(parents=True)
        (repo_dir / "engine.py").write_text("def initial_symbol():\n    return 1\n", encoding="utf-8")

        db_path = vault / "mimir_symbols.db"
        index_all(vault, db_path, force=False)

        # Fresh DB (<24h) should skip re-indexing even when a new file exists
        (repo_dir / "added.py").write_text("def fresh_skipped_symbol():\n    return 2\n", encoding="utf-8")
        now = time.time()
        os.utime(db_path, (now - 3600, now - 3600))  # 1 hour old
        self.assertFalse(refresh_index_if_stale(vault, db_path, now=now))
        self.assertEqual(search_symbols(db_path, "fresh_skipped_symbol", exact=True), [])

        # Stale DB (>24h) should trigger incremental re-index and pick up new symbol
        stale_mtime = now - (25 * 3600)
        os.utime(db_path, (stale_mtime, stale_mtime))
        self.assertTrue(refresh_index_if_stale(vault, db_path, now=now))

        matches = search_symbols(db_path, "fresh_skipped_symbol", exact=True)
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["repo"], "kratos")

        # DB mtime must be updated even when subsequent check finds zero file diffs
        os.utime(db_path, (stale_mtime, stale_mtime))
        self.assertTrue(refresh_index_if_stale(vault, db_path, now=now))
        self.assertGreater(db_path.stat().st_mtime, stale_mtime)

    def test_refresh_index_if_stale_env_and_failsafe(self) -> None:
        """Verify MIMIR_MAX_INDEX_AGE_HOURS config and fail-safe handling on read-only/errors."""
        vault = Path(self.temp_dir)
        db_path = vault / "missing.db"
        self.assertFalse(refresh_index_if_stale(vault, db_path))

        with patch.dict(os.environ, {"MIMIR_MAX_INDEX_AGE_HOURS": "2"}, clear=False):
            self.assertEqual(resolve_max_index_age_seconds(), 7200.0)
        with patch.dict(os.environ, {"MIMIR_MAX_INDEX_AGE_HOURS": "0"}, clear=False):
            self.assertIsNone(resolve_max_index_age_seconds())
        with patch.dict(os.environ, {"MIMIR_MAX_INDEX_AGE_HOURS": "not-a-number"}, clear=False):
            self.assertEqual(resolve_max_index_age_seconds(), 86400.0)

        repo_dir = vault / "Forge" / "odin"
        repo_dir.mkdir(parents=True)
        (repo_dir / "mod.py").write_text("def resilient_query_symbol():\n    pass\n", encoding="utf-8")
        db_path = vault / "ro.db"
        index_all(vault, db_path, force=False)
        stale_mtime = time.time() - (26 * 3600)
        os.utime(db_path, (stale_mtime, stale_mtime))

        # Read-only DB must skip without crashing
        with patch("os.access", return_value=False):
            self.assertFalse(refresh_index_if_stale(vault, db_path))

        # Indexing exception must be swallowed safely
        with patch("mimir_code_graph.index_all", side_effect=RuntimeError("disk full")):
            self.assertFalse(refresh_index_if_stale(vault, db_path))

    def test_cli_query_auto_refreshes_unless_disabled(self) -> None:
        """Verify CLI symbol queries auto-refresh stale DB unless --no-auto-refresh or --index is used."""
        vault = Path(self.temp_dir)
        repo_dir = vault / "Forge" / "iris"
        repo_dir.mkdir(parents=True)
        (repo_dir / "base.py").write_text("def base_handler():\n    pass\n", encoding="utf-8")
        db_path = vault / "cli_mimir.db"
        index_all(vault, db_path, force=False)

        (repo_dir / "new_route.py").write_text("def newly_added_cli_symbol():\n    pass\n", encoding="utf-8")
        stale_mtime = time.time() - (30 * 3600)
        os.utime(db_path, (stale_mtime, stale_mtime))

        env = {"VAULT": str(vault), "MIMIR_DB_PATH": str(db_path)}
        with patch.dict(os.environ, env, clear=False):
            with patch.object(sys, "argv", ["mimir-code", "newly_added_cli_symbol", "--no-auto-refresh", "--json"]):
                self.assertEqual(main(), 0)
            self.assertEqual(search_symbols(db_path, "newly_added_cli_symbol", exact=True), [])

            with patch.object(sys, "argv", ["mimir-code", "newly_added_cli_symbol", "--json"]):
                self.assertEqual(main(), 0)
            self.assertEqual(len(search_symbols(db_path, "newly_added_cli_symbol", exact=True)), 1)

            with patch("mimir_code_graph.refresh_index_if_stale") as mock_refresh:
                with patch.object(sys, "argv", ["mimir-code", "--index", "--json"]):
                    self.assertEqual(main(), 0)
                mock_refresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
