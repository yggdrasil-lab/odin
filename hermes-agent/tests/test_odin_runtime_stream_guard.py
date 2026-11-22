#!/usr/bin/env python3
"""Direct interface unit tests for odin_runtime.agy_stream_guard."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

HERMES_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

from odin_runtime.agy_stream_guard import (  # noqa: E402
    UsageSnapshot,
    account_usage,
    is_output_cap,
    native_tool_decision,
    own_process_group_kwargs,
)


class TestOdinRuntimeStreamGuard(unittest.TestCase):
    """Tests for cached/thinking usage accounting, native-tool decisions, and output caps."""

    def test_account_usage_oneshot_folds_cached_and_thinking(self) -> None:
        snap = account_usage(
            {
                "input_tokens": 120,
                "output_tokens": 30,
                "cache_read_tokens": 80,
                "thinking_tokens": 45,
                "total_tokens": 150,
            }
        )
        self.assertIsInstance(snap, UsageSnapshot)
        self.assertEqual(snap.input_tokens, 120)
        self.assertEqual(snap.cached_tokens, 80)
        self.assertEqual(snap.prompt_tokens, 200)
        self.assertEqual(snap.output_tokens, 30)
        self.assertEqual(snap.thinking_tokens, 45)
        self.assertEqual(snap.total_tokens, 230)

    def test_account_usage_worker_cumulative_deltas_and_baseline_preservation(self) -> None:
        baseline: dict[str, int] = {}
        turn1 = account_usage(
            {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 5, "thinking_tokens": 12},
            baseline,
        )
        self.assertEqual((turn1.prompt_tokens, turn1.output_tokens, turn1.cached_tokens, turn1.thinking_tokens, turn1.total_tokens), (105, 10, 5, 12, 115))
        self.assertNotIn("total_tokens", baseline)

        turn2 = account_usage({"input_tokens": 250, "output_tokens": 40}, baseline)
        self.assertEqual((turn2.prompt_tokens, turn2.output_tokens, turn2.cached_tokens, turn2.thinking_tokens, turn2.total_tokens), (150, 30, 0, 0, 180))
        self.assertEqual(baseline["cache_read_tokens"], 5)

        turn3 = account_usage({"input_tokens": 400, "cache_read_tokens": 30, "thinking_tokens": 20}, baseline)
        self.assertEqual((turn3.prompt_tokens, turn3.output_tokens, turn3.cached_tokens, turn3.thinking_tokens, turn3.total_tokens), (175, 0, 25, 8, 175))

        # Lower counter signals agy restart -> resets only that counter.
        turn4 = account_usage({"input_tokens": 50, "output_tokens": 55}, baseline)
        self.assertEqual((turn4.input_tokens, turn4.output_tokens), (50, 15))

    def test_native_tool_decision_retries_once_then_fails_loud(self) -> None:
        retry_with_tools = native_tool_decision(
            "run_command", retry_used=False, has_output=False, has_tools=True
        )
        self.assertEqual(retry_with_tools.action, "retry")
        self.assertIn("<tool_call>", retry_with_tools.correction_prompt)

        retry_plain = native_tool_decision(
            "run_command", retry_used=False, has_output=False, has_tools=False
        )
        self.assertEqual(retry_plain.action, "retry")
        self.assertIn("Answer directly with plain text", retry_plain.correction_prompt)

        second_attempt = native_tool_decision(
            "run_command", retry_used=True, has_output=False, has_tools=True
        )
        self.assertEqual(second_attempt.action, "fail")
        self.assertIn("run_command", second_attempt.error_message)

        after_output = native_tool_decision(
            "run_command", retry_used=False, has_output=True, has_tools=True
        )
        self.assertEqual(after_output.action, "fail")

    def test_is_output_cap_and_process_group_kwargs(self) -> None:
        signals = ("context length", "prompt is too long")
        self.assertTrue(
            is_output_cap(
                "Antigravity model error: Your previous response was cut off because it exceeded the output token limit",
                signals,
            )
        )
        self.assertFalse(
            is_output_cap(
                "prompt is too long and exceeded the output token limit",
                signals,
            )
        )
        self.assertFalse(is_output_cap("upstream connection reset", signals))
        self.assertEqual(own_process_group_kwargs(os_name="posix"), {"start_new_session": True})
        self.assertEqual(own_process_group_kwargs(os_name="nt"), {"creationflags": 0x08000200})


if __name__ == "__main__":
    unittest.main()
