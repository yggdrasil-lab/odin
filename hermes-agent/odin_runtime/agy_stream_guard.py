"""Antigravity stream usage accounting, native-tool retry policy, and output-cap detection."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
import os
from typing import Any, Literal

_OUTPUT_CAP_PHRASES: tuple[str, ...] = (
    "output token limit",
    "response was cut off",
)


@dataclass(frozen=True)
class UsageSnapshot:
    """Normalized per-turn token counters for OpenAI-compatible usage reporting."""

    input_tokens: int
    output_tokens: int
    cached_tokens: int
    thinking_tokens: int
    prompt_tokens: int
    total_tokens: int
    raw_total_tokens: int


@dataclass(frozen=True)
class NativeToolDecision:
    """Deterministic decision for handling an unauthorized native tool step."""

    action: Literal["retry", "fail"]
    tool_name: str
    correction_prompt: str
    error_message: str


def account_usage(
    raw: Mapping[str, Any],
    baseline: MutableMapping[str, int] | None = None,
) -> UsageSnapshot:
    """Compute per-turn usage, folding cached tokens into prompt_tokens and keeping thinking."""
    if baseline is not None:
        return _account_worker_delta(raw, baseline)
    input_tokens = int(raw.get("input_tokens", 0) or 0)
    output_tokens = int(raw.get("output_tokens", 0) or 0)
    cached_tokens = int(raw.get("cache_read_tokens", 0) or 0)
    thinking_tokens = int(raw.get("thinking_tokens", 0) or 0)
    raw_total = int(raw.get("total_tokens", input_tokens + output_tokens) or 0)
    prompt_tokens = input_tokens + cached_tokens
    total_tokens = max(raw_total, prompt_tokens + output_tokens)
    return UsageSnapshot(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        thinking_tokens=thinking_tokens,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
        raw_total_tokens=raw_total,
    )


def native_tool_decision(
    tool: str,
    *,
    retry_used: bool,
    has_output: bool,
    has_tools: bool,
) -> NativeToolDecision:
    """Decide whether to retry a native tool step once or fail fast with RuntimeError."""
    tool_name = str(tool or "")
    correction = _build_native_tool_correction(tool_name, has_tools=has_tools)
    error_message = (
        f"Antigravity attempted native tool invocation '{tool_name}' "
        "(blocked by host security policy; retry using <tool_call> XML block)"
    )
    action: Literal["retry", "fail"] = "retry" if (not retry_used and not has_output) else "fail"
    return NativeToolDecision(
        action=action,
        tool_name=tool_name,
        correction_prompt=correction,
        error_message=error_message,
    )


def is_output_cap(msg: str, input_signals: Iterable[str]) -> bool:
    """Return True when `msg` reports agy output truncation without input overflow signals."""
    error_lower = (msg or "").lower()
    if not any(phrase in error_lower for phrase in _OUTPUT_CAP_PHRASES):
        return False
    return not any(signal.lower() in error_lower for signal in input_signals)


def own_process_group_kwargs(
    *,
    os_name: str | None = None,
    creationflags: int = 0x08000200,
) -> dict[str, Any]:
    """Return Popen process-group kwargs without PR_SET_PDEATHSIG thread-death traps."""
    resolved_os = os.name if os_name is None else os_name
    if resolved_os == "nt":
        return {"creationflags": creationflags}
    return {"start_new_session": True}


def _counter_delta(
    usage_data: Mapping[str, Any],
    baseline: MutableMapping[str, int],
    field: str,
) -> int:
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


def _account_worker_delta(
    raw: Mapping[str, Any],
    baseline: MutableMapping[str, int],
) -> UsageSnapshot:
    input_delta = _counter_delta(raw, baseline, "input_tokens")
    output_delta = _counter_delta(raw, baseline, "output_tokens")
    cached_delta = _counter_delta(raw, baseline, "cache_read_tokens")
    thinking_delta = _counter_delta(raw, baseline, "thinking_tokens")
    if "total_tokens" in raw:
        raw_total = _counter_delta(raw, baseline, "total_tokens")
    else:
        raw_total = input_delta + output_delta
    prompt_tokens = input_delta + cached_delta
    total_tokens = max(raw_total, prompt_tokens + output_delta)
    return UsageSnapshot(
        input_tokens=input_delta,
        output_tokens=output_delta,
        cached_tokens=cached_delta,
        thinking_tokens=thinking_delta,
        prompt_tokens=prompt_tokens,
        total_tokens=total_tokens,
        raw_total_tokens=raw_total,
    )


def _build_native_tool_correction(tool_name: str, *, has_tools: bool) -> str:
    if has_tools:
        return (
            "\n\n### CORRECTION: USE <tool_call> XML BLOCKS ONLY\n"
            f"Your previous attempt invoked Antigravity's built-in tool '{tool_name}', "
            "which was killed immediately by host security policy without executing.\n"
            "Do NOT call Antigravity native tools. Emit ONLY a "
            '<tool_call>{"name": "...", "arguments": {...}}</tool_call> '
            "XML block using the declared host tools above."
        )
    return (
        "\n\n### CORRECTION: DO NOT INVOKE NATIVE TOOLS\n"
        f"Your previous attempt invoked Antigravity's built-in tool '{tool_name}', "
        "which was killed immediately by host security policy without executing.\n"
        "Do NOT call Antigravity native tools. Answer directly with plain text."
    )
