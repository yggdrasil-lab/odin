#!/usr/bin/env python3
"""Direct interface unit tests for odin_runtime.pm_venv_bridge."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

HERMES_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

from odin_runtime.pm_venv_bridge import (  # noqa: E402
    PTH_FILENAME,
    bridge_stamp_digest,
    link_base_site,
    merged_pythonpath,
)


class TestOdinRuntimePmBridge(unittest.TestCase):
    """Tests for PM base-venv .pth linking, PYTHONPATH ordering, and stamp invalidation."""

    def test_link_base_site_writes_updates_and_skips_same_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base_site = root / "base" / "site-packages"
            selected_site = root / "gen1" / "site-packages"
            base_site.mkdir(parents=True)
            selected_site.mkdir(parents=True)

            pth = link_base_site(selected_site, base_site)
            self.assertEqual(pth, selected_site / PTH_FILENAME)
            self.assertEqual(pth.read_text(encoding="utf-8"), f"{base_site}\n")

            # Stale content is overwritten cleanly.
            pth.write_text("/stale/site-packages\n", encoding="utf-8")
            link_base_site(selected_site, base_site)
            self.assertEqual(pth.read_text(encoding="utf-8"), f"{base_site}\n")

            # Same directory or missing directory returns None without touching files.
            self.assertIsNone(link_base_site(base_site, base_site))
            self.assertIsNone(link_base_site(selected_site, root / "missing"))

    def test_link_base_site_logs_warning_on_os_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base_site = root / "base"
            selected_site = root / "selected"
            base_site.mkdir()
            selected_site.mkdir()
            with (
                patch.object(Path, "write_text", side_effect=OSError("read-only")),
                self.assertLogs("odin_runtime.pm_venv_bridge", level="WARNING"),
            ):
                self.assertIsNone(link_base_site(selected_site, base_site))

    def test_merged_pythonpath_orders_and_deduplicates_entries(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            project = root / "hermes"
            selected = root / "gen1" / "site-packages"
            base = root / "base" / "site-packages"
            extra = root / "other"
            for d in (project, selected, base, extra):
                d.mkdir(parents=True)

            new_sys_path, py_env = merged_pythonpath(
                project,
                selected,
                [str(extra), str(selected), str(base)],
                base_site=base,
            )
            self.assertEqual(new_sys_path, [str(project.resolve()), str(selected), str(base), str(extra)])
            self.assertEqual(py_env, os.pathsep.join([str(project.resolve()), str(selected), str(base)]))

            no_sel_path, no_sel_env = merged_pythonpath(project, None, [str(extra)], base_site=base)
            self.assertEqual(no_sel_path, [str(project.resolve()), str(extra)])
            self.assertEqual(no_sel_env, str(project.resolve()))

    def test_bridge_stamp_digest_changes_when_environments_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir)
            (project / "pm").mkdir()
            env_py = project / "pm" / "environments.py"
            env_py.write_text("v1 = True\n", encoding="utf-8")
            digest1 = bridge_stamp_digest(project)

            env_py.write_text("v1 = False\n", encoding="utf-8")
            digest2 = bridge_stamp_digest(project)
            self.assertNotEqual(digest1, digest2)


if __name__ == "__main__":
    unittest.main()
