#!/usr/bin/env python3
"""Build-time patch: teach Antigravity plugin to report request-scoped token deltas in worker mode.

Problem
-------
In worker mode (interactive streaming CLI via ``agy --input-format stream-json``),
agy's ``result`` event reports cumulative session usage across all turns in that
process (e.g. Turn 1: ~63k, Turn 2: ~130k, ..., Turn 27: ~1.67M tokens).

The Antigravity plugin (``antigravity-subscription-directsdk``) previously passed
``usage_data["input_tokens"]`` directly as ``usage.prompt_tokens``.
In multi-step agent turns, this inflated ``last_prompt_tokens`` and the usage anchor
to ~1,685,746 tokens against a 200,000 token context window. Preflight context compression
could not shrink this phantom context, causing:

    "Context compression could not bring this session under the model's context window
    (~1,685,746 tokens vs 200,000)... Session auto-reset — the conversation exceeded
    the maximum context size and could not be compressed further."

Fix
---
Track worker cumulative usage on ``AntigravityClient._worker_usage`` and calculate
request-scoped deltas in ``AntigravityStream``:
    input_tokens = raw_input - prev_input
    output_tokens = max(0, raw_output - prev_output)
    cached_tokens = max(0, raw_cached - prev_cached)

On stream success, update the worker baseline. On worker reset or process termination,
clear the baseline back to {}.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

TARGET_CLIENT = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/client.py")
TARGET_STREAM = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/stream.py")
TARGET_TESTS = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk/tests/test_antigravity_plugin.py")

MARKER_CLIENT = "# odin-patch: track worker cumulative usage"
MARKER_STREAM = "# odin-patch: agy worker usage delta calculation"
MARKER_TESTS = "# odin-patch: test worker usage deltas"


def patch_client() -> int:
    if not TARGET_CLIENT.is_file():
        print(f"[odin-patch] ERROR: {TARGET_CLIENT} not found", file=sys.stderr)
        return 1

    src = TARGET_CLIENT.read_text(encoding="utf-8")
    if MARKER_CLIENT in src:
        print("[odin-patch] client.py worker usage patch already applied; skipping")
        return 0

    init_anchor = "        self._worker_history: list[dict[str, Any]] = []\n"
    if init_anchor not in src:
        print("[odin-patch] ERROR: _worker_history anchor not found in client.py", file=sys.stderr)
        return 1
    src = src.replace(
        init_anchor,
        init_anchor + f"        self._worker_usage: dict[str, int] = {{}}  {MARKER_CLIENT}\n",
        1,
    )

    term_anchor = "            self._worker_history = []\n"
    if term_anchor not in src:
        print("[odin-patch] ERROR: _terminate_worker_locked anchor not found in client.py", file=sys.stderr)
        return 1
    src = src.replace(
        term_anchor,
        term_anchor + "            self._worker_usage = {}  # odin-patch: clear worker usage on terminate\n",
        1,
    )

    TARGET_CLIENT.write_text(src, encoding="utf-8")
    print(f"[odin-patch] patched {TARGET_CLIENT} (client worker usage tracking)")
    return 0


def patch_stream() -> int:
    if not TARGET_STREAM.is_file():
        print(f"[odin-patch] ERROR: {TARGET_STREAM} not found", file=sys.stderr)
        return 1

    src = TARGET_STREAM.read_text(encoding="utf-8")
    if MARKER_STREAM in src:
        print("[odin-patch] stream.py worker usage delta patch already applied; skipping")
        return 0

    old_usage_block = (
        "            input_tokens = int(usage_data.get(\"input_tokens\", 0) or 0)\n"
        "            output_tokens = int(usage_data.get(\"output_tokens\", 0) or 0)\n"
        "            total_tokens = int(usage_data.get(\"total_tokens\", input_tokens + output_tokens) or 0)\n"
        "            cached_tokens = int(usage_data.get(\"cache_read_tokens\", 0) or 0)\n"
        "\n"
        "            usage = SimpleNamespace(\n"
        "                prompt_tokens=input_tokens,\n"
        "                completion_tokens=output_tokens,\n"
        "                total_tokens=total_tokens,\n"
        "                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),\n"
        "            )"
    )

    new_usage_block = (
        f"            {MARKER_STREAM}\n"
        "            raw_input = int(usage_data.get(\"input_tokens\", 0) or 0)\n"
        "            raw_output = int(usage_data.get(\"output_tokens\", 0) or 0)\n"
        "            raw_cached = int(usage_data.get(\"cache_read_tokens\", 0) or 0)\n"
        "\n"
        "            if self.is_worker and hasattr(self.client, \"_worker_usage\"):\n"
        "                prev_usage = self.client._worker_usage\n"
        "                prev_input = int(prev_usage.get(\"input_tokens\", 0) or 0)\n"
        "                prev_output = int(prev_usage.get(\"output_tokens\", 0) or 0)\n"
        "                prev_cached = int(prev_usage.get(\"cache_read_tokens\", 0) or 0)\n"
        "\n"
        "                if raw_input >= prev_input:\n"
        "                    input_tokens = raw_input - prev_input\n"
        "                    output_tokens = max(0, raw_output - prev_output)\n"
        "                    cached_tokens = max(0, raw_cached - prev_cached)\n"
        "                else:\n"
        "                    input_tokens = raw_input\n"
        "                    output_tokens = raw_output\n"
        "                    cached_tokens = raw_cached\n"
        "\n"
        "                self._latest_worker_usage = {\n"
        "                    \"input_tokens\": raw_input,\n"
        "                    \"output_tokens\": raw_output,\n"
        "                    \"cache_read_tokens\": raw_cached,\n"
        "                }\n"
        "            else:\n"
        "                input_tokens = raw_input\n"
        "                output_tokens = raw_output\n"
        "                cached_tokens = raw_cached\n"
        "\n"
        "            total_tokens = input_tokens + output_tokens\n"
        "\n"
        "            usage = SimpleNamespace(\n"
        "                prompt_tokens=input_tokens,\n"
        "                completion_tokens=output_tokens,\n"
        "                total_tokens=total_tokens,\n"
        "                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),\n"
        "            )"
    )

    if old_usage_block not in src:
        print("[odin-patch] ERROR: usage calculation block not found in stream.py", file=sys.stderr)
        return 1
    src = src.replace(old_usage_block, new_usage_block, 1)

    old_update_block = (
        "                    self.client._update_worker_history(self.messages)\n"
        "                    if self.worker_lock and self.worker_lock.locked():"
    )
    new_update_block = (
        "                    self.client._update_worker_history(self.messages)\n"
        "                    if hasattr(self, \"_latest_worker_usage\") and hasattr(self.client, \"_worker_usage\"):\n"
        "                        self.client._worker_usage = self._latest_worker_usage\n"
        "                    if self.worker_lock and self.worker_lock.locked():"
    )
    if old_update_block not in src:
        print("[odin-patch] ERROR: _update_worker_history anchor not found in stream.py", file=sys.stderr)
        return 1
    src = src.replace(old_update_block, new_update_block, 1)

    TARGET_STREAM.write_text(src, encoding="utf-8")
    print(f"[odin-patch] patched {TARGET_STREAM} (stream worker usage delta calculation)")
    return 0


def patch_tests() -> int:
    if not TARGET_TESTS.is_file():
        print(f"[odin-patch] ERROR: {TARGET_TESTS} not found", file=sys.stderr)
        return 1

    src = TARGET_TESTS.read_text(encoding="utf-8")
    if MARKER_TESTS in src:
        print("[odin-patch] test_antigravity_plugin.py patch already applied; skipping")
        return 0

    test_case = f"""
    {MARKER_TESTS}
    def test_worker_multi_turn_usage_deltas(self):
        client = AntigravityClient(cwd="/tmp")
        self.assertEqual(client._worker_usage, {{}})

        # Turn 1: cumulative 100 input, 10 output
        t1_events = [
            json.dumps({{"event": "init", "conversation_id": "conv-worker"}}),
            json.dumps({{
                "event": "result",
                "result": {{
                    "status": "SUCCESS",
                    "response": "turn 1",
                    "usage": {{"input_tokens": 100, "output_tokens": 10, "total_tokens": 110, "cache_read_tokens": 0}},
                }},
            }}),
        ]
        proc1 = MagicMock()
        proc1.stdin = MagicMock()
        proc1.stdout = io.StringIO("\\n".join(t1_events) + "\\n")
        proc1.stderr = io.StringIO("")
        proc1.poll.return_value = None
        proc1.wait.return_value = 0

        stream1 = AntigravityStream(
            proc=proc1, client=client, model="gemini-3.8-flash-high",
            timeout=30.0, is_worker=True, messages=[{{"role": "user", "content": "1"}}],
        )
        chunks1 = list(stream1)
        usage1 = [c for c in chunks1 if getattr(c, "usage", None)][0].usage
        self.assertEqual(usage1.prompt_tokens, 100)
        self.assertEqual(usage1.completion_tokens, 10)
        self.assertEqual(client._worker_usage["input_tokens"], 100)

        # Turn 2: cumulative 250 input (+150), 25 output (+15)
        t2_events = [
            json.dumps({{
                "event": "result",
                "result": {{
                    "status": "SUCCESS",
                    "response": "turn 2",
                    "usage": {{"input_tokens": 250, "output_tokens": 25, "total_tokens": 275, "cache_read_tokens": 50}},
                }},
            }}),
        ]
        proc2 = MagicMock()
        proc2.stdin = MagicMock()
        proc2.stdout = io.StringIO("\\n".join(t2_events) + "\\n")
        proc2.stderr = io.StringIO("")
        proc2.poll.return_value = None
        proc2.wait.return_value = 0

        stream2 = AntigravityStream(
            proc=proc2, client=client, model="gemini-3.8-flash-high",
            timeout=30.0, is_worker=True, messages=[{{"role": "user", "content": "1"}}, {{"role": "assistant", "content": "turn 1"}}, {{"role": "user", "content": "2"}}],
        )
        chunks2 = list(stream2)
        usage2 = [c for c in chunks2 if getattr(c, "usage", None)][0].usage
        self.assertEqual(usage2.prompt_tokens, 150)
        self.assertEqual(usage2.completion_tokens, 15)
        self.assertEqual(usage2.prompt_tokens_details.cached_tokens, 50)
        self.assertEqual(client._worker_usage["input_tokens"], 250)

        # Resetting/terminating worker clears baseline
        client._terminate_worker()
        self.assertEqual(client._worker_usage, {{}})
"""
    main_anchor = "if __name__ == \"__main__\":"
    if main_anchor not in src:
        print("[odin-patch] ERROR: if __name__ == '__main__' anchor not found in test_antigravity_plugin.py", file=sys.stderr)
        return 1

    src = src.replace(main_anchor, test_case + "\n" + main_anchor, 1)
    TARGET_TESTS.write_text(src, encoding="utf-8")
    print(f"[odin-patch] patched {TARGET_TESTS} (added test_worker_multi_turn_usage_deltas)")
    return 0


def main() -> int:
    rc = patch_client()
    if rc != 0:
        return rc
    rc = patch_stream()
    if rc != 0:
        return rc
    rc = patch_tests()
    if rc != 0:
        return rc

    # Run tests to verify all tests pass
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


if __name__ == "__main__":
    sys.exit(main())
