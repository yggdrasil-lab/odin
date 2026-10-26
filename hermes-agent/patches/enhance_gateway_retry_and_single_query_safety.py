#!/usr/bin/env python3
"""Build-time patch: improve gateway retry resilience and single-query safety error messaging.

Problem
-------
1. Kanban worker startup in `kanban_worker_client.py:run` only caught
   `GatewayUnavailableError` when waiting for owner admission. If `connect_gateway`
   raised `GatewayClientError("Gateway connection failed; no local fallback")`
   due to transient socket connection failures (e.g. while the gateway daemon is
   starting or binding its listener), the worker exited immediately with rc 1 and
   crashed the attempt instead of retrying up to the deadline.

2. In unattended execution modes (such as single-query mode `-q` or automated
   kanban runs), running commands with inline script flags (`python -c`, `bash -c`, etc.)
   triggers `_unattended_deny` with a generic message:
   "Find an alternative approach that avoids this command."
   This fails to explain why inline flags are restricted or recommend standard alternatives
   like `execute_code`, dedicated file tools, or temporary script files.

Fix
---
1. Patch `kanban_worker_client.py:run` to catch `GatewayClientError` when it represents
   a transient connection failure (containing "Gateway connection failed" or
   "Gateway connection timed out") and sleep/retry up to `deadline`.

2. Patch `tools/approval.py:_unattended_deny` to provide tailored, actionable advice when
   the flagged command is blocked due to inline code execution flags (`-c`/`-e`),
   directing the caller to `execute_code`, script files, or dedicated inspection tools.
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET_WORKER = Path("/opt/hermes/hermes_cli/kanban_worker_client.py")
MARKER_WORKER = "# odin-patch: retry transient gateway connection failures during worker startup"

ANCHOR_WORKER = (
    "        except GatewayUnavailableError as exc:\n"
    "            if exc.state not in _TRANSIENT_OWNER_STATES or time.monotonic() >= deadline:\n"
    "                raise\n"
    "            await asyncio.sleep(1)\n"
)

REPLACEMENT_WORKER = (
    "        except GatewayUnavailableError as exc:\n"
    "            if exc.state not in _TRANSIENT_OWNER_STATES or time.monotonic() >= deadline:\n"
    "                raise\n"
    "            await asyncio.sleep(1)\n"
    "        except GatewayClientError as exc:\n"
    f"            {MARKER_WORKER}\n"
    "            if \"Gateway connection failed\" not in str(exc) and \"Gateway connection timed out\" not in str(exc):\n"
    "                raise\n"
    "            if time.monotonic() >= deadline:\n"
    "                raise\n"
    "            await asyncio.sleep(1)\n"
)

TARGET_APPROVAL = Path("/opt/hermes/tools/approval.py")
MARKER_APPROVAL = "# odin-patch: clarify single-query and unattended execution error messaging for -c/-e flags"

ANCHOR_APPROVAL = (
    "def _unattended_deny(command: str, ctx: _Unattended) -> dict | None:\n"
    "    \"\"\"Deny-mode handling for one unattended context (cron / -q / webhook); None = allow.\"\"\"\n"
    "    if ctx.mode() != \"deny\":\n"
    "        return None\n"
    "\n"
    "    def block(subject: str) -> dict:\n"
    "        return {\"approved\": False, \"message\": ctx.block_message(\n"
    "            subject, noun=\"dangerous commands\",\n"
    "            advice=\"Find an alternative approach that avoids this command.\")}\n"
)

REPLACEMENT_APPROVAL = (
    "def _unattended_deny(command: str, ctx: _Unattended) -> dict | None:\n"
    "    \"\"\"Deny-mode handling for one unattended context (cron / -q / webhook); None = allow.\"\"\"\n"
    "    if ctx.mode() != \"deny\":\n"
    "        return None\n"
    "\n"
    f"    {MARKER_APPROVAL}\n"
    "    def block(subject: str, advice: str = \"Find an alternative approach that avoids this command.\") -> dict:\n"
    "        return {\"approved\": False, \"message\": ctx.block_message(\n"
    "            subject, noun=\"dangerous commands\",\n"
    "            advice=advice)}\n"
    "\n"
    "    is_dangerous, pattern_key, description = detect_dangerous_command(command)\n"
    "    if is_dangerous and not _is_permanently_approved(pattern_key):\n"
    "        if \"via -e/-c flag\" in description or \"via -c/-lc flag\" in description:\n"
    "            advice = (\n"
    "                \"Inline code flags (-c/-e) are blocked in unattended execution. \"\n"
    "                \"Use execute_code for Python logic, write scripts to temporary files via write_file and run them, \"\n"
    "                \"or use dedicated inspection tools (read_file, search_files).\"\n"
    "            )\n"
    "        else:\n"
    "            advice = \"Find an alternative approach that avoids this command.\"\n"
    "        result = block(f\"Command flagged as dangerous ({description})\", advice=advice)\n"
    "        if ctx.name == \"single_query\":\n"
    "            result.update(pattern_key=pattern_key, description=description)\n"
    "        return result\n"
    "    return None\n"
)

ANCHOR_APPROVAL_FULL = (
    "def _unattended_deny(command: str, ctx: _Unattended) -> dict | None:\n"
    "    \"\"\"Deny-mode handling for one unattended context (cron / -q / webhook); None = allow.\"\"\"\n"
    "    if ctx.mode() != \"deny\":\n"
    "        return None\n"
    "\n"
    "    def block(subject: str) -> dict:\n"
    "        return {\"approved\": False, \"message\": ctx.block_message(\n"
    "            subject, noun=\"dangerous commands\",\n"
    "            advice=\"Find an alternative approach that avoids this command.\")}\n"
    "\n"
    "    is_dangerous, pattern_key, description = detect_dangerous_command(command)\n"
    "    if is_dangerous and not _is_permanently_approved(pattern_key):\n"
    "        result = block(f\"Command flagged as dangerous ({description})\")\n"
    "        if ctx.name == \"single_query\":\n"
    "            result.update(pattern_key=pattern_key, description=description)\n"
    "        return result\n"
    "    return None\n"
)


def patch_worker(target: Path = TARGET_WORKER) -> bool:
    if not target.is_file():
        print(f"[odin-patch] ERROR: {target} not found", file=sys.stderr)
        return False
    src = target.read_text(encoding="utf-8")
    if MARKER_WORKER in src:
        print("[odin-patch] kanban_worker_client retry patch already applied; skipping")
        return True
    if ANCHOR_WORKER not in src:
        print(f"[odin-patch] ERROR: retry anchor not found in {target}; upstream changed", file=sys.stderr)
        return False
    target.write_text(src.replace(ANCHOR_WORKER, REPLACEMENT_WORKER, 1), encoding="utf-8")
    print(f"[odin-patch] patched {target} (gateway retry resilience)")
    return True


def patch_approval(target: Path = TARGET_APPROVAL) -> bool:
    if not target.is_file():
        print(f"[odin-patch] ERROR: {target} not found", file=sys.stderr)
        return False
    src = target.read_text(encoding="utf-8")
    if MARKER_APPROVAL in src:
        print("[odin-patch] approval error messaging patch already applied; skipping")
        return True
    if ANCHOR_APPROVAL_FULL not in src:
        print(f"[odin-patch] ERROR: approval anchor not found in {target}; upstream changed", file=sys.stderr)
        return False
    target.write_text(src.replace(ANCHOR_APPROVAL_FULL, REPLACEMENT_APPROVAL, 1), encoding="utf-8")
    print(f"[odin-patch] patched {target} (clarified single-query/unattended error advice)")
    return True


def main() -> int:
    ok_worker = patch_worker()
    ok_approval = patch_approval()
    return 0 if (ok_worker and ok_approval) else 1


if __name__ == "__main__":
    sys.exit(main())
