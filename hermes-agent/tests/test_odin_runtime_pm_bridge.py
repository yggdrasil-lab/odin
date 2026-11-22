#!/usr/bin/env python3
"""Direct interface unit tests for odin_runtime.pm_venv_bridge."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
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

    def test_pre_imported_bridge_survives_sys_path_clear_with_temp_pm_venv(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root, base_site, selected = _setup_temp_pm_venv(Path(tmpdir))
            probe = (
                "import json, os, site, sys; from pathlib import Path; "
                "project_root, selected, base_site = map(Path, sys.argv[1:4]); "
                "from odin_runtime.pm_venv_bridge import link_base_site, merged_pythonpath; "
                "sys.path[:] = [e for e in sys.path if Path(e).name not in ('site-packages', 'dist-packages') "
                "and Path(e).resolve() != project_root.resolve()]; "
                "link_base_site(selected, base_site); site.addsitedir(str(selected)); site.addsitedir(str(base_site)); "
                "sys.path[:], os.environ['PYTHONPATH'] = merged_pythonpath(project_root, selected, sys.path, base_site=base_site); "
                "import odin_runtime; "
                "print(json.dumps({'sys_path': sys.path, 'pythonpath': os.environ['PYTHONPATH']}))"
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join([str(project_root), str(base_site)]),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            res = subprocess.run(
                [sys.executable, "-c", probe, str(project_root), str(selected), str(base_site)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            self.assertEqual((selected / PTH_FILENAME).read_text(encoding="utf-8"), f"{base_site}\n")
            expected = [str(project_root.resolve()), str(selected), str(base_site)]
            self.assertEqual(payload["sys_path"][:3], expected)
            self.assertEqual(payload["pythonpath"].split(os.pathsep), expected)


def _setup_temp_pm_venv(root: Path) -> tuple[Path, Path, Path]:
    project_root = root / "hermes"
    base_site = project_root / ".venv" / "lib" / "python3.14" / "site-packages"
    install_key = hashlib.sha256(str(project_root.resolve()).encode("utf-8")).hexdigest()[:16]
    gen_dir = root / "home" / "installs" / install_key / "environments" / "gen1"
    selected = gen_dir / "lib" / "python3.14" / "site-packages"
    base_site.mkdir(parents=True)
    selected.mkdir(parents=True)
    (gen_dir / "pyvenv.cfg").write_text("version = 3.14.9\n", encoding="utf-8")
    (gen_dir.parents[1] / "facts.json").write_text(
        json.dumps({"packages": {"venv": {"environment": str(gen_dir)}}}),
        encoding="utf-8",
    )
    shutil.copytree(HERMES_AGENT_DIR / "odin_runtime", base_site / "odin_runtime")
    return project_root, base_site, selected


if __name__ == "__main__":
    unittest.main()
