#!/usr/bin/env python3
"""Build-time patch: fix 3 interlocking antigravity-subscription-directsdk v1.0.29 regressions.

Problems
--------
1. Thread-death SIGKILL (process.py):
   v1.0.29 added Linux ``prctl(PR_SET_PDEATHSIG, SIGKILL)`` in ``_own_process_group()``.
   On Linux, ``PR_SET_PDEATHSIG`` fires when the spawning *thread* terminates, not when
   the parent process exits. Hermes spawns an ephemeral ``threading.Thread`` per streaming
   API call (``chat_completion_helpers.py``), so Linux sends SIGKILL (-9) to every
   persistent ``agy`` worker as soon as Turn 1 returns, causing return-code -9 races and
   forcing continuous ``--conversation`` respawns.

2. Baseline wipe & cached token under-count (client.py, stream.py):
   When respawning a worker with ``--conversation <id>``, ``_terminate_worker_locked()``
   and ``_get_or_spawn_worker()`` unconditionally reset ``_worker_usage_baseline = {}``,
   causing ``_delta_worker_usage()`` to emit raw cumulative counters from ``agy`` SQLite
   history. Additionally, ``agy v1.5.13`` reports ``input_tokens`` (uncached only) disjoint
   from ``cache_read_tokens`` (cached), while Hermes ``usage_pricing.py`` expects OpenAI
   Chat Completions ``prompt_tokens`` to include cached tokens and subtracts
   ``cache_read_tokens`` back out. Without adding ``cached_tokens`` to ``prompt_tokens``,
   cached turns under-count uncached tokens and cumulative ``cache_read_tokens`` on
   respawn drives false 160k+ context compactions.

3. Native tool neutralization response leak (stream.py, client.py, Issue #52):
   When ``agy`` emits a non-media native tool step (e.g. ``terminal``), v1.0.29 calls
   ``self.close()`` and yields ``neutralized_text`` (``[Antigravity] Native tool invocation ...``)
   with ``finish_reason="stop"``. Hermes treats non-empty content + ``finish_reason="stop"``
   as a completed final answer and delivers internal bridge text verbatim to Discord.

Fixes
-----
1. Remove ``PR_SET_PDEATHSIG`` ``preexec_fn`` from ``process.py:_own_process_group()`` while
   preserving ``start_new_session=True`` (``os.setsid()``) and existing cleanup registries.
2. Preserve ``_worker_usage_baseline`` across ``keep_conversation=True`` / ``--conversation <id>``
   respawns (reset only on fresh conversations), and map ``prompt_tokens = input_tokens + cached_tokens``
   and ``total_tokens = max(total_tokens, prompt_tokens + output_tokens)`` in ``stream.py``.
3. Keep immediate process termination on non-media native tool steps, never emit
   ``neutralized_text`` with ``finish_reason="stop"``, perform a bounded 1x internal retry
   on a fresh ``agy`` turn with an explicit ``<tool_call>`` reminder when zero chunks were
   yielded yet, and raise a Hermes-retryable ``RuntimeError`` if retry fails or partial
   content was already streamed.
"""
from __future__ import annotations

import argparse
import py_compile
import sys
from pathlib import Path

DEFAULT_PLUGIN_DIR = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk")

MARKER_PROCESS = "# odin-patch: remove PR_SET_PDEATHSIG so ephemeral streaming threads do not SIGKILL workers"
MARKER_CLIENT = "# odin-patch: preserve _worker_usage_baseline across --conversation respawns and support native-tool retry"
MARKER_STREAM = "# odin-patch: fix prompt_tokens cached accounting and native tool neutralization leak"
MARKER_TESTS_MAIN = "# odin-patch: test v1.0.29 fixes in test_antigravity_plugin.py"
MARKER_TESTS_LIFETIME = "# odin-patch: test thread-survival in test_process_lifetime.py"
MARKER_TESTS_USAGE = "# odin-patch: test cached prompt_tokens sum in test_worker_usage.py"
MARKER_TESTS_REVIEW = "# odin-patch: test cached prompt_tokens sum in test_review_regressions.py"


def _replace_exact(src: str, anchor: str, replacement: str, label: str) -> str:
    count = src.count(anchor)
    if count != 1:
        raise ValueError(f"{label}: expected anchor count == 1, got {count}")
    return src.replace(anchor, replacement, 1)


def patch_process(plugin_dir: Path) -> None:
    target = plugin_dir / "process.py"
    if not target.is_file():
        raise FileNotFoundError(f"ERROR: {target} not found")
    src = target.read_text(encoding="utf-8")
    if MARKER_PROCESS in src:
        print(f"[odin-patch] {target.name} already patched; skipping")
        return

    anchor = (
        "def _own_process_group() -> dict[str, Any]:\n"
        "    \"\"\"Popen kwargs for background processes in a group we can kill cleanly.\n"
        "\n"
        "    On Linux the child also gets PR_SET_PDEATHSIG(SIGKILL): if the hosting\n"
        "    Hermes worker dies without running the plugin's cleanup (crash, dispatcher\n"
        "    kill), the kernel terminates agy instead of leaving it reparented to init,\n"
        "    holding its request open and its memory (issue #50). The preexec_fn\n"
        "    re-checks the parent after fork to close the classic PDEATHSIG race where\n"
        "    the parent dies between fork and prctl.\n"
        "    \"\"\"\n"
        "    if os.name == \"nt\":\n"
        "        return {\"creationflags\": getattr(subprocess, \"CREATE_NEW_PROCESS_GROUP\", 0x00000200) | _CREATE_NO_WINDOW}\n"
        "    kwargs: dict[str, Any] = {\"start_new_session\": True}\n"
        "    if sys.platform == \"linux\":\n"
        "        kwargs[\"preexec_fn\"] = _pdeathsig_preexec(os.getpid())\n"
        "    return kwargs\n"
    )
    replacement = (
        "def _own_process_group() -> dict[str, Any]:\n"
        "    \"\"\"Popen kwargs for background processes in a group we can kill cleanly.\n"
        "\n"
        "    Uses ``start_new_session=True`` (``os.setsid()``) on POSIX so ``_kill_process_tree``\n"
        "    can reap the child process group. Omits Linux ``PR_SET_PDEATHSIG(SIGKILL)`` because\n"
        "    PDEATHSIG fires when the spawning thread exits, which kills persistent ``agy`` workers\n"
        "    after ephemeral Hermes streaming-call threads finish.\n"
        "    \"\"\"\n"
        f"    {MARKER_PROCESS}\n"
        "    if os.name == \"nt\":\n"
        "        return {\"creationflags\": getattr(subprocess, \"CREATE_NEW_PROCESS_GROUP\", 0x00000200) | _CREATE_NO_WINDOW}\n"
        "    return {\"start_new_session\": True}\n"
    )
    src = _replace_exact(src, anchor, replacement, "process.py:_own_process_group")
    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (removed PR_SET_PDEATHSIG)")


def patch_client(plugin_dir: Path) -> None:
    target = plugin_dir / "client.py"
    if not target.is_file():
        raise FileNotFoundError(f"ERROR: {target} not found")
    src = target.read_text(encoding="utf-8")
    if MARKER_CLIENT in src:
        print(f"[odin-patch] {target.name} already patched; skipping")
        return

    anchor_term = (
        "        # The session is gone: any later usage from it belongs to a dead\n"
        "        # session, and the next spawn must start from a clean baseline.\n"
        "        self._worker_usage_baseline = {}\n"
        "        # Full-send diagnostics compare only fresh-context requests, never\n"
        "        # session counters or deltas. Their reference survives worker resets.\n"
        "        if not keep_conversation:\n"
        "            self._worker_conversation_id = None\n"
        "            self._conversation_history = []\n"
    )
    repl_term = (
        f"        {MARKER_CLIENT}\n"
        "        # Full-send diagnostics compare only fresh-context requests, never\n"
        "        # session counters or deltas. Their reference survives worker resets.\n"
        "        if not keep_conversation:\n"
        "            self._worker_usage_baseline = {}\n"
        "            self._worker_conversation_id = None\n"
        "            self._conversation_history = []\n"
    )
    src = _replace_exact(src, anchor_term, repl_term, "client.py:_terminate_worker_locked")

    anchor_spawn = (
        "            self._worker_media_rules = read_media_rules(self._isolated_gemini_dir)\n"
        "            self._worker_usage_baseline = {}\n"
        "            self._active_processes.add(proc)\n"
    )
    repl_spawn = (
        "            self._worker_media_rules = read_media_rules(self._isolated_gemini_dir)\n"
        "            if not conversation_id:\n"
        "                self._worker_usage_baseline = {}\n"
        "            self._active_processes.add(proc)\n"
    )
    src = _replace_exact(src, anchor_spawn, repl_spawn, "client.py:_get_or_spawn_worker")

    anchor_oneshot_stream = (
        "        stream_iter = AntigravityStream(\n"
        "            proc=proc,\n"
        "            client=self,\n"
        "            model=model,\n"
        "            timeout=timeout,\n"
        "            tools=tools,\n"
        "            is_worker=False,\n"
        "            prompt_chars=len(prompt_text),\n"
        "            # The turn's messages feed the native media-attach allowlist\n"
        "            # (read_file/view_file on referenced media paths only).\n"
        "            messages=messages,\n"
        "            # Oneshots run concurrently when the worker is busy. They have no\n"
        "            # comparable prior sample and must not mutate the worker watch.\n"
        "            delivery_watch=None,\n"
        "        )\n"
    )
    repl_oneshot_stream = (
        "        stream_iter = AntigravityStream(\n"
        "            proc=proc,\n"
        "            client=self,\n"
        "            model=model,\n"
        "            timeout=timeout,\n"
        "            tools=tools,\n"
        "            tool_choice=tool_choice,\n"
        "            effort=effort,\n"
        "            is_worker=False,\n"
        "            prompt_chars=len(prompt_text),\n"
        "            # The turn's messages feed the native media-attach allowlist\n"
        "            # (read_file/view_file on referenced media paths only).\n"
        "            messages=messages,\n"
        "            # Oneshots run concurrently when the worker is busy. They have no\n"
        "            # comparable prior sample and must not mutate the worker watch.\n"
        "            delivery_watch=None,\n"
        "        )\n"
    )
    src = _replace_exact(src, anchor_oneshot_stream, repl_oneshot_stream, "client.py:_run_oneshot_completion")

    anchor_create = "    def _create_chat_completion(\n"
    respawn_method = (
        "    def _respawn_for_native_tool_retry(\n"
        "        self,\n"
        "        *,\n"
        "        old_proc: subprocess.Popen,\n"
        "        is_worker: bool,\n"
        "        model: str,\n"
        "        effort: str | None,\n"
        "        messages: list[dict[str, Any]],\n"
        "        tools: list[dict[str, Any]] | None,\n"
        "        tool_choice: Any,\n"
        "        tool_name: str,\n"
        "    ) -> tuple[subprocess.Popen, dict[str, int] | None]:\n"
        "        \"\"\"Kill old_proc and start one fresh turn reminding the model to use <tool_call> XML.\"\"\"\n"
        "        base_prompt = _format_messages_as_prompt(\n"
        "            messages,\n"
        "            model=model,\n"
        "            tools=tools,\n"
        "            tool_choice=tool_choice,\n"
        "            media_sink=self._materialized_media,\n"
        "        )\n"
        "        if tools:\n"
        "            correction = (\n"
        "                \"\\n\\n### CORRECTION: USE <tool_call> XML BLOCKS ONLY\\n\"\n"
        "                f\"Your previous attempt invoked Antigravity's built-in tool '{tool_name}', \"\n"
        "                \"which was killed immediately by host security policy without executing.\\n\"\n"
        "                \"Do NOT call Antigravity native tools. Emit ONLY a \"\n"
        "                \"<tool_call>{\\\"name\\\": \\\"...\\\", \\\"arguments\\\": {...}}</tool_call> \"\n"
        "                \"XML block using the declared host tools above.\"\n"
        "            )\n"
        "        else:\n"
        "            correction = (\n"
        "                \"\\n\\n### CORRECTION: DO NOT INVOKE NATIVE TOOLS\\n\"\n"
        "                f\"Your previous attempt invoked Antigravity's built-in tool '{tool_name}', \"\n"
        "                \"which was killed immediately by host security policy without executing.\\n\"\n"
        "                \"Do NOT call Antigravity native tools. Answer directly with plain text.\"\n"
        "            )\n"
        "        prompt_payload = base_prompt + correction\n"
        "        dump_prompt_debug(\n"
        "            prompt=prompt_payload,\n"
        "            branch=\"native_tool_retry\",\n"
        "            model=model,\n"
        "            messages=messages,\n"
        "        )\n"
        "\n"
        "        resolved_effort = effort\n"
        "        if resolved_effort is None:\n"
        "            _, resolved_effort = self._resolve_model_and_effort(model)\n"
        "\n"
        "        if is_worker:\n"
        "            with self._lock:\n"
        "                self._raise_if_cancelled()\n"
        "                if self._worker_proc is old_proc:\n"
        "                    self._terminate_worker_locked(keep_conversation=False)\n"
        "                else:\n"
        "                    self._active_processes.discard(old_proc)\n"
        "                    self._terminate_process(old_proc)\n"
        "                self._purge_isolated_conversations()\n"
        "            proc = self._get_or_spawn_worker(model, resolved_effort, conversation_id=None)\n"
        "            with self._lock:\n"
        "                baseline: dict[str, int] | None = self._worker_usage_baseline\n"
        "            event_msg = {\"event\": \"user\", \"message\": {\"content\": prompt_payload}}\n"
        "            try:\n"
        "                self._raise_if_cancelled()\n"
        "                if proc.stdin:\n"
        "                    proc.stdin.write(json.dumps(event_msg) + \"\\n\")\n"
        "                    proc.stdin.flush()\n"
        "            except (BrokenPipeError, OSError) as exc:\n"
        "                with self._lock:\n"
        "                    self._terminate_worker_locked(keep_conversation=False)\n"
        "                raise RuntimeError(\n"
        "                    f\"Antigravity attempted native tool invocation '{tool_name}' \"\n"
        "                    \"(blocked by host security policy; retry failed on worker stdin)\"\n"
        "                ) from exc\n"
        "            return proc, baseline\n"
        "\n"
        "        with self._lock:\n"
        "            self._raise_if_cancelled()\n"
        "            self._active_processes.discard(old_proc)\n"
        "            self._terminate_process(old_proc)\n"
        "            sync_isolated_token(self._isolated_home)\n"
        "            cmd_args = [self._command, *self._args]\n"
        "            if model:\n"
        "                cmd_args.extend([\"--model\", model])\n"
        "            if resolved_effort:\n"
        "                cmd_args.extend([\"--effort\", resolved_effort])\n"
        "            proc = subprocess.Popen(\n"
        "                cmd_args,\n"
        "                stdin=subprocess.PIPE,\n"
        "                stdout=subprocess.PIPE,\n"
        "                stderr=subprocess.PIPE,\n"
        "                text=True,\n"
        "                encoding=\"utf-8\",\n"
        "                errors=\"replace\",\n"
        "                bufsize=1,\n"
        "                cwd=self._cwd,\n"
        "                env=self._child_env(),\n"
        "                **_own_process_group(),\n"
        "            )\n"
        "            self._active_processes.add(proc)\n"
        "            register_tracked_process(proc)\n"
        "            start_stderr_drainer(proc)\n"
        "        try:\n"
        "            self._raise_if_cancelled()\n"
        "            if proc.stdin:\n"
        "                proc.stdin.write(prompt_payload + \"\\n\")\n"
        "                proc.stdin.flush()\n"
        "                proc.stdin.close()\n"
        "        except OSError:\n"
        "            self._raise_if_cancelled()\n"
        "        return proc, None\n"
        "\n"
    )
    src = _replace_exact(src, anchor_create, respawn_method + anchor_create, "client.py:_create_chat_completion")

    anchor_worker_stream_1 = (
        "                stream_iter = AntigravityStream(\n"
        "                    proc=proc,\n"
        "                    client=self,\n"
        "                    model=resolved_model,\n"
        "                    timeout=effective_timeout,\n"
        "                    tools=tools,\n"
        "                    is_worker=True,\n"
        "                    worker_lock=self._worker_lock,\n"
        "                    worker_lock_held=worker_acquired,\n"
        "                    messages=messages_list,\n"
        "                    usage_baseline=worker_usage_baseline,\n"
        "                    prompt_chars=len(prompt_payload),\n"
        "                    delivery_watch=self._delivery_watch if comparable_full_send else None,\n"
        "                    delivery_sample_key=f\"{resolved_model}:{effort}\",\n"
        "                )\n"
    )
    repl_worker_stream_1 = (
        "                stream_iter = AntigravityStream(\n"
        "                    proc=proc,\n"
        "                    client=self,\n"
        "                    model=resolved_model,\n"
        "                    timeout=effective_timeout,\n"
        "                    tools=tools,\n"
        "                    tool_choice=tool_choice,\n"
        "                    effort=effort,\n"
        "                    is_worker=True,\n"
        "                    worker_lock=self._worker_lock,\n"
        "                    worker_lock_held=worker_acquired,\n"
        "                    messages=messages_list,\n"
        "                    usage_baseline=worker_usage_baseline,\n"
        "                    prompt_chars=len(prompt_payload),\n"
        "                    delivery_watch=self._delivery_watch if comparable_full_send else None,\n"
        "                    delivery_sample_key=f\"{resolved_model}:{effort}\",\n"
        "                )\n"
    )
    src = _replace_exact(src, anchor_worker_stream_1, repl_worker_stream_1, "client.py:worker_stream_1")

    anchor_worker_stream_2 = (
        "                    stream_iter = AntigravityStream(\n"
        "                        proc=proc,\n"
        "                        client=self,\n"
        "                        model=resolved_model,\n"
        "                        timeout=effective_timeout,\n"
        "                        tools=tools,\n"
        "                        is_worker=True,\n"
        "                        worker_lock=self._worker_lock,\n"
        "                        worker_lock_held=True,\n"
        "                        messages=messages_list,\n"
        "                        usage_baseline=worker_usage_baseline,\n"
        "                        prompt_chars=len(prompt_payload),\n"
        "                        delivery_watch=self._delivery_watch if comparable_full_send else None,\n"
        "                        delivery_sample_key=f\"{resolved_model}:{effort}\",\n"
        "                    )\n"
    )
    repl_worker_stream_2 = (
        "                    stream_iter = AntigravityStream(\n"
        "                        proc=proc,\n"
        "                        client=self,\n"
        "                        model=resolved_model,\n"
        "                        timeout=effective_timeout,\n"
        "                        tools=tools,\n"
        "                        tool_choice=tool_choice,\n"
        "                        effort=effort,\n"
        "                        is_worker=True,\n"
        "                        worker_lock=self._worker_lock,\n"
        "                        worker_lock_held=True,\n"
        "                        messages=messages_list,\n"
        "                        usage_baseline=worker_usage_baseline,\n"
        "                        prompt_chars=len(prompt_payload),\n"
        "                        delivery_watch=self._delivery_watch if comparable_full_send else None,\n"
        "                        delivery_sample_key=f\"{resolved_model}:{effort}\",\n"
        "                    )\n"
    )
    src = _replace_exact(src, anchor_worker_stream_2, repl_worker_stream_2, "client.py:worker_stream_2")

    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (preserved usage baseline on resume + native tool retry)")


def patch_stream(plugin_dir: Path) -> None:
    target = plugin_dir / "stream.py"
    if not target.is_file():
        raise FileNotFoundError(f"ERROR: {target} not found")
    src = target.read_text(encoding="utf-8")
    if MARKER_STREAM in src:
        print(f"[odin-patch] {target.name} already patched; skipping")
        return

    anchor_init_sig = (
        "        delivery_watch: Any = None,\n"
        "        delivery_sample_key: str = \"\",\n"
        "    ):\n"
    )
    repl_init_sig = (
        "        delivery_watch: Any = None,\n"
        "        delivery_sample_key: str = \"\",\n"
        "        tool_choice: Any = None,\n"
        "        effort: str | None = None,\n"
        "    ):\n"
    )
    src = _replace_exact(src, anchor_init_sig, repl_init_sig, "stream.py:__init__ signature")

    anchor_init_attrs = (
        "        self.timeout = timeout\n"
        "        self.has_tools = bool(tools)\n"
        "        self.is_worker = is_worker\n"
    )
    repl_init_attrs = (
        "        self.timeout = timeout\n"
        "        self._tools = tools\n"
        "        self._tool_choice = tool_choice\n"
        "        self._effort = effort\n"
        "        self._native_tool_retry_used = False\n"
        "        self.has_tools = bool(tools)\n"
        "        self.is_worker = is_worker\n"
    )
    src = _replace_exact(src, anchor_init_attrs, repl_init_attrs, "stream.py:__init__ attrs")

    anchor_gen_head = (
        "        gemini_dir = getattr(self.client, \"_isolated_gemini_dir\", None)\n"
        "        watchdog_stop = threading.Event()\n"
        "        watchdog_thread: threading.Thread | None = None\n"
        "\n"
        "        if gemini_dir:\n"
        "            def _watchdog_loop() -> None:\n"
        "                while not watchdog_stop.wait(timeout=0.3):\n"
        "                    if time.monotonic() - start_time < 1.0:\n"
        "                        continue\n"
        "                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=start_wall)\n"
        "                    if quota_err:\n"
        "                        self._early_error = quota_err\n"
        "                        logger.warning(\n"
        "                            \"Antigravity process hit early quota limit: %s; terminating to prevent hang.\",\n"
        "                            quota_err,\n"
        "                        )\n"
        "                        self.client._terminate_process(self.proc)\n"
        "                        break\n"
        "\n"
        "            watchdog_thread = threading.Thread(\n"
        "                target=_watchdog_loop,\n"
        "                name=\"agy-quota-watchdog\",\n"
        "                daemon=True,\n"
        "            )\n"
        "            watchdog_thread.start()\n"
        "\n"
        "        try:\n"
    )
    repl_gen_head = (
        f"        {MARKER_STREAM}\n"
        "        gemini_dir = getattr(self.client, \"_isolated_gemini_dir\", None)\n"
        "        watchdog_stop = threading.Event()\n"
        "        watchdog_thread: threading.Thread | None = None\n"
        "\n"
        "        def _make_watchdog_target(stop_ev: threading.Event, target_proc: subprocess.Popen, t0: float, w0: float):\n"
        "            def _watchdog_loop() -> None:\n"
        "                while not stop_ev.wait(timeout=0.3):\n"
        "                    if time.monotonic() - t0 < 1.0:\n"
        "                        continue\n"
        "                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=w0)\n"
        "                    if quota_err:\n"
        "                        self._early_error = quota_err\n"
        "                        logger.warning(\n"
        "                            \"Antigravity process hit early quota limit: %s; terminating to prevent hang.\",\n"
        "                            quota_err,\n"
        "                        )\n"
        "                        self.client._terminate_process(target_proc)\n"
        "                        break\n"
        "            return _watchdog_loop\n"
        "\n"
        "        if gemini_dir:\n"
        "            watchdog_thread = threading.Thread(\n"
        "                target=_make_watchdog_target(watchdog_stop, self.proc, start_time, start_wall),\n"
        "                name=\"agy-quota-watchdog\",\n"
        "                daemon=True,\n"
        "            )\n"
        "            watchdog_thread.start()\n"
        "\n"
        "        try:\n"
    )
    src = _replace_exact(src, anchor_gen_head, repl_gen_head, "stream.py:_stream_generator head")

    anchor_native_tool = (
        "                        else:\n"
        "                            logger.warning(\n"
        "                                \"Antigravity attempted native tool invocation '%s'; neutralizing to prevent host execution.\",\n"
        "                                step.get(\"tool_name\"),\n"
        "                            )\n"
        "                            self.close()\n"
        "                            # Recorded so the post-loop positive-evidence check\n"
        "                            # does not treat this security abort as an empty\n"
        "                            # failed turn: the process is terminated right here\n"
        "                            # on purpose (see\n"
        "                            # test_native_tool_step_neutralization_in_stream).\n"
        "                            # The stop cause is recorded too, so diagnostics do\n"
        "                            # not misreport this deliberate abort as a deadline.\n"
        "                            neutralized_tool_step = True\n"
        "                            stop_reason = \"native tool step neutralized\"\n"
        "                            # Issue #52: a bare empty completion reads as \"no\n"
        "                            # content or reasoning\", so Hermes nudged, got the\n"
        "                            # same silence on every retry, and silently failed\n"
        "                            # the turn over to a different provider while the\n"
        "                            # quota meter still showed headroom. The block is\n"
        "                            # surfaced as real content instead: the host sees\n"
        "                            # WHY the turn stopped, the quota was never denied,\n"
        "                            # and on any Hermes retry the explanation sits in\n"
        "                            # the model's history so it can re-issue the step\n"
        "                            # as a <tool_call> block. Containment is unchanged:\n"
        "                            # the process dies here, nothing was executed.\n"
        "                            #\n"
        "                            # The chunk is emitted in ONE piece together with\n"
        "                            # the finish reason at the post-loop finish yield:\n"
        "                            # a yield here would suspend the generator after\n"
        "                            # close(), and __next__'s ``if self._closed`` guard\n"
        "                            # would end the stream before the finish chunk\n"
        "                            # could ever be emitted.\n"
        "                            has_content = True\n"
        "                            neutralized_text = (\n"
        "                                f\"[Antigravity] Native tool invocation '{tool_name}' was blocked by host security policy; \"\n"
        "                                \"nothing was executed. Re-issue the step as a <tool_call> block using the host's tools \"\n"
        "                                \"instead of Antigravity's native tools.\"\n"
        "                            )\n"
        "                            break\n"
    )
    repl_native_tool = (
        "                        else:\n"
        "                            logger.warning(\n"
        "                                \"Antigravity attempted native tool invocation '%s'; neutralizing to prevent host execution.\",\n"
        "                                step.get(\"tool_name\"),\n"
        "                            )\n"
        "                            if (\n"
        "                                not self._native_tool_retry_used\n"
        "                                and not has_content\n"
        "                                and not has_tool_calls\n"
        "                                and self.client is not None\n"
        "                                and hasattr(self.client, \"_respawn_for_native_tool_retry\")\n"
        "                            ):\n"
        "                                self._native_tool_retry_used = True\n"
        "                                native_tool_retry_name = tool_name\n"
        "                                break\n"
        "                            self.close()\n"
        "                            raise RuntimeError(\n"
        "                                f\"Antigravity attempted native tool invocation '{tool_name}' \"\n"
        "                                \"(blocked by host security policy; retry using <tool_call> XML block)\"\n"
        "                            )\n"
    )
    src = _replace_exact(src, anchor_native_tool, repl_native_tool, "stream.py:native tool step")

    # Wrap the inner read loop (while time.monotonic() < deadline: ... break) in a bounded retry loop.
    loop_start_anchor = "        try:\n            while time.monotonic() < deadline:\n"
    loop_end_anchor = (
        "                    success = (\n"
        "                        status == \"SUCCESS\"\n"
        "                        and not error_msg\n"
        "                        and bool(has_content or has_tool_calls or text_buffer or final_resp)\n"
        "                    )\n"
        "                    break\n"
        "\n"
        "            if not saw_result and stop_reason is None:\n"
    )
    if src.count(loop_start_anchor) != 1:
        raise ValueError(f"stream.py:loop_start_anchor expected count == 1, got {src.count(loop_start_anchor)}")
    if src.count(loop_end_anchor) != 1:
        raise ValueError(f"stream.py:loop_end_anchor expected count == 1, got {src.count(loop_end_anchor)}")

    try_idx = src.index(loop_start_anchor)
    body_start = try_idx + len("        try:\n")
    end_idx = src.index(loop_end_anchor)
    break_pos = end_idx + len(
        "                    success = (\n"
        "                        status == \"SUCCESS\"\n"
        "                        and not error_msg\n"
        "                        and bool(has_content or has_tool_calls or text_buffer or final_resp)\n"
        "                    )\n"
        "                    break\n"
    )
    loop_slice = src[body_start:break_pos]
    indented_slice = "".join(("    " + line if line.strip() else line) for line in loop_slice.splitlines(keepends=True))
    retry_wrapper_prefix = (
        "            while True:\n"
        "                native_tool_retry_name: str | None = None\n"
    )
    retry_wrapper_suffix = (
        "                if native_tool_retry_name is not None:\n"
        "                    watchdog_stop.set()\n"
        "                    if watchdog_thread and watchdog_thread.is_alive():\n"
        "                        watchdog_thread.join(timeout=0.2)\n"
        "                    new_proc, new_baseline = self.client._respawn_for_native_tool_retry(\n"
        "                        old_proc=self.proc,\n"
        "                        is_worker=self.is_worker,\n"
        "                        model=self.model,\n"
        "                        effort=self._effort,\n"
        "                        messages=list(self.messages or []),\n"
        "                        tools=self._tools,\n"
        "                        tool_choice=self._tool_choice,\n"
        "                        tool_name=native_tool_retry_name,\n"
        "                    )\n"
        "                    self.proc = new_proc\n"
        "                    if self.is_worker and new_baseline is not None:\n"
        "                        self.usage_baseline = new_baseline\n"
        "                    self.conversation_id = \"\"\n"
        "                    self._early_error = None\n"
        "                    text_buffer = \"\"\n"
        "                    usage_data = {}\n"
        "                    has_content = False\n"
        "                    has_tool_calls = False\n"
        "                    used_tool_call_ids.clear()\n"
        "                    next_tool_call_index = 0\n"
        "                    in_tool_call = False\n"
        "                    md_state = _MarkdownCodeState()\n"
        "                    error_msg = \"\"\n"
        "                    status = \"\"\n"
        "                    saw_result = False\n"
        "                    success = False\n"
        "                    stop_reason = None\n"
        "                    last_stdout_at = None\n"
        "                    start_time = time.monotonic()\n"
        "                    start_wall = time.time()\n"
        "                    deadline = start_time + self.timeout\n"
        "                    if gemini_dir:\n"
        "                        watchdog_stop = threading.Event()\n"
        "                        watchdog_thread = threading.Thread(\n"
        "                            target=_make_watchdog_target(watchdog_stop, self.proc, start_time, start_wall),\n"
        "                            name=\"agy-quota-watchdog\",\n"
        "                            daemon=True,\n"
        "                        )\n"
        "                        watchdog_thread.start()\n"
        "                    continue\n"
        "                break\n"
    )
    src = src[:body_start] + retry_wrapper_prefix + indented_slice + retry_wrapper_suffix + src[break_pos:]

    anchor_finish_usage = (
        "            if neutralized_tool_step:\n"
        "                # Issue #52: the security abort carries its explanation and\n"
        "                # the finish reason in one chunk \u2014 content makes the host\n"
        "                # treat the turn as answered instead of empty.\n"
        "                yield self._make_chunk(content=neutralized_text, finish_reason=finish_reason)\n"
        "            else:\n"
        "                yield self._make_chunk(finish_reason=finish_reason)\n"
        "\n"
        "            usage = SimpleNamespace(\n"
        "                prompt_tokens=input_tokens,\n"
        "                completion_tokens=output_tokens,\n"
        "                total_tokens=total_tokens,\n"
        "                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),\n"
        "            )\n"
    )
    repl_finish_usage = (
        "            yield self._make_chunk(finish_reason=finish_reason)\n"
        "\n"
        "            prompt_tokens = input_tokens + cached_tokens\n"
        "            usage = SimpleNamespace(\n"
        "                prompt_tokens=prompt_tokens,\n"
        "                completion_tokens=output_tokens,\n"
        "                total_tokens=max(total_tokens, prompt_tokens + output_tokens),\n"
        "                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),\n"
        "            )\n"
    )
    src = _replace_exact(src, anchor_finish_usage, repl_finish_usage, "stream.py:finish_and_usage")

    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (prompt_tokens cached fix + native tool recovery)")


def patch_tests(plugin_dir: Path) -> None:
    # 1. tests/test_antigravity_plugin.py (executed during Docker build by enhance_agy_prompt_preamble.py)
    main_test = plugin_dir / "tests" / "test_antigravity_plugin.py"
    if not main_test.is_file():
        raise FileNotFoundError(f"ERROR: {main_test} not found")
    src = main_test.read_text(encoding="utf-8")
    if MARKER_TESTS_MAIN not in src:
        anchor_native = (
            "    def test_native_tool_step_neutralization_in_stream(self):\n"
            "        # TemporaryDirectory (not /tmp): on Windows \"/tmp\" resolves to a\n"
            "        # drive-rooted \\tmp with no guaranteed write access, and an\n"
            "        # explicit cwd is never cleaned by close() \u2014 addCleanup removes it.\n"
            "        tmp_dir = tempfile.TemporaryDirectory()\n"
            "        self.addCleanup(tmp_dir.cleanup)\n"
            "        client = AntigravityClient(cwd=tmp_dir.name)\n"
            "        fake_events = [\n"
            "            json.dumps({\"event\": \"init\", \"conversation_id\": \"test-sec-conv\"}),\n"
            "            json.dumps({\"event\": \"step_update\", \"step_update\": {\"step_type\": \"tool\", \"tool_name\": \"run_command\"}}),\n"
            "        ]\n"
            "        mock_proc = MagicMock()\n"
            "        mock_proc.poll.return_value = None\n"
            "        mock_proc.stdout.readline.side_effect = [f\"{line}\\n\" for line in fake_events] + [\"\"]\n"
            "        mock_proc.stdin = MagicMock()\n"
            "\n"
            "        with patch(\"subprocess.Popen\", return_value=mock_proc), patch.object(client, \"_terminate_process\") as mock_term:\n"
            "            stream = client.chat.completions.create(\n"
            "                model=\"gemini-3.8-flash\",\n"
            "                messages=[{\"role\": \"user\", \"content\": \"hi\"}],\n"
            "                stream=True,\n"
            "            )\n"
            "            chunks = list(stream)\n"
            "            mock_term.assert_called_once_with(mock_proc)\n"
            "        # Issue #52: the abort is surfaced as explanatory content instead of\n"
            "        # a bare empty completion that Hermes classified as \"no content\" and\n"
            "        # silently failed over to another provider.\n"
            "        contents = [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]\n"
            "        self.assertEqual(len(contents), 1)\n"
            "        self.assertIn(\"run_command\", contents[0])\n"
            "        self.assertIn(\"blocked by host security policy\", contents[0])\n"
            "        self.assertIn(\"<tool_call>\", contents[0])\n"
            "        finishes = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]\n"
            "        self.assertEqual(finishes, [\"stop\"])\n"
        )
        repl_native = (
            f"    {MARKER_TESTS_MAIN}\n"
            "    def test_native_tool_step_neutralization_in_stream(self):\n"
            "        tmp_dir = tempfile.TemporaryDirectory()\n"
            "        self.addCleanup(tmp_dir.cleanup)\n"
            "        client = AntigravityClient(cwd=tmp_dir.name)\n"
            "        native_events = [\n"
            "            json.dumps({\"event\": \"init\", \"conversation_id\": \"test-sec-conv\"}),\n"
            "            json.dumps({\"event\": \"step_update\", \"step_update\": {\"step_type\": \"tool\", \"tool_name\": \"run_command\"}}),\n"
            "        ]\n"
            "        proc1 = MagicMock()\n"
            "        proc1.poll.return_value = None\n"
            "        proc1.stdout.readline.side_effect = [f\"{line}\\n\" for line in native_events] + [\"\"]\n"
            "        proc1.stdin = MagicMock()\n"
            "        proc2 = MagicMock()\n"
            "        proc2.poll.return_value = None\n"
            "        proc2.stdout.readline.side_effect = [f\"{line}\\n\" for line in native_events] + [\"\"]\n"
            "        proc2.stdin = MagicMock()\n"
            "        worker_procs = [proc1, proc2]\n"
            "\n"
            "        def _fake_popen(cmd, *args, **kwargs):\n"
            "            if isinstance(cmd, (list, tuple)) and \"models\" in cmd:\n"
            "                m_proc = MagicMock()\n"
            "                m_proc.communicate.return_value = (\"\", \"\")\n"
            "                m_proc.returncode = 0\n"
            "                return m_proc\n"
            "            return worker_procs.pop(0)\n"
            "\n"
            "        with patch(\"subprocess.Popen\", side_effect=_fake_popen), patch.object(client, \"_terminate_process\") as mock_term:\n"
            "            stream = client.chat.completions.create(\n"
            "                model=\"gemini-3.8-flash\",\n"
            "                messages=[{\"role\": \"user\", \"content\": \"hi\"}],\n"
            "                stream=True,\n"
            "            )\n"
            "            collected = []\n"
            "            with self.assertRaises(RuntimeError) as ctx:\n"
            "                for chunk in stream:\n"
            "                    collected.append(chunk)\n"
            "            self.assertEqual(mock_term.call_count, 2)\n"
            "            self.assertIn(\"run_command\", str(ctx.exception))\n"
            "            self.assertEqual(collected, [])\n"
        )
        src = _replace_exact(src, anchor_native, repl_native, "test_antigravity_plugin.py:native_tool")

        anchor_pg = (
            "    def test_windows_process_group_creation(self):\n"
            "        from client import _own_process_group\n"
            "        with patch(\"os.name\", \"nt\"):\n"
            "            flags = _own_process_group()\n"
            "            # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW\n"
            "            self.assertEqual(flags, {\"creationflags\": 0x08000200})\n"
            "        with patch(\"os.name\", \"posix\"):\n"
            "            flags = _own_process_group()\n"
            "            # Linux additionally arms PR_SET_PDEATHSIG via preexec_fn\n"
            "            # (issue #50); macOS/other POSIX keep the bare setsid contract.\n"
            "            self.assertEqual(flags.pop(\"start_new_session\", None), True)\n"
            "            if sys.platform == \"linux\":\n"
            "                self.assertTrue(callable(flags.pop(\"preexec_fn\", None)))\n"
            "            self.assertEqual(flags, {})\n"
        )
        repl_pg = (
            "    def test_windows_process_group_creation(self):\n"
            "        from client import _own_process_group\n"
            "        with patch(\"os.name\", \"nt\"):\n"
            "            flags = _own_process_group()\n"
            "            self.assertEqual(flags, {\"creationflags\": 0x08000200})\n"
            "        with patch(\"os.name\", \"posix\"):\n"
            "            flags = _own_process_group()\n"
            "            self.assertEqual(flags, {\"start_new_session\": True})\n"
        )
        src = _replace_exact(src, anchor_pg, repl_pg, "test_antigravity_plugin.py:process_group")
        main_test.write_text(src, encoding="utf-8")
        py_compile.compile(str(main_test), doraise=True)
        print(f"[odin-patch] patched {main_test}")

    # 2. tests/test_process_lifetime.py (optional file present in upstream v1.0.29)
    lifetime_test = plugin_dir / "tests" / "test_process_lifetime.py"
    if lifetime_test.is_file():
        lsrc = lifetime_test.read_text(encoding="utf-8")
        if MARKER_TESTS_LIFETIME not in lsrc:
            anchor_lt = (
                "class TestOwnProcessGroup(unittest.TestCase):\n"
                "    @unittest.skipUnless(sys.platform == \"linux\", \"PDEATHSIG is Linux-only\")\n"
                "    def test_linux_arms_pdeathsig_preexec(self):\n"
                "        kwargs = _own_process_group()\n"
                "        self.assertTrue(kwargs[\"start_new_session\"])\n"
                "        self.assertTrue(callable(kwargs[\"preexec_fn\"]))\n"
                "\n"
                "    @unittest.skipIf(sys.platform == \"linux\", \"only the non-Linux path\")\n"
                "    def test_non_linux_has_no_preexec(self):\n"
                "        kwargs = _own_process_group()\n"
                "        self.assertNotIn(\"preexec_fn\", kwargs)\n"
                "\n"
                "    @unittest.skipUnless(sys.platform == \"linux\", \"real fork test\")\n"
                "    def test_pdeathsig_kills_orphaned_grandchild(self):\n"
                "        \"\"\"Real reproduction of issue #50: parent dies hard, grandchild must die.\"\"\"\n"
                "        child_code = (\n"
                "            \"import subprocess, sys, os\\n\"\n"
                "            \"sys.path.insert(0, sys.argv[1])\\n\"\n"
                "            \"from process import _own_process_group\\n\"\n"
                "            \"gc = subprocess.Popen(['sleep', '300'], stdout=subprocess.DEVNULL,\"\n"
                "            \" **_own_process_group())\\n\"\n"
                "            \"print(gc.pid); sys.stdout.flush()\\n\"\n"
                "            \"os._exit(0)\\n\"\n"
                "        )\n"
                "        parent = subprocess.Popen(\n"
                "            [sys.executable, \"-c\", child_code, _PLUGIN_ROOT],\n"
                "            stdout=subprocess.PIPE,\n"
                "            text=True,\n"
                "        )\n"
                "        gcpid = int(parent.communicate()[0].strip())\n"
                "        survived = None\n"
                "        for _ in range(50):\n"
                "            try:\n"
                "                os.kill(gcpid, 0)\n"
                "                survived = True\n"
                "            except ProcessLookupError:\n"
                "                survived = False\n"
                "                break\n"
                "            time.sleep(0.1)\n"
                "        self.assertIs(survived, False, f\"grandchild {gcpid} survived parent death\")\n"
            )
            repl_lt = (
                f"{MARKER_TESTS_LIFETIME}\n"
                "class TestOwnProcessGroup(unittest.TestCase):\n"
                "    @unittest.skipIf(os.name == \"nt\", \"POSIX setsid path\")\n"
                "    def test_posix_uses_setsid_without_pdeathsig_preexec(self):\n"
                "        kwargs = _own_process_group()\n"
                "        self.assertEqual(kwargs, {\"start_new_session\": True})\n"
                "\n"
                "    @unittest.skipIf(os.name == \"nt\", \"POSIX thread-exit survival\")\n"
                "    def test_thread_exit_does_not_sigkill_worker_child(self):\n"
                "        import threading\n"
                "        holder = {}\n"
                "        def spawn():\n"
                "            holder[\"proc\"] = subprocess.Popen([\"sleep\", \"30\"], stdout=subprocess.DEVNULL, **_own_process_group())\n"
                "        t = threading.Thread(target=spawn)\n"
                "        t.start()\n"
                "        t.join()\n"
                "        time.sleep(0.2)\n"
                "        proc = holder[\"proc\"]\n"
                "        try:\n"
                "            self.assertIsNone(proc.poll())\n"
                "        finally:\n"
                "            terminate_process(proc)\n"
            )
            lsrc = _replace_exact(lsrc, anchor_lt, repl_lt, "test_process_lifetime.py:TestOwnProcessGroup")
            lifetime_test.write_text(lsrc, encoding="utf-8")
            py_compile.compile(str(lifetime_test), doraise=True)
            print(f"[odin-patch] patched {lifetime_test}")

    # 3. tests/test_worker_usage.py (optional file present in upstream v1.0.29)
    usage_test = plugin_dir / "tests" / "test_worker_usage.py"
    if usage_test.is_file():
        usrc = usage_test.read_text(encoding="utf-8")
        if MARKER_TESTS_USAGE not in usrc:
            anchor_wu = (
                "        self.assertEqual(usage1.prompt_tokens, 100)\n"
                "        self.assertEqual(usage1.completion_tokens, 10)\n"
                "        self.assertEqual(usage1.prompt_tokens_details.cached_tokens, 5)\n"
                "        self.assertEqual(usage1.total_tokens, 110)  # fallback: 100 + 10\n"
                "\n"
                "        self.assertEqual(usage2.prompt_tokens, 150)\n"
                "        self.assertEqual(usage2.completion_tokens, 30)  # 40 - 10\n"
                "        self.assertEqual(usage2.prompt_tokens_details.cached_tokens, 0)\n"
                "        self.assertEqual(usage2.total_tokens, 180)  # fallback: 150 + 30\n"
                "\n"
                "        self.assertEqual(usage3.prompt_tokens, 150)\n"
                "        self.assertEqual(usage3.completion_tokens, 0)\n"
                "        self.assertEqual(usage3.prompt_tokens_details.cached_tokens, 25)  # 30 - 5\n"
                "        self.assertEqual(usage3.total_tokens, 150)  # fallback: 150 + 0\n"
            )
            repl_wu = (
                f"        {MARKER_TESTS_USAGE}\n"
                "        self.assertEqual(usage1.prompt_tokens, 105)\n"
                "        self.assertEqual(usage1.completion_tokens, 10)\n"
                "        self.assertEqual(usage1.prompt_tokens_details.cached_tokens, 5)\n"
                "        self.assertEqual(usage1.total_tokens, 115)\n"
                "\n"
                "        self.assertEqual(usage2.prompt_tokens, 150)\n"
                "        self.assertEqual(usage2.completion_tokens, 30)  # 40 - 10\n"
                "        self.assertEqual(usage2.prompt_tokens_details.cached_tokens, 0)\n"
                "        self.assertEqual(usage2.total_tokens, 180)  # fallback: 150 + 30\n"
                "\n"
                "        self.assertEqual(usage3.prompt_tokens, 175)\n"
                "        self.assertEqual(usage3.completion_tokens, 0)\n"
                "        self.assertEqual(usage3.prompt_tokens_details.cached_tokens, 25)  # 30 - 5\n"
                "        self.assertEqual(usage3.total_tokens, 175)\n"
            )
            usrc = _replace_exact(usrc, anchor_wu, repl_wu, "test_worker_usage.py:cached_prompt_tokens")
            usage_test.write_text(usrc, encoding="utf-8")
            py_compile.compile(str(usage_test), doraise=True)
            print(f"[odin-patch] patched {usage_test}")

    # 4. tests/test_review_regressions.py (optional file present in upstream v1.0.29)
    review_test = plugin_dir / "tests" / "test_review_regressions.py"
    if review_test.is_file():
        rsrc = review_test.read_text(encoding="utf-8")
        if MARKER_TESTS_REVIEW not in rsrc:
            anchor_rr = (
                "    def test_cache_tokens_count_for_diagnostic_not_usage(self):\n"
                "        watch = DeliveryWatch()\n"
                "        watch.observe(64147, 95990)\n"
                "        with self.assertNoLogs('delivery', level='WARNING'):\n"
                "            chunks = list(self.stream(watch, 96651, 100000))\n"
                "        sample = watch.last_sample\n"
                "        self.assertIsNotNone(sample)\n"
                "        assert sample is not None\n"
                "        self.assertEqual(sample.input_tokens, 196651)\n"
                "        self.assertEqual(chunks[-1].usage.prompt_tokens, 96651)\n"
            )
            repl_rr = (
                f"    {MARKER_TESTS_REVIEW}\n"
                "    def test_cache_tokens_count_for_diagnostic_and_openai_prompt_tokens(self):\n"
                "        watch = DeliveryWatch()\n"
                "        watch.observe(64147, 95990)\n"
                "        with self.assertNoLogs('delivery', level='WARNING'):\n"
                "            chunks = list(self.stream(watch, 96651, 100000))\n"
                "        sample = watch.last_sample\n"
                "        self.assertIsNotNone(sample)\n"
                "        assert sample is not None\n"
                "        self.assertEqual(sample.input_tokens, 196651)\n"
                "        self.assertEqual(chunks[-1].usage.prompt_tokens, 196651)\n"
            )
            rsrc = _replace_exact(rsrc, anchor_rr, repl_rr, "test_review_regressions.py:cached_prompt_tokens")
            review_test.write_text(rsrc, encoding="utf-8")
            py_compile.compile(str(review_test), doraise=True)
            print(f"[odin-patch] patched {review_test}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patch agy plugin v1.0.29 regressions and verify syntax.")
    parser.add_argument(
        "--plugin-dir",
        default=str(DEFAULT_PLUGIN_DIR),
        help="Path to antigravity-subscription-directsdk directory",
    )
    args = parser.parse_args(argv)
    plugin_dir = Path(args.plugin_dir)

    try:
        patch_process(plugin_dir)
        patch_client(plugin_dir)
        patch_stream(plugin_dir)
        patch_tests(plugin_dir)
    except Exception as exc:
        print(f"[odin-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
