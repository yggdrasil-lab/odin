#!/usr/bin/env python3
"""Build-time patch: bake output budget awareness, tool discipline, and Auckland timezone into agy plugin preamble.

Problem
-------
Gemini 3.8 Flash (especially in high-reasoning mode) can spend tens of thousands of
hidden reasoning tokens on complex turns or large multi-file drafts, hitting the
per-response completion token ceiling:
    "Antigravity model error: Your previous response was cut off because it exceeded the output token limit"

Additionally, models can over-generate speculative commentary alongside tool calls,
emit duplicate tool calls, hallucinate execution success before receiving tool outputs,
or misinterpret relative time (e.g. UTC vs Auckland, New Zealand).

Fix
---
Bake explicit operational rules into ``_PROMPT_PREAMBLE`` in ``prompt.py``:
1. Output budget & brevity: Keep responses concise and incremental; never draft massive
   multi-file changes in a single turn.
2. Tool discipline & boundaries: Emit only tool calls and stop immediately; batch independent
   read-only tool calls in parallel while keeping file edits focused; never claim success
   without inspecting verified tool output.
3. Timezone: Align all relative temporal references with Pacific/Auckland (UTC+12/UTC+13 DST).
4. Fail-loud architecture & zero silent fallbacks: Raise explicit exceptions and exit non-zero immediately on errors instead of speculative fallbacks.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

DEFAULT_PLUGIN_DIR = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk")
MARKER = "# odin-patch: agy prompt preamble enhancements"
TEST_MARKER = "# odin-patch: test_prompt_preamble_operational_rules"

ANCHOR = (
    "    \"- After emitting tool calls, STOP generating immediately. Do NOT generate results, execution output, or commentary after tool calls.\",\n"
    "    \"- If no tool is needed, respond naturally with standard text.\",\n"
    "    \"- When the user's latest message introduces a new question or task, do NOT continue prior completed tasks or tool loops. Address the user's latest request directly.\",\n"
    ")\n"
)

INJECTION = (
    "    \"- After emitting tool calls, STOP generating immediately. Do NOT generate results, execution output, or commentary after tool calls.\",\n"
    "    \"- If no tool is needed, respond naturally with standard text.\",\n"
    "    \"- When the user's latest message introduces a new question or task, do NOT continue prior completed tasks or tool loops. Address the user's latest request directly.\",\n"
    f"    {MARKER}\n"
    "    \"OUTPUT BUDGET & BREVITY:\",\n"
    "    \"- Each response has a hard per-turn completion token limit covering both visible text and hidden reasoning tokens.\",\n"
    "    \"- Stay concise, direct, and focused on the immediate turn. NEVER attempt massive multi-file changes or huge speculative diffs in a single response.\",\n"
    "    \"- If a task requires editing multiple files or drafting large artifacts, keep each file edit focused and incremental across turns.\",\n"
    "    \"TOOL DISCIPLINE & EXECUTION BOUNDARIES:\",\n"
    "    \"- When calling tools, emit ONLY the tool call(s) and stop immediately. Do NOT emit duplicate tool calls, speculative code blocks, or conversational chatter alongside tool calls.\",\n"
    "    \"- Batch independent read-only tool calls (e.g. read_file, search_files, grep) in parallel within a single turn, while keeping file edits focused and incremental.\",\n"
    "    \"- Never assume or claim an action succeeded until you have received and inspected the actual tool output in the subsequent turn.\",\n"
    "    \"TIMEZONE & LOCAL TIME:\",\n"
    "    \"- The user lives in Auckland, New Zealand (Pacific/Auckland, UTC+12 / UTC+13 with DST). All relative time references ('today', 'yesterday', 'tomorrow', 'morning', 'tonight', dates) must align strictly with Auckland local time.\",\n"
    "    \"TURN ANCHORING & ZERO REGURGITATION:\",\n"
    "    \"- Always anchor your response strictly to the user's latest incoming message. Respond ONLY to what the user is asking right now.\",\n"
    "    \"- After context compaction or across turns, NEVER repeat a previous response or re-deliver a past wrap-up, retrospective, or completed task summary.\",\n"
    "    \"- If a task or wrap-up was already finished earlier, or if the user asks a follow-up question or points out a duplicate response, address their immediate question directly instead of re-summarizing previous work.\",\n"
    "    \"- Discard stale in-flight summaries and '[STILL IN PROGRESS]' markers if newer user instructions exist or if the requested actions were already completed.\",\n"
    "    \"FAIL-LOUD ARCHITECTURE & ZERO SILENT FALLBACKS:\",\n"
    "    \"- Anything that can fail must fail loudly and immediately. NEVER implement silent fallbacks, speculative secondary endpoints, catch-all default values, or broad exception suppression to mask errors.\",\n"
    "    \"- If required environment variables, credentials, network dependencies, or configuration keys are missing or invalid, raise an explicit exception or exit non-zero immediately so the root cause is resolved at the source.\",\n"
    ")\n"
)

TEST_INJECTION = f"""
    {TEST_MARKER}
    def test_prompt_preamble_operational_rules(self):
        '''Verify build-time preamble includes output budget, parallel read-only tool batching, Auckland timezone, turn anchoring, and fail-loud invariant.'''
        from prompt import _PROMPT_PREAMBLE
        preamble_str = " ".join(_PROMPT_PREAMBLE)
        self.assertIn("OUTPUT BUDGET & BREVITY:", preamble_str)
        self.assertIn("TOOL DISCIPLINE & EXECUTION BOUNDARIES:", preamble_str)
        self.assertIn("Batch independent read-only tool calls", preamble_str)
        self.assertNotIn("one file or step at a time", preamble_str)
        self.assertNotIn("Focus on one tool or file edit per turn", preamble_str)
        self.assertIn("Pacific/Auckland", preamble_str)
        self.assertIn("TURN ANCHORING & ZERO REGURGITATION:", preamble_str)
        self.assertIn("FAIL-LOUD ARCHITECTURE & ZERO SILENT FALLBACKS:", preamble_str)
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patch agy plugin _PROMPT_PREAMBLE and verify tests.")
    parser.add_argument(
        "--plugin-dir",
        default=str(DEFAULT_PLUGIN_DIR),
        help="Path to antigravity-subscription-directsdk directory",
    )
    args = parser.parse_args(argv)
    plugin_dir = Path(args.plugin_dir)
    target = plugin_dir / "prompt.py"
    target_tests = plugin_dir / "tests" / "test_antigravity_plugin.py"

    if not target.is_file():
        print(f"[odin-patch] ERROR: {target} not found", file=sys.stderr)
        return 1

    src = target.read_text(encoding="utf-8")
    if MARKER in src:
        print("[odin-patch] agy prompt preamble enhancements already applied; skipping prompt.py")
    else:
        if ANCHOR not in src:
            print("[odin-patch] ERROR: _PROMPT_PREAMBLE anchor not found in prompt.py", file=sys.stderr)
            return 1

        src = src.replace(ANCHOR, INJECTION, 1)
        target.write_text(src, encoding="utf-8")
        print(f"[odin-patch] patched {target} (added output budget, tool discipline, Auckland timezone to _PROMPT_PREAMBLE)")

    if target_tests.is_file():
        tests_src = target_tests.read_text(encoding="utf-8")
        if TEST_MARKER in tests_src:
            print("[odin-patch] test_prompt_preamble_operational_rules already present; skipping test injection")
        else:
            main_anchor = "if __name__ == \"__main__\":"
            if main_anchor not in tests_src:
                print("[odin-patch] ERROR: if __name__ == '__main__' anchor not found in test_antigravity_plugin.py", file=sys.stderr)
                return 1
            tests_src = tests_src.replace(main_anchor, TEST_INJECTION + "\n" + main_anchor, 1)
            target_tests.write_text(tests_src, encoding="utf-8")
            print(f"[odin-patch] added test_prompt_preamble_operational_rules to {target_tests}")

        # Run tests to verify all tests pass
        print("[odin-patch] Running test suite to verify patch...")
        env = os.environ.copy()
        existing_pp = env.get("PYTHONPATH", "")
        pp_parts = [str(plugin_dir)]
        if Path("/opt/hermes").is_dir():
            pp_parts.append("/opt/hermes")
        if existing_pp:
            pp_parts.append(existing_pp)
        env["PYTHONPATH"] = os.pathsep.join(pp_parts)
        test_run = subprocess.run(
            [sys.executable, str(target_tests)],
            capture_output=True,
            text=True,
            cwd=str(plugin_dir),
            env=env,
        )
        if test_run.returncode != 0:
            print(f"[odin-patch] ERROR: Tests failed after patching:\n{test_run.stdout}\n{test_run.stderr}", file=sys.stderr)
            return test_run.returncode

        print(f"[odin-patch] Verified: all tests passed successfully:\n{test_run.stderr.strip()}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
