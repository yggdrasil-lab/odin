#!/usr/bin/env python3
"""Build-time patch: offload startup API media sweep off the asyncio event loop.

Problem
-------
At gateway startup, ``recover_api_turns`` calls ``recover_authority_api_turns(adapter, authority)``
synchronously on the asyncio event loop. Inside ``recover_authority_api_turns``,
``collect_unheld_api_images(store)`` and ``compact_settled_api_payloads(store)`` run
unindexed full SQLite scans over ``messages.content`` in ``state.db``. On large databases
(~1.9 GB), this blocks the event loop for >50 seconds, triggering Discord websocket
heartbeat timeouts (``socket_closed``) during startup.

Fix
---
Keep ``store = authority_persistence(authority)`` and ``pending_targets`` session
recovery synchronous, and offload ``collect_unheld_api_images(store)`` plus
``compact_settled_api_payloads(store)`` to a background daemon thread. The worker
thread explicitly re-enters ``with owner_scope(authority):`` to preserve the
``ContextVar`` media root required by ``get_hermes_home()`` / ``_media_root()``, and
wraps execution in ``try / except Exception`` logging a warning so shutdown DB-closed
races are handled cleanly without silencing errors.
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

DEFAULT_TARGET = Path("/opt/hermes/gateway/session_api_turn.py")
MARKER = "# ODIN_PATCH: startup_api_media_sweep_offload_v1"

ANCHOR = (
    "    from gateway.session_ingress_media import collect_unheld_api_images\n"
    "    import logging\n"
    "    store = authority_persistence(authority)\n"
    "    collect_unheld_api_images(store)\n"
    "    from gateway.session_api_media import compact_settled_api_payloads\n"
    "    compact_settled_api_payloads(store)\n"
)

REPLACEMENT = (
    f"    {MARKER}\n"
    "    from gateway.session_authorities import owner_scope\n"
    "    from gateway.session_ingress_media import collect_unheld_api_images\n"
    "    from gateway.session_api_media import compact_settled_api_payloads\n"
    "    import logging\n"
    "    import threading\n"
    "    store = authority_persistence(authority)\n"
    "\n"
    "    def _run_startup_api_media_sweep() -> None:\n"
    "        try:\n"
    "            with owner_scope(authority):\n"
    "                collect_unheld_api_images(store)\n"
    "                compact_settled_api_payloads(store)\n"
    "        except Exception as exc:\n"
    "            logging.getLogger(__name__).warning(\n"
    "                \"Startup API media sweep failed for authority %s: %s\",\n"
    "                getattr(authority, \"name\", \"default\"),\n"
    "                exc,\n"
    "            )\n"
    "\n"
    "    threading.Thread(\n"
    "        target=_run_startup_api_media_sweep,\n"
    "        name=f\"api-media-sweep-{getattr(authority, 'name', 'default')}\",\n"
    "        daemon=True,\n"
    "    ).start()\n"
)


def patch_file(path: str | Path = DEFAULT_TARGET) -> bool:
    """Apply the startup API media sweep offload patch to ``path``.

    Returns True if the file was modified, False if already patched.
    Raises FileNotFoundError if ``path`` does not exist, or RuntimeError if the
    upstream anchor cannot be matched uniquely.
    """
    target = Path(path)
    if not target.is_file():
        raise FileNotFoundError(f"Target file not found: {target}")

    src = target.read_text(encoding="utf-8")
    if MARKER in src:
        print(f"[odin-patch] {target} already patched; skipping")
        return False

    count = src.count(ANCHOR)
    if count != 1:
        raise RuntimeError(
            f"Expected exact anchor match (count=1) in {target}, got {count}; upstream changed"
        )

    patched = src.replace(ANCHOR, REPLACEMENT, 1)
    ast.parse(patched, filename=str(target))
    target.write_text(patched, encoding="utf-8")
    print(f"[odin-patch] patched {target} (offloaded startup API media sweep)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offload startup API media sweep in session_api_turn.py to a daemon thread."
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help=f"Path to session_api_turn.py (default: {DEFAULT_TARGET})",
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
