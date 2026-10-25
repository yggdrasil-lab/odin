#!/usr/bin/env python3
"""Build-time patch: route Mnemosyne host LLM operations to auxiliary.memory task.

Problem
-------
By default, Mnemosyne memory consolidation (summarize_memories) and structured fact
extraction (extract_facts) run against an OpenAI-compatible HTTP API configured via
MNEMOSYNE_LLM_BASE_URL. In Odin, this pointed to Muninn (http://muninn:4000/v1) with
MNEMOSYNE_LLM_MODEL=memory-extract, directly consuming DeepSeek V4-Flash tokens.

Furthermore, HermesAuxLLMBackend in hermes_memory_provider was wired to task="compression",
which uses auxiliary.compression and falls back to Muninn compress (Gemini 2.5 Flash on Google API),
skipping the user's Antigravity (agy) subscription quotas (Claude Sonnet 5.5 and Gemini 4 Argon)
and the main loop fallback chain. Additionally, standalone / MCP invocations of Mnemosyne
never registered the host LLM backend lazily.

Fix
---
1. Patch hermes_memory_provider.hermes_llm_adapter so HermesAuxLLMBackend uses task="memory".
   This allows config.yaml to define auxiliary.memory on the Antigravity subscription
   (antigravity-subscription-directsdk: gemini-3.8-flash-high) with the exact same
   fallback chain as the main agent loop (claude-sonnet-5-5-high -> gemini-4-argon-high -> agent-loop).
2. Patch mnemosyne.core.local_llm._try_host_llm to lazily call register_hermes_host_llm()
   if get_host_llm_backend() is None, ensuring standalone CLI and MCP tool executions
   automatically connect to the Hermes host LLM when MNEMOSYNE_HOST_LLM_ENABLED=true.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER_ADAPTER = "# odin-patch: route host LLM to auxiliary.memory task"
MARKER_LOCAL_LLM = "# odin-patch: lazy register hermes host LLM backend"


def patch_adapter() -> bool:
    try:
        import hermes_memory_provider.hermes_llm_adapter as adapter_mod
        target = Path(adapter_mod.__file__)
    except Exception:
        candidate = Path("/opt/hermes/.venv/lib/python3.14/site-packages/hermes_memory_provider/hermes_llm_adapter.py")
        if candidate.is_file():
            target = candidate
        else:
            print("[odin-patch] ERROR: hermes_llm_adapter.py not found", file=sys.stderr)
            return False

    src = target.read_text(encoding="utf-8")
    if MARKER_ADAPTER in src:
        print("[odin-patch] hermes_llm_adapter already patched; skipping")
        return True

    old_task = '    task = "compression"\n'
    new_task = f'    {MARKER_ADAPTER}\n    task = "memory"\n'
    if old_task not in src:
        print("[odin-patch] ERROR: task = \"compression\" not found in hermes_llm_adapter.py", file=sys.stderr)
        return False

    target.write_text(src.replace(old_task, new_task, 1), encoding="utf-8")
    print(f"[odin-patch] patched {target} (routed task to memory)")
    return True


def patch_local_llm() -> bool:
    try:
        import mnemosyne.core.local_llm as local_llm_mod
        target = Path(local_llm_mod.__file__)
    except Exception:
        candidate = Path("/opt/hermes/.venv/lib/python3.14/site-packages/mnemosyne/core/local_llm.py")
        if candidate.is_file():
            target = candidate
        else:
            print("[odin-patch] ERROR: local_llm.py not found", file=sys.stderr)
            return False

    src = target.read_text(encoding="utf-8")
    if MARKER_LOCAL_LLM in src:
        print("[odin-patch] local_llm already patched; skipping")
        return True

    old_check = (
        "    if get_host_llm_backend() is None:\n"
        "        return (False, None)\n"
    )
    new_check = (
        f"    {MARKER_LOCAL_LLM}\n"
        "    if get_host_llm_backend() is None:\n"
        "        try:\n"
        "            from hermes_memory_provider.hermes_llm_adapter import register_hermes_host_llm\n"
        "            register_hermes_host_llm()\n"
        "        except Exception:\n"
        "            pass\n"
        "    if get_host_llm_backend() is None:\n"
        "        return (False, None)\n"
    )

    if old_check not in src:
        print("[odin-patch] ERROR: get_host_llm_backend() check not found in local_llm.py", file=sys.stderr)
        return False

    target.write_text(src.replace(old_check, new_check, 1), encoding="utf-8")
    print(f"[odin-patch] patched {target} (lazy registration of host LLM)")
    return True


def main() -> int:
    ok = patch_adapter() and patch_local_llm()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
