#!/usr/bin/env python3
"""test_mimir_code_graph.py - Comprehensive Unit Tests for Mimir Cross-Repo Code Graph & Symbol Navigator."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
