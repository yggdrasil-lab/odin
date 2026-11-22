#!/usr/bin/env python3
"""Direct interface unit tests for patches.patch_runner and patches.specs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

HERMES_AGENT_DIR = Path(__file__).resolve().parent.parent
PATCHES_DIR = HERMES_AGENT_DIR / "patches"
for path_entry in (HERMES_AGENT_DIR, PATCHES_DIR):
    if str(path_entry) not in sys.path:
        sys.path.insert(0, str(path_entry))

import patch_runner  # noqa: E402
from patch_runner import Hunk, PatchRunnerError, PatchSpec, PatchStatus, apply_all, audit_all  # noqa: E402
import specs as odin_specs  # noqa: E402


class TestPatchRunner(unittest.TestCase):
    """Tests for count==1 enforcement, idempotency, retired_probe, atomic syntax checks, and CLI."""

    def test_apply_is_idempotent_and_dry_run_does_not_mutate(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "target.py"
            original = "def greet():\n    return 'hi'\n"
            target.write_text(original, encoding="utf-8")
            spec = PatchSpec(
                id="sample",
                root_key="r",
                rel_path="target.py",
                marker="# odin-marker",
                hunks=(
                    Hunk(
                        label="greet",
                        anchor="    return 'hi'\n",
                        replacement="    # odin-marker\n    return 'hello'\n",
                    ),
                ),
            )
            dry_outcomes = apply_all((spec,), {"r": root}, dry_run=True)
            self.assertEqual(dry_outcomes[0].status, PatchStatus.PENDING)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

            applied = apply_all((spec,), {"r": root})
            self.assertEqual(applied[0].status, PatchStatus.APPLIED)
            self.assertTrue(applied[0].modified)
            after_first = target.read_text(encoding="utf-8")
            self.assertIn("# odin-marker", after_first)

            rerun = apply_all((spec,), {"r": root})
            self.assertEqual(rerun[0].status, PatchStatus.APPLIED)
            self.assertFalse(rerun[0].modified)
            self.assertEqual(target.read_text(encoding="utf-8"), after_first)

    def test_fails_loudly_on_missing_ambiguous_or_syntax_error_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "mod.py"
            dup_src = "x = 1\nx = 1\n"
            target.write_text(dup_src, encoding="utf-8")

            ambig = PatchSpec(
                id="dup",
                root_key="r",
                rel_path="mod.py",
                marker="# m",
                hunks=(Hunk(label="dup", anchor="x = 1\n", replacement="# m\nx = 2\n"),),
            )
            with self.assertRaises(PatchRunnerError):
                apply_all((ambig,), {"r": root})
            self.assertEqual(target.read_text(encoding="utf-8"), dup_src)

            bad_syntax = PatchSpec(
                id="bad",
                root_key="r",
                rel_path="mod.py",
                marker="# m",
                hunks=(Hunk(label="bad", anchor="x = 1\nx = 1\n", replacement="# m\ndef broken(:\n"),),
            )
            with self.assertRaises(PatchRunnerError):
                apply_all((bad_syntax,), {"r": root})
            self.assertEqual(target.read_text(encoding="utf-8"), dup_src)

    def test_retired_probe_policies_and_wrapped_indent_hunk(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            target = root / "loop.py"
            target.write_text(
                "def run():\n    if True:\n        x = 1\n    done = True\n",
                encoding="utf-8",
            )
            retired_spec = PatchSpec(
                id="ret",
                root_key="r",
                rel_path="loop.py",
                marker="# m",
                retired_probe="done = True",
                hunks=(Hunk(label="h", anchor="x = 1", replacement="# m\nx = 2"),),
            )
            with self.assertRaises(PatchRunnerError):
                apply_all((retired_spec,), {"r": root}, on_retired="fail")
            skipped = apply_all((retired_spec,), {"r": root}, on_retired="skip")
            self.assertEqual(skipped[0].status, PatchStatus.RETIRED_UPSTREAM)

            wrap_spec = PatchSpec(
                id="wrap",
                root_key="r",
                rel_path="loop.py",
                marker="# wrapped",
                hunks=(
                    Hunk(
                        label="wrap",
                        anchor="    if True:\n",
                        replacement="    # wrapped\n    while True:\n        if True:\n",
                        end_anchor="    done = True\n",
                        end_replacement="        break\n    done = True\n",
                        indent_delta=4,
                    ),
                ),
            )
            outcomes = apply_all((wrap_spec,), {"r": root})
            self.assertEqual(outcomes[0].status, PatchStatus.APPLIED)
            self.assertIn("            x = 1\n", target.read_text(encoding="utf-8"))

    def test_audit_cli_json_and_specs_clean_of_dashes_and_retired_targets(self) -> None:
        specs = odin_specs.get_specs()
        self.assertEqual(len(specs), 19)
        by_id = {s.id: s for s in specs}
        self.assertIn("pm_environments_bridge", by_id)
        self.assertIn("pm_packages_bridge", by_id)
        self.assertNotIn("fix_managed_worker_routed_env", by_id)
        self.assertIn(
            "# Phase 4: the summary exists, so archive what the compaction drops or shortens, then assemble.",
            by_id["pre_compress_handover"].hunks[-1].anchor,
        )
        self.assertIn(
            "store = authority_persistence(authority)",
            by_id["startup_api_media_sweep_offload"].hunks[0].anchor,
        )
        raw_specs_text = (PATCHES_DIR / "specs.py").read_text(encoding="utf-8")
        self.assertNotIn("hermes_memory_provider", raw_specs_text)
        self.assertNotIn("\u2014", raw_specs_text)
        self.assertNotIn("\u2013", raw_specs_text)

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for rel in ("a.py",):
                (root / rel).write_text("x = 1\n", encoding="utf-8")
            out = audit_all(
                (
                    PatchSpec(
                        id="ok",
                        root_key="r",
                        rel_path="a.py",
                        marker="# m",
                        hunks=(Hunk(label="h", anchor="x = 1\n", replacement="# m\nx = 2\n"),),
                    ),
                ),
                {"r": root},
            )
            self.assertEqual(out[0].status, PatchStatus.PENDING)
            serialized = json.loads(patch_runner._format_outcomes(out, True))
            self.assertEqual(serialized[0]["status"], "PENDING")

    def test_pm_environments_spec_imports_bridge_before_sys_path_clear(self) -> None:
        by_id = {s.id: s for s in odin_specs.get_specs()}
        pm_spec = by_id["pm_environments_bridge"]
        act_hunk = next(h for h in pm_spec.hunks if h.label == "activate_dependencies")
        import_pos = act_hunk.replacement.index("from odin_runtime.pm_venv_bridge import")
        clear_pos = act_hunk.replacement.index("sys.path[:] = [entry for entry in sys.path")
        self.assertLess(import_pos, clear_pos)

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root, base_site, selected = _setup_patched_pm_tree(Path(tmpdir), pm_spec)
            probe = (
                "import json, os, pathlib, sys; "
                "from pm.environments import activate_dependencies; "
                "root = pathlib.Path(sys.argv[1]); "
                "activate_dependencies(root); "
                "print(json.dumps({'sys_path': sys.path, 'pythonpath': os.environ['PYTHONPATH']}))"
            )
            env = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": os.pathsep.join([str(project_root), str(base_site)]),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            res = subprocess.run(
                [sys.executable, "-c", probe, str(project_root)],
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            payload = json.loads(res.stdout)
            pth_file = selected / "odin-base-site.pth"
            self.assertTrue(pth_file.is_file())
            self.assertEqual(pth_file.read_text(encoding="utf-8"), f"{base_site}\n")
            self.assertIn(str(base_site), payload["sys_path"])
            self.assertIn(str(base_site), payload["pythonpath"].split(os.pathsep))


def _setup_patched_pm_tree(tmp_root: Path, pm_spec: PatchSpec) -> tuple[Path, Path, Path]:
    project_root = tmp_root / "hermes"
    base_site = project_root / ".venv" / "lib" / "python3.14" / "site-packages"
    selected = project_root / "installs" / "gen1" / "lib" / "python3.14" / "site-packages"
    (project_root / "pm").mkdir(parents=True)
    selected.mkdir(parents=True)
    shutil.copytree(HERMES_AGENT_DIR / "odin_runtime", base_site / "odin_runtime")
    (project_root / "pm" / "__init__.py").write_text("", encoding="utf-8")
    (project_root / "pm" / "environments.py").write_text(
        _FAKE_PM_ENVIRONMENTS_SRC.format(base_site=str(base_site), selected=str(selected)),
        encoding="utf-8",
    )
    outcomes = apply_all((pm_spec,), {"hermes": project_root})
    assert outcomes[0].status == PatchStatus.APPLIED
    return project_root, base_site, selected


_FAKE_PM_ENVIRONMENTS_SRC = """from __future__ import annotations
import os
from pathlib import Path
import sys

_BASE_SITE = Path({base_site!r})
_SELECTED = Path({selected!r})

def base_venv(project_root: Path) -> Path:
    return project_root / ".venv"

def site_packages(venv: Path) -> Path:
    return _SELECTED if venv.name == "gen1" else _BASE_SITE

def committed_venv(project_root: Path) -> Path | None:
    return _SELECTED.parents[2]

def activate_dependencies(project_root: Path) -> None:
    selected = _SELECTED
    import site

    sys.path[:] = [entry for entry in sys.path
                   if Path(entry).name not in ("site-packages", "dist-packages")
                   and Path(entry).resolve() != project_root.resolve()]
    # uv editable members are activated by .pth files, not by sys.path alone.
    site.addsitedir(str(selected))
    sys.path[:] = [str(project_root.resolve()), str(selected),
                   *[entry for entry in sys.path if Path(entry).resolve() != selected.resolve()]]
    os.environ["PYTHONPATH"] = os.pathsep.join([str(project_root.resolve()), str(selected)])

def activation_environment(project_root: Path) -> dict[str, str]:
    env = dict(os.environ)
    environment = committed_venv(project_root)
    env["PYTHONPATH"] = os.pathsep.join([str(project_root.resolve()),
                                         *([str(site_packages(environment))] if environment else [])])
    return env
"""


if __name__ == "__main__":
    unittest.main()
