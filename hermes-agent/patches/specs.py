"""Declarative PatchSpec definitions delegating Odin hooks into `odin_runtime`."""

from __future__ import annotations

import sys

from patch_runner import Hunk, PatchSpec

_MARKER_OUTPUT_CAP = "# odin-patch: agy output-truncation wording"
_MARKER_PROCESS = "# odin-patch: remove PR_SET_PDEATHSIG via odin_runtime"
_MARKER_CLIENT = "# odin-patch: preserve _worker_usage_baseline and native-tool retry"
_MARKER_STREAM = "# odin-patch: delegate stream usage and native tool guard to odin_runtime"
_MARKER_MODELS = "# odin-patch: delegate agy catalog policy to odin_runtime"
_MARKER_INIT = "# odin-patch: enforce HERMES_AGY_ALLOWED_MODELS via odin_runtime"
_MARKER_PREAMBLE = "# odin-patch: append odin_runtime PREAMBLE_RULES"
_MARKER_COMPRESS = "# odin-patch: delegate pre-compress handover and replay guard to odin_runtime"
_MARKER_PM_ENV = "# odin-patch: preserve base_venv site-packages via odin_runtime"
_MARKER_PM_PKG = "# odin-patch: stamp and link base_venv site-packages via odin_runtime"
_MARKER_MNEMOSYNE_ADAPTER = "# odin-patch: route host LLM to auxiliary.memory task"
_MARKER_MNEMOSYNE_LLM = "# odin-patch: lazy register hermes host LLM backend"
_MARKER_MNEMOSYNE_INSTALL = "# odin-patch: anchor mnemosyne install memory regex"
_MARKER_APPROVAL = "# odin-patch: clarify unattended execution error messaging for -c/-e flags"
_MARKER_MEDIA_SWEEP = "# odin-patch: startup_api_media_sweep_offload"
_MARKER_TEST_MAIN = "# odin-patch: align upstream antigravity tests with odin hooks"
_MARKER_TEST_LIFETIME = "# odin-patch: align test_process_lifetime with setsid contract"
_MARKER_TEST_USAGE = "# odin-patch: align test_worker_usage with cached prompt_tokens"
_MARKER_TEST_REVIEW = "# odin-patch: align test_review_regressions with cached prompt_tokens"

_LOOP_SUCCESS_TAIL = (
    "                    success = (\n"
    '                        status == "SUCCESS"\n'
    "                        and not error_msg\n"
    "                        and bool(has_content or has_tool_calls or text_buffer or final_resp)\n"
    "                    )\n"
    "                    break\n"
)

_LOOP_SUCCESS_TAIL_INDENTED = (
    "                        success = (\n"
    '                            status == "SUCCESS"\n'
    "                            and not error_msg\n"
    "                            and bool(has_content or has_tool_calls or text_buffer or final_resp)\n"
    "                        )\n"
    "                        break\n"
)

_RETRY_WRAPPER_SUFFIX = (
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
    '                    self.conversation_id = ""\n'
    "                    self._early_error = None\n"
    '                    text_buffer = ""\n'
    "                    usage_data = {}\n"
    "                    has_content = False\n"
    "                    has_tool_calls = False\n"
    "                    used_tool_call_ids.clear()\n"
    "                    next_tool_call_index = 0\n"
    "                    in_tool_call = False\n"
    "                    md_state = _MarkdownCodeState()\n"
    '                    error_msg = ""\n'
    '                    status = ""\n'
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
    '                            name="agy-quota-watchdog",\n'
    "                            daemon=True,\n"
    "                        )\n"
    "                        watchdog_thread.start()\n"
    "                    continue\n"
    "                break\n"
)

_AGY_OUTPUT_CAP_SPEC = PatchSpec(
    id="agy_output_cap",
    root_key="hermes",
    rel_path="agent/model_metadata.py",
    marker=_MARKER_OUTPUT_CAP,
    retired_probe='"output token limit"',
    hunks=(
        Hunk(
            label="is_output_cap_error",
            anchor=(
                "    error_lower = error_msg.lower()\n"
                "    # The OpenAI-style split names neither max_tokens nor"
            ),
            replacement=(
                "    error_lower = error_msg.lower()\n"
                f"    {_MARKER_OUTPUT_CAP}\n"
                "    from odin_runtime.agy_stream_guard import is_output_cap as _odin_is_output_cap\n"
                "    if _odin_is_output_cap(error_lower, _INPUT_OVERFLOW_SIGNALS):\n"
                "        return True\n"
                "    # The OpenAI-style split names neither max_tokens nor"
            ),
        ),
    ),
)

_AGY_PROCESS_SPEC = PatchSpec(
    id="agy_v1029_process",
    root_key="agy_plugin",
    rel_path="process.py",
    marker=_MARKER_PROCESS,
    retired_probe=lambda src: 'kwargs["preexec_fn"] = _pdeathsig_preexec' not in src,
    hunks=(
        Hunk(
            label="_own_process_group",
            anchor=(
                "def _own_process_group() -> dict[str, Any]:\n"
                '    """Popen kwargs for background processes in a group we can kill cleanly.\n\n'
                "    On Linux the child also gets PR_SET_PDEATHSIG(SIGKILL): if the hosting\n"
                "    Hermes worker dies without running the plugin's cleanup (crash, dispatcher\n"
                "    kill), the kernel terminates agy instead of leaving it reparented to init,\n"
                "    holding its request open and its memory (issue #50). The preexec_fn\n"
                "    re-checks the parent after fork to close the classic PDEATHSIG race where\n"
                "    the parent dies between fork and prctl.\n"
                '    """\n'
                '    if os.name == "nt":\n'
                '        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | _CREATE_NO_WINDOW}\n'
                '    kwargs: dict[str, Any] = {"start_new_session": True}\n'
                '    if sys.platform == "linux":\n'
                '        kwargs["preexec_fn"] = _pdeathsig_preexec(os.getpid())\n'
                "    return kwargs\n"
            ),
            replacement=(
                "def _own_process_group() -> dict[str, Any]:\n"
                f"    {_MARKER_PROCESS}\n"
                "    from odin_runtime.agy_stream_guard import own_process_group_kwargs\n"
                "    flags = getattr(subprocess, \"CREATE_NEW_PROCESS_GROUP\", 0x00000200) | _CREATE_NO_WINDOW\n"
                "    return own_process_group_kwargs(creationflags=flags)\n"
            ),
        ),
    ),
)

_AGY_CLIENT_SPEC = PatchSpec(
    id="agy_v1029_client",
    root_key="agy_plugin",
    rel_path="client.py",
    marker=_MARKER_CLIENT,
    retired_probe="def _respawn_for_native_tool_retry",
    hunks=(
        Hunk(
            label="_terminate_worker_locked",
            anchor=(
                "        # The session is gone: any later usage from it belongs to a dead\n"
                "        # session, and the next spawn must start from a clean baseline.\n"
                "        self._worker_usage_baseline = {}\n"
                "        # Full-send diagnostics compare only fresh-context requests, never\n"
                "        # session counters or deltas. Their reference survives worker resets.\n"
                "        if not keep_conversation:\n"
                "            self._worker_conversation_id = None\n"
                "            self._conversation_history = []\n"
            ),
            replacement=(
                f"        {_MARKER_CLIENT}\n"
                "        if not keep_conversation:\n"
                "            self._worker_usage_baseline = {}\n"
                "            self._worker_conversation_id = None\n"
                "            self._conversation_history = []\n"
            ),
        ),
        Hunk(
            label="_get_or_spawn_worker",
            anchor=(
                "            self._worker_media_rules = read_media_rules(self._isolated_gemini_dir)\n"
                "            self._worker_usage_baseline = {}\n"
                "            self._active_processes.add(proc)\n"
            ),
            replacement=(
                "            self._worker_media_rules = read_media_rules(self._isolated_gemini_dir)\n"
                "            if not conversation_id:\n"
                "                self._worker_usage_baseline = {}\n"
                "            self._active_processes.add(proc)\n"
            ),
        ),
        Hunk(
            label="_run_oneshot_completion",
            anchor=(
                "        stream_iter = AntigravityStream(\n"
                "            proc=proc,\n"
                "            client=self,\n"
                "            model=model,\n"
                "            timeout=timeout,\n"
                "            tools=tools,\n"
                "            is_worker=False,\n"
            ),
            replacement=(
                "        stream_iter = AntigravityStream(\n"
                "            proc=proc,\n"
                "            client=self,\n"
                "            model=model,\n"
                "            timeout=timeout,\n"
                "            tools=tools,\n"
                "            tool_choice=tool_choice,\n"
                "            effort=effort,\n"
                "            is_worker=False,\n"
            ),
        ),
        Hunk(
            label="_respawn_for_native_tool_retry",
            anchor="    def _create_chat_completion(\n",
            replacement=(
                "    def _respawn_for_native_tool_retry(\n"
                "        self, *, old_proc: subprocess.Popen, is_worker: bool, model: str,\n"
                "        effort: str | None, messages: list[dict[str, Any]],\n"
                "        tools: list[dict[str, Any]] | None, tool_choice: Any, tool_name: str,\n"
                "    ) -> tuple[subprocess.Popen, dict[str, int] | None]:\n"
                "        from odin_runtime.agy_stream_guard import native_tool_decision\n"
                "        base_prompt = _format_messages_as_prompt(\n"
                "            messages, model=model, tools=tools, tool_choice=tool_choice,\n"
                "            media_sink=self._materialized_media,\n"
                "        )\n"
                "        decision = native_tool_decision(\n"
                "            tool_name, retry_used=False, has_output=False, has_tools=bool(tools),\n"
                "        )\n"
                "        prompt_payload = base_prompt + decision.correction_prompt\n"
                '        dump_prompt_debug(prompt=prompt_payload, branch="native_tool_retry", model=model, messages=messages)\n'
                "        resolved_effort = effort if effort is not None else self._resolve_model_and_effort(model)[1]\n"
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
                "            try:\n"
                "                self._raise_if_cancelled()\n"
                "                if proc.stdin:\n"
                '                    proc.stdin.write(json.dumps({"event": "user", "message": {"content": prompt_payload}}) + "\\n")\n'
                "                    proc.stdin.flush()\n"
                "            except (BrokenPipeError, OSError) as exc:\n"
                "                with self._lock:\n"
                "                    self._terminate_worker_locked(keep_conversation=False)\n"
                "                raise RuntimeError(decision.error_message) from exc\n"
                "            return proc, baseline\n"
                "        with self._lock:\n"
                "            self._raise_if_cancelled()\n"
                "            self._active_processes.discard(old_proc)\n"
                "            self._terminate_process(old_proc)\n"
                "            sync_isolated_token(self._isolated_home)\n"
                "            cmd_args = [self._command, *self._args]\n"
                '            if model: cmd_args.extend(["--model", model])\n'
                '            if resolved_effort: cmd_args.extend(["--effort", resolved_effort])\n'
                "            proc = subprocess.Popen(\n"
                '                cmd_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,\n'
                '                text=True, encoding="utf-8", errors="replace", bufsize=1,\n'
                "                cwd=self._cwd, env=self._child_env(), **_own_process_group(),\n"
                "            )\n"
                "            self._active_processes.add(proc)\n"
                "            register_tracked_process(proc)\n"
                "            start_stderr_drainer(proc)\n"
                "        try:\n"
                "            self._raise_if_cancelled()\n"
                "            if proc.stdin:\n"
                '                proc.stdin.write(prompt_payload + "\\n")\n'
                "                proc.stdin.flush()\n"
                "                proc.stdin.close()\n"
                "        except OSError:\n"
                "            self._raise_if_cancelled()\n"
                "        return proc, None\n\n"
                "    def _create_chat_completion(\n"
            ),
        ),
        Hunk(
            label="worker_stream_1",
            anchor=(
                "                stream_iter = AntigravityStream(\n"
                "                    proc=proc,\n"
                "                    client=self,\n"
                "                    model=resolved_model,\n"
                "                    timeout=effective_timeout,\n"
                "                    tools=tools,\n"
                "                    is_worker=True,\n"
            ),
            replacement=(
                "                stream_iter = AntigravityStream(\n"
                "                    proc=proc,\n"
                "                    client=self,\n"
                "                    model=resolved_model,\n"
                "                    timeout=effective_timeout,\n"
                "                    tools=tools,\n"
                "                    tool_choice=tool_choice,\n"
                "                    effort=effort,\n"
                "                    is_worker=True,\n"
            ),
        ),
        Hunk(
            label="worker_stream_2",
            anchor=(
                "                    stream_iter = AntigravityStream(\n"
                "                        proc=proc,\n"
                "                        client=self,\n"
                "                        model=resolved_model,\n"
                "                        timeout=effective_timeout,\n"
                "                        tools=tools,\n"
                "                        is_worker=True,\n"
            ),
            replacement=(
                "                    stream_iter = AntigravityStream(\n"
                "                        proc=proc,\n"
                "                        client=self,\n"
                "                        model=resolved_model,\n"
                "                        timeout=effective_timeout,\n"
                "                        tools=tools,\n"
                "                        tool_choice=tool_choice,\n"
                "                        effort=effort,\n"
                "                        is_worker=True,\n"
            ),
        ),
    ),
)

_AGY_STREAM_SPEC = PatchSpec(
    id="agy_v1029_stream",
    root_key="agy_plugin",
    rel_path="stream.py",
    marker=_MARKER_STREAM,
    retired_probe="thinking_tokens",
    hunks=(
        Hunk(
            label="_UsageDeltas_and_delta_worker",
            anchor=(
                "class _UsageDeltas(NamedTuple):\n"
                '    """Per-turn usage derived from cumulative worker-session counters."""\n\n'
                "    input_tokens: int\n"
                "    output_tokens: int\n"
                "    total_tokens: int\n"
                "    cache_read_tokens: int\n"
            ),
            replacement=(
                "from odin_runtime.agy_stream_guard import (\n"
                "    account_usage as _odin_account_usage,\n"
                "    native_tool_decision as _odin_native_tool_decision,\n"
                ")\n\n\n"
                "class _UsageDeltas(NamedTuple):\n"
                '    """Per-turn usage derived from cumulative worker-session counters."""\n\n'
                "    input_tokens: int\n"
                "    output_tokens: int\n"
                "    total_tokens: int\n"
                "    cache_read_tokens: int\n"
                "    thinking_tokens: int\n"
            ),
        ),
        Hunk(
            label="_delta_worker_usage_delegate",
            anchor=(
                "    return _UsageDeltas(\n"
                "        input_tokens=input_delta,\n"
                "        output_tokens=output_delta,\n"
                "        total_tokens=total_delta,\n"
                '        cache_read_tokens=_counter_delta(usage_data, baseline, "cache_read_tokens"),\n'
                "    )\n"
            ),
            replacement=(
                "    return _UsageDeltas(\n"
                "        input_tokens=input_delta,\n"
                "        output_tokens=output_delta,\n"
                "        total_tokens=total_delta,\n"
                '        cache_read_tokens=_counter_delta(usage_data, baseline, "cache_read_tokens"),\n'
                '        thinking_tokens=_counter_delta(usage_data, baseline, "thinking_tokens"),\n'
                "    )\n"
            ),
        ),
        Hunk(
            label="_usage_totals",
            anchor=(
                "    def _usage_totals(self, usage_data: dict[str, Any]) -> tuple[int, int, int, int]:\n"
                '        """(input, output, total, cached) token counts for the usage chunk.\n\n'
                "        Persistent worker sessions report cumulative usage, so worker streams\n"
                "        forward per-turn deltas against their session baseline. Oneshot\n"
                "        processes already report per-turn usage and are forwarded raw; they\n"
                "        never touch the worker baseline.\n"
                '        """\n'
                "        if self.is_worker and self.usage_baseline is not None:\n"
                "            deltas = _delta_worker_usage(usage_data, self.usage_baseline)\n"
                "            return (\n"
                "                deltas.input_tokens,\n"
                "                deltas.output_tokens,\n"
                "                deltas.total_tokens,\n"
                "                deltas.cache_read_tokens,\n"
                "            )\n"
                '        input_tokens = int(usage_data.get("input_tokens", 0) or 0)\n'
                '        output_tokens = int(usage_data.get("output_tokens", 0) or 0)\n'
                '        total_tokens = int(usage_data.get("total_tokens", input_tokens + output_tokens) or 0)\n'
                '        cached_tokens = int(usage_data.get("cache_read_tokens", 0) or 0)\n'
                "        return input_tokens, output_tokens, total_tokens, cached_tokens\n"
            ),
            replacement=(
                "    def _usage_totals(self, usage_data: dict[str, Any]) -> tuple[int, int, int, int, int]:\n"
                "        baseline = self.usage_baseline if (self.is_worker and self.usage_baseline is not None) else None\n"
                "        snap = _odin_account_usage(usage_data, baseline)\n"
                "        return (\n"
                "            snap.prompt_tokens,\n"
                "            snap.output_tokens,\n"
                "            snap.total_tokens,\n"
                "            snap.cached_tokens,\n"
                "            snap.thinking_tokens,\n"
                "        )\n"
            ),
        ),
        Hunk(
            label="__init__signature",
            anchor=(
                "        delivery_watch: Any = None,\n"
                '        delivery_sample_key: str = "",\n'
                "    ):\n"
            ),
            replacement=(
                "        delivery_watch: Any = None,\n"
                '        delivery_sample_key: str = "",\n'
                "        tool_choice: Any = None,\n"
                "        effort: str | None = None,\n"
                "    ):\n"
            ),
        ),
        Hunk(
            label="__init__attrs",
            anchor=(
                "        self.timeout = timeout\n"
                "        self.has_tools = bool(tools)\n"
                "        self.is_worker = is_worker\n"
            ),
            replacement=(
                "        self.timeout = timeout\n"
                "        self._tools = tools\n"
                "        self._tool_choice = tool_choice\n"
                "        self._effort = effort\n"
                "        self._native_tool_retry_used = False\n"
                "        self.has_tools = bool(tools)\n"
                "        self.is_worker = is_worker\n"
            ),
        ),
        Hunk(
            label="_stream_generator_head",
            anchor=(
                '        gemini_dir = getattr(self.client, "_isolated_gemini_dir", None)\n'
                "        watchdog_stop = threading.Event()\n"
                "        watchdog_thread: threading.Thread | None = None\n\n"
                "        if gemini_dir:\n"
                "            def _watchdog_loop() -> None:\n"
                "                while not watchdog_stop.wait(timeout=0.3):\n"
                "                    if time.monotonic() - start_time < 1.0:\n"
                "                        continue\n"
                "                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=start_wall)\n"
                "                    if quota_err:\n"
                "                        self._early_error = quota_err\n"
                "                        logger.warning(\n"
                '                            "Antigravity process hit early quota limit: %s; terminating to prevent hang.",\n'
                "                            quota_err,\n"
                "                        )\n"
                "                        self.client._terminate_process(self.proc)\n"
                "                        break\n\n"
                "            watchdog_thread = threading.Thread(\n"
                "                target=_watchdog_loop,\n"
                '                name="agy-quota-watchdog",\n'
                "                daemon=True,\n"
                "            )\n"
                "            watchdog_thread.start()\n\n"
                "        try:\n"
            ),
            replacement=(
                f"        {_MARKER_STREAM}\n"
                '        gemini_dir = getattr(self.client, "_isolated_gemini_dir", None)\n'
                "        watchdog_stop = threading.Event()\n"
                "        watchdog_thread: threading.Thread | None = None\n\n"
                "        def _make_watchdog_target(stop_ev: threading.Event, target_proc: subprocess.Popen, t0: float, w0: float):\n"
                "            def _watchdog_loop() -> None:\n"
                "                while not stop_ev.wait(timeout=0.3):\n"
                "                    if time.monotonic() - t0 < 1.0:\n"
                "                        continue\n"
                "                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=w0)\n"
                "                    if quota_err:\n"
                "                        self._early_error = quota_err\n"
                '                        logger.warning("Antigravity process hit early quota limit: %s; terminating to prevent hang.", quota_err)\n'
                "                        self.client._terminate_process(target_proc)\n"
                "                        break\n"
                "            return _watchdog_loop\n\n"
                "        if gemini_dir:\n"
                "            watchdog_thread = threading.Thread(\n"
                "                target=_make_watchdog_target(watchdog_stop, self.proc, start_time, start_wall),\n"
                '                name="agy-quota-watchdog",\n'
                "                daemon=True,\n"
                "            )\n"
                "            watchdog_thread.start()\n\n"
                "        try:\n"
            ),
        ),
        Hunk(
            label="native_tool_step",
            anchor=(
                "                        else:\n"
                "                            logger.warning(\n"
                "                                \"Antigravity attempted native tool invocation '%s'; neutralizing to prevent host execution.\",\n"
                '                                step.get("tool_name"),\n'
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
                '                            stop_reason = "native tool step neutralized"\n'
                '                            # Issue #52: a bare empty completion reads as "no\n'
                '                            # content or reasoning", so Hermes nudged, got the\n'
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
                '                                "nothing was executed. Re-issue the step as a <tool_call> block using the host\'s tools "\n'
                '                                "instead of Antigravity\'s native tools."\n'
                "                            )\n"
                "                            break\n"
            ),
            replacement=(
                "                        else:\n"
                "                            logger.warning(\n"
                "                                \"Antigravity attempted native tool invocation '%s'; neutralizing to prevent host execution.\",\n"
                '                                step.get("tool_name"),\n'
                "                            )\n"
                "                            decision = _odin_native_tool_decision(\n"
                "                                tool_name,\n"
                "                                retry_used=self._native_tool_retry_used,\n"
                "                                has_output=bool(has_content or has_tool_calls),\n"
                "                                has_tools=self.has_tools,\n"
                "                            )\n"
                '                            if decision.action == "retry" and self.client is not None and hasattr(self.client, "_respawn_for_native_tool_retry"):\n'
                "                                self._native_tool_retry_used = True\n"
                "                                native_tool_retry_name = tool_name\n"
                "                                break\n"
                "                            self.close()\n"
                "                            raise RuntimeError(decision.error_message)\n"
            ),
        ),
        Hunk(
            label="native_tool_retry_loop",
            anchor="        try:\n            while time.monotonic() < deadline:\n",
            replacement=(
                "        try:\n"
                "            while True:\n"
                "                native_tool_retry_name: str | None = None\n"
                "                while time.monotonic() < deadline:\n"
            ),
            end_anchor=_LOOP_SUCCESS_TAIL + "\n            if not saw_result and stop_reason is None:\n",
            end_replacement=(
                _LOOP_SUCCESS_TAIL_INDENTED
                + _RETRY_WRAPPER_SUFFIX
                + "\n            if not saw_result and stop_reason is None:\n"
            ),
            indent_delta=4,
        ),
        Hunk(
            label="usage_totals_unpack",
            anchor="            input_tokens, output_tokens, total_tokens, cached_tokens = self._usage_totals(usage_data)\n",
            replacement="            prompt_tokens, output_tokens, total_tokens, cached_tokens, thinking_tokens = self._usage_totals(usage_data)\n",
        ),
        Hunk(
            label="delivery_watch_prompt_tokens",
            anchor=(
                "                watch.observe(\n"
                "                    self.prompt_chars, input_tokens + cached_tokens,\n"
                "                    sample_key=self.delivery_sample_key,\n"
                "                )\n"
            ),
            replacement=(
                "                watch.observe(\n"
                "                    self.prompt_chars, prompt_tokens,\n"
                "                    sample_key=self.delivery_sample_key,\n"
                "                )\n"
            ),
        ),
        Hunk(
            label="finish_and_usage",
            anchor=(
                "            if neutralized_tool_step:\n"
                "                # Issue #52: the security abort carries its explanation and\n"
                "                # the finish reason in one chunk \u2014 content makes the host\n"
                "                # treat the turn as answered instead of empty.\n"
                "                yield self._make_chunk(content=neutralized_text, finish_reason=finish_reason)\n"
                "            else:\n"
                "                yield self._make_chunk(finish_reason=finish_reason)\n\n"
                "            usage = SimpleNamespace(\n"
                "                prompt_tokens=input_tokens,\n"
                "                completion_tokens=output_tokens,\n"
                "                total_tokens=total_tokens,\n"
                "                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),\n"
                "            )\n"
            ),
            replacement=(
                "            yield self._make_chunk(finish_reason=finish_reason)\n\n"
                "            usage = SimpleNamespace(\n"
                "                prompt_tokens=prompt_tokens,\n"
                "                completion_tokens=output_tokens,\n"
                "                total_tokens=total_tokens,\n"
                "                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),\n"
                "                completion_tokens_details=SimpleNamespace(reasoning_tokens=thinking_tokens),\n"
                "            )\n"
            ),
        ),
        Hunk(
            label="collect_stream_completion_fallback_usage",
            anchor=(
                "    if not usage:\n"
                "        usage = SimpleNamespace(\n"
                "            prompt_tokens=0,\n"
                "            completion_tokens=0,\n"
                "            total_tokens=0,\n"
                "            prompt_tokens_details=SimpleNamespace(cached_tokens=0),\n"
                "        )\n"
            ),
            replacement=(
                "    if not usage:\n"
                "        usage = SimpleNamespace(\n"
                "            prompt_tokens=0,\n"
                "            completion_tokens=0,\n"
                "            total_tokens=0,\n"
                "            prompt_tokens_details=SimpleNamespace(cached_tokens=0),\n"
                "            completion_tokens_details=SimpleNamespace(reasoning_tokens=0),\n"
                "        )\n"
            ),
        ),
    ),
)

_AGY_MODELS_SPEC = PatchSpec(
    id="agy_catalog_models",
    root_key="agy_plugin",
    rel_path="models.py",
    marker=_MARKER_MODELS,
    retired_probe='"claude-sonnet-5-5": ("low", "medium", "high")',
    hunks=(
        Hunk(
            label="imports",
            anchor=(
                "import logging\n"
                "import re\n"
                "import subprocess\n"
                "import threading\n"
                "import time\n"
            ),
            replacement=(
                f"{_MARKER_MODELS}\n"
                "import logging\n"
                "import os\n"
                "import re\n"
                "import subprocess\n"
                "import threading\n"
                "import time\n"
                "from odin_runtime.agy_policy import (\n"
                "    AgyPolicy,\n"
                "    default_aux_model as default_aux_model_name,\n"
                "    default_model_name,\n"
                "    derive_agy_home,\n"
                "    effective_fallback_models,\n"
                "    effective_known_efforts,\n"
                "    effective_model_aliases,\n"
                "    filter_catalog,\n"
                ")\n"
            ),
        ),
        Hunk(
            label="_KNOWN_EFFORTS_and_ALIASES",
            anchor=(
                "_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = {\n"
                '    "gemini-3.8-flash": ("low", "medium", "high"),\n'
                '    "gemini-3.7-flash": ("low", "medium", "high"),\n'
                '    "gemini-3.6-flash": ("low", "medium", "high"),\n'
                '    "gemini-3.1-pro": ("low", "high"),\n'
                '    "claude-sonnet-4-6": (),\n'
                '    "claude-opus-4-6-thinking": (),\n'
                '    "gpt-oss-120b": ("medium",),\n'
                "}\n\n"
                "_FALLBACK_MODELS = list(_KNOWN_EFFORTS)\n\n"
                "_MODEL_ALIASES = {\n"
                '    "default": "gemini-3.8-flash",\n'
                '    "flash": "gemini-3.8-flash",\n'
                '    "gemini-flash": "gemini-3.8-flash",\n'
                '    "gemini-3.8": "gemini-3.8-flash",\n'
                '    "pro": "gemini-3.1-pro",\n'
                '    "gemini-pro": "gemini-3.1-pro",\n'
                '    "gemini-3.1": "gemini-3.1-pro",\n'
                '    "sonnet": "claude-sonnet-4-6",\n'
                '    "claude-sonnet": "claude-sonnet-4-6",\n'
                '    "claude-sonnet-5-5": "claude-sonnet-4-6",\n'
                '    "opus": "claude-opus-4-6-thinking",\n'
                '    "claude-opus": "claude-opus-4-6-thinking",\n'
                '    "claude-opus-5-5": "claude-opus-4-6-thinking",\n'
                "}\n"
            ),
            replacement=(
                "_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = effective_known_efforts()\n"
                "_FALLBACK_MODELS = effective_fallback_models()\n"
                "_MODEL_ALIASES = effective_model_aliases()\n"
            ),
        ),
        Hunk(
            label="parse_catalog",
            anchor=(
                "        efforts = grouped.setdefault(base, [])\n"
                "        if effort and effort not in efforts:\n"
                "            efforts.append(effort)\n"
                "    return {\n"
                "        base: tuple(sorted(efforts, key=_EFFORT_RANK.__getitem__))\n"
                "        for base, efforts in grouped.items()\n"
                "    }\n"
            ),
            replacement=(
                "        efforts = grouped.setdefault(base, [])\n"
                "        if effort and effort not in efforts:\n"
                "            efforts.append(effort)\n"
                "    return filter_catalog({\n"
                "        base: tuple(sorted(efforts, key=_EFFORT_RANK.__getitem__))\n"
                "        for base, efforts in grouped.items()\n"
                "    })\n"
            ),
        ),
        Hunk(
            label="load_catalog",
            anchor=(
                "    global _catalog_cache\n"
                "    try:\n"
                "        from .process import _no_window, resolve_agy_command\n"
                "    except ImportError:\n"
                "        from process import _no_window, resolve_agy_command\n\n"
                "    with _catalog_lock:\n"
                "        if only_if_stale:\n"
                "            fresh = _cached_catalog()\n"
                "            if fresh is not None:\n"
                "                return fresh\n"
                "        try:\n"
                "            res = subprocess.run(\n"
                '                [resolve_agy_command(), "models"],\n'
                "                capture_output=True,\n"
                "                text=True,\n"
                "                timeout=timeout,\n"
                "                **_no_window(),\n"
                "            )\n"
                "            catalog = parse_catalog(res.stdout)\n"
            ),
            replacement=(
                "    global _catalog_cache\n"
                "    try:\n"
                "        from .process import _no_window, resolve_agy_command, resolve_real_token_path\n"
                "    except ImportError:\n"
                "        from process import _no_window, resolve_agy_command, resolve_real_token_path\n\n"
                "    with _catalog_lock:\n"
                "        if only_if_stale:\n"
                "            fresh = _cached_catalog()\n"
                "            if fresh is not None:\n"
                "                return fresh\n"
                "        try:\n"
                '            run_kwargs: dict[str, Any] = {"capture_output": True, "text": True, "timeout": timeout, **_no_window()}\n'
                "            agy_home = derive_agy_home(resolve_real_token_path())\n"
                "            if agy_home is not None:\n"
                '                run_kwargs["env"] = {**os.environ, "HOME": agy_home}\n'
                '            res = subprocess.run([resolve_agy_command(), "models"], **run_kwargs)\n'
                "            catalog = parse_catalog(res.stdout)\n"
            ),
        ),
        Hunk(
            label="cached_and_efforts",
            anchor=(
                "def _cached_catalog() -> dict[str, tuple[str, ...]] | None:\n"
                "    cache = _catalog_cache\n"
                "    if cache is not None and time.monotonic() < cache[0]:\n"
                "        return cache[1]\n"
                "    return None\n\n\n"
                "def model_efforts(base_model: str, *, refresh: bool = True) -> tuple[str, ...] | None:\n"
                '    """Efforts agy accepts for `base_model`; () means bare name, None means unknown.\n\n'
                "    Order: a fresh `agy models` result, then the built-in table, then (when\n"
                "    `refresh` is true) a new `agy models` call. While a cached result is\n"
                "    fresh, a model it does not list is not looked up again until the cache\n"
                "    expires, so an unknown name costs no process spawn per request.\n"
                '    """\n'
                "    cached = _cached_catalog()\n"
                "    if cached is not None and base_model in cached:\n"
                "        return cached[base_model]\n"
                "    if base_model in _KNOWN_EFFORTS:\n"
                "        return _KNOWN_EFFORTS[base_model]\n"
                "    if refresh and cached is None:\n"
                "        return load_catalog(only_if_stale=True).get(base_model)\n"
                "    return None\n"
            ),
            replacement=(
                "def _cached_catalog() -> dict[str, tuple[str, ...]] | None:\n"
                "    cache = _catalog_cache\n"
                "    if cache is not None and time.monotonic() < cache[0]:\n"
                "        return filter_catalog(cache[1])\n"
                "    return None\n\n\n"
                "def model_efforts(base_model: str, *, refresh: bool = True) -> tuple[str, ...] | None:\n"
                "    return AgyPolicy.from_env().model_efforts(\n"
                "        base_model,\n"
                "        cached_catalog=_cached_catalog(),\n"
                "        load_catalog_fn=lambda: load_catalog(only_if_stale=True),\n"
                "        refresh=refresh,\n"
                "    )\n"
            ),
        ),
        Hunk(
            label="resolve_model_and_effort",
            anchor=(
                "def resolve_model_alias(\n"
                "    model: str, catalog: dict[str, tuple[str, ...]] | None = None\n"
                ") -> str:\n"
                '    """Resolve user alias to concrete base model, prioritizing the live agy catalog."""\n'
                '    m = (model or "gemini-3.8-flash").strip()\n'
                "    lower_m = m.lower()\n\n"
                "    if catalog:\n"
                "        # Exact match or base match already exists in catalog\n"
                "        if lower_m in catalog:\n"
                "            return lower_m\n"
                "        base, suffix = split_effort_suffix(lower_m)\n"
                "        if base in catalog:\n"
                "            return lower_m\n\n"
                '        lookup_term = "flash" if base == "default" else base\n\n'
                "        # Dynamic family matching against whatever models agy actually returned\n"
                "        family_keywords = {\n"
                '            ("sonnet", "claude-sonnet"): "sonnet",\n'
                '            ("opus", "claude-opus"): "opus",\n'
                '            ("pro", "gemini-pro", "gemini-3.1"): "pro",\n'
                '            ("flash", "gemini-flash", "gemini-3.8"): "flash",\n'
                '            ("gpt", "gpt-oss", "oss"): "gpt",\n'
                "        }\n"
                "        for triggers, kw in family_keywords.items():\n"
                "            if lookup_term in triggers:\n"
                "                matches = [k for k in catalog if kw in k.lower()]\n"
                "                if matches:\n"
                "                    # Sort descending so newer versions win if multiple exist (5-5 before 4-6, 3.8 before 3.7)\n"
                "                    best = sorted(matches, reverse=True)[0]\n"
                '                    return f"{best}-{suffix}" if suffix else best\n\n'
                "        # General substring match against catalog\n"
                "        matches = [k for k in catalog if lookup_term in k.lower()]\n"
                "        if matches:\n"
                "            best = sorted(matches, reverse=True)[0]\n"
                '            return f"{best}-{suffix}" if suffix else best\n\n'
                "        # Check explicit fallback aliases if not matched dynamically in catalog\n"
                "        if lookup_term in _MODEL_ALIASES:\n"
                "            aliased = _MODEL_ALIASES[lookup_term]\n"
                "            if aliased in catalog:\n"
                '                return f"{aliased}-{suffix}" if suffix else aliased\n\n'
                "    base, suffix = split_effort_suffix(lower_m)\n"
                "    fallback = _MODEL_ALIASES.get(base, base)\n"
                "    if suffix and fallback != base:\n"
                '        return f"{fallback}-{suffix}"\n'
                "    return _MODEL_ALIASES.get(lower_m, m)\n\n\n"
                "def resolve_model_and_effort(\n"
                "    model: str | None,\n"
                "    reasoning_effort: str | None = None,\n"
                ") -> tuple[str, str | None]:\n"
                '    """Map user/hermes model request to concrete CLI model ID and effort level.\n\n'
                "    Returns (model id, effort). The effort is None for models that agy selects\n"
                "    by bare name; the client then omits --effort.\n"
                '    """\n'
                '    raw_model = str(model or "gemini-3.8-flash").strip()\n'
                "    catalog = _cached_catalog()\n"
                "    if catalog is None:\n"
                "        catalog = load_catalog(only_if_stale=True)\n\n"
                "    m = resolve_model_alias(raw_model, catalog)\n\n"
                "    base_model, suffix_effort = split_effort_suffix(m)\n"
                "    supported = model_efforts(base_model, refresh=False)\n"
                "    if supported is None:\n"
                "        supported = _heuristic_efforts(base_model, suffix_effort is not None)\n"
                "    if not supported:\n"
                "        return base_model, None\n\n"
                "    # Resolve effort: explicit argument > model suffix > config setting > default\n"
                "    effort = _normalize_effort(reasoning_effort)\n"
                "    if not effort and suffix_effort:\n"
                "        effort = suffix_effort\n"
                "    if not effort:\n"
                "        try:\n"
                "            from hermes_cli.config import load_config_readonly\n"
                '            cfg_effort = load_config_readonly().get("agent", {}).get("reasoning_effort")\n'
                "            effort = _normalize_effort(cfg_effort)\n"
                "        except Exception:\n"
                "            pass\n"
                "    if not effort:\n"
                '        effort = "medium"\n\n'
                "    concrete_effort = _nearest_effort(effort, supported)\n"
                '    return f"{base_model}-{concrete_effort}", concrete_effort\n'
            ),
            replacement=(
                "def resolve_model_alias(\n"
                "    model: str, catalog: dict[str, tuple[str, ...]] | None = None\n"
                ") -> str:\n"
                "    return AgyPolicy.from_env().resolve_model_alias(model, catalog)\n\n\n"
                "def _read_config_effort() -> str | None:\n"
                "    try:\n"
                "        from hermes_cli.config import load_config_readonly\n"
                '        return load_config_readonly().get("agent", {}).get("reasoning_effort")\n'
                "    except Exception as exc:\n"
                '        logger.debug("Config reasoning_effort unavailable: %s", exc)\n'
                "        return None\n\n\n"
                "def resolve_model_and_effort(\n"
                "    model: str | None,\n"
                "    reasoning_effort: str | None = None,\n"
                ") -> tuple[str, str | None]:\n"
                "    catalog = _cached_catalog()\n"
                "    if catalog is None:\n"
                "        catalog = load_catalog(only_if_stale=True)\n"
                "    return AgyPolicy.from_env().resolve_model_and_effort(\n"
                "        model,\n"
                "        reasoning_effort,\n"
                "        catalog=catalog,\n"
                "        config_effort=_read_config_effort(),\n"
                "    )\n"
            ),
        ),
    ),
)

_AGY_INIT_SPEC = PatchSpec(
    id="agy_catalog_init",
    root_key="agy_plugin",
    rel_path="__init__.py",
    marker=_MARKER_INIT,
    retired_probe="HERMES_AGY_ALLOWED_MODELS",
    hunks=(
        Hunk(
            label="imports",
            anchor=(
                "try:\n"
                "    from .models import (\n"
                "        _FALLBACK_MODELS,\n"
                "        _MODEL_ALIASES,\n"
                "        _cached_catalog,\n"
                "        _heuristic_efforts,\n"
                "        load_catalog,\n"
                "        model_efforts,\n"
                "        resolve_model_alias,\n"
                "        split_effort_suffix,\n"
                "    )\n"
                "except ImportError:\n"
                "    # Loaded outside a package (e.g. a flat source tree under test):\n"
                "    # the absolute name is the same module.\n"
                "    from models import (\n"
                "        _FALLBACK_MODELS,\n"
                "        _MODEL_ALIASES,\n"
                "        _cached_catalog,\n"
                "        _heuristic_efforts,\n"
                "        load_catalog,\n"
                "        model_efforts,\n"
                "        resolve_model_alias,\n"
                "        split_effort_suffix,\n"
                "    )\n"
            ),
            replacement=(
                f"{_MARKER_INIT}\n"
                "from odin_runtime.agy_policy import (\n"
                "    AgyPolicy,\n"
                "    default_aux_model as default_aux_model_name,\n"
                "    effective_fallback_models,\n"
                "    filter_catalog,\n"
                ")\n"
                "try:\n"
                "    from .models import (\n"
                "        _FALLBACK_MODELS,\n"
                "        _MODEL_ALIASES,\n"
                "        _cached_catalog,\n"
                "        _heuristic_efforts,\n"
                "        load_catalog,\n"
                "        model_efforts,\n"
                "        resolve_model_alias,\n"
                "        split_effort_suffix,\n"
                "    )\n"
                "except ImportError:\n"
                "    from models import (\n"
                "        _FALLBACK_MODELS,\n"
                "        _MODEL_ALIASES,\n"
                "        _cached_catalog,\n"
                "        _heuristic_efforts,\n"
                "        load_catalog,\n"
                "        model_efforts,\n"
                "        resolve_model_alias,\n"
                "        split_effort_suffix,\n"
                "    )\n"
            ),
        ),
        Hunk(
            label="supported_reasoning_efforts",
            anchor=(
                '        name = (model or "").strip()\n'
                "        catalog = _cached_catalog()\n"
                "        resolved_name = resolve_model_alias(name, catalog)\n"
                "        base, suffix_effort = split_effort_suffix(resolved_name)\n"
                "        # No process spawn here: use the cached catalog and the built-in table.\n"
                "        efforts = model_efforts(base, refresh=False)\n"
                "        if efforts is None:\n"
                "            efforts = _heuristic_efforts(base, suffix_effort is not None)\n"
                "        return efforts\n"
            ),
            replacement=(
                "        return AgyPolicy.from_env().supported_reasoning_efforts(\n"
                "            model, catalog=_cached_catalog()\n"
                "        )\n"
            ),
        ),
        Hunk(
            label="fetch_models_and_aux",
            anchor=(
                "        catalog = load_catalog(timeout=timeout)\n"
                "        if catalog:\n"
                "            return list(catalog)\n"
                "        return list(_FALLBACK_MODELS)\n"
            ),
            replacement=(
                "        catalog = filter_catalog(load_catalog(timeout=timeout))\n"
                "        if catalog:\n"
                "            return list(catalog)\n"
                "        return effective_fallback_models()\n\n"
                "    @property\n"
                "    def default_aux_model(self) -> str:\n"
                "        policy = AgyPolicy.from_env()\n"
                "        if policy.allowed_models is not None or policy.allowed_efforts is not None:\n"
                "            return policy.default_aux_model()\n"
                '        return getattr(self, "_default_aux_model", "gemini-3.8-flash")\n\n'
                "    @default_aux_model.setter\n"
                "    def default_aux_model(self, value: str) -> None:\n"
                "        self._default_aux_model = value\n\n"
                "    def resolve_aux_model(self, *, vision: bool = False) -> str:\n"
                "        policy = AgyPolicy.from_env()\n"
                "        if policy.allowed_models is not None or policy.allowed_efforts is not None:\n"
                "            return policy.default_aux_model()\n"
                '        return ""\n'
            ),
        ),
        Hunk(
            label="lazy_capabilities",
            anchor=(
                "    def get(self, model: Any, default: Any = None) -> Any:\n"
                '        name = str(model or "").strip()\n'
                "        if not name:\n"
                "            return default\n"
                "        verdict = self._declare(name.lower())\n"
            ),
            replacement=(
                "    def get(self, model: Any, default: Any = None) -> Any:\n"
                '        name = str(model or "").strip()\n'
                "        if not name:\n"
                "            return default\n"
                "        target = AgyPolicy.from_env().lazy_capability_target(name, catalog=_cached_catalog())\n"
                "        if target is None:\n"
                "            return default\n"
                "        verdict = self._declare(target)\n"
            ),
        ),
        Hunk(
            label="default_aux_model",
            anchor='    default_aux_model="gemini-3.8-flash",\n',
            replacement="    default_aux_model=default_aux_model_name(),\n",
        ),
    ),
)

_AGY_PREAMBLE_SPEC = PatchSpec(
    id="agy_prompt_preamble",
    root_key="agy_plugin",
    rel_path="prompt.py",
    marker=_MARKER_PREAMBLE,
    hunks=(
        Hunk(
            label="import_preamble_rules",
            anchor="_PROMPT_PREAMBLE = (\n",
            replacement=(
                f"{_MARKER_PREAMBLE}\n"
                "from odin_runtime.agy_policy import PREAMBLE_RULES as _ODIN_PREAMBLE_RULES\n\n"
                "_PROMPT_PREAMBLE = (\n"
            ),
        ),
        Hunk(
            label="append_preamble_rules",
            anchor=(
                '    "- When the user\'s latest message introduces a new question or task, do NOT continue prior completed tasks or tool loops. Address the user\'s latest request directly.",\n'
                ")\n"
            ),
            replacement=(
                '    "- When the user\'s latest message introduces a new question or task, do NOT continue prior completed tasks or tool loops. Address the user\'s latest request directly.",\n'
                "    *_ODIN_PREAMBLE_RULES,\n"
                ")\n"
            ),
        ),
    ),
)

_AGY_TESTS_LIFETIME_SPEC = PatchSpec(
    id="agy_tests_lifetime",
    root_key="agy_plugin",
    rel_path="tests/test_process_lifetime.py",
    marker=_MARKER_TEST_LIFETIME,
    hunks=(
        Hunk(
            label="TestOwnProcessGroup",
            anchor=(
                "class TestOwnProcessGroup(unittest.TestCase):\n"
                '    @unittest.skipUnless(sys.platform == "linux", "PDEATHSIG is Linux-only")\n'
                "    def test_linux_arms_pdeathsig_preexec(self):\n"
                "        kwargs = _own_process_group()\n"
                '        self.assertTrue(kwargs["start_new_session"])\n'
                '        self.assertTrue(callable(kwargs["preexec_fn"]))\n\n'
                '    @unittest.skipIf(sys.platform == "linux", "only the non-Linux path")\n'
                "    def test_non_linux_has_no_preexec(self):\n"
                "        kwargs = _own_process_group()\n"
                '        self.assertNotIn("preexec_fn", kwargs)\n\n'
                '    @unittest.skipUnless(sys.platform == "linux", "real fork test")\n'
                "    def test_pdeathsig_kills_orphaned_grandchild(self):\n"
                '        """Real reproduction of issue #50: parent dies hard, grandchild must die."""\n'
                "        child_code = (\n"
                '            "import subprocess, sys, os\\n"\n'
                '            "sys.path.insert(0, sys.argv[1])\\n"\n'
                '            "from process import _own_process_group\\n"\n'
                '            "gc = subprocess.Popen([\'sleep\', \'300\'], stdout=subprocess.DEVNULL,"\n'
                '            " **_own_process_group())\\n"\n'
                '            "print(gc.pid); sys.stdout.flush()\\n"\n'
                '            "os._exit(0)\\n"\n'
                "        )\n"
                "        parent = subprocess.Popen(\n"
                '            [sys.executable, "-c", child_code, _PLUGIN_ROOT],\n'
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
                '        self.assertIs(survived, False, f"grandchild {gcpid} survived parent death")\n'
            ),
            replacement=(
                f"{_MARKER_TEST_LIFETIME}\n"
                "class TestOwnProcessGroup(unittest.TestCase):\n"
                '    @unittest.skipIf(os.name == "nt", "POSIX setsid path")\n'
                "    def test_posix_uses_setsid_without_pdeathsig_preexec(self):\n"
                "        kwargs = _own_process_group()\n"
                '        self.assertEqual(kwargs, {"start_new_session": True})\n\n'
                '    @unittest.skipIf(os.name == "nt", "POSIX thread-exit survival")\n'
                "    def test_thread_exit_does_not_sigkill_worker_child(self):\n"
                "        import threading\n"
                "        holder = {}\n"
                "        def spawn():\n"
                '            holder["proc"] = subprocess.Popen(["sleep", "30"], stdout=subprocess.DEVNULL, **_own_process_group())\n'
                "        t = threading.Thread(target=spawn)\n"
                "        t.start()\n"
                "        t.join()\n"
                "        time.sleep(0.2)\n"
                '        proc = holder["proc"]\n'
                "        try:\n"
                "            self.assertIsNone(proc.poll())\n"
                "        finally:\n"
                "            terminate_process(proc)\n"
            ),
        ),
    ),
)

_AGY_TESTS_USAGE_SPEC = PatchSpec(
    id="agy_tests_usage",
    root_key="agy_plugin",
    rel_path="tests/test_worker_usage.py",
    marker=_MARKER_TEST_USAGE,
    hunks=(
        Hunk(
            label="pin_load_catalog",
            anchor=(
                "        patcher_keychains = patch(\"process._link_macos_keychains\")\n"
                "        patcher_auth.start()\n"
                "        patcher_token.start()\n"
                "        patcher_cmd.start()\n"
                "        patcher_keychains.start()\n"
                "        self.addCleanup(patcher_auth.stop)\n"
                "        self.addCleanup(patcher_token.stop)\n"
                "        self.addCleanup(patcher_cmd.stop)\n"
                "        self.addCleanup(patcher_keychains.stop)\n"
            ),
            replacement=(
                "        patcher_keychains = patch(\"process._link_macos_keychains\")\n"
                "        patcher_catalog = patch(\"models.load_catalog\", return_value={})\n"
                "        patcher_auth.start()\n"
                "        patcher_token.start()\n"
                "        patcher_cmd.start()\n"
                "        patcher_keychains.start()\n"
                "        patcher_catalog.start()\n"
                "        self.addCleanup(patcher_auth.stop)\n"
                "        self.addCleanup(patcher_token.stop)\n"
                "        self.addCleanup(patcher_cmd.stop)\n"
                "        self.addCleanup(patcher_keychains.stop)\n"
                "        self.addCleanup(patcher_catalog.stop)\n"
            ),
        ),
        Hunk(
            label="cached_prompt_tokens",
            anchor=(
                "        self.assertEqual(usage1.prompt_tokens, 100)\n"
                "        self.assertEqual(usage1.completion_tokens, 10)\n"
                "        self.assertEqual(usage1.prompt_tokens_details.cached_tokens, 5)\n"
                "        self.assertEqual(usage1.total_tokens, 110)  # fallback: 100 + 10\n\n"
                "        self.assertEqual(usage2.prompt_tokens, 150)\n"
                "        self.assertEqual(usage2.completion_tokens, 30)  # 40 - 10\n"
                "        self.assertEqual(usage2.prompt_tokens_details.cached_tokens, 0)\n"
                "        self.assertEqual(usage2.total_tokens, 180)  # fallback: 150 + 30\n\n"
                "        self.assertEqual(usage3.prompt_tokens, 150)\n"
                "        self.assertEqual(usage3.completion_tokens, 0)\n"
                "        self.assertEqual(usage3.prompt_tokens_details.cached_tokens, 25)  # 30 - 5\n"
                "        self.assertEqual(usage3.total_tokens, 150)  # fallback: 150 + 0\n"
            ),
            replacement=(
                f"        {_MARKER_TEST_USAGE}\n"
                "        self.assertEqual(usage1.prompt_tokens, 105)\n"
                "        self.assertEqual(usage1.completion_tokens, 10)\n"
                "        self.assertEqual(usage1.prompt_tokens_details.cached_tokens, 5)\n"
                "        self.assertEqual(usage1.total_tokens, 115)\n\n"
                "        self.assertEqual(usage2.prompt_tokens, 150)\n"
                "        self.assertEqual(usage2.completion_tokens, 30)  # 40 - 10\n"
                "        self.assertEqual(usage2.prompt_tokens_details.cached_tokens, 0)\n"
                "        self.assertEqual(usage2.total_tokens, 180)  # fallback: 150 + 30\n\n"
                "        self.assertEqual(usage3.prompt_tokens, 175)\n"
                "        self.assertEqual(usage3.completion_tokens, 0)\n"
                "        self.assertEqual(usage3.prompt_tokens_details.cached_tokens, 25)  # 30 - 5\n"
                "        self.assertEqual(usage3.total_tokens, 175)\n"
            ),
        ),
    ),
)

_AGY_TESTS_REVIEW_SPEC = PatchSpec(
    id="agy_tests_review",
    root_key="agy_plugin",
    rel_path="tests/test_review_regressions.py",
    marker=_MARKER_TEST_REVIEW,
    hunks=(
        Hunk(
            label="cached_prompt_tokens",
            anchor=(
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
            ),
            replacement=(
                f"    {_MARKER_TEST_REVIEW}\n"
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
            ),
        ),
    ),
)

_AGY_TESTS_MAIN_SPEC = PatchSpec(
    id="agy_tests_main",
    root_key="agy_plugin",
    rel_path="tests/test_antigravity_plugin.py",
    marker=_MARKER_TEST_MAIN,
    verify_cmd=(
        sys.executable,
        "-m",
        "unittest",
        "tests.test_antigravity_plugin",
        "tests.test_process_lifetime",
        "tests.test_worker_usage",
        "tests.test_review_regressions",
    ),
    hunks=(
        Hunk(
            label="claude_55_empty_catalog",
            anchor=(
                '        with patch("models._catalog_cache", None), \\\n'
                '                patch("models.load_catalog", return_value={}):\n'
                '            self.assertEqual(client._resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-4-6", None))\n'
                '            self.assertEqual(client._resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-4-6-thinking", None))\n'
            ),
            replacement=(
                f"        {_MARKER_TEST_MAIN}\n"
                '        with patch("models._catalog_cache", None), \\\n'
                '                patch("models.load_catalog", return_value={}):\n'
                '            self.assertEqual(client._resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-5-5-low", "low"))\n'
                '            self.assertEqual(client._resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-5-5-high", "high"))\n'
            ),
        ),
        Hunk(
            label="claude_55_efforts",
            anchor=(
                '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-4-6"), ())\n'
                '        self.assertEqual(profile.supported_reasoning_efforts("claude-opus-4-6-thinking"), ())\n'
                '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-5-5"), ())\n'
                '        self.assertEqual(profile.supported_reasoning_efforts("gpt-oss-120b-medium"), ("medium",))\n'
            ),
            replacement=(
                '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-4-6"), ())\n'
                '        self.assertEqual(profile.supported_reasoning_efforts("claude-opus-4-6-thinking"), ())\n'
                '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-5-5"), ("low", "medium", "high"))\n'
                '        self.assertEqual(profile.supported_reasoning_efforts("gpt-oss-120b-medium"), ("medium",))\n'
            ),
        ),
        Hunk(
            label="native_tool_step",
            anchor=(
                "    def test_native_tool_step_neutralization_in_stream(self):\n"
                '        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a\n'
                "        # drive-rooted \\tmp with no guaranteed write access, and an\n"
                "        # explicit cwd is never cleaned by close() \u2014 addCleanup removes it.\n"
                "        tmp_dir = tempfile.TemporaryDirectory()\n"
                "        self.addCleanup(tmp_dir.cleanup)\n"
                "        client = AntigravityClient(cwd=tmp_dir.name)\n"
                "        fake_events = [\n"
                '            json.dumps({"event": "init", "conversation_id": "test-sec-conv"}),\n'
                '            json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command"}}),\n'
                "        ]\n"
                "        mock_proc = MagicMock()\n"
                "        mock_proc.poll.return_value = None\n"
                '        mock_proc.stdout.readline.side_effect = [f"{line}\\n" for line in fake_events] + [""]\n'
                "        mock_proc.stdin = MagicMock()\n\n"
                '        with patch("subprocess.Popen", return_value=mock_proc), patch.object(client, "_terminate_process") as mock_term:\n'
                "            stream = client.chat.completions.create(\n"
                '                model="gemini-3.8-flash",\n'
                '                messages=[{"role": "user", "content": "hi"}],\n'
                "                stream=True,\n"
                "            )\n"
                "            chunks = list(stream)\n"
                "            mock_term.assert_called_once_with(mock_proc)\n"
                '        # Issue #52: the abort is surfaced as explanatory content instead of\n'
                '        # a bare empty completion that Hermes classified as "no content" and\n'
                "        # silently failed over to another provider.\n"
                "        contents = [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]\n"
                "        self.assertEqual(len(contents), 1)\n"
                '        self.assertIn("run_command", contents[0])\n'
                '        self.assertIn("blocked by host security policy", contents[0])\n'
                '        self.assertIn("<tool_call>", contents[0])\n'
                "        finishes = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]\n"
                '        self.assertEqual(finishes, ["stop"])\n'
            ),
            replacement=(
                "    def test_native_tool_step_neutralization_in_stream(self):\n"
                "        tmp_dir = tempfile.TemporaryDirectory()\n"
                "        self.addCleanup(tmp_dir.cleanup)\n"
                "        client = AntigravityClient(cwd=tmp_dir.name)\n"
                "        native_events = [\n"
                '            json.dumps({"event": "init", "conversation_id": "test-sec-conv"}),\n'
                '            json.dumps({"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "run_command"}}),\n'
                "        ]\n"
                "        proc1 = MagicMock()\n"
                "        proc1.poll.return_value = None\n"
                '        proc1.stdout.readline.side_effect = [f"{line}\\n" for line in native_events] + [""]\n'
                "        proc1.stdin = MagicMock()\n"
                "        proc2 = MagicMock()\n"
                "        proc2.poll.return_value = None\n"
                '        proc2.stdout.readline.side_effect = [f"{line}\\n" for line in native_events] + [""]\n'
                "        proc2.stdin = MagicMock()\n"
                "        worker_procs = [proc1, proc2]\n\n"
                "        def _fake_popen(cmd, *args, **kwargs):\n"
                '            if isinstance(cmd, (list, tuple)) and "models" in cmd:\n'
                "                m_proc = MagicMock()\n"
                '                m_proc.communicate.return_value = ("", "")\n'
                "                m_proc.returncode = 0\n"
                "                return m_proc\n"
                "            return worker_procs.pop(0)\n\n"
                '        with patch("subprocess.Popen", side_effect=_fake_popen), patch.object(client, "_terminate_process") as mock_term:\n'
                "            stream = client.chat.completions.create(\n"
                '                model="gemini-3.8-flash",\n'
                '                messages=[{"role": "user", "content": "hi"}],\n'
                "                stream=True,\n"
                "            )\n"
                "            collected = []\n"
                "            with self.assertRaises(RuntimeError) as ctx:\n"
                "                for chunk in stream:\n"
                "                    collected.append(chunk)\n"
                "            self.assertEqual(mock_term.call_count, 2)\n"
                '            self.assertIn("run_command", str(ctx.exception))\n'
                "            self.assertEqual(collected, [])\n"
            ),
        ),
        Hunk(
            label="process_group",
            anchor=(
                "    def test_windows_process_group_creation(self):\n"
                "        from client import _own_process_group\n"
                '        with patch("os.name", "nt"):\n'
                "            flags = _own_process_group()\n"
                "            # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW\n"
                '            self.assertEqual(flags, {"creationflags": 0x08000200})\n'
                '        with patch("os.name", "posix"):\n'
                "            flags = _own_process_group()\n"
                "            # Linux additionally arms PR_SET_PDEATHSIG via preexec_fn\n"
                "            # (issue #50); macOS/other POSIX keep the bare setsid contract.\n"
                '            self.assertEqual(flags.pop("start_new_session", None), True)\n'
                '            if sys.platform == "linux":\n'
                '                self.assertTrue(callable(flags.pop("preexec_fn", None)))\n'
                "            self.assertEqual(flags, {})\n"
            ),
            replacement=(
                "    def test_windows_process_group_creation(self):\n"
                "        from client import _own_process_group\n"
                '        with patch("os.name", "nt"):\n'
                "            flags = _own_process_group()\n"
                '            self.assertEqual(flags, {"creationflags": 0x08000200})\n'
                '        with patch("os.name", "posix"):\n'
                "            flags = _own_process_group()\n"
                '            self.assertEqual(flags, {"start_new_session": True})\n'
            ),
        ),
        Hunk(
            label="prompt_preamble_test",
            anchor='if __name__ == "__main__":',
            replacement=(
                "    def test_prompt_preamble_operational_rules(self):\n"
                "        from prompt import _PROMPT_PREAMBLE\n"
                '        preamble_str = " ".join(_PROMPT_PREAMBLE)\n'
                '        self.assertIn("OUTPUT BUDGET & BREVITY:", preamble_str)\n'
                '        self.assertIn("TOOL DISCIPLINE & EXECUTION BOUNDARIES:", preamble_str)\n'
                '        self.assertIn("Batch independent read-only tool calls", preamble_str)\n'
                '        self.assertNotIn("one file or step at a time", preamble_str)\n'
                '        self.assertNotIn("Focus on one tool or file edit per turn", preamble_str)\n'
                '        self.assertIn("Pacific/Auckland", preamble_str)\n'
                '        self.assertIn("TURN ANCHORING & ZERO REGURGITATION:", preamble_str)\n'
                '        self.assertIn("FAIL-LOUD ARCHITECTURE & ZERO SILENT FALLBACKS:", preamble_str)\n\n\n'
                'if __name__ == "__main__":'
            ),
        ),
    ),
)

_PRE_COMPRESS_SPEC = PatchSpec(
    id="pre_compress_handover",
    root_key="hermes",
    rel_path="agent/context_compressor.py",
    marker=_MARKER_COMPRESS,
    hunks=(
        Hunk(
            label="synthetic_row_prefixes",
            anchor=(
                '    "[Planning state preserved", "[ASYNC DELEGATION", "[OUT-OF-BAND", "Cronjob Response:",\n'
                ")\n"
            ),
            replacement=(
                '    "[Planning state preserved", "[ASYNC DELEGATION", "[OUT-OF-BAND", "Cronjob Response:",\n'
                '    "[STILL IN PROGRESS",\n'
                ")\n"
            ),
        ),
        Hunk(
            label="synthetic_user_turn",
            anchor=(
                "        } or text.startswith((\n"
                '            _BACKGROUND_PROCESS_NOTIFICATION_PREFIX, TODO_INJECTION_HEADER + "\\n", _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX,\n'
                "        ))\n"
            ),
            replacement=(
                "        } or text.startswith((\n"
                '            _BACKGROUND_PROCESS_NOTIFICATION_PREFIX, TODO_INJECTION_HEADER + "\\n", _LENGTH_CONTINUATION_DROPPED_TOOLS_PREFIX,\n'
                '            "[STILL IN PROGRESS",\n'
                "        ))\n"
            ),
        ),
        Hunk(
            label="_find_inflight_user_task",
            anchor=(
                "        if last_user_idx < 0:\n"
                "            return None\n\n"
                "        for msg in reversed(messages[last_user_idx + 1:]):\n"
                '            if not isinstance(msg, dict) or msg.get("role") != "assistant":\n'
                "                # Trailing tool result (or anything else): still mid-task.\n"
                "                break\n"
                '            if msg.get("tool_calls"):\n'
                "                break\n"
                '            if _content_text_for_contains(msg.get("content")).strip():\n'
                "                # Final answer already delivered \u2014 replaying the ask would\n"
                "                # hand the model finished work as a fresh instruction.\n"
                "                return None\n"
                "            # Empty assistant row (a bare reasoning/stub turn): keep looking.\n"
                "        return messages[last_user_idx]\n"
            ),
            replacement=(
                "        if last_user_idx < 0:\n"
                "            return None\n"
                "        from odin_runtime.pre_compress_handover import should_suppress_inflight_replay\n"
                "        if should_suppress_inflight_replay(messages, last_user_idx):\n"
                "            return None\n"
                "        return messages[last_user_idx]\n"
            ),
        ),
        Hunk(
            label="_reappend_inflight_user_task",
            anchor=(
                "        # Keep any reply pointer: the restated task still needs its disambiguation.\n"
                '        task_text = _restated_request_text(inflight.get("content"))\n'
                "        if not task_text:\n"
                "            return compressed\n"
            ),
            replacement=(
                "        # Keep any reply pointer: the restated task still needs its disambiguation.\n"
                '        task_text = _restated_request_text(inflight.get("content"))\n'
                "        if not task_text:\n"
                "            return compressed\n"
                "        from odin_runtime.pre_compress_handover import should_suppress_reappend_text\n"
                "        if should_suppress_reappend_text(task_text):\n"
                "            return compressed\n"
            ),
        ),
        Hunk(
            label="phase4_handover_call",
            anchor=(
                "        if not summary:\n"
                "            summary = self._fallback_summary_for_window(\n"
                "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
                "            )\n"
                "        # Phase 4: the summary exists, so archive what the compaction drops or shortens, then assemble.\n"
            ),
            replacement=(
                "        if not summary:\n"
                "            summary = self._fallback_summary_for_window(\n"
                "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
                "            )\n"
                f"        {_MARKER_COMPRESS}\n"
                "        try:\n"
                "            from odin_runtime.pre_compress_handover import run_pre_compress_handover\n"
                "            run_pre_compress_handover(\n"
                "                turns_to_summarize=turns_to_summarize,\n"
                "                summary=summary,\n"
                "                display_tokens=display_tokens,\n"
                '                session_id=getattr(self, "_session_id", None),\n'
                "            )\n"
                "        except Exception as _wrapup_err:\n"
                '            logger.warning("odin-patch: pre-compression wrapup failed: %s", _wrapup_err, exc_info=True)\n'
                "        # Phase 4: the summary exists, so archive what the compaction drops or shortens, then assemble.\n"
            ),
        ),
    ),
)

_PM_ENVIRONMENTS_SPEC = PatchSpec(
    id="pm_environments_bridge",
    root_key="hermes",
    rel_path="pm/environments.py",
    marker=_MARKER_PM_ENV,
    hunks=(
        Hunk(
            label="activate_dependencies",
            anchor=(
                "    import site\n\n"
                "    sys.path[:] = [entry for entry in sys.path\n"
                '                   if Path(entry).name not in ("site-packages", "dist-packages")\n'
                "                   and Path(entry).resolve() != project_root.resolve()]\n"
                "    # uv editable members are activated by .pth files, not by sys.path alone.\n"
                "    site.addsitedir(str(selected))\n"
                "    sys.path[:] = [str(project_root.resolve()), str(selected),\n"
                "                   *[entry for entry in sys.path if Path(entry).resolve() != selected.resolve()]]\n"
                '    os.environ["PYTHONPATH"] = os.pathsep.join([str(project_root.resolve()), str(selected)])\n'
            ),
            replacement=(
                "    import site\n\n"
                f"    {_MARKER_PM_ENV}\n"
                "    from odin_runtime.pm_venv_bridge import link_base_site, merged_pythonpath\n"
                "    base_site = site_packages(base_venv(project_root))\n"
                "    sys.path[:] = [entry for entry in sys.path\n"
                '                   if Path(entry).name not in ("site-packages", "dist-packages")\n'
                "                   and Path(entry).resolve() != project_root.resolve()]\n"
                "    link_base_site(selected, base_site)\n"
                "    site.addsitedir(str(selected))\n"
                "    if base_site.is_dir() and base_site.resolve() != selected.resolve():\n"
                "        site.addsitedir(str(base_site))\n"
                '    sys.path[:], os.environ["PYTHONPATH"] = merged_pythonpath(\n'
                "        project_root, selected, sys.path, base_site=base_site\n"
                "    )\n"
            ),
        ),
        Hunk(
            label="activation_environment",
            anchor=(
                '    env["PYTHONPATH"] = os.pathsep.join([str(project_root.resolve()),\n'
                "                                         *([str(site_packages(environment))] if environment else [])])\n"
            ),
            replacement=(
                "    from odin_runtime.pm_venv_bridge import merged_pythonpath\n"
                "    base_site = site_packages(base_venv(project_root))\n"
                "    selected_site = site_packages(environment) if environment else None\n"
                '    _, env["PYTHONPATH"] = merged_pythonpath(\n'
                "        project_root, selected_site, (), base_site=base_site\n"
                "    )\n"
            ),
        ),
    ),
)

_PM_PACKAGES_SPEC = PatchSpec(
    id="pm_packages_bridge",
    root_key="hermes",
    rel_path="pm/packages.py",
    marker=_MARKER_PM_PKG,
    hunks=(
        Hunk(
            label="expected_stamp",
            anchor=(
                "        h = hashlib.sha256()\n"
                '        h.update(_uv_lock_digest(self.project_root() / "uv.lock"))\n'
            ),
            replacement=(
                f"        {_MARKER_PM_PKG}\n"
                "        from odin_runtime.pm_venv_bridge import bridge_stamp_digest\n"
                "        h = hashlib.sha256()\n"
                '        h.update(_uv_lock_digest(self.project_root() / "uv.lock"))\n'
                "        h.update(bridge_stamp_digest(self.project_root()))\n"
            ),
        ),
        Hunk(
            label="apply_link_base_site",
            anchor="            environment.check(resolved_lock)\n",
            replacement=(
                "            environment.check(resolved_lock)\n"
                "            from pm.environments import base_venv, site_packages\n"
                "            from odin_runtime.pm_venv_bridge import link_base_site\n"
                "            link_base_site(site_packages(candidate), site_packages(base_venv(project)))\n"
            ),
        ),
    ),
)

_MNEMOSYNE_ADAPTER_SPEC = PatchSpec(
    id="mnemosyne_adapter",
    root_key="site_packages",
    rel_path="mnemosyne_hermes/hermes_llm_adapter.py",
    marker=_MARKER_MNEMOSYNE_ADAPTER,
    retired_probe='    task = "memory"\n',
    hunks=(
        Hunk(
            label="auxiliary_memory_task",
            anchor='    task = "compression"\n',
            replacement=f'    {_MARKER_MNEMOSYNE_ADAPTER}\n    task = "memory"\n',
        ),
    ),
)

_MNEMOSYNE_LOCAL_LLM_SPEC = PatchSpec(
    id="mnemosyne_local_llm",
    root_key="site_packages",
    rel_path="mnemosyne/core/local_llm.py",
    marker=_MARKER_MNEMOSYNE_LLM,
    retired_probe="register_hermes_host_llm",
    hunks=(
        Hunk(
            label="lazy_register_host_llm",
            anchor=(
                "    if get_host_llm_backend() is None:\n"
                "        return (False, None)\n"
            ),
            replacement=(
                f"    {_MARKER_MNEMOSYNE_LLM}\n"
                "    if get_host_llm_backend() is None:\n"
                "        try:\n"
                "            from mnemosyne_hermes.hermes_llm_adapter import register_hermes_host_llm\n"
                "            register_hermes_host_llm()\n"
                "        except Exception as exc:\n"
                '            logger.warning("Lazy Hermes host LLM registration failed: %s", exc)\n'
                "    if get_host_llm_backend() is None:\n"
                "        return (False, None)\n"
            ),
        ),
    ),
)

_MNEMOSYNE_INSTALL_SPEC = PatchSpec(
    id="mnemosyne_install_regex",
    root_key="site_packages",
    rel_path="mnemosyne/install.py",
    marker=_MARKER_MNEMOSYNE_INSTALL,
    retired_probe="r'(^memory:\\s*)\\n(\\s*provider:\\s*\\S+)?'",
    hunks=(
        Hunk(
            label="anchor_memory_regex",
            anchor=(
                "        new_config = re.sub(\n"
                "            r'(memory:\\s*)\\n(\\s*provider:\\s*\\S+)?',\n"
                "            r'\\1\\n  provider: mnemosyne\\n',\n"
                "            config_text,\n"
                "            count=1,\n"
                "        )\n"
            ),
            replacement=(
                f"        {_MARKER_MNEMOSYNE_INSTALL}\n"
                "        new_config = re.sub(\n"
                "            r'(^memory:\\s*)\\n(\\s*provider:\\s*\\S+)?',\n"
                "            r'\\1\\n  provider: mnemosyne\\n',\n"
                "            config_text,\n"
                "            count=1,\n"
                "            flags=re.MULTILINE,\n"
                "        )\n"
            ),
        ),
    ),
)

_APPROVAL_GUIDANCE_SPEC = PatchSpec(
    id="unattended_approval_guidance",
    root_key="hermes",
    rel_path="tools/approval.py",
    marker=_MARKER_APPROVAL,
    hunks=(
        Hunk(
            label="_unattended_deny",
            anchor=(
                "def _unattended_deny(command: str, ctx: _Unattended) -> dict | None:\n"
                '    """Deny-mode handling for one unattended context (cron / -q / webhook); None = allow."""\n'
                '    if ctx.mode() != "deny":\n'
                "        return None\n\n"
                "    def block(subject: str) -> dict:\n"
                '        return {"approved": False, "message": ctx.block_message(\n'
                '            subject, noun="dangerous commands",\n'
                '            advice="Find an alternative approach that avoids this command.")}\n\n'
                "    pending = _unapproved_findings(command, _is_permanently_approved)\n"
                "    if pending is not None:\n"
                "        keys, description = pending\n"
                '        result = block(f"Command flagged as dangerous ({description})")\n'
                '        if ctx.name == "single_query":\n'
                "            result.update(pattern_key=keys[0], description=description)\n"
                "        return result\n"
                "    return None\n"
            ),
            replacement=(
                "def _unattended_deny(command: str, ctx: _Unattended) -> dict | None:\n"
                '    """Deny-mode handling for one unattended context (cron / -q / webhook); None = allow."""\n'
                '    if ctx.mode() != "deny":\n'
                "        return None\n\n"
                f"    {_MARKER_APPROVAL}\n"
                '    def block(subject: str, advice: str = "Find an alternative approach that avoids this command.") -> dict:\n'
                '        return {"approved": False, "message": ctx.block_message(\n'
                '            subject, noun="dangerous commands", advice=advice)}\n\n'
                "    pending = _unapproved_findings(command, _is_permanently_approved)\n"
                "    if pending is not None:\n"
                "        keys, description = pending\n"
                '        if "via -e/-c flag" in description or "via -c/-lc flag" in description:\n'
                "            advice = (\n"
                '                "Inline code flags (-c/-e) are blocked in unattended execution. "\n'
                '                "Use execute_code for Python logic, write scripts to temporary files via write_file and run them, "\n'
                '                "or use dedicated inspection tools (read_file, search_files)."\n'
                "            )\n"
                "        else:\n"
                '            advice = "Find an alternative approach that avoids this command."\n'
                '        result = block(f"Command flagged as dangerous ({description})", advice=advice)\n'
                '        if ctx.name == "single_query":\n'
                "            result.update(pattern_key=keys[0], description=description)\n"
                "        return result\n"
                "    return None\n"
            ),
        ),
    ),
)

_MEDIA_SWEEP_OFFLOAD_SPEC = PatchSpec(
    id="startup_api_media_sweep_offload",
    root_key="hermes",
    rel_path="gateway/session_api_turn.py",
    marker=_MARKER_MEDIA_SWEEP,
    retired_probe="def _run_startup_api_media_sweep",
    hunks=(
        Hunk(
            label="recover_authority_api_turns",
            anchor=(
                "    from gateway.session_ingress_media import collect_unheld_api_images\n"
                "    import logging\n"
                "    store = authority_persistence(authority)\n"
                "    collect_unheld_api_images(store)\n"
                "    from gateway.session_api_media import compact_settled_api_payloads\n"
                "    compact_settled_api_payloads(store)\n"
            ),
            replacement=(
                f"    {_MARKER_MEDIA_SWEEP}\n"
                "    from gateway.session_authorities import owner_scope\n"
                "    from gateway.session_ingress_media import collect_unheld_api_images\n"
                "    from gateway.session_api_media import compact_settled_api_payloads\n"
                "    import logging\n"
                "    import threading\n"
                "    store = authority_persistence(authority)\n\n"
                "    def _run_startup_api_media_sweep() -> None:\n"
                "        try:\n"
                "            with owner_scope(authority):\n"
                "                collect_unheld_api_images(store)\n"
                "                compact_settled_api_payloads(store)\n"
                "        except Exception as exc:\n"
                "            logging.getLogger(__name__).warning(\n"
                '                "Startup API media sweep failed for authority %s: %s",\n'
                '                getattr(authority, "name", "default"),\n'
                "                exc,\n"
                "            )\n\n"
                "    threading.Thread(\n"
                "        target=_run_startup_api_media_sweep,\n"
                "        name=f\"api-media-sweep-{getattr(authority, 'name', 'default')}\",\n"
                "        daemon=True,\n"
                "    ).start()\n"
            ),
        ),
    ),
)

ALL_SPECS: tuple[PatchSpec, ...] = (
    _AGY_OUTPUT_CAP_SPEC,
    _AGY_PROCESS_SPEC,
    _AGY_CLIENT_SPEC,
    _AGY_STREAM_SPEC,
    _AGY_MODELS_SPEC,
    _AGY_INIT_SPEC,
    _AGY_PREAMBLE_SPEC,
    _AGY_TESTS_LIFETIME_SPEC,
    _AGY_TESTS_USAGE_SPEC,
    _AGY_TESTS_REVIEW_SPEC,
    _AGY_TESTS_MAIN_SPEC,
    _PRE_COMPRESS_SPEC,
    _PM_ENVIRONMENTS_SPEC,
    _PM_PACKAGES_SPEC,
    _MNEMOSYNE_ADAPTER_SPEC,
    _MNEMOSYNE_LOCAL_LLM_SPEC,
    _MNEMOSYNE_INSTALL_SPEC,
    _APPROVAL_GUIDANCE_SPEC,
    _MEDIA_SWEEP_OFFLOAD_SPEC,
)


def get_specs() -> tuple[PatchSpec, ...]:
    """Return ordered declarative PatchSpec definitions for Odin image builds."""
    return ALL_SPECS
