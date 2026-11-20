#!/usr/bin/env python3
"""Unit tests for build-time ContextCompressor pre-compress wrapup patch."""

from __future__ import annotations

import ast
import py_compile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"
PATCH_SCRIPT = PATCHES_DIR / "hook_pre_compress_wrapup.py"

if str(PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(PATCHES_DIR))

import hook_pre_compress_wrapup as hook_patch  # noqa: E402


def _build_context_compressor_fixture(phase4_mode: str = "v2") -> str:
    """Construct a minimal ContextCompressor source matching upstream anchors."""
    if phase4_mode == "v2":
        phase4_block = (
            "        if not summary:\n"
            "            summary = self._fallback_summary_for_window(\n"
            "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
            "            )\n"
            "        # Phase 4: the summary exists, so archive what the compaction drops or shortens, then assemble.\n"
            "        messages, pruned_count = self._commit_window(canonical_messages, plan, pruned_count, compress_start, compress_end)\n"
            "        compressed = self._assemble_with_archive_list(messages, compress_start, compress_end, scan, summary)\n"
        )
    elif phase4_mode == "legacy":
        phase4_block = (
            "        if not summary:\n"
            "            summary = self._fallback_summary_for_window(\n"
            "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
            "            )\n"
            "        # Phase 4: Assemble compressed message list\n"
            "        compressed = self._assemble_compressed(messages, compress_start, compress_end, scan, summary)\n"
        )
    elif phase4_mode == "duplicate_v2":
        single_v2 = (
            "        if not summary:\n"
            "            summary = self._fallback_summary_for_window(\n"
            "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
            "            )\n"
            "        # Phase 4: the summary exists, so archive what the compaction drops or shortens, then assemble.\n"
            "        messages, pruned_count = self._commit_window(canonical_messages, plan, pruned_count, compress_start, compress_end)\n"
            "        compressed = self._assemble_with_archive_list(messages, compress_start, compress_end, scan, summary)\n"
        )
        phase4_block = single_v2 + single_v2
    elif phase4_mode == "missing":
        phase4_block = (
            "        if not summary:\n"
            "            summary = self._fallback_summary_for_window(\n"
            "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
            "            )\n"
            "        # Phase 4: Unexpected upstream signature\n"
            "        compressed = self._unexpected_assemble(messages)\n"
        )
    else:
        raise ValueError(f"Unsupported phase4_mode: {phase4_mode}")

    return (
        'from __future__ import annotations\n'
        'import logging\n'
        'from typing import Any\n\n'
        'logger = logging.getLogger(__name__)\n'
        '_BACKGROUND_PROCESS_NOTIFICATION_PREFIX = "[Background"\n'
        'TODO_INJECTION_HEADER = "[Todo]"\n'
        '_LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX = "[Dropped]"\n'
        '_SYNTHETIC_USER_ROW_PREFIXES = (\n'
        '    "[System:", "[CONTEXT",\n'
        '    "[Planning state preserved", "[ASYNC DELEGATION", "[OUT-OF-BAND", "Cronjob Response:",\n'
        ')\n\n\n'
        'def _content_text_for_contains(content: Any) -> str:\n'
        '    return content if isinstance(content, str) else ""\n\n\n'
        'def _restated_request_text(content: Any) -> str:\n'
        '    return _content_text_for_contains(content)\n\n\n'
        'class ContextCompressor:\n'
        '    @classmethod\n'
        '    def _is_synthetic_compression_user_turn(cls, message: Any) -> bool:\n'
        '        text = _content_text_for_contains(message.get("content")).strip()\n'
        '        return text in {\n'
        '            "stub",\n'
        '        } or text.startswith((\n'
        '            _BACKGROUND_PROCESS_NOTIFICATION_PREFIX, TODO_INJECTION_HEADER + "\\n", _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX,\n'
        '        ))\n\n'
        '    @classmethod\n'
        '    def _find_inflight_user_task(cls, messages: list[dict[str, Any]], last_user_idx: int) -> dict[str, Any] | None:\n'
        '        if last_user_idx < 0:\n'
        '            return None\n'
        '\n'
        '        for msg in reversed(messages[last_user_idx + 1:]):\n'
        '            if not isinstance(msg, dict) or msg.get("role") != "assistant":\n'
        '                # Trailing tool result (or anything else): still mid-task.\n'
        '                break\n'
        '            if msg.get("tool_calls"):\n'
        '                break\n'
        '            if _content_text_for_contains(msg.get("content")).strip():\n'
        '                # Final answer already delivered \u2014 replaying the ask would\n'
        '                # hand the model finished work as a fresh instruction.\n'
        '                return None\n'
        '            # Empty assistant row (a bare reasoning/stub turn): keep looking.\n'
        '        return messages[last_user_idx]\n\n'
        '    def _reappend_inflight_user_task(self, compressed: list[dict[str, Any]], inflight: dict[str, Any]) -> list[dict[str, Any]]:\n'
        '        # Keep any reply pointer: the restated task still needs its disambiguation.\n'
        '        task_text = _restated_request_text(inflight.get("content"))\n'
        '        if not task_text:\n'
        '            return compressed\n'
        '        return compressed + [{"role": "user", "content": task_text}]\n\n'
        '    def compress(self, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:\n'
        '        canonical_messages = list(messages)\n'
        '        plan = None\n'
        '        pruned_count = 0\n'
        '        compress_start = 0\n'
        '        compress_end = len(messages)\n'
        '        telemetry: dict[str, Any] = {}\n'
        '        turns_to_summarize = list(messages)\n'
        '        feasibility_skip = False\n'
        '        display_tokens = 1000\n'
        '        scan = None\n'
        '        summary = ""\n'
        f'{phase4_block}'
        '        return compressed\n\n'
        '    def _assemble_compressed(\n'
        '        self, messages: list[dict[str, Any]], compress_start: int, compress_end: int, scan: Any, summary: str,\n'
        '    ) -> list[dict[str, Any]]:\n'
        '        return list(messages)\n'
    )


def _run_patch(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(PATCH_SCRIPT), str(target)],
        capture_output=True,
        text=True,
        check=False,
    )


class TestHookPreCompressWrapup(unittest.TestCase):
    """Tests for Phase 4 V2/legacy patching, idempotency, and fail-loud guards."""

    def test_patches_phase4_v2_fixture_and_compiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "context_compressor.py"
            target.write_text(_build_context_compressor_fixture("v2"), encoding="utf-8")

            res = _run_patch(target)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            py_compile.compile(str(target), doraise=True)

            patched = target.read_text(encoding="utf-8")
            self.assertIn(hook_patch.MARKER, patched)
            self.assertIn(hook_patch.INFLIGHT_MARKER, patched)
            self.assertIn(hook_patch.SYNTHETIC_MARKER, patched)
            self.assertIn("def _trigger_odin_pre_compress_wrapup(", patched)

            fallback_idx = patched.index("summary = self._fallback_summary_for_window(")
            wrapup_idx = patched.index("self._trigger_odin_pre_compress_wrapup(")
            phase4_idx = patched.index("# Phase 4: the summary exists, so archive what the compaction drops or shortens, then assemble.")
            commit_idx = patched.index("messages, pruned_count = self._commit_window(")
            assemble_idx = patched.index("compressed = self._assemble_with_archive_list(")

            self.assertLess(fallback_idx, wrapup_idx)
            self.assertLess(wrapup_idx, phase4_idx)
            self.assertLess(phase4_idx, commit_idx)
            self.assertLess(commit_idx, assemble_idx)

            tree = ast.parse(patched)
            compressor_cls = next(
                node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "ContextCompressor"
            )
            method_names = {node.name for node in compressor_cls.body if isinstance(node, ast.FunctionDef)}
            self.assertIn("_trigger_odin_pre_compress_wrapup", method_names)
            self.assertIn("_trigger_odin_discord_thread_handover", method_names)

    def test_patches_legacy_phase4_fixture_and_compiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "context_compressor.py"
            target.write_text(_build_context_compressor_fixture("legacy"), encoding="utf-8")

            res = _run_patch(target)
            self.assertEqual(res.returncode, 0, msg=res.stderr)
            py_compile.compile(str(target), doraise=True)

            patched = target.read_text(encoding="utf-8")
            fallback_idx = patched.index("summary = self._fallback_summary_for_window(")
            wrapup_idx = patched.index("self._trigger_odin_pre_compress_wrapup(")
            phase4_idx = patched.index("# Phase 4: Assemble compressed message list")
            assemble_idx = patched.index("compressed = self._assemble_compressed(messages, compress_start, compress_end, scan, summary)")

            self.assertLess(fallback_idx, wrapup_idx)
            self.assertLess(wrapup_idx, phase4_idx)
            self.assertLess(phase4_idx, assemble_idx)

    def test_idempotent_on_rerun_for_v2_and_legacy(self) -> None:
        for mode in ("v2", "legacy"):
            with self.subTest(mode=mode):
                with tempfile.TemporaryDirectory() as tmpdir:
                    target = Path(tmpdir) / "context_compressor.py"
                    target.write_text(_build_context_compressor_fixture(mode), encoding="utf-8")

                    first = _run_patch(target)
                    self.assertEqual(first.returncode, 0, msg=first.stderr)
                    after_first = target.read_text(encoding="utf-8")

                    second = _run_patch(target)
                    self.assertEqual(second.returncode, 0, msg=second.stderr)
                    self.assertIn("already up to date", second.stdout)
                    self.assertEqual(target.read_text(encoding="utf-8"), after_first)
                    py_compile.compile(str(target), doraise=True)

    def test_fails_loudly_when_call_anchor_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "context_compressor.py"
            original = _build_context_compressor_fixture("missing")
            target.write_text(original, encoding="utf-8")

            res = _run_patch(target)
            self.assertEqual(res.returncode, 1)
            self.assertIn("CALL_ANCHOR not found", res.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_fails_loudly_when_call_anchor_count_not_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "context_compressor.py"
            original = _build_context_compressor_fixture("duplicate_v2")
            target.write_text(original, encoding="utf-8")

            res = _run_patch(target)
            self.assertEqual(res.returncode, 1)
            self.assertIn("expected 1 CALL_ANCHOR match", res.stderr)
            self.assertIn("found 2", res.stderr)
            self.assertEqual(target.read_text(encoding="utf-8"), original)


if __name__ == "__main__":
    unittest.main()
