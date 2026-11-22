#!/usr/bin/env python3
"""Unit tests for mnemosyne-dashboard/patch_sqlite_vec.py."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ODIN_ROOT = Path(__file__).resolve().parents[2]
DASHBOARD_DIR = ODIN_ROOT / "mnemosyne-dashboard"
if str(DASHBOARD_DIR) not in sys.path:
    sys.path.insert(0, str(DASHBOARD_DIR))

import patch_sqlite_vec  # noqa: E402


def _build_dashboard_core_fixture(*, include_import: bool = True) -> str:
    imp = "import sqlite3\n" if include_import else ""
    return (
        "from __future__ import annotations\n"
        f"{imp}"
        "from pathlib import Path\n\n"
        "class DashboardStore:\n"
        "    def __init__(self, db_path: Path):\n"
        "        self.db_path = db_path\n\n"
        "    def connect(self) -> sqlite3.Connection:\n"
        '        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)\n\n'
        "    def connect_rw(self) -> sqlite3.Connection:\n"
        "        return sqlite3.connect(str(self.db_path), timeout=10)\n"
    )


class TestPatchSqliteVec(unittest.TestCase):
    """Tests for single logged sqlite_vec hook injection and fail-loud behavior."""

    def test_patches_idempotently_with_single_hook_and_zero_silent_pass(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "dashboard_core.py"
            target.write_text(_build_dashboard_core_fixture(include_import=True), encoding="utf-8")

            self.assertEqual(patch_sqlite_vec.main(["--target", str(target)]), 0)
            after_first = target.read_text(encoding="utf-8")
            self.assertIn(patch_sqlite_vec.MARKER, after_first)
            self.assertEqual(after_first.count("sqlite_vec.load"), 1)
            self.assertNotIn("except Exception:\n            pass", after_first)
            self.assertNotIn("except Exception:\n        pass", after_first)

            self.assertEqual(patch_sqlite_vec.main(["--target", str(target)]), 0)
            self.assertEqual(target.read_text(encoding="utf-8"), after_first)

    def test_fails_loudly_when_sqlite3_import_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "dashboard_core.py"
            original = _build_dashboard_core_fixture(include_import=False)
            target.write_text(original, encoding="utf-8")

            self.assertEqual(patch_sqlite_vec.main(["--target", str(target)]), 1)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_hook_loads_sqlite_vec_and_logs_errors_loudly(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            db_path = tmp / "mnemosyne.db"
            sqlite3.connect(str(db_path)).close()

            target = tmp / "dashboard_core.py"
            target.write_text(_build_dashboard_core_fixture(include_import=True), encoding="utf-8")
            self.assertTrue(patch_sqlite_vec.patch_file(target))

            loaded_conns: list[sqlite3.Connection] = []
            fake_vec = types.ModuleType("sqlite_vec")
            fake_vec.load = lambda con: loaded_conns.append(con)  # type: ignore[attr-defined]

            orig_connect = sqlite3.connect
            mod_name = "test_patched_dashboard_core"
            try:
                with patch.dict(sys.modules, {"sqlite_vec": fake_vec}):
                    spec = importlib.util.spec_from_file_location(mod_name, str(target))
                    assert spec is not None and spec.loader is not None
                    mod = importlib.util.module_from_spec(spec)
                    sys.modules[mod_name] = mod
                    spec.loader.exec_module(mod)

                    store = mod.DashboardStore(db_path)
                    con_ro = store.connect()
                    con_ro.close()
                    con_rw = store.connect_rw()
                    con_rw.close()
                    self.assertEqual(len(loaded_conns), 2)

                    def boom(_con: sqlite3.Connection) -> None:
                        raise RuntimeError("vec load boom")

                    fake_vec.load = boom  # type: ignore[attr-defined]
                    with (
                        self.assertLogs(mod_name, level="ERROR") as logs,
                        self.assertRaises(RuntimeError),
                    ):
                        store.connect()
                    self.assertIn("Failed to load sqlite-vec extension", "\n".join(logs.output))
            finally:
                sqlite3.connect = orig_connect
                sys.modules.pop(mod_name, None)


if __name__ == "__main__":
    unittest.main()
