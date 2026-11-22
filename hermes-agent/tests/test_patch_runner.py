#!/usr/bin/env python3
"""Direct interface unit tests for patches.patch_runner and patches.specs."""

from __future__ import annotations

import json
from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
