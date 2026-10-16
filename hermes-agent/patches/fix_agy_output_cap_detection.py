#!/usr/bin/env python3
"""Build-time patch: teach Hermes to recognise agy's output-truncation wording.

Problem
-------
When a Gemini generation via the Antigravity CLI (agy) exhausts its OUTPUT token
budget, agy raises:

    Antigravity model error: Your previous response was cut off because it
    exceeded the output token limit

Hermes' error classifier matches the generic "token limit" phrase in
``_CONTEXT_OVERFLOW_PATTERNS`` and routes the error into the context-overflow
handler (``agent/turn_overflow.py``). That handler first asks
``agent.model_metadata.is_output_cap_error`` whether this is really an output-cap
error, but that helper only fires when the message mentions ``max_tokens`` /
``max_output_tokens`` / ... which agy's wording never does. The handler then
falls through to the "unexplained rejection on a local endpoint" branch and
tells the user that another request on the server may have been holding its
context, which is false.

Fix
---
Inject an early return into ``is_output_cap_error`` for explicit output
truncation wording (unless the message also describes an oversized input).
The overflow handler then fails the turn fast with an accurate
"output cap, not a context overflow" notice instead of blaming context size.

The patch is idempotent. It exits non-zero if the upstream anchor moves, so a
base image refactor is noticed at build time instead of silently dropping the fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path("/opt/hermes/agent/model_metadata.py")
MARKER = "# odin-patch: agy output-truncation wording"

ANCHOR = (
    "def is_output_cap_error(error_msg: str) -> bool:\n"
)
BODY_ANCHOR = "    error_lower = error_msg.lower()\n"

INJECTION = (
    f"    {MARKER}\n"
    "    # agy/Gemini: \"Your previous response was cut off because it exceeded the output token limit\".\n"
    "    # No max_tokens mention, so the generic gates below miss it and the overflow handler blames context.\n"
    "    if any(p in error_lower for p in (\"output token limit\", \"response was cut off\")) and not any(\n"
    "        p in error_lower for p in _INPUT_OVERFLOW_SIGNALS\n"
    "    ):\n"
    "        return True\n"
)


def main() -> int:
    if not TARGET.is_file():
        print(f"[odin-patch] ERROR: {TARGET} not found", file=sys.stderr)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print("[odin-patch] agy output-cap patch already applied; skipping")
        return 0

    fn_start = src.find(ANCHOR)
    if fn_start == -1:
        print("[odin-patch] ERROR: is_output_cap_error() signature not found; upstream changed", file=sys.stderr)
        return 1

    body_at = src.find(BODY_ANCHOR, fn_start)
    next_def = src.find("\ndef ", fn_start + len(ANCHOR))
    if body_at == -1 or (next_def != -1 and body_at > next_def):
        print("[odin-patch] ERROR: error_lower assignment not found in is_output_cap_error()", file=sys.stderr)
        return 1

    insert_at = body_at + len(BODY_ANCHOR)
    TARGET.write_text(src[:insert_at] + INJECTION + src[insert_at:], encoding="utf-8")
    print(f"[odin-patch] patched {TARGET} (agy output-truncation detection)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
