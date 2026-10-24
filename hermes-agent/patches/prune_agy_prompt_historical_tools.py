#!/usr/bin/env python3
"""Build-time patch: prune historical tool calls and raw tool outputs from completed turns in agy prompt.

Problem
-------
In multi-turn sessions (e.g. Discord threads or interactive tasks), _format_messages_as_prompt
previously accumulated all intermediate <tool_call> tags and raw Tool Result dumps across every
completed turn in history into the "Conversation transcript:".
In extended sessions, this caused prompts to explode to >138,000 characters (~35,000 tokens) of
stale tool calls, memory recall dumps, and raw command outputs from past tasks.

When models (such as Gemini 3.8 Flash High during fallback or quota events) process this massive
wall of past tool executions, they lose focus on the user's latest follow-up question and hallucinate
that the past turn's research task is still active, repeatedly re-emitting previous tool calls and
duplicate responses.

Fix
---
1. In _format_messages_as_prompt:
   Identify the active turn boundary (the latest user message index in history_messages).
   For prior completed turns (i < active_user_idx):
   - Retain user queries and final delivered assistant text responses.
   - Omit intermediate tool calls (<tool_call> tags) and raw tool results (role == "tool").
   For the active turn (i >= active_user_idx):
   - Fully preserve all user messages, assistant tool calls, and tool results so multi-step execution
     continues seamlessly.
2. In _format_delta_prompt:
   When last_role == "tool", instruct the model to evaluate the tool result and continue addressing
   the active user request without repeating prior summaries.
3. Update and add unit tests in test_antigravity_plugin.py to verify 100% test pass.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/prompt.py")
TARGET_TESTS = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/tests/test_antigravity_plugin.py")
MARKER = "# odin-patch: prune historical tool calls and raw tool results from prior turns"
TEST_MARKER = "# odin-patch: test_prune_historical_tool_calls_from_completed_turns"

PROMPT_ANCHOR = """    transcript: list[str] = []
    prior_messages = history_messages[:-1] if last_role == "user" and len(history_messages) > 1 else history_messages

    for message in prior_messages:
        role = str(message.get("role") or "unknown").strip().lower()
        rendered_content = _render_message_content(message.get("content"))

        if role == "tool":
            tool_id = str(message.get("tool_call_id") or message.get("name") or "tool").strip()
            transcript.append(f"Tool Result ({tool_id}):\\n{rendered_content}")
            continue

        if role == "assistant":
            parts = []
            if "Tool Result (" in rendered_content:
                rendered_content = rendered_content.split("Tool Result (")[0].strip()
            if rendered_content:
                parts.append(rendered_content)
            if tool_calls := message.get("tool_calls"):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        call_obj = {
                            "id": tc.get("id") or "call_1",
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "{}") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments", {}), ensure_ascii=False)
                            }
                        }
                        parts.append(f"<tool_call>{json.dumps(call_obj, ensure_ascii=False)}</tool_call>")
            if parts:
                transcript.append("Assistant:\\n" + "\\n".join(parts))
            continue"""

PROMPT_REPLACEMENT = """    transcript: list[str] = []
    prior_messages = history_messages[:-1] if last_role == "user" and len(history_messages) > 1 else history_messages

    __MARKER__
    active_user_idx = max(
        (i for i, m in enumerate(history_messages) if str(m.get("role") or "").strip().lower() == "user"),
        default=-1,
    )

    for i, message in enumerate(prior_messages):
        role = str(message.get("role") or "unknown").strip().lower()
        rendered_content = _render_message_content(message.get("content"))
        is_prior_turn = active_user_idx >= 0 and i < active_user_idx

        if role == "tool":
            if is_prior_turn:
                continue
            tool_id = str(message.get("tool_call_id") or message.get("name") or "tool").strip()
            transcript.append(f"Tool Result ({tool_id}):\\n{rendered_content}")
            continue

        if role == "assistant":
            parts = []
            if "Tool Result (" in rendered_content:
                rendered_content = rendered_content.split("Tool Result (")[0].strip()
            if rendered_content:
                parts.append(rendered_content)
            if not is_prior_turn and (tool_calls := message.get("tool_calls")):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        fn = tc.get("function") or {}
                        call_obj = {
                            "id": tc.get("id") or "call_1",
                            "type": "function",
                            "function": {
                                "name": fn.get("name", ""),
                                "arguments": fn.get("arguments", "{}") if isinstance(fn.get("arguments"), str) else json.dumps(fn.get("arguments", {}), ensure_ascii=False)
                            }
                        }
                        parts.append(f"<tool_call>{json.dumps(call_obj, ensure_ascii=False)}</tool_call>")
            if parts:
                transcript.append("Assistant:\\n" + "\\n".join(parts))
            continue""".replace("__MARKER__", MARKER)

DELTA_ANCHOR = """    if last_role == "tool":
        parts.append("Continue the conversation from the latest tool result.")"""

DELTA_REPLACEMENT = """    if last_role == "tool":
        # odin-patch: emphasize addressing active user request after tool result
        parts.append(
            "Evaluate the latest tool results above and continue addressing the user's request. "
            "If no further tools are needed, respond directly to the user without repeating prior summaries."
        )"""

TEST_UPDATE_ANCHOR = """        # Verify real tool result is present
        self.assertIn("Tool Result (call_1):\\nreal data", prompt)"""

TEST_UPDATE_REPLACEMENT = """        # Verify historical tool result from completed turn was pruned to prevent clutter
        self.assertNotIn("Tool Result (call_1):\\nreal data", prompt)
        self.assertIn("Assistant:\\nReal summary", prompt)"""

DELTA_TEST_ANCHOR_1 = """        # Delta ending with tool
        tool_delta = [{"role": "tool", "tool_call_id": "t1", "content": "output"}]
        d_prompt2 = _format_delta_prompt(tool_delta)
        self.assertIn("Continue the conversation from the latest tool result.", d_prompt2)"""

DELTA_TEST_REPLACEMENT_1 = """        # Delta ending with tool
        tool_delta = [{"role": "tool", "tool_call_id": "t1", "content": "output"}]
        d_prompt2 = _format_delta_prompt(tool_delta)
        self.assertIn("Evaluate the latest tool results above and continue addressing the user's request.", d_prompt2)"""

DELTA_TEST_ANCHOR_2 = """        delta = _format_delta_prompt(incoming_extended[2:])
        self.assertIn("<tool_call>", delta)
        self.assertIn("Tool Result (c1):\\nok", delta)
        self.assertIn("Continue the conversation from the latest tool result.", delta)"""

DELTA_TEST_REPLACEMENT_2 = """        delta = _format_delta_prompt(incoming_extended[2:])
        self.assertIn("<tool_call>", delta)
        self.assertIn("Tool Result (c1):\\nok", delta)
        self.assertIn("Evaluate the latest tool results above and continue addressing the user's request.", delta)"""

NEW_TEST_CODE = """    __TEST_MARKER__
    def test_prune_historical_tool_calls_from_completed_turns(self):
        '''Verify historical tool calls and tool results from prior turns are pruned, while active turn tools are kept.'''
        messages = [
            {"role": "user", "content": "Turn 1: check file"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "read", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "content of old file"},
            {"role": "assistant", "content": "Turn 1 answer: file is ok."},
            {"role": "user", "content": "Turn 2: run check"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c2", "function": {"name": "check", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c2", "content": "active check output"},
        ]
        prompt = _format_messages_as_prompt(messages)
        # Prior turn tool calls and tool results must be pruned
        self.assertNotIn("content of old file", prompt)
        self.assertNotIn('"read"', prompt)
        self.assertIn("Turn 1 answer: file is ok.", prompt)
        # Active turn tool calls and tool results must be preserved
        self.assertIn("active check output", prompt)
        self.assertIn("check", prompt)
        self.assertIn("### LATEST TOOL RESULTS RECEIVED.", prompt)
""".replace("__TEST_MARKER__", TEST_MARKER)


def patch_prompt() -> int:
    if not TARGET.is_file():
        print(f"[odin-patch] ERROR: {TARGET} not found", file=sys.stderr)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print("[odin-patch] agy prompt historical tool pruning already applied; skipping prompt.py")
        return 0

    if PROMPT_ANCHOR not in src:
        print("[odin-patch] ERROR: PROMPT_ANCHOR not found in prompt.py", file=sys.stderr)
        return 1
    src = src.replace(PROMPT_ANCHOR, PROMPT_REPLACEMENT, 1)

    if DELTA_ANCHOR not in src:
        print("[odin-patch] ERROR: DELTA_ANCHOR not found in prompt.py", file=sys.stderr)
        return 1
    src = src.replace(DELTA_ANCHOR, DELTA_REPLACEMENT, 1)

    TARGET.write_text(src, encoding="utf-8")
    print(f"[odin-patch] patched {TARGET} (pruned historical tool calls and results from completed turns)")
    return 0


def patch_tests() -> int:
    if not TARGET_TESTS.is_file():
        print(f"[odin-patch] WARNING: {TARGET_TESTS} not found; skipping test patch", file=sys.stderr)
        return 0

    src = TARGET_TESTS.read_text(encoding="utf-8")
    if TEST_MARKER in src:
        print("[odin-patch] test_prune_historical_tool_calls_from_completed_turns already present; skipping test patch")
        return 0

    if TEST_UPDATE_ANCHOR in src:
        src = src.replace(TEST_UPDATE_ANCHOR, TEST_UPDATE_REPLACEMENT, 1)
    else:
        print("[odin-patch] WARNING: TEST_UPDATE_ANCHOR not found in tests", file=sys.stderr)

    if DELTA_TEST_ANCHOR_1 in src:
        src = src.replace(DELTA_TEST_ANCHOR_1, DELTA_TEST_REPLACEMENT_1, 1)
    else:
        print("[odin-patch] WARNING: DELTA_TEST_ANCHOR_1 not found in tests", file=sys.stderr)

    if DELTA_TEST_ANCHOR_2 in src:
        src = src.replace(DELTA_TEST_ANCHOR_2, DELTA_TEST_REPLACEMENT_2, 1)
    else:
        print("[odin-patch] WARNING: DELTA_TEST_ANCHOR_2 not found in tests", file=sys.stderr)

    main_anchor = "if __name__ == \"__main__\":"
    if main_anchor not in src:
        print("[odin-patch] ERROR: if __name__ == '__main__' anchor not found in test_antigravity_plugin.py", file=sys.stderr)
        return 1
    src = src.replace(main_anchor, NEW_TEST_CODE + "\n" + main_anchor, 1)

    TARGET_TESTS.write_text(src, encoding="utf-8")
    print(f"[odin-patch] updated tests in {TARGET_TESTS}")

    print("[odin-patch] Running test suite to verify patch...")
    test_run = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "-s", str(TARGET_TESTS.parent)],
        capture_output=True,
        text=True,
    )
    if test_run.returncode != 0:
        print(f"[odin-patch] ERROR: Tests failed after patching:\n{test_run.stdout}\n{test_run.stderr}", file=sys.stderr)
        return test_run.returncode

    print(f"[odin-patch] Verified: all tests passed successfully:\n{test_run.stderr.strip()}")
    return 0


def main() -> int:
    rc = patch_prompt()
    if rc != 0:
        return rc
    return patch_tests()


if __name__ == "__main__":
    sys.exit(main())
