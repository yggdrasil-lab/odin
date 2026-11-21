#!/usr/bin/env python3
"""Build-time patch: fix agy catalog HOME derivation and Claude 5.5 / Gemini 4 effort aliases.

Problems
--------
1. ``models.load_catalog()`` spawns ``agy models`` inheriting the gateway process
   ``HOME`` (often ``/root``), while persisted ``agy`` subscription credentials live under
   ``/opt/data/home/.gemini/antigravity-cli``. Because ``agy models`` ignores
   ``ANTIGRAVITY_CONFIG_DIR`` and inspects ``$HOME``, catalog discovery fails and returns
   an empty catalog.
2. ``_KNOWN_EFFORTS`` omits ``claude-sonnet-5-5``, ``claude-opus-5-5``, and
   ``gemini-4-{hydrogen,neon,carbon,argon}``, while ``_MODEL_ALIASES`` downgrades
   ``claude-sonnet-5-5`` -> ``claude-sonnet-4-6`` and
   ``claude-opus-5-5`` -> ``claude-opus-4-6-thinking`` (bare names without effort).
   When the catalog is empty, Pool 2 Claude 5.5 requests crash on retired 4.6 names.

Fixes
-----
1. Derive ``HOME`` from ``process.resolve_real_token_path()`` (``token_path.parents[2]``)
   when available and pass it in ``env`` to ``subprocess.run([resolve_agy_command(), "models"], ...)``.
2. Register ``("low", "medium", "high")`` efforts for ``claude-sonnet-5-5``,
   ``claude-opus-5-5``, and ``gemini-4-{hydrogen,neon,carbon,argon}``.
3. Remove stale ``5-5 -> 4-6`` alias downgrades and point ``sonnet``/``claude-sonnet`` ->
   ``claude-sonnet-5-5`` and ``opus``/``claude-opus`` -> ``claude-opus-5-5``.
4. Align upstream assertions in ``tests/test_antigravity_plugin.py``.
"""
from __future__ import annotations

import argparse
import py_compile
import sys
from pathlib import Path

DEFAULT_PLUGIN_DIR = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk")

MARKER_MODELS = "# odin-patch: agy catalog HOME derivation and claude-5-5/gemini-4 efforts"
MARKER_TESTS = "# odin-patch: align upstream antigravity tests with claude-5-5 efforts"

ANCHOR_IMPORTS = (
    "import logging\n"
    "import re\n"
    "import subprocess\n"
    "import threading\n"
    "import time\n"
)
REPL_IMPORTS = (
    f"{MARKER_MODELS}\n"
    "import logging\n"
    "import os\n"
    "from pathlib import Path\n"
    "import re\n"
    "import subprocess\n"
    "import threading\n"
    "import time\n"
)

ANCHOR_TABLES = (
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
)
REPL_TABLES = (
    "_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = {\n"
    '    "gemini-3.8-flash": ("low", "medium", "high"),\n'
    '    "gemini-3.7-flash": ("low", "medium", "high"),\n'
    '    "gemini-3.6-flash": ("low", "medium", "high"),\n'
    '    "gemini-3.1-pro": ("low", "high"),\n'
    '    "gemini-4-hydrogen": ("low", "medium", "high"),\n'
    '    "gemini-4-neon": ("low", "medium", "high"),\n'
    '    "gemini-4-carbon": ("low", "medium", "high"),\n'
    '    "gemini-4-argon": ("low", "medium", "high"),\n'
    '    "claude-sonnet-5-5": ("low", "medium", "high"),\n'
    '    "claude-opus-5-5": ("low", "medium", "high"),\n'
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
    '    "sonnet": "claude-sonnet-5-5",\n'
    '    "claude-sonnet": "claude-sonnet-5-5",\n'
    '    "opus": "claude-opus-5-5",\n'
    '    "claude-opus": "claude-opus-5-5",\n'
    "}\n"
)

ANCHOR_LOAD_CATALOG = (
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
)
REPL_LOAD_CATALOG = (
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
    '            run_kwargs: dict[str, Any] = {\n'
    '                "capture_output": True,\n'
    '                "text": True,\n'
    '                "timeout": timeout,\n'
    "                **_no_window(),\n"
    "            }\n"
    "            token_path = resolve_real_token_path()\n"
    "            if token_path is not None:\n"
    "                tp = Path(token_path)\n"
    "                if len(tp.parents) > 2 and tp.parents[2] != Path(tp.anchor):\n"
    '                    run_kwargs["env"] = {**os.environ, "HOME": str(tp.parents[2])}\n'
    "            res = subprocess.run(\n"
    '                [resolve_agy_command(), "models"],\n'
    "                **run_kwargs,\n"
    "            )\n"
    "            catalog = parse_catalog(res.stdout)\n"
)

ANCHOR_TEST_EMPTY_CATALOG = (
    '        with patch("models._catalog_cache", None), \\\n'
    '                patch("models.load_catalog", return_value={}):\n'
    '            self.assertEqual(client._resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-4-6", None))\n'
    '            self.assertEqual(client._resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-4-6-thinking", None))\n'
)
REPL_TEST_EMPTY_CATALOG = (
    f"        {MARKER_TESTS}\n"
    '        with patch("models._catalog_cache", None), \\\n'
    '                patch("models.load_catalog", return_value={}):\n'
    '            self.assertEqual(client._resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-5-5-low", "low"))\n'
    '            self.assertEqual(client._resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-5-5-high", "high"))\n'
)

ANCHOR_TEST_EFFORTS = (
    '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-4-6"), ())\n'
    '        self.assertEqual(profile.supported_reasoning_efforts("claude-opus-4-6-thinking"), ())\n'
    '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-5-5"), ())\n'
    '        self.assertEqual(profile.supported_reasoning_efforts("gpt-oss-120b-medium"), ("medium",))\n'
)
REPL_TEST_EFFORTS = (
    '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-4-6"), ())\n'
    '        self.assertEqual(profile.supported_reasoning_efforts("claude-opus-4-6-thinking"), ())\n'
    '        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-5-5"), ("low", "medium", "high"))\n'
    '        self.assertEqual(profile.supported_reasoning_efforts("gpt-oss-120b-medium"), ("medium",))\n'
)


def _replace_exact(src: str, anchor: str, replacement: str, label: str) -> str:
    count = src.count(anchor)
    if count != 1:
        raise ValueError(f"{label}: expected anchor count == 1, got {count}")
    return src.replace(anchor, replacement, 1)


def patch_models(plugin_dir: Path) -> bool:
    target = plugin_dir / "models.py"
    if not target.is_file():
        raise FileNotFoundError(f"ERROR: {target} not found")
    src = target.read_text(encoding="utf-8")
    if MARKER_MODELS in src:
        print(f"[odin-patch] {target.name} already patched; skipping")
        return False

    src = _replace_exact(src, ANCHOR_IMPORTS, REPL_IMPORTS, "models.py:imports")
    src = _replace_exact(src, ANCHOR_TABLES, REPL_TABLES, "models.py:_KNOWN_EFFORTS_and_ALIASES")
    src = _replace_exact(src, ANCHOR_LOAD_CATALOG, REPL_LOAD_CATALOG, "models.py:load_catalog")
    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (catalog HOME derivation + claude-5-5/gemini-4 efforts)")
    return True


def patch_tests(plugin_dir: Path) -> bool:
    target = plugin_dir / "tests" / "test_antigravity_plugin.py"
    if not target.is_file():
        raise FileNotFoundError(f"ERROR: {target} not found")
    src = target.read_text(encoding="utf-8")
    if MARKER_TESTS in src:
        print(f"[odin-patch] {target.name} already patched; skipping")
        return False

    src = _replace_exact(src, ANCHOR_TEST_EMPTY_CATALOG, REPL_TEST_EMPTY_CATALOG, "test_antigravity_plugin.py:empty_catalog")
    src = _replace_exact(src, ANCHOR_TEST_EFFORTS, REPL_TEST_EFFORTS, "test_antigravity_plugin.py:supported_efforts")
    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (aligned claude-5-5 assertions)")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Patch antigravity models.py for catalog HOME derivation and Claude 5.5 efforts."
    )
    parser.add_argument(
        "--plugin-dir",
        default=str(DEFAULT_PLUGIN_DIR),
        help=f"Path to antigravity-subscription-directsdk directory (default: {DEFAULT_PLUGIN_DIR})",
    )
    args = parser.parse_args(argv)
    plugin_dir = Path(args.plugin_dir)

    try:
        patch_models(plugin_dir)
        patch_tests(plugin_dir)
    except Exception as exc:
        print(f"[odin-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
