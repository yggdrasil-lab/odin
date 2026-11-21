"""Standalone tests for antigravity-subscription-directsdk v1.0.29 build-time patches."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"
if str(PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(PATCHES_DIR))

import fix_agy_v1029_worker_usage_and_native_tool as agy_patch  # noqa: E402

EM_DASH = "\u2014"


def _build_process_fixture_clean() -> str:
    return '''from __future__ import annotations
import contextlib, logging, os, signal, subprocess, sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)
AGY_MARKER_BASE_URL = "agy://local"
_CREATE_NO_WINDOW = 0x08000000

def _pdeathsig_preexec(parent_pid: int):
    def _setup() -> None:
        pass
    return _setup

def _own_process_group() -> dict[str, Any]:
    """Popen kwargs for background processes in a group we can kill cleanly.

    On Linux the child also gets PR_SET_PDEATHSIG(SIGKILL): if the hosting
    Hermes worker dies without running the plugin's cleanup (crash, dispatcher
    kill), the kernel terminates agy instead of leaving it reparented to init,
    holding its request open and its memory (issue #50). The preexec_fn
    re-checks the parent after fork to close the classic PDEATHSIG race where
    the parent dies between fork and prctl.
    """
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200) | _CREATE_NO_WINDOW}
    kwargs: dict[str, Any] = {"start_new_session": True}
    if sys.platform == "linux":
        kwargs["preexec_fn"] = _pdeathsig_preexec(os.getpid())
    return kwargs

def terminate_process(proc: Any) -> None:
    if os.name != "nt":
        pid = getattr(proc, "pid", None)
        if isinstance(pid, int) and pid > 0:
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(pid), signal.SIGKILL)
    with contextlib.suppress(Exception):
        proc.kill()
    with contextlib.suppress(Exception):
        proc.wait(timeout=2)

def _kill_process_tree(pid: int) -> None:
    if pid > 0 and os.name != "nt":
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(pid), signal.SIGKILL)

def is_authenticated() -> bool:
    return True

def resolve_agy_command() -> str:
    return "agy"

def resolve_real_token_path():
    return None

def setup_isolated_home(cwd: str) -> tuple[Path, Path]:
    home = Path(cwd) / "home"
    gdir = home / ".gemini" / "antigravity-cli"
    gdir.mkdir(parents=True, exist_ok=True)
    return home, gdir

def sync_isolated_token(home: Path) -> None:
    pass

def read_media_rules(gdir: Path) -> frozenset[str]:
    return frozenset()

def write_media_read_permissions(gdir: Path, paths: set[str]) -> None:
    pass

def build_child_env(home: Path, **_: Any) -> dict[str, str]:
    return {"HOME": str(home)}

def reap_orphaned_plugin_workers() -> None:
    pass

def register_tracked_process(proc: Any) -> None:
    pass

def start_stderr_drainer(proc: Any) -> None:
    pass

def _check_early_quota_error(*_: Any, **__: Any) -> str | None:
    return None
'''


def _build_support_fixtures() -> dict[str, str]:
    models_src = '''from __future__ import annotations
_FALLBACK_MODELS = []
_MODEL_ALIASES = {}
def _normalize_effort(effort):
    return effort
def resolve_model_and_effort(model, explicit_effort=None, *, command=None):
    return model or "gemini-4-hydrogen-high", explicit_effort or "high"
'''
    prompt_src = '''from __future__ import annotations
import json, re
from types import SimpleNamespace

_PROMPT_PREAMBLE = "BASE PREAMBLE"
_ROLE_LABELS = {"user": "User", "assistant": "Assistant"}
_TOOL_CALL_OPEN = "<tool_call>"
_TOOL_CALL_CLOSE = "</tool_call>"
_MEDIA_RE = re.compile(r"!\\[[^\\]]*\\]\\((/[^)]+)\\)|\\[Image\\]\\((/[^)]+)\\)")

class _MarkdownCodeState:
    def advance(self, text: str) -> None:
        pass
    def clone(self) -> "_MarkdownCodeState":
        return _MarkdownCodeState()
    def in_code(self) -> bool:
        return False

def _find_tool_call_close(text: str) -> int:
    return text.find(_TOOL_CALL_CLOSE)

def _find_unquoted_tool_call_open(text: str, state: _MarkdownCodeState) -> int:
    return text.find(_TOOL_CALL_OPEN)

def _has_closed_tool_call_frame(text: str) -> bool:
    return _TOOL_CALL_OPEN in text and _TOOL_CALL_CLOSE in text

def _has_unfinished_tool_call_frame(text: str, state: _MarkdownCodeState) -> bool:
    return text.find(_TOOL_CALL_OPEN) != -1 and _TOOL_CALL_CLOSE not in text

def _has_unparseable_tool_call_frame(text: str, state: _MarkdownCodeState) -> bool:
    return False

def _longest_tool_call_prefix_match(text: str) -> int:
    return 0

def _parse_tool_block(text: str, state: Any = None):
    m = re.search(r"<tool_call>\\s*(\\{.*?\\})\\s*</tool_call>", text, re.DOTALL)
    if not m:
        return [], text
    try:
        payload = json.loads(m.group(1))
    except Exception:
        return [], text
    call = SimpleNamespace(
        id="call_1",
        type="function",
        function=SimpleNamespace(
            name=payload.get("name", ""),
            arguments=json.dumps(payload.get("arguments", {})),
        ),
    )
    extra = (text[:m.start()] + text[m.end():]).strip()
    return [call], extra

def _format_messages_as_prompt(messages, *, model=None, tools=None, tool_choice=None, media_sink=None) -> str:
    parts = [str(m.get("content", "")) for m in (messages or [])]
    if tools:
        parts.append("TOOLS:" + json.dumps([t.get("function", {}).get("name") for t in tools]))
    return "\\n".join(parts)

def _format_delta_prompt(messages, *, pending_user_text="", media_sink=None) -> str:
    return pending_user_text or "\\n".join(str(m.get("content", "")) for m in (messages or []))

def _latest_user_text(messages) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""

def _messages_match_prefix(ref_history, messages) -> bool:
    return bool(ref_history) and len(messages) > len(ref_history) and messages[:len(ref_history)] == ref_history

def _render_message_content(content, media_sink=None) -> str:
    return str(content)

def _is_plugin_owned_media_path(path: str) -> bool:
    return False

def media_reference_paths(messages, media_sink=None) -> set[str]:
    paths: set[str] = set()
    for m in messages or []:
        c = str(m.get("content", ""))
        for g1, g2 in _MEDIA_RE.findall(c):
            p = g1 or g2
            if p:
                paths.add(p)
    return paths
'''
    delivery_src = '''from __future__ import annotations
class DeliveryWatch:
    def __init__(self):
        self.last_sample = None
    def observe(self, prompt_chars: int, tokens: int, sample_key: str = "") -> None:
        self.last_sample = type("Sample", (), {"prompt_chars": prompt_chars, "input_tokens": tokens})()
'''
    debug_src = '''from __future__ import annotations
def dump_prompt_debug(**_: Any) -> None:
    pass
'''
    return {
        "models.py": models_src,
        "prompt.py": prompt_src,
        "delivery.py": delivery_src,
        "debug.py": debug_src,
    }


def _build_stream_fixture() -> str:
    return f'''from __future__ import annotations
import contextlib, json, logging, os, re, subprocess, threading, time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any, Iterator, NamedTuple

from process import _check_early_quota_error
from prompt import (
    _TOOL_CALL_CLOSE,
    _MarkdownCodeState,
    _find_tool_call_close,
    _find_unquoted_tool_call_open,
    _has_closed_tool_call_frame,
    _has_unfinished_tool_call_frame,
    _has_unparseable_tool_call_frame,
    _longest_tool_call_prefix_match,
    _parse_tool_block,
    media_reference_paths,
)

logger = logging.getLogger(__name__)
_NATIVE_MEDIA_TOOLS = frozenset({{"read_file", "view_file"}})
_TOOL_INFO_PATH_RE = re.compile(r"(/[^\\s\\\"'<>]+)")

def _path_is_native_media_target(candidate: str, media_paths: set[str]) -> bool:
    return candidate in media_paths

class _UsageDeltas(NamedTuple):
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_read_tokens: int

def _counter_delta(usage_data: dict[str, Any], baseline: dict[str, int], field: str) -> int:
    if field not in usage_data:
        return 0
    value = int(usage_data[field] or 0)
    previous = baseline.get(field)
    if previous is None or value < previous:
        baseline[field] = value
        return value
    delta = value - previous
    baseline[field] = value
    return delta

def _delta_worker_usage(usage_data: dict[str, Any], baseline: dict[str, int]) -> _UsageDeltas:
    input_delta = _counter_delta(usage_data, baseline, "input_tokens")
    output_delta = _counter_delta(usage_data, baseline, "output_tokens")
    if "total_tokens" in usage_data:
        total_delta = _counter_delta(usage_data, baseline, "total_tokens")
    else:
        total_delta = input_delta + output_delta
    return _UsageDeltas(
        input_tokens=input_delta,
        output_tokens=output_delta,
        total_tokens=total_delta,
        cache_read_tokens=_counter_delta(usage_data, baseline, "cache_read_tokens"),
    )

class AntigravityStream(Iterator[Any]):
    def __init__(
        self,
        *,
        proc: subprocess.Popen,
        client: Any,
        model: str,
        timeout: float,
        tools: list[dict[str, Any]] | None = None,
        is_worker: bool = False,
        worker_lock: threading.Lock | None = None,
        worker_lock_held: bool | None = None,
        messages: list[dict[str, Any]] | None = None,
        usage_baseline: dict[str, int] | None = None,
        prompt_chars: int = 0,
        delivery_watch: Any = None,
        delivery_sample_key: str = "",
    ):
        self.proc = proc
        self.client = client
        self.model = model
        self.timeout = timeout
        self.has_tools = bool(tools)
        self.is_worker = is_worker
        self.worker_lock = worker_lock
        self.messages = messages
        self._media_paths = self._collect_media_reference_paths()
        self._worker_lock_held = bool(worker_lock_held and worker_lock is not None)
        self.usage_baseline = usage_baseline
        self.prompt_chars = max(int(prompt_chars or 0), 0)
        self.delivery_watch = delivery_watch
        self.delivery_sample_key = delivery_sample_key
        self.conversation_id = ""
        self._closed = False
        self._interrupted = False
        self._finished = False
        self._early_error: str | None = None
        self._generator = self._stream_generator()
        self._async_executor: ThreadPoolExecutor | None = None

    def __iter__(self):
        return self

    def _raise_if_cancelled(self) -> None:
        if getattr(self.client, "_cancelled", False) is True:
            self._interrupted = True
            self.close()
            raise RuntimeError("Antigravity request cancelled.")

    def __next__(self):
        self._raise_if_cancelled()
        if self._closed:
            raise StopIteration
        try:
            return next(self._generator)
        except StopIteration:
            self._closed = True
            if not self.is_worker:
                self.close()
            raise
        except Exception:
            self.close()
            raise

    def _release_worker_lock_once(self) -> None:
        if self._worker_lock_held:
            self._worker_lock_held = False
            with contextlib.suppress(Exception):
                self.worker_lock.release()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self.is_worker:
            if not self._finished and self.proc is self.client._worker_proc:
                self._interrupted = True
                self.client._terminate_worker()
            self._release_worker_lock_once()
        else:
            with self.client._lock:
                self.client._terminate_process(self.proc)
                self.client._active_processes.discard(self.proc)

    def _collect_media_reference_paths(self) -> set[str]:
        return media_reference_paths(self.messages)

    def _is_permitted_native_media_step(self, tool_name: str, tool_info: str) -> bool:
        if tool_name not in _NATIVE_MEDIA_TOOLS:
            return False
        for candidate in _TOOL_INFO_PATH_RE.findall(tool_info):
            if _path_is_native_media_target(candidate, self._media_paths):
                return True
        return False

    def _make_chunk(self, *, content=None, tool_calls=None, finish_reason=None):
        delta = SimpleNamespace(role="assistant", content=content, tool_calls=tool_calls, reasoning=None, reasoning_content=None)
        choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
        return SimpleNamespace(id=self.conversation_id or "agy-test", choices=[choice], model=self.model, usage=None)

    def _usage_totals(self, usage_data: dict[str, Any]) -> tuple[int, int, int, int]:
        if self.is_worker and self.usage_baseline is not None:
            deltas = _delta_worker_usage(usage_data, self.usage_baseline)
            return (
                deltas.input_tokens,
                deltas.output_tokens,
                deltas.total_tokens,
                deltas.cache_read_tokens,
            )
        input_tokens = int(usage_data.get("input_tokens", 0) or 0)
        output_tokens = int(usage_data.get("output_tokens", 0) or 0)
        total_tokens = int(usage_data.get("total_tokens", input_tokens + output_tokens) or 0)
        cached_tokens = int(usage_data.get("cache_read_tokens", 0) or 0)
        return input_tokens, output_tokens, total_tokens, cached_tokens

    def _empty_result_message(self, **kw: Any) -> str:
        return f"empty/no result ({{kw}})"

    def _get_stderr_tail(self) -> str:
        return ""

    def _stream_generator(self) -> Iterator[Any]:
        deadline = time.monotonic() + self.timeout
        text_buffer = ""
        usage_data: dict[str, Any] = {{}}
        has_tool_calls = False
        used_tool_call_ids: set[str] = set()
        next_tool_call_index = 0

        def identify_calls(calls):
            nonlocal next_tool_call_index
            for call in calls:
                call.index = next_tool_call_index
                next_tool_call_index += 1
                yield call

        has_content = False
        in_tool_call = False
        md_state = _MarkdownCodeState()
        error_msg = ""
        status = ""
        saw_result = False
        neutralized_tool_step = False
        neutralized_text = ""
        success = False
        start_time = time.monotonic()
        start_wall = time.time()
        stop_reason: str | None = None
        last_stdout_at: float | None = None

        gemini_dir = getattr(self.client, "_isolated_gemini_dir", None)
        watchdog_stop = threading.Event()
        watchdog_thread: threading.Thread | None = None

        if gemini_dir:
            def _watchdog_loop() -> None:
                while not watchdog_stop.wait(timeout=0.3):
                    if time.monotonic() - start_time < 1.0:
                        continue
                    quota_err = _check_early_quota_error(gemini_dir, min_mtime=start_wall)
                    if quota_err:
                        self._early_error = quota_err
                        logger.warning(
                            "Antigravity process hit early quota limit: %s; terminating to prevent hang.",
                            quota_err,
                        )
                        self.client._terminate_process(self.proc)
                        break

            watchdog_thread = threading.Thread(
                target=_watchdog_loop,
                name="agy-quota-watchdog",
                daemon=True,
            )
            watchdog_thread.start()

        try:
            while time.monotonic() < deadline:
                self._raise_if_cancelled()
                line = self.proc.stdout.readline() if self.proc.stdout else ""
                if not line:
                    if self.proc.poll() is not None:
                        stop_reason = "stdout closed"
                        break
                    break
                last_stdout_at = time.monotonic()
                line = line.strip()
                if not line.startswith("{{"):
                    continue
                event = json.loads(line)
                event_type = event.get("event")
                if event_type != "init":
                    watchdog_stop.set()
                if not self.conversation_id:
                    self.conversation_id = event.get("conversation_id", "")

                if event_type == "init":
                    init_data = event.get("init", {{}})
                    if not self.conversation_id:
                        self.conversation_id = init_data.get("conversation_id", "")
                    if self.conversation_id and self.is_worker and self.client and hasattr(self.client, "_set_worker_conversation_id"):
                        self.client._set_worker_conversation_id(self.conversation_id)

                elif event_type == "step_update":
                    step = event.get("step_update", {{}})
                    if step.get("step_type") == "tool":
                        tool_name = str(step.get("tool_name") or "")
                        if self._is_permitted_native_media_step(tool_name, str(step.get("tool_info") or "")):
                            logger.info("Antigravity native media attach allowed: %s", tool_name)
                        else:
                            logger.warning(
                                "Antigravity attempted native tool invocation '%s'; neutralizing to prevent host execution.",
                                step.get("tool_name"),
                            )
                            self.close()
                            # Recorded so the post-loop positive-evidence check
                            # does not treat this security abort as an empty
                            # failed turn: the process is terminated right here
                            # on purpose (see
                            # test_native_tool_step_neutralization_in_stream).
                            # The stop cause is recorded too, so diagnostics do
                            # not misreport this deliberate abort as a deadline.
                            neutralized_tool_step = True
                            stop_reason = "native tool step neutralized"
                            # Issue #52: a bare empty completion reads as "no
                            # content or reasoning", so Hermes nudged, got the
                            # same silence on every retry, and silently failed
                            # the turn over to a different provider while the
                            # quota meter still showed headroom. The block is
                            # surfaced as real content instead: the host sees
                            # WHY the turn stopped, the quota was never denied,
                            # and on any Hermes retry the explanation sits in
                            # the model's history so it can re-issue the step
                            # as a <tool_call> block. Containment is unchanged:
                            # the process dies here, nothing was executed.
                            #
                            # The chunk is emitted in ONE piece together with
                            # the finish reason at the post-loop finish yield:
                            # a yield here would suspend the generator after
                            # close(), and __next__'s ``if self._closed`` guard
                            # would end the stream before the finish chunk
                            # could ever be emitted.
                            has_content = True
                            neutralized_text = (
                                f"[Antigravity] Native tool invocation '{{tool_name}}' was blocked by host security policy; "
                                "nothing was executed. Re-issue the step as a <tool_call> block using the host's tools "
                                "instead of Antigravity's native tools."
                            )
                            break
                    if "usage" in step:
                        usage_data = step["usage"]
                    text_delta = step.get("text_delta")
                    if text_delta:
                        if not self.has_tools:
                            has_content = True
                            yield self._make_chunk(content=text_delta)
                        else:
                            text_buffer += text_delta
                            parsed_calls, extra_text = _parse_tool_block(text_buffer)
                            if parsed_calls:
                                has_tool_calls = True
                                for call_delta in identify_calls(parsed_calls):
                                    yield self._make_chunk(tool_calls=[call_delta])
                                text_buffer = extra_text
                            elif "<tool_call>" not in text_buffer:
                                has_content = True
                                yield self._make_chunk(content=text_buffer)
                                text_buffer = ""

                elif event_type == "result":
                    res = event.get("result", {{}})
                    if not self.conversation_id:
                        self.conversation_id = res.get("conversation_id", "")
                    if self.conversation_id and self.is_worker and self.client and hasattr(self.client, "_set_worker_conversation_id"):
                        self.client._set_worker_conversation_id(self.conversation_id)
                    status = res.get("status", "")
                    if "usage" in res:
                        usage_data = res["usage"]
                    if "error" in res:
                        error_msg = res["error"]
                    saw_result = True
                    final_resp = res.get("response", "")
                    if final_resp and not has_content and not has_tool_calls and not text_buffer:
                        text_buffer = final_resp
                    success = (
                        status == "SUCCESS"
                        and not error_msg
                        and bool(has_content or has_tool_calls or text_buffer or final_resp)
                    )
                    break

            if not saw_result and stop_reason is None:
                stop_reason = f"stream deadline {{self.timeout:g}}s reached"
            now = time.monotonic()
            stop_detail = f"{{stop_reason or 'result'}} after {{now - start_time:.1f}}s"
            if text_buffer and not has_tool_calls and not neutralized_tool_step:
                parsed_calls, extra_text = _parse_tool_block(text_buffer, state=md_state)
                if parsed_calls:
                    has_tool_calls = True
                    for call_delta in identify_calls(parsed_calls):
                        yield self._make_chunk(tool_calls=[call_delta])
                elif not has_content:
                    has_content = True
                    yield self._make_chunk(content=text_buffer)
            if not neutralized_tool_step and not success:
                raise RuntimeError("Antigravity execution failed: " + self._empty_result_message(saw_result=saw_result, status=status))

            finish_reason = "tool_calls" if has_tool_calls else "stop"
            input_tokens, output_tokens, total_tokens, cached_tokens = self._usage_totals(usage_data)

            if neutralized_tool_step:
                # Issue #52: the security abort carries its explanation and
                # the finish reason in one chunk {EM_DASH} content makes the host
                # treat the turn as answered instead of empty.
                yield self._make_chunk(content=neutralized_text, finish_reason=finish_reason)
            else:
                yield self._make_chunk(finish_reason=finish_reason)

            usage = SimpleNamespace(
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
                total_tokens=total_tokens,
                prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens),
            )
            yield SimpleNamespace(
                id=self.conversation_id or f"agy-{{int(time.time() * 1000)}}",
                choices=[],
                model=self.model,
                usage=usage,
            )
            self._finished = True
        except Exception:
            success = False
            raise
        finally:
            watchdog_stop.set()
            if watchdog_thread and watchdog_thread.is_alive():
                watchdog_thread.join(timeout=0.2)
            if self.is_worker:
                if success and not self._interrupted and getattr(self.client, "_cancelled", False) is not True:
                    self.client._update_worker_history(self.messages)
                    self._release_worker_lock_once()
                else:
                    self.close()
            else:
                self.close()

def collect_stream_completion(stream: AntigravityStream) -> Any:
    chunks = list(stream)
    usage = None
    for c in chunks:
        if getattr(c, "usage", None) is not None:
            usage = c.usage
    if not usage:
        usage = SimpleNamespace(
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            prompt_tokens_details=SimpleNamespace(cached_tokens=0),
        )
    return SimpleNamespace(chunks=chunks, usage=usage)
'''


def _build_client_fixture() -> str:
    return '''from __future__ import annotations
import json, logging, os, subprocess, tempfile, threading, time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from models import _FALLBACK_MODELS, _MODEL_ALIASES, _normalize_effort, resolve_model_and_effort
from process import (
    AGY_MARKER_BASE_URL,
    _kill_process_tree,
    _own_process_group,
    build_child_env,
    is_authenticated,
    read_media_rules,
    reap_orphaned_plugin_workers,
    register_tracked_process,
    resolve_agy_command,
    resolve_real_token_path,
    setup_isolated_home,
    start_stderr_drainer,
    sync_isolated_token,
    terminate_process,
    write_media_read_permissions,
)
from prompt import (
    _PROMPT_PREAMBLE,
    _ROLE_LABELS,
    _format_delta_prompt,
    _latest_user_text,
    _format_messages_as_prompt,
    _longest_tool_call_prefix_match,
    _messages_match_prefix,
    _parse_tool_block,
    _render_message_content,
    _is_plugin_owned_media_path,
    media_reference_paths,
)
from delivery import DeliveryWatch
from stream import AntigravityStream, collect_stream_completion
from debug import dump_prompt_debug

logger = logging.getLogger(__name__)
_DEFAULT_TIMEOUT_SECONDS = 300.0

class AntigravityClient:
    def __init__(self, *, cwd: str | None = None, command: str | None = None, args=None, **_):
        self._command = command or resolve_agy_command()
        self._args = list(args or ["--output-format", "stream-json", "--disable-slash-commands"])
        self._temp_dir = None
        if cwd:
            self._cwd = cwd
        else:
            self._temp_dir = tempfile.TemporaryDirectory(prefix="hermes_agy_")
            self._cwd = self._temp_dir.name
        self._isolated_home, self._isolated_gemini_dir = setup_isolated_home(self._cwd)
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create_chat_completion))
        self.is_closed = False
        self._cancelled = False
        self._lock = threading.RLock()
        self._worker_lock = threading.Lock()
        self._worker_proc = None
        self._worker_model = None
        self._worker_effort = None
        self._worker_conversation_id = None
        self._worker_history = []
        self._conversation_history = []
        self._worker_media_rules = frozenset()
        self._worker_usage_baseline = {}
        self._active_processes = set()
        self._worker_last_activity = {}
        self._worker_cpu_at_activity = {}
        self._worker_idle_timer = None
        self._delivery_watch = DeliveryWatch()
        self._materialized_media = []

    def _raise_if_cancelled(self):
        if self._cancelled:
            raise RuntimeError("cancelled")

    def _resolve_model_and_effort(self, model=None, explicit_effort=None):
        return resolve_model_and_effort(model, explicit_effort, command=self._command)

    def _child_env(self):
        return build_child_env(self._isolated_home)

    def _terminate_process(self, proc):
        terminate_process(proc)

    def _cancel_idle_timer_locked(self):
        pass

    def _arm_idle_timer_locked(self):
        pass

    def _terminate_worker_locked(self, keep_conversation: bool = False) -> None:
        self._cancel_idle_timer_locked()
        if self._worker_proc is not None:
            proc = self._worker_proc
            self._worker_proc = None
            self._worker_model = None
            self._worker_effort = None
            self._worker_history = []
            self._active_processes.discard(proc)
            self._worker_last_activity.pop(proc, None)
            self._worker_cpu_at_activity.pop(proc, None)
            self._terminate_process(proc)
        # The session is gone: any later usage from it belongs to a dead
        # session, and the next spawn must start from a clean baseline.
        self._worker_usage_baseline = {}
        # Full-send diagnostics compare only fresh-context requests, never
        # session counters or deltas. Their reference survives worker resets.
        if not keep_conversation:
            self._worker_conversation_id = None
            self._conversation_history = []

    def _terminate_worker(self, keep_conversation: bool = False) -> None:
        with self._lock:
            self._terminate_worker_locked(keep_conversation=keep_conversation)

    def _set_worker_conversation_id(self, conv_id: str) -> None:
        with self._lock:
            if conv_id and not self._cancelled:
                self._worker_conversation_id = conv_id

    def _purge_isolated_conversations(self) -> None:
        pass

    def _update_worker_history(self, messages) -> None:
        with self._lock:
            msg_list = list(messages or [])
            self._worker_history = msg_list
            self._conversation_history = msg_list

    def _get_or_spawn_worker(
        self,
        model: str,
        effort: str | None,
        conversation_id: str | None = None,
    ) -> subprocess.Popen:
        with self._lock:
            self._raise_if_cancelled()
            if (
                self._worker_proc is not None
                and self._worker_proc.poll() is None
                and self._worker_model == model
                and self._worker_effort == effort
            ):
                return self._worker_proc
            self._terminate_worker_locked(keep_conversation=bool(conversation_id))
            sync_isolated_token(self._isolated_home)
            cmd_args = [self._command, "--input-format", "stream-json", *self._args]
            if conversation_id:
                cmd_args.extend(["--conversation", conversation_id])
            if model:
                cmd_args.extend(["--model", model])
            if effort:
                cmd_args.extend(["--effort", effort])
            proc = subprocess.Popen(
                cmd_args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=self._child_env(),
                **_own_process_group(),
            )
            start_stderr_drainer(proc)
            self._worker_proc = proc
            self._worker_model = model
            self._worker_effort = effort
            self._worker_history = list(self._conversation_history) if conversation_id else []
            self._worker_media_rules = read_media_rules(self._isolated_gemini_dir)
            self._worker_usage_baseline = {}
            self._active_processes.add(proc)
            register_tracked_process(proc)
            return proc

    def _run_oneshot_completion(
        self,
        *,
        model: str,
        effort: str | None,
        messages: list[dict[str, Any]],
        timeout: float,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        stream: bool,
    ) -> Any:
        prompt_text = _format_messages_as_prompt(
            messages,
            model=model,
            tools=tools,
            tool_choice=tool_choice,
            media_sink=self._materialized_media,
        )
        proc = subprocess.Popen([self._command, *self._args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        stream_iter = AntigravityStream(
            proc=proc,
            client=self,
            model=model,
            timeout=timeout,
            tools=tools,
            is_worker=False,
            prompt_chars=len(prompt_text),
            # The turn's messages feed the native media-attach allowlist
            # (read_file/view_file on referenced media paths only).
            messages=messages,
            # Oneshots run concurrently when the worker is busy. They have no
            # comparable prior sample and must not mutate the worker watch.
            delivery_watch=None,
        )
        return stream_iter if stream else collect_stream_completion(stream_iter)

    def _create_chat_completion(
        self,
        *,
        model: str | None = None,
        messages: list[dict[str, Any]] | None = None,
        timeout: float | None = None,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        stream: bool = False,
        reasoning_effort: str | None = None,
        **extra_kwargs: Any,
    ) -> Any:
        resolved_model, effort = self._resolve_model_and_effort(model, reasoning_effort)
        messages_list = list(messages or [])
        effective_timeout = float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else _DEFAULT_TIMEOUT_SECONDS
        worker_acquired = self._worker_lock.acquire(blocking=False)
        if worker_acquired:
            try:
                with self._lock:
                    ref_history = (
                        self._worker_history
                        if (self._worker_proc is not None and self._worker_proc.poll() is None)
                        else self._conversation_history
                    )
                    is_continuation = _messages_match_prefix(ref_history, messages_list)
                    conv_id_to_resume = self._worker_conversation_id if is_continuation else None
                comparable_full_send = not is_continuation
                if is_continuation:
                    proc = self._get_or_spawn_worker(resolved_model, effort, conversation_id=conv_id_to_resume)
                    delta_msgs = messages_list[len(ref_history):]
                    prompt_payload = _format_delta_prompt(delta_msgs, pending_user_text=_latest_user_text(messages_list), media_sink=self._materialized_media)
                else:
                    with self._lock:
                        if self._worker_proc is not None or self._worker_conversation_id:
                            self._terminate_worker_locked(keep_conversation=False)
                    proc = self._get_or_spawn_worker(resolved_model, effort, conversation_id=None)
                    prompt_payload = _format_messages_as_prompt(messages_list, model=resolved_model, tools=tools, tool_choice=tool_choice, media_sink=self._materialized_media)
                event_msg = {"event": "user", "message": {"content": prompt_payload}}
                proc.stdin.write(json.dumps(event_msg) + "\\n")
                proc.stdin.flush()
                with self._lock:
                    worker_usage_baseline = self._worker_usage_baseline
                stream_iter = AntigravityStream(
                    proc=proc,
                    client=self,
                    model=resolved_model,
                    timeout=effective_timeout,
                    tools=tools,
                    is_worker=True,
                    worker_lock=self._worker_lock,
                    worker_lock_held=worker_acquired,
                    messages=messages_list,
                    usage_baseline=worker_usage_baseline,
                    prompt_chars=len(prompt_payload),
                    delivery_watch=self._delivery_watch if comparable_full_send else None,
                    delivery_sample_key=f"{resolved_model}:{effort}",
                )
                if stream:
                    return stream_iter
                if False:
                    stream_iter = AntigravityStream(
                        proc=proc,
                        client=self,
                        model=resolved_model,
                        timeout=effective_timeout,
                        tools=tools,
                        is_worker=True,
                        worker_lock=self._worker_lock,
                        worker_lock_held=True,
                        messages=messages_list,
                        usage_baseline=worker_usage_baseline,
                        prompt_chars=len(prompt_payload),
                        delivery_watch=self._delivery_watch if comparable_full_send else None,
                        delivery_sample_key=f"{resolved_model}:{effort}",
                    )
                completion = collect_stream_completion(stream_iter)
                if self._worker_lock.locked():
                    self._worker_lock.release()
                return completion
            except Exception:
                self._terminate_worker()
                if self._worker_lock.locked():
                    self._worker_lock.release()
                raise
        return self._run_oneshot_completion(
            model=resolved_model,
            effort=effort,
            messages=messages_list,
            timeout=effective_timeout,
            tools=tools,
            tool_choice=tool_choice,
            stream=stream,
        )

    def close(self) -> None:
        self.is_closed = True
        self._terminate_worker(keep_conversation=False)
        if self._temp_dir is not None:
            self._temp_dir.cleanup()
'''


def _build_main_test_fixture() -> str:
    return f'''import json, sys, tempfile, unittest
from unittest.mock import MagicMock, patch

class AntigravityPluginTests(unittest.TestCase):
    def test_native_tool_step_neutralization_in_stream(self):
        # TemporaryDirectory (not /tmp): on Windows "/tmp" resolves to a
        # drive-rooted \\tmp with no guaranteed write access, and an
        # explicit cwd is never cleaned by close() {EM_DASH} addCleanup removes it.
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        client = AntigravityClient(cwd=tmp_dir.name)
        fake_events = [
            json.dumps({{"event": "init", "conversation_id": "test-sec-conv"}}),
            json.dumps({{"event": "step_update", "step_update": {{"step_type": "tool", "tool_name": "run_command"}}}}),
        ]
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout.readline.side_effect = [f"{{line}}\\n" for line in fake_events] + [""]
        mock_proc.stdin = MagicMock()

        with patch("subprocess.Popen", return_value=mock_proc), patch.object(client, "_terminate_process") as mock_term:
            stream = client.chat.completions.create(
                model="gemini-3.8-flash",
                messages=[{{"role": "user", "content": "hi"}}],
                stream=True,
            )
            chunks = list(stream)
            mock_term.assert_called_once_with(mock_proc)
        # Issue #52: the abort is surfaced as explanatory content instead of
        # a bare empty completion that Hermes classified as "no content" and
        # silently failed over to another provider.
        contents = [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]
        self.assertEqual(len(contents), 1)
        self.assertIn("run_command", contents[0])
        self.assertIn("blocked by host security policy", contents[0])
        self.assertIn("<tool_call>", contents[0])
        finishes = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]
        self.assertEqual(finishes, ["stop"])

    def test_windows_process_group_creation(self):
        from client import _own_process_group
        with patch("os.name", "nt"):
            flags = _own_process_group()
            # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            self.assertEqual(flags, {{"creationflags": 0x08000200}})
        with patch("os.name", "posix"):
            flags = _own_process_group()
            # Linux additionally arms PR_SET_PDEATHSIG via preexec_fn
            # (issue #50); macOS/other POSIX keep the bare setsid contract.
            self.assertEqual(flags.pop("start_new_session", None), True)
            if sys.platform == "linux":
                self.assertTrue(callable(flags.pop("preexec_fn", None)))
            self.assertEqual(flags, {{}})
'''


def _write_v1029_plugin_fixture(plugin_dir: Path) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    tests_dir = plugin_dir / "tests"
    tests_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "process.py").write_text(_build_process_fixture_clean(), encoding="utf-8")
    for fname, content in _build_support_fixtures().items():
        (plugin_dir / fname).write_text(content, encoding="utf-8")
    (plugin_dir / "stream.py").write_text(_build_stream_fixture(), encoding="utf-8")
    (plugin_dir / "client.py").write_text(_build_client_fixture(), encoding="utf-8")
    (tests_dir / "test_antigravity_plugin.py").write_text(_build_main_test_fixture(), encoding="utf-8")


@contextlib.contextmanager
def _import_patched_plugin(plugin_dir: Path):
    mod_names = ("process", "models", "prompt", "delivery", "debug", "stream", "client")
    saved = {name: sys.modules.pop(name, None) for name in mod_names}
    sys.path.insert(0, str(plugin_dir))
    try:
        loaded = {}
        for name in mod_names:
            spec = importlib.util.spec_from_file_location(name, plugin_dir / f"{name}.py")
            assert spec and spec.loader
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
            loaded[name] = mod
        yield loaded
    finally:
        if str(plugin_dir) in sys.path:
            sys.path.remove(str(plugin_dir))
        for name in mod_names:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


class TestAgyV1029Patches(unittest.TestCase):
    def test_patch_applies_cleanly_idempotently_and_fails_loudly_on_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_v1029_plugin_fixture(plugin_dir)

            rc1 = agy_patch.main(["--plugin-dir", str(plugin_dir)])
            self.assertEqual(rc1, 0)
            self.assertIn(agy_patch.MARKER_PROCESS, (plugin_dir / "process.py").read_text(encoding="utf-8"))
            self.assertIn(agy_patch.MARKER_CLIENT, (plugin_dir / "client.py").read_text(encoding="utf-8"))
            self.assertIn(agy_patch.MARKER_STREAM, (plugin_dir / "stream.py").read_text(encoding="utf-8"))

            # Idempotent re-run succeeds with zero changes
            before_client = (plugin_dir / "client.py").read_text(encoding="utf-8")
            rc2 = agy_patch.main(["--plugin-dir", str(plugin_dir)])
            self.assertEqual(rc2, 0)
            self.assertEqual((plugin_dir / "client.py").read_text(encoding="utf-8"), before_client)

        # Missing anchor in any module must fail loudly with ValueError
        for broken_file, target_str in (
            ("process.py", "def _own_process_group"),
            ("client.py", "# The session is gone: any later usage from it belongs to a dead"),
            ("stream.py", "if neutralized_tool_step:"),
            ("stream.py", "cache_read_tokens: int\n"),
            ("tests/test_antigravity_plugin.py", "def test_native_tool_step_neutralization_in_stream"),
        ):
            with tempfile.TemporaryDirectory() as tmpdir:
                pdir = Path(tmpdir)
                _write_v1029_plugin_fixture(pdir)
                fpath = pdir / broken_file
                fpath.write_text(fpath.read_text(encoding="utf-8").replace(target_str, "drifted_symbol", 1), encoding="utf-8")
                with self.assertRaises(ValueError):
                    if broken_file == "process.py":
                        agy_patch.patch_process(pdir)
                    elif broken_file == "client.py":
                        agy_patch.patch_client(pdir)
                    elif broken_file == "stream.py":
                        agy_patch.patch_stream(pdir)
                    else:
                        agy_patch.patch_tests(pdir)

    def test_own_process_group_survives_spawning_thread_exit(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_v1029_plugin_fixture(plugin_dir)
            agy_patch.patch_process(plugin_dir)

            with _import_patched_plugin(plugin_dir) as mods:
                proc_mod = mods["process"]
                with patch("os.name", "posix"):
                    kwargs = proc_mod._own_process_group()
                    self.assertEqual(kwargs, {"start_new_session": True})

                holder: dict[str, subprocess.Popen] = {}

                def spawn_from_ephemeral_thread() -> None:
                    holder["proc"] = subprocess.Popen(
                        [sys.executable, "-c", "import time; time.sleep(30)"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        **proc_mod._own_process_group(),
                    )

                worker_thread = threading.Thread(target=spawn_from_ephemeral_thread)
                worker_thread.start()
                worker_thread.join(timeout=5)
                self.assertFalse(worker_thread.is_alive())

                child = holder["proc"]
                try:
                    time.sleep(0.25)
                    self.assertIsNone(child.poll(), "child process was SIGKILLed when spawning thread exited")
                    self.assertEqual(os.getpgid(child.pid), child.pid)
                finally:
                    proc_mod.terminate_process(child)

    def test_baseline_preserved_across_conversation_respawn_and_cached_prompt_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_v1029_plugin_fixture(plugin_dir)
            agy_patch.main(["--plugin-dir", str(plugin_dir)])

            with _import_patched_plugin(plugin_dir) as mods:
                AntigravityClient = mods["client"].AntigravityClient
                client = AntigravityClient(cwd=tmpdir)
                self.addCleanup(client.close)

                # Replay exact cumulative usage structure observed on live agy v1.5.13
                turn1_events = [
                    {"event": "init", "conversation_id": "conv-1"},
                    {"event": "step_update", "step_update": {"text_delta": "One."}},
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "One.",
                            "usage": {
                                "input_tokens": 10428,
                                "output_tokens": 19,
                                "total_tokens": 10447,
                                "cache_read_tokens": 0,
                            },
                        },
                    },
                ]
                turn2_events = [
                    {"event": "init", "conversation_id": "conv-1"},
                    {"event": "step_update", "step_update": {"text_delta": "Two."}},
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "Two.",
                            "usage": {
                                "input_tokens": 12731,
                                "output_tokens": 38,
                                "total_tokens": 12769,
                                "cache_read_tokens": 8374,
                            },
                        },
                    },
                ]
                turn3_events = [
                    {"event": "init", "conversation_id": "conv-1"},
                    {"event": "step_update", "step_update": {"text_delta": "Three."}},
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "Three.",
                            "usage": {
                                "input_tokens": 15162,
                                "output_tokens": 59,
                                "total_tokens": 15221,
                                "cache_read_tokens": 17002,
                            },
                        },
                    },
                ]
                fresh_events = [
                    {"event": "init", "conversation_id": "conv-fresh"},
                    {"event": "step_update", "step_update": {"text_delta": "Fresh."}},
                    {
                        "event": "result",
                        "result": {
                            "status": "SUCCESS",
                            "response": "Fresh.",
                            "usage": {
                                "input_tokens": 500,
                                "output_tokens": 12,
                                "total_tokens": 512,
                                "cache_read_tokens": 0,
                            },
                        },
                    },
                ]

                def _make_proc(events_list):
                    p = MagicMock()
                    p.poll.return_value = None
                    p.stdin = MagicMock()
                    p.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events_list] + [""]
                    return p

                p_worker_a = _make_proc(turn1_events + turn2_events)
                p_worker_b = _make_proc(turn3_events)
                p_worker_fresh = _make_proc(fresh_events)

                def _usage_from(chunks):
                    for c in chunks:
                        if getattr(c, "usage", None) is not None:
                            return c.usage
                    self.fail("missing usage chunk")

                m1 = [{"role": "user", "content": "u1"}]
                m2 = m1 + [{"role": "assistant", "content": "One."}, {"role": "user", "content": "u2"}]
                m3 = m2 + [{"role": "assistant", "content": "Two."}, {"role": "user", "content": "u3"}]

                with patch("subprocess.Popen", side_effect=[p_worker_a, p_worker_b, p_worker_fresh]):
                    u1 = _usage_from(list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=m1, stream=True)))
                    self.assertEqual(u1.prompt_tokens, 10428)
                    self.assertEqual(u1.prompt_tokens_details.cached_tokens, 0)
                    self.assertEqual(u1.completion_tokens, 19)
                    self.assertEqual(u1.total_tokens, 10447)

                    u2 = _usage_from(list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=m2, stream=True)))
                    # Deltas: uncached = 12731 - 10428 = 2303, cached = 8374 -> prompt_tokens = 10677
                    self.assertEqual(u2.prompt_tokens, 10677)
                    self.assertEqual(u2.prompt_tokens_details.cached_tokens, 8374)
                    self.assertEqual(u2.completion_tokens, 19)
                    self.assertEqual(u2.total_tokens, 10696)

                    # Simulate worker exit & --conversation respawn; baseline MUST survive
                    p_worker_a.poll.return_value = 0
                    u3 = _usage_from(list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=m3, stream=True)))
                    # Deltas: uncached = 15162 - 12731 = 2431, cached = 17002 - 8374 = 8628 -> prompt_tokens = 11059
                    self.assertEqual(u3.prompt_tokens, 11059)
                    self.assertEqual(u3.prompt_tokens_details.cached_tokens, 8628)
                    self.assertEqual(u3.completion_tokens, 21)
                    self.assertEqual(u3.total_tokens, 11080)
                    self.assertEqual(
                        client._worker_usage_baseline,
                        {"input_tokens": 15162, "output_tokens": 59, "total_tokens": 15221, "cache_read_tokens": 17002},
                    )

                    # Fresh non-continuation turn MUST reset baseline before spawning fresh worker
                    u_fresh = _usage_from(
                        list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=[{"role": "user", "content": "new session"}], stream=True))
                    )
                    self.assertEqual(u_fresh.prompt_tokens, 500)
                    self.assertEqual(u_fresh.completion_tokens_details.reasoning_tokens, 0)
                    self.assertEqual(client._worker_usage_baseline["input_tokens"], 500)

    @staticmethod
    def _mock_stream_proc(events_list: list[dict[str, Any]]) -> MagicMock:
        proc = MagicMock()
        proc.poll.return_value = None
        proc.stdin = MagicMock()
        proc.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events_list] + [""]
        return proc

    def _extract_usage(self, chunks: list[Any]) -> Any:
        for chunk in chunks:
            if getattr(chunk, "usage", None) is not None:
                return chunk.usage
        self.fail("missing usage chunk")

    @staticmethod
    def _usage_turn_event(conv_id: str, text: str, usage: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {"event": "init", "conversation_id": conv_id},
            {"event": "step_update", "step_update": {"text_delta": text}},
            {"event": "result", "result": {"status": "SUCCESS", "response": text, "usage": usage}},
        ]

    def test_worker_thinking_tokens_delta_and_respawn_preservation(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_v1029_plugin_fixture(plugin_dir)
            agy_patch.main(["--plugin-dir", str(plugin_dir)])

            with _import_patched_plugin(plugin_dir) as mods:
                client = mods["client"].AntigravityClient(cwd=tmpdir)
                self.addCleanup(client.close)
                t1 = self._usage_turn_event("conv-think", "One.", {"input_tokens": 1000, "output_tokens": 50, "thinking_tokens": 1200, "total_tokens": 2250, "cache_read_tokens": 0})
                t2 = self._usage_turn_event("conv-think", "Two.", {"input_tokens": 1400, "output_tokens": 90, "thinking_tokens": 3700, "total_tokens": 5190, "cache_read_tokens": 900})
                t3 = self._usage_turn_event("conv-think", "Three.", {"input_tokens": 1700, "output_tokens": 130, "thinking_tokens": 5100, "total_tokens": 6930, "cache_read_tokens": 2000})
                p_a, p_b = self._mock_stream_proc(t1 + t2), self._mock_stream_proc(t3)

                m1 = [{"role": "user", "content": "q1"}]
                m2 = m1 + [{"role": "assistant", "content": "One."}, {"role": "user", "content": "q2"}]
                m3 = m2 + [{"role": "assistant", "content": "Two."}, {"role": "user", "content": "q3"}]

                with patch("subprocess.Popen", side_effect=[p_a, p_b]):
                    u1 = self._extract_usage(list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=m1, stream=True)))
                    u2 = self._extract_usage(list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=m2, stream=True)))
                    p_a.poll.return_value = 0
                    u3 = self._extract_usage(list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=m3, stream=True)))

                self.assertEqual(u1.completion_tokens_details.reasoning_tokens, 1200)
                self.assertEqual(u2.completion_tokens_details.reasoning_tokens, 2500)
                self.assertEqual(u3.completion_tokens_details.reasoning_tokens, 1400)
                self.assertEqual(client._worker_usage_baseline["thinking_tokens"], 5100)

    def test_oneshot_and_fallback_reasoning_tokens_emission(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_v1029_plugin_fixture(plugin_dir)
            agy_patch.main(["--plugin-dir", str(plugin_dir)])

            with _import_patched_plugin(plugin_dir) as mods:
                client = mods["client"].AntigravityClient(cwd=tmpdir)
                self.addCleanup(client.close)
                oneshot_events = self._usage_turn_event(
                    "conv-oneshot", "Solo.",
                    {"input_tokens": 300, "output_tokens": 40, "thinking_tokens": 450, "total_tokens": 790, "cache_read_tokens": 0},
                )
                p_oneshot = self._mock_stream_proc(oneshot_events)
                self.assertTrue(client._worker_lock.acquire(blocking=False))
                try:
                    with patch("subprocess.Popen", return_value=p_oneshot):
                        u_oneshot = self._extract_usage(
                            list(client.chat.completions.create(model="gemini-4-hydrogen-high", messages=[{"role": "user", "content": "oneshot"}], stream=True))
                        )
                finally:
                    client._worker_lock.release()
                self.assertEqual(u_oneshot.completion_tokens_details.reasoning_tokens, 450)
                self.assertEqual(client._worker_usage_baseline, {})

                fallback = mods["stream"].collect_stream_completion([])
                self.assertEqual(fallback.usage.completion_tokens_details.reasoning_tokens, 0)

    def test_native_tool_interception_recovers_and_blocks_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_v1029_plugin_fixture(plugin_dir)
            agy_patch.main(["--plugin-dir", str(plugin_dir)])

            with _import_patched_plugin(plugin_dir) as mods:
                AntigravityClient = mods["client"].AntigravityClient
                tools = [{"type": "function", "function": {"name": "terminal", "parameters": {}}}]

                def _make_proc(events_list):
                    p = MagicMock()
                    p.poll.return_value = None
                    p.stdin = MagicMock()
                    p.stdout.readline.side_effect = [json.dumps(e) + "\n" for e in events_list] + [""]
                    return p

                # 1. Recovery path: native "terminal" killed immediately -> 1x retry emits <tool_call> XML -> tool_calls stop
                client1 = AntigravityClient(cwd=tmpdir)
                self.addCleanup(client1.close)
                proc_bad = _make_proc([
                    {"event": "init", "conversation_id": "c-bad"},
                    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "terminal"}},
                ])
                proc_good = _make_proc([
                    {"event": "init", "conversation_id": "c-good"},
                    {"event": "step_update", "step_update": {"text_delta": '<tool_call>{"name": "terminal", "arguments": {"cmd": "ls"}}</tool_call>'}},
                    {"event": "result", "result": {"status": "SUCCESS", "usage": {"input_tokens": 50, "output_tokens": 10, "total_tokens": 60}}},
                ])
                with patch("subprocess.Popen", side_effect=[proc_bad, proc_good]), patch.object(client1, "_terminate_process") as term1:
                    chunks = list(
                        client1.chat.completions.create(
                            model="gemini-4-hydrogen-high",
                            messages=[{"role": "user", "content": "list files"}],
                            tools=tools,
                            stream=True,
                        )
                    )
                term1.assert_called_once_with(proc_bad)
                sent_retry_payload = proc_good.stdin.write.call_args[0][0]
                self.assertIn("USE <tool_call> XML BLOCKS ONLY", sent_retry_payload)
                contents = [c.choices[0].delta.content for c in chunks if c.choices and c.choices[0].delta.content]
                self.assertEqual(contents, [])
                tool_deltas = [c.choices[0].delta.tool_calls for c in chunks if c.choices and c.choices[0].delta.tool_calls]
                self.assertEqual(len(tool_deltas), 1)
                self.assertEqual(tool_deltas[0][0].function.name, "terminal")
                finishes = [c.choices[0].finish_reason for c in chunks if c.choices and c.choices[0].finish_reason]
                self.assertEqual(finishes, ["tool_calls"])

                # 2. Persistent native tool attempt on retry: kills both processes, zero leaked content, raises RuntimeError
                client2 = AntigravityClient(cwd=tmpdir)
                self.addCleanup(client2.close)
                proc_bad_a = _make_proc([
                    {"event": "init", "conversation_id": "c1"},
                    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "terminal"}},
                ])
                proc_bad_b = _make_proc([
                    {"event": "init", "conversation_id": "c2"},
                    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "terminal"}},
                ])
                collected: list[object] = []
                with patch("subprocess.Popen", side_effect=[proc_bad_a, proc_bad_b]), patch.object(client2, "_terminate_process") as term2:
                    stream = client2.chat.completions.create(
                        model="gemini-4-hydrogen-high",
                        messages=[{"role": "user", "content": "run cmd"}],
                        tools=tools,
                        stream=True,
                    )
                    with self.assertRaises(RuntimeError) as ctx:
                        for chunk in stream:
                            collected.append(chunk)
                self.assertEqual(term2.call_count, 2)
                self.assertIn("terminal", str(ctx.exception))
                self.assertEqual(collected, [])

                # 3. Partial assistant text already emitted before native tool -> fail immediately without retry
                client3 = AntigravityClient(cwd=tmpdir)
                self.addCleanup(client3.close)
                proc_partial = _make_proc([
                    {"event": "init", "conversation_id": "cp"},
                    {"event": "step_update", "step_update": {"text_delta": "Checking files... "}},
                    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "terminal"}},
                ])
                partial_chunks: list[str] = []
                with patch("subprocess.Popen", return_value=proc_partial) as popen3, patch.object(client3, "_terminate_process") as term3:
                    stream = client3.chat.completions.create(
                        model="gemini-4-hydrogen-high",
                        messages=[{"role": "user", "content": "check"}],
                        stream=True,
                    )
                    with self.assertRaises(RuntimeError):
                        for c in stream:
                            if c.choices and c.choices[0].delta.content:
                                partial_chunks.append(c.choices[0].delta.content)
                self.assertEqual(popen3.call_count, 1)
                term3.assert_called_once_with(proc_partial)
                self.assertEqual(partial_chunks, ["Checking files... "])

                # 4. Permitted native media step remains allowed
                client4 = AntigravityClient(cwd=tmpdir)
                self.addCleanup(client4.close)
                proc_media = _make_proc([
                    {"event": "init", "conversation_id": "cm"},
                    {"event": "step_update", "step_update": {"step_type": "tool", "tool_name": "read_file", "tool_info": "/tmp/receipt.jpg"}},
                    {"event": "step_update", "step_update": {"text_delta": "Total is $10."}},
                    {"event": "result", "result": {"status": "SUCCESS", "response": "Total is $10.", "usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25}}},
                ])
                with patch("subprocess.Popen", return_value=proc_media):
                    m_chunks = list(
                        client4.chat.completions.create(
                            model="gemini-4-hydrogen-high",
                            messages=[{"role": "user", "content": "Read [Image](/tmp/receipt.jpg)"}],
                            stream=True,
                        )
                    )
                self.assertEqual(
                    [c.choices[0].delta.content for c in m_chunks if c.choices and c.choices[0].delta.content],
                    ["Total is $10."],
                )
                self.assertEqual(
                    [c.choices[0].finish_reason for c in m_chunks if c.choices and c.choices[0].finish_reason],
                    ["stop"],
                )


if __name__ == "__main__":
    unittest.main()
