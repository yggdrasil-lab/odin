#!/usr/bin/env python3
"""Patch dashboard_core.py in mnemosyne-dashboard to load sqlite-vec extension.

Installs a single module-level sqlite3.connect wrapper that enables extension
loading, binds sqlite_vec, and logs explicit errors before failing loudly.
"""
from __future__ import annotations

import argparse
import ast
import py_compile
import sys
from pathlib import Path

MARKER = "# odin-patch: bind sqlite-vec extension"
TARGET_IMPORT = "import sqlite3\n"
HOOK_BLOCK = (
    "import sqlite3\n"
    f"{MARKER}\n"
    "import logging as _sqlite_vec_logging\n"
    "import sqlite_vec as _sqlite_vec\n\n"
    "_sqlite_vec_logger = _sqlite_vec_logging.getLogger(__name__)\n"
    "_orig_sqlite_connect = getattr(sqlite3.connect, '_odin_orig_connect', sqlite3.connect)\n\n\n"
    "def _vec_sqlite_connect(*args, **kwargs):\n"
    "    _con = _orig_sqlite_connect(*args, **kwargs)\n"
    "    try:\n"
    "        _con.enable_load_extension(True)\n"
    "        _sqlite_vec.load(_con)\n"
    "    except Exception as exc:\n"
    "        _sqlite_vec_logger.error('Failed to load sqlite-vec extension: %s', exc, exc_info=True)\n"
    "        _con.close()\n"
    "        raise\n"
    "    return _con\n\n\n"
    "_vec_sqlite_connect._odin_orig_connect = _orig_sqlite_connect  # type: ignore[attr-defined]\n"
    "sqlite3.connect = _vec_sqlite_connect\n"
)


def _resolve_default_target() -> Path:
    app_target = Path("/app/dashboard_core.py")
    return app_target if app_target.is_file() else Path("dashboard_core.py")


def patch_file(target: Path) -> bool:
    """Apply the single logged sqlite_vec connection hook to `target`."""
    if not target.is_file():
        raise FileNotFoundError(f"Target file not found: {target}")

    content = target.read_text(encoding="utf-8")
    if MARKER in content:
        print(f"[odin-patch] {target} already patched with sqlite-vec binding; skipping.")
        return False

    count = content.count(TARGET_IMPORT)
    if count != 1:
        raise RuntimeError(
            f"Expected exact 'import sqlite3' anchor (count=1) in {target}, got {count}"
        )

    patched = content.replace(TARGET_IMPORT, HOOK_BLOCK, 1)
    ast.parse(patched, filename=str(target))
    target.write_text(patched, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] Successfully patched {target} with logged sqlite-vec hook.")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Patch dashboard_core.py to load sqlite-vec.")
    parser.add_argument("--target", type=Path, default=None, help="Path to dashboard_core.py")
    args = parser.parse_args(argv)
    target = args.target if args.target is not None else _resolve_default_target()
    try:
        patch_file(target)
    except Exception as exc:
        print(f"[odin-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
