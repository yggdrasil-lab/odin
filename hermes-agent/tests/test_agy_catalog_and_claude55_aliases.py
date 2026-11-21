#!/usr/bin/env python3
"""Standalone tests for fix_agy_catalog_and_claude55_aliases build-time patch."""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PATCHES_DIR = Path(__file__).resolve().parent.parent / "patches"
if str(PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(PATCHES_DIR))

import fix_agy_catalog_and_claude55_aliases as alias_patch  # noqa: E402


def _build_models_fixture(*, include_anchor: bool = True) -> str:
    if not include_anchor:
        return "def load_catalog():\n    return {}\n"
    return '''from __future__ import annotations

import logging
import re
import subprocess
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = {
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("low", "medium", "high"),
    "gemini-3.1-pro": ("low", "high"),
    "claude-sonnet-4-6": (),
    "claude-opus-4-6-thinking": (),
    "gpt-oss-120b": ("medium",),
}

_FALLBACK_MODELS = list(_KNOWN_EFFORTS)

_MODEL_ALIASES = {
    "default": "gemini-3.8-flash",
    "flash": "gemini-3.8-flash",
    "gemini-flash": "gemini-3.8-flash",
    "gemini-3.8": "gemini-3.8-flash",
    "pro": "gemini-3.1-pro",
    "gemini-pro": "gemini-3.1-pro",
    "gemini-3.1": "gemini-3.1-pro",
    "sonnet": "claude-sonnet-4-6",
    "claude-sonnet": "claude-sonnet-4-6",
    "claude-sonnet-5-5": "claude-sonnet-4-6",
    "opus": "claude-opus-4-6-thinking",
    "claude-opus": "claude-opus-4-6-thinking",
    "claude-opus-5-5": "claude-opus-4-6-thinking",
}

_EFFORT_SUFFIXES = (("-xhigh", "high"), ("-high", "high"), ("-medium", "medium"), ("-low", "low"))
_EFFORT_RANK = {"low": 0, "medium": 1, "high": 2}
_CATALOG_TTL_SECONDS = 3600.0
_CATALOG_FAILURE_TTL_SECONDS = 60.0
_catalog_lock = threading.Lock()
_catalog_cache = None

def _normalize_effort(effort):
    return effort.strip().lower() if effort else None

def split_effort_suffix(model: str) -> tuple[str, str | None]:
    for suffix, effort in _EFFORT_SUFFIXES:
        if model.endswith(suffix):
            return model[: -len(suffix)], effort
    return model, None

def parse_catalog(text: str) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for line in text.splitlines():
        token = line.strip().split()[0] if line.strip() else ""
        if not token:
            continue
        base, effort = split_effort_suffix(token)
        efforts = grouped.setdefault(base, [])
        if effort and effort not in efforts:
            efforts.append(effort)
    return {k: tuple(sorted(v, key=_EFFORT_RANK.__getitem__)) for k, v in grouped.items()}

def _cached_catalog():
    return _catalog_cache[1] if _catalog_cache and time.monotonic() < _catalog_cache[0] else None

def load_catalog(timeout: float = 15.0, *, only_if_stale: bool = False) -> dict[str, tuple[str, ...]]:
    global _catalog_cache
    try:
        from .process import _no_window, resolve_agy_command
    except ImportError:
        from process import _no_window, resolve_agy_command

    with _catalog_lock:
        if only_if_stale:
            fresh = _cached_catalog()
            if fresh is not None:
                return fresh
        try:
            res = subprocess.run(
                [resolve_agy_command(), "models"],
                capture_output=True,
                text=True,
                timeout=timeout,
                **_no_window(),
            )
            catalog = parse_catalog(res.stdout)
        except Exception as exc:
            logger.debug("Antigravity model catalog unavailable: %s", exc)
            catalog = {}
        ttl = _CATALOG_TTL_SECONDS if catalog else _CATALOG_FAILURE_TTL_SECONDS
        _catalog_cache = (time.monotonic() + ttl, catalog)
        return catalog

def model_efforts(base_model: str, *, refresh: bool = True):
    cached = _cached_catalog()
    if cached is not None and base_model in cached:
        return cached[base_model]
    if base_model in _KNOWN_EFFORTS:
        return _KNOWN_EFFORTS[base_model]
    if refresh and cached is None:
        return load_catalog(only_if_stale=True).get(base_model)
    return None

def _nearest_effort(requested: str, supported: tuple[str, ...]) -> str:
    target = _EFFORT_RANK[requested]
    return min(supported, key=lambda e: (abs(_EFFORT_RANK[e] - target), -_EFFORT_RANK[e]))

def resolve_model_alias(model: str, catalog: dict[str, tuple[str, ...]] | None = None) -> str:
    m = (model or "gemini-3.8-flash").strip()
    lower_m = m.lower()
    base, suffix = split_effort_suffix(lower_m)
    fallback = _MODEL_ALIASES.get(base, base)
    if suffix and fallback != base:
        return f"{fallback}-{suffix}"
    return _MODEL_ALIASES.get(lower_m, m)

def resolve_model_and_effort(model: str | None, reasoning_effort: str | None = None) -> tuple[str, str | None]:
    raw_model = str(model or "gemini-3.8-flash").strip()
    catalog = _cached_catalog() or load_catalog(only_if_stale=True)
    m = resolve_model_alias(raw_model, catalog)
    base_model, suffix_effort = split_effort_suffix(m)
    supported = model_efforts(base_model, refresh=False)
    if not supported:
        return base_model, None
    effort = _normalize_effort(reasoning_effort) or suffix_effort or "medium"
    return f"{base_model}-{_nearest_effort(effort, supported)}", _nearest_effort(effort, supported)
'''


def _build_upstream_test_fixture(*, include_anchor: bool = True) -> str:
    if not include_anchor:
        return "class TestDummy:\n    pass\n"
    return '''class TestPlugin:
    def test_base(self):
        with patch("models._catalog_cache", None), \\
                patch("models.load_catalog", return_value={}):
            self.assertEqual(client._resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-4-6", None))
            self.assertEqual(client._resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-4-6-thinking", None))

    def test_efforts(self):
        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-4-6"), ())
        self.assertEqual(profile.supported_reasoning_efforts("claude-opus-4-6-thinking"), ())
        self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-5-5"), ())
        self.assertEqual(profile.supported_reasoning_efforts("gpt-oss-120b-medium"), ("medium",))
'''


def _write_plugin_tree(root: Path, *, models_src: str, tests_src: str) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "models.py").write_text(models_src, encoding="utf-8")
    (root / "tests" / "test_antigravity_plugin.py").write_text(tests_src, encoding="utf-8")


def _load_patched_models(models_path: Path, token_path: Path | None) -> types.ModuleType:
    stub_process = types.ModuleType("process")
    stub_process._no_window = lambda: {}  # type: ignore[attr-defined]
    stub_process.resolve_agy_command = lambda: "/usr/local/bin/agy"  # type: ignore[attr-defined]
    stub_process.resolve_real_token_path = lambda: token_path  # type: ignore[attr-defined]
    spec = importlib.util.spec_from_file_location("patched_agy_models_test", str(models_path))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"process": stub_process}):
        spec.loader.exec_module(mod)
    return mod


class TestAgyCatalogAndClaude55Aliases(unittest.TestCase):
    def test_patches_models_and_tests_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=True),
                tests_src=_build_upstream_test_fixture(include_anchor=True),
            )
            self.assertEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)
            after_first_models = (plugin_dir / "models.py").read_text(encoding="utf-8")
            after_first_tests = (plugin_dir / "tests" / "test_antigravity_plugin.py").read_text(encoding="utf-8")

            self.assertEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)
            self.assertEqual((plugin_dir / "models.py").read_text(encoding="utf-8"), after_first_models)
            self.assertEqual((plugin_dir / "tests" / "test_antigravity_plugin.py").read_text(encoding="utf-8"), after_first_tests)
            self.assertEqual(after_first_models.count(alias_patch.MARKER_MODELS), 1)
            self.assertEqual(after_first_tests.count(alias_patch.MARKER_TESTS), 1)

            self.assertIn('"claude-sonnet-5-5-low", "low"', after_first_tests)
            self.assertIn('"claude-opus-5-5-high", "high"', after_first_tests)
            self.assertIn('supported_reasoning_efforts("claude-sonnet-5-5"), ("low", "medium", "high")', after_first_tests)

    def test_fails_loudly_when_anchors_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=False),
                tests_src=_build_upstream_test_fixture(include_anchor=True),
            )
            self.assertNotEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)

        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=True),
                tests_src=_build_upstream_test_fixture(include_anchor=False),
            )
            self.assertNotEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)

    def test_load_catalog_derives_home_from_token_path_and_resolves_efforts(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=True),
                tests_src=_build_upstream_test_fixture(include_anchor=True),
            )
            self.assertTrue(alias_patch.patch_models(plugin_dir))

            token_path = Path("/opt/data/home/.gemini/antigravity-cli/jetski-standalone-oauth-token")
            mod = _load_patched_models(plugin_dir / "models.py", token_path)
            stub_proc = types.ModuleType("process")
            stub_proc._no_window = lambda: {}  # type: ignore[attr-defined]
            stub_proc.resolve_agy_command = lambda: "/usr/local/bin/agy"  # type: ignore[attr-defined]
            stub_proc.resolve_real_token_path = lambda: token_path  # type: ignore[attr-defined]

            captured_env: dict[str, str] = {}

            def fake_run(cmd, **kwargs):
                captured_env.update(kwargs.get("env") or {})
                return SimpleNamespace(stdout="claude-sonnet-5-5-high\nclaude-opus-5-5-medium\n")

            with patch.dict(sys.modules, {"process": stub_proc}), \
                    patch.dict(os.environ, {"HOME": "/root"}, clear=False), \
                    patch.object(mod.subprocess, "run", side_effect=fake_run):
                catalog = mod.load_catalog()

            self.assertEqual(captured_env.get("HOME"), "/opt/data/home")
            self.assertIn("claude-sonnet-5-5", catalog)
            self.assertIn("claude-opus-5-5", catalog)

            # Empty catalog fallback must resolve Claude 5.5 and Gemini 4 without 4-6 downgrades
            with patch.object(mod, "_catalog_cache", None), patch.object(mod, "load_catalog", return_value={}):
                self.assertEqual(mod.resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-5-5-low", "low"))
                self.assertEqual(mod.resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-5-5-high", "high"))
                self.assertEqual(mod.resolve_model_and_effort("sonnet", "medium"), ("claude-sonnet-5-5-medium", "medium"))
                self.assertEqual(mod.resolve_model_and_effort("opus", "high"), ("claude-opus-5-5-high", "high"))
                for gemini_model in ("gemini-4-hydrogen", "gemini-4-neon", "gemini-4-carbon", "gemini-4-argon"):
                    self.assertEqual(mod._KNOWN_EFFORTS[gemini_model], ("low", "medium", "high"))
                    self.assertEqual(mod.resolve_model_and_effort(gemini_model, "high"), (f"{gemini_model}-high", "high"))


if __name__ == "__main__":
    unittest.main()
