#!/usr/bin/env python3
"""Build-time patch: forward internal service env allowlist to routed managed workers.

Problem
-------
When ``routed=True`` inside ``gateway/session_managed_worker.py:_worker_env``, Hermes
strips inherited launch credentials and reads only per-profile ``.env`` files. Removing
``MUNINN_MASTER_KEY``, ``TYPESAFE_API_KEY``, ``TAVILY_API_KEY``, ``SEARXNG_URL``, and
``CAMOFOX_URL`` causes specialist workers to fail Muninn ``compress`` / ``agent-loop``
with HTTP 401, breaks TypeSafe Jev smart approvals, and disables Huginn web tools.

Fix
---
After applying per-profile secrets in ``_worker_env``, when ``routed=True``, forward
allowlisted service env vars from ``os.environ`` (only when non-empty and not already
explicitly set by the profile). Default allowlist is extensible via
``HERMES_ROUTED_WORKER_ENV_ALLOWLIST`` (comma-separated). Gateway, Discord, and server
credentials remain strictly scrubbed.
"""
from __future__ import annotations

import argparse
import ast
import py_compile
import sys
from pathlib import Path

DEFAULT_TARGET = Path("/opt/hermes/gateway/session_managed_worker.py")
MARKER = "# odin-patch: routed managed worker internal service env allowlist"

HELPERS_BLOCK = (
    f"{MARKER}\n"
    "_DEFAULT_ROUTED_WORKER_ENV_ALLOWLIST: tuple[str, ...] = (\n"
    '    "MUNINN_MASTER_KEY",\n'
    '    "TYPESAFE_API_KEY",\n'
    '    "TAVILY_API_KEY",\n'
    '    "SEARXNG_URL",\n'
    '    "CAMOFOX_URL",\n'
    ")\n"
    "_SCRUBBED_ROUTED_CREDENTIALS: frozenset[str] = frozenset({\n"
    '    "DISCORD_BOT_TOKEN",\n'
    '    "DISCORD_ALLOWED_USERS",\n'
    '    "GATEWAY_ALLOW_ALL_USERS",\n'
    '    "API_SERVER_KEY",\n'
    '    "OPENAI_API_KEY",\n'
    "})\n\n\n"
    "def _is_scrubbed_routed_credential(name: str) -> bool:\n"
    "    upper = name.strip().upper()\n"
    '    return upper in _SCRUBBED_ROUTED_CREDENTIALS or upper.startswith("API_SERVER_")\n\n\n'
    "def _resolve_routed_worker_env_allowlist() -> tuple[str, ...]:\n"
    '    raw = os.environ.get("HERMES_ROUTED_WORKER_ENV_ALLOWLIST", "")\n'
    "    extras = tuple(\n"
    "        item.strip()\n"
    '        for item in raw.split(",")\n'
    "        if item.strip() and not _is_scrubbed_routed_credential(item)\n"
    "    )\n"
    "    return tuple(dict.fromkeys((*_DEFAULT_ROUTED_WORKER_ENV_ALLOWLIST, *extras)))\n\n\n"
)

ANCHOR_DEF = "def _worker_env(authority):\n"

ANCHOR_UPDATE = (
    "    else:\n"
    "        secrets = {**_launch_env_only_credentials(home, env), **secrets}\n"
    "    env.update({k: v for k, v in secrets.items() if v is not None})\n"
    "    env['HERMES_HOME'] = str(home)\n"
)

REPL_UPDATE = (
    "    else:\n"
    "        secrets = {**_launch_env_only_credentials(home, env), **secrets}\n"
    "    env.update({k: v for k, v in secrets.items() if v is not None})\n"
    "    if routed:\n"
    "        for key in _resolve_routed_worker_env_allowlist():\n"
    "            if key not in env and not _is_scrubbed_routed_credential(key):\n"
    "                val = os.environ.get(key)\n"
    "                if val:\n"
    "                    env[key] = val\n"
    "        for key in [k for k in env if _is_scrubbed_routed_credential(k)]:\n"
    "            if key not in secrets:\n"
    "                del env[key]\n"
    "    env['HERMES_HOME'] = str(home)\n"
)


def _replace_exact(src: str, anchor: str, replacement: str, label: str) -> str:
    count = src.count(anchor)
    if count != 1:
        raise RuntimeError(f"{label}: expected exact anchor count == 1, got {count}")
    return src.replace(anchor, replacement, 1)


def patch_file(path: str | Path = DEFAULT_TARGET) -> bool:
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Target file not found: {target}")

    src = target.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"[odin-patch] {target} already patched; skipping")
        return False

    patched = _replace_exact(src, ANCHOR_DEF, HELPERS_BLOCK + ANCHOR_DEF, "session_managed_worker.py:_worker_env_def")
    patched = _replace_exact(patched, ANCHOR_UPDATE, REPL_UPDATE, "session_managed_worker.py:_worker_env_update")
    ast.parse(patched, filename=str(target))
    target.write_text(patched, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (routed worker service env allowlist)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch session_managed_worker.py to forward service env allowlist to routed workers."
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help=f"Path to session_managed_worker.py (default: {DEFAULT_TARGET})",
    )
    args = parser.parse_args(argv)

    try:
        patch_file(args.target)
    except Exception as exc:
        print(f"[odin-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
