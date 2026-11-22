#!/usr/bin/env python3
"""Standalone tests for fix_agy_catalog_and_claude55_aliases build-time patch."""

from __future__ import annotations

import contextlib
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from dataclasses import dataclass, field
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
    if not effort:
        return None
    e = str(effort).strip().lower()
    if e in ("none", "off", "minimal"):
        return "low"
    if e in ("xhigh", "max", "ultra"):
        return "high"
    if e in ("low", "medium", "high"):
        return e
    return "medium"

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
    return {
        base: tuple(sorted(efforts, key=_EFFORT_RANK.__getitem__))
        for base, efforts in grouped.items()
    }

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

def _cached_catalog() -> dict[str, tuple[str, ...]] | None:
    cache = _catalog_cache
    if cache is not None and time.monotonic() < cache[0]:
        return cache[1]
    return None


def model_efforts(base_model: str, *, refresh: bool = True) -> tuple[str, ...] | None:
    """Efforts agy accepts for `base_model`; () means bare name, None means unknown.

    Order: a fresh `agy models` result, then the built-in table, then (when
    `refresh` is true) a new `agy models` call. While a cached result is
    fresh, a model it does not list is not looked up again until the cache
    expires, so an unknown name costs no process spawn per request.
    """
    cached = _cached_catalog()
    if cached is not None and base_model in cached:
        return cached[base_model]
    if base_model in _KNOWN_EFFORTS:
        return _KNOWN_EFFORTS[base_model]
    if refresh and cached is None:
        return load_catalog(only_if_stale=True).get(base_model)
    return None

def _heuristic_efforts(base_model: str, has_suffix: bool) -> tuple[str, ...]:
    return ("low", "medium", "high") if has_suffix else ()

def _nearest_effort(requested: str, supported: tuple[str, ...]) -> str:
    target = _EFFORT_RANK[requested]
    return min(supported, key=lambda e: (abs(_EFFORT_RANK[e] - target), -_EFFORT_RANK[e]))

def resolve_model_alias(
    model: str, catalog: dict[str, tuple[str, ...]] | None = None
) -> str:
    """Resolve user alias to concrete base model, prioritizing the live agy catalog."""
    m = (model or "gemini-3.8-flash").strip()
    lower_m = m.lower()

    if catalog:
        # Exact match or base match already exists in catalog
        if lower_m in catalog:
            return lower_m
        base, suffix = split_effort_suffix(lower_m)
        if base in catalog:
            return lower_m

        lookup_term = "flash" if base == "default" else base

        # Dynamic family matching against whatever models agy actually returned
        family_keywords = {
            ("sonnet", "claude-sonnet"): "sonnet",
            ("opus", "claude-opus"): "opus",
            ("pro", "gemini-pro", "gemini-3.1"): "pro",
            ("flash", "gemini-flash", "gemini-3.8"): "flash",
            ("gpt", "gpt-oss", "oss"): "gpt",
        }
        for triggers, kw in family_keywords.items():
            if lookup_term in triggers:
                matches = [k for k in catalog if kw in k.lower()]
                if matches:
                    # Sort descending so newer versions win if multiple exist (5-5 before 4-6, 3.8 before 3.7)
                    best = sorted(matches, reverse=True)[0]
                    return f"{best}-{suffix}" if suffix else best

        # General substring match against catalog
        matches = [k for k in catalog if lookup_term in k.lower()]
        if matches:
            best = sorted(matches, reverse=True)[0]
            return f"{best}-{suffix}" if suffix else best

        # Check explicit fallback aliases if not matched dynamically in catalog
        if lookup_term in _MODEL_ALIASES:
            aliased = _MODEL_ALIASES[lookup_term]
            if aliased in catalog:
                return f"{aliased}-{suffix}" if suffix else aliased

    base, suffix = split_effort_suffix(lower_m)
    fallback = _MODEL_ALIASES.get(base, base)
    if suffix and fallback != base:
        return f"{fallback}-{suffix}"
    return _MODEL_ALIASES.get(lower_m, m)


def resolve_model_and_effort(
    model: str | None,
    reasoning_effort: str | None = None,
) -> tuple[str, str | None]:
    """Map user/hermes model request to concrete CLI model ID and effort level.

    Returns (model id, effort). The effort is None for models that agy selects
    by bare name; the client then omits --effort.
    """
    raw_model = str(model or "gemini-3.8-flash").strip()
    catalog = _cached_catalog()
    if catalog is None:
        catalog = load_catalog(only_if_stale=True)

    m = resolve_model_alias(raw_model, catalog)

    base_model, suffix_effort = split_effort_suffix(m)
    supported = model_efforts(base_model, refresh=False)
    if supported is None:
        supported = _heuristic_efforts(base_model, suffix_effort is not None)
    if not supported:
        return base_model, None

    # Resolve effort: explicit argument > model suffix > config setting > default
    effort = _normalize_effort(reasoning_effort)
    if not effort and suffix_effort:
        effort = suffix_effort
    if not effort:
        try:
            from hermes_cli.config import load_config_readonly
            cfg_effort = load_config_readonly().get("agent", {}).get("reasoning_effort")
            effort = _normalize_effort(cfg_effort)
        except Exception:
            pass
    if not effort:
        effort = "medium"

    concrete_effort = _nearest_effort(effort, supported)
    return f"{base_model}-{concrete_effort}", concrete_effort
'''


def _build_init_fixture(*, include_anchor: bool = True) -> str:
    if not include_anchor:
        return "antigravity_profile = None\n"
    return '''from __future__ import annotations

from dataclasses import fields
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

try:
    from .models import (
        _FALLBACK_MODELS,
        _MODEL_ALIASES,
        _cached_catalog,
        _heuristic_efforts,
        load_catalog,
        model_efforts,
        resolve_model_alias,
        split_effort_suffix,
    )
except ImportError:
    # Loaded outside a package (e.g. a flat source tree under test):
    # the absolute name is the same module.
    from models import (
        _FALLBACK_MODELS,
        _MODEL_ALIASES,
        _cached_catalog,
        _heuristic_efforts,
        load_catalog,
        model_efforts,
        resolve_model_alias,
        split_effort_suffix,
    )


class AntigravitySubscriptionDirectSDKProfile(ProviderProfile):
    def supported_reasoning_efforts(
        self, model: str | None
    ) -> tuple[str, ...] | None:
        name = (model or "").strip()
        catalog = _cached_catalog()
        resolved_name = resolve_model_alias(name, catalog)
        base, suffix_effort = split_effort_suffix(resolved_name)
        # No process spawn here: use the cached catalog and the built-in table.
        efforts = model_efforts(base, refresh=False)
        if efforts is None:
            efforts = _heuristic_efforts(base, suffix_effort is not None)
        return efforts

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ) -> list[str] | None:
        catalog = load_catalog(timeout=timeout)
        if catalog:
            return list(catalog)
        return list(_FALLBACK_MODELS)


class _LazyModelCapabilities(dict):
    def get(self, model: Any, default: Any = None) -> Any:
        name = str(model or "").strip()
        if not name:
            return default
        verdict = self._declare(name.lower())
        if verdict is not None:
            return verdict
        return default

    @staticmethod
    def _declare(base: str) -> dict[str, Any] | None:
        base, _suffix = split_effort_suffix(base)
        if base.startswith("gemini"):
            return {"supports_vision": True}
        if base.startswith("claude"):
            return {"supports_vision": True}
        return None


_PROFILE_EXTRA_FIELDS: dict[str, Any] = {"model_capabilities": _LazyModelCapabilities()}

antigravity_profile = AntigravitySubscriptionDirectSDKProfile(
    name="antigravity-subscription-directsdk",
    aliases=("antigravity", "agy"),
    default_aux_model="gemini-3.8-flash",
    fallback_models=(),
    **_PROFILE_EXTRA_FIELDS,
)

register_provider(antigravity_profile)
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


def _write_plugin_tree(
    root: Path,
    *,
    models_src: str,
    init_src: str | None = None,
    tests_src: str,
) -> None:
    (root / "tests").mkdir(parents=True, exist_ok=True)
    (root / "models.py").write_text(models_src, encoding="utf-8")
    (root / "__init__.py").write_text(
        _build_init_fixture(include_anchor=True) if init_src is None else init_src,
        encoding="utf-8",
    )
    (root / "tests" / "test_antigravity_plugin.py").write_text(tests_src, encoding="utf-8")


def _build_provider_stubs() -> dict[str, types.ModuleType]:
    @dataclass
    class ProviderProfile:
        name: str = ""
        aliases: tuple[str, ...] = ()
        default_aux_model: str = ""
        fallback_models: tuple[str, ...] = ()
        model_capabilities: dict[str, dict[str, object]] = field(default_factory=dict)

    providers_mod = types.ModuleType("providers")
    registered: dict[str, ProviderProfile] = {}
    providers_mod.register_provider = lambda p: registered.setdefault(p.name, p)  # type: ignore[attr-defined]
    providers_mod._registered = registered  # type: ignore[attr-defined]

    base_mod = types.ModuleType("providers.base")
    base_mod.ProviderProfile = ProviderProfile  # type: ignore[attr-defined]
    return {"providers": providers_mod, "providers.base": base_mod}


@contextlib.contextmanager
def _load_patched_plugin(
    plugin_dir: Path,
    token_path: Path | None,
    module_prefix: str = "patched_agy_test",
):
    stub_process = types.ModuleType("process")
    stub_process._no_window = lambda: {}  # type: ignore[attr-defined]
    stub_process.resolve_agy_command = lambda: "/usr/local/bin/agy"  # type: ignore[attr-defined]
    stub_process.resolve_real_token_path = lambda: token_path  # type: ignore[attr-defined]

    models_spec = importlib.util.spec_from_file_location("models", str(plugin_dir / "models.py"))
    assert models_spec is not None and models_spec.loader is not None
    models_mod = importlib.util.module_from_spec(models_spec)

    init_spec = importlib.util.spec_from_file_location(f"{module_prefix}_init", str(plugin_dir / "__init__.py"))
    assert init_spec is not None and init_spec.loader is not None
    init_mod = importlib.util.module_from_spec(init_spec)

    stubs = {
        "process": stub_process,
        "models": models_mod,
        **_build_provider_stubs(),
    }
    with patch.dict(sys.modules, stubs):
        models_spec.loader.exec_module(models_mod)
        init_spec.loader.exec_module(init_mod)
        yield models_mod, init_mod


class TestAgyCatalogAndClaude55Aliases(unittest.TestCase):
    def test_patches_models_init_and_tests_idempotently(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=True),
                init_src=_build_init_fixture(include_anchor=True),
                tests_src=_build_upstream_test_fixture(include_anchor=True),
            )
            self.assertEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)
            after_first_models = (plugin_dir / "models.py").read_text(encoding="utf-8")
            after_first_init = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
            after_first_tests = (plugin_dir / "tests" / "test_antigravity_plugin.py").read_text(encoding="utf-8")

            self.assertEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)
            self.assertEqual((plugin_dir / "models.py").read_text(encoding="utf-8"), after_first_models)
            self.assertEqual((plugin_dir / "__init__.py").read_text(encoding="utf-8"), after_first_init)
            self.assertEqual((plugin_dir / "tests" / "test_antigravity_plugin.py").read_text(encoding="utf-8"), after_first_tests)
            self.assertEqual(after_first_models.count(alias_patch.MARKER_MODELS), 1)
            self.assertEqual(after_first_init.count(alias_patch.MARKER_INIT), 1)
            self.assertEqual(after_first_tests.count(alias_patch.MARKER_TESTS), 1)

            self.assertIn('"claude-sonnet-5-5-low", "low"', after_first_tests)
            self.assertIn('"claude-opus-5-5-high", "high"', after_first_tests)
            self.assertIn('supported_reasoning_efforts("claude-sonnet-5-5"), ("low", "medium", "high")', after_first_tests)

    def test_fails_loudly_when_anchors_missing(self) -> None:
        cases = [
            (_build_models_fixture(include_anchor=False), _build_init_fixture(include_anchor=True), _build_upstream_test_fixture(include_anchor=True)),
            (_build_models_fixture(include_anchor=True), _build_init_fixture(include_anchor=False), _build_upstream_test_fixture(include_anchor=True)),
            (_build_models_fixture(include_anchor=True), _build_init_fixture(include_anchor=True), _build_upstream_test_fixture(include_anchor=False)),
        ]
        for models_src, init_src, tests_src in cases:
            with tempfile.TemporaryDirectory() as tmpdir:
                plugin_dir = Path(tmpdir)
                _write_plugin_tree(plugin_dir, models_src=models_src, init_src=init_src, tests_src=tests_src)
                self.assertNotEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)

    def test_unlocked_home_derivation_and_claude55_efforts_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=True),
                init_src=_build_init_fixture(include_anchor=True),
                tests_src=_build_upstream_test_fixture(include_anchor=True),
            )
            self.assertEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)

            token_path = Path("/opt/data/home/.gemini/antigravity-cli/jetski-standalone-oauth-token")
            clean_env = {
                k: v
                for k, v in os.environ.items()
                if k not in ("HERMES_AGY_ALLOWED_MODELS", "HERMES_AGY_ALLOWED_EFFORTS")
            }
            clean_env["HOME"] = "/root"
            with patch.dict(os.environ, clean_env, clear=True):
                with _load_patched_plugin(plugin_dir, token_path, "unlocked_test") as (mod, init_mod):
                    captured_env: dict[str, str] = {}

                    def fake_run(cmd, **kwargs):
                        captured_env.update(kwargs.get("env") or {})
                        return SimpleNamespace(stdout="claude-sonnet-5-5-high\nclaude-opus-5-5-medium\n")

                    with patch.object(mod.subprocess, "run", side_effect=fake_run):
                        catalog = mod.load_catalog()

                    self.assertEqual(captured_env.get("HOME"), "/opt/data/home")
                    self.assertIn("claude-sonnet-5-5", catalog)
                    self.assertIn("claude-opus-5-5", catalog)
                    self.assertEqual(init_mod.antigravity_profile.default_aux_model, "gemini-3.8-flash")

                    with patch.object(mod, "_catalog_cache", None), patch.object(mod, "load_catalog", return_value={}):
                        self.assertEqual(mod.resolve_model_and_effort("claude-sonnet-5-5", "low"), ("claude-sonnet-5-5-low", "low"))
                        self.assertEqual(mod.resolve_model_and_effort("claude-opus-5-5", "high"), ("claude-opus-5-5-high", "high"))
                        self.assertEqual(mod.resolve_model_and_effort("sonnet", "medium"), ("claude-sonnet-5-5-medium", "medium"))

    @contextlib.contextmanager
    def _patched_loaded_plugin(self, env_overrides: dict[str, str], prefix: str):
        with tempfile.TemporaryDirectory() as tmpdir:
            plugin_dir = Path(tmpdir)
            _write_plugin_tree(
                plugin_dir,
                models_src=_build_models_fixture(include_anchor=True),
                init_src=_build_init_fixture(include_anchor=True),
                tests_src=_build_upstream_test_fixture(include_anchor=True),
            )
            self.assertEqual(alias_patch.main(["--plugin-dir", str(plugin_dir)]), 0)
            with patch.dict(os.environ, env_overrides, clear=False), _load_patched_plugin(plugin_dir, None, prefix) as mods:
                yield mods

    def test_strict_hydrogen_lock_filters_catalog_and_profile(self) -> None:
        locked_env = {
            "HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high",
        }
        raw_catalog = (
            "gemini-4-hydrogen-low\n"
            "gemini-4-hydrogen-medium\n"
            "gemini-4-hydrogen-high\n"
            "gemini-4-hydrogen-xhigh\n"
            "gemini-4-neon-high\n"
            "gemini-4-argon-high\n"
            "gemini-4-carbon-high\n"
            "claude-sonnet-5-5-high\n"
            "gemini-3.8-flash-high\n"
            "gpt-oss-120b-medium\n"
        )
        with self._patched_loaded_plugin(locked_env, "locked_catalog") as (mod, init_mod):
            profile = init_mod.antigravity_profile
            self.assertEqual(mod._KNOWN_EFFORTS, {"gemini-4-hydrogen": ("low", "medium", "high")})
            self.assertEqual(mod._FALLBACK_MODELS, ["gemini-4-hydrogen"])
            self.assertEqual(mod._MODEL_ALIASES["default"], "gemini-4-hydrogen")
            self.assertEqual(mod._MODEL_ALIASES["flash"], "gemini-4-hydrogen")
            self.assertEqual(profile.default_aux_model, "gemini-4-hydrogen-low")
            self.assertEqual(profile.resolve_aux_model(), "gemini-4-hydrogen-low")
            self.assertEqual(mod.parse_catalog(raw_catalog), {"gemini-4-hydrogen": ("low", "medium", "high")})

            with patch.object(mod.subprocess, "run", return_value=SimpleNamespace(stdout=raw_catalog)):
                self.assertEqual(profile.fetch_models(), ["gemini-4-hydrogen"])
            with patch.object(mod, "_catalog_cache", None), patch.object(mod.subprocess, "run", side_effect=FileNotFoundError("agy")):
                self.assertEqual(profile.fetch_models(), ["gemini-4-hydrogen"])
            self.assertEqual(profile.supported_reasoning_efforts("gemini-4-hydrogen"), ("low", "medium", "high"))
            self.assertEqual(profile.supported_reasoning_efforts("claude-sonnet-5-5"), ())
            self.assertIsNotNone(profile.model_capabilities.get("gemini-4-hydrogen-high"))
            self.assertIsNone(profile.model_capabilities.get("claude-sonnet-5-5-high"))

    def test_strict_hydrogen_lock_resolves_allowed_and_rejects_disallowed(self) -> None:
        locked_env = {
            "HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high",
        }
        parsed = {"gemini-4-hydrogen": ("low", "medium", "high")}
        disallowed = (
            "claude-sonnet-5-5-high",
            "gemini-4-neon-high",
            "gemini-4-argon-high",
            "gemini-4-carbon-high",
            "gpt-oss-120b",
            "sonnet",
            "opus",
            "gemini-3.8-flash",
        )
        with self._patched_loaded_plugin(locked_env, "locked_resolve") as (mod, _):
            with patch.object(mod, "_catalog_cache", None), patch.object(mod, "load_catalog", return_value=parsed):
                self.assertEqual(mod.resolve_model_alias("default", parsed), "gemini-4-hydrogen")
                self.assertEqual(mod.resolve_model_alias("flash", parsed), "gemini-4-hydrogen")
                self.assertEqual(mod.resolve_model_and_effort(None), ("gemini-4-hydrogen-medium", "medium"))
                self.assertEqual(mod.resolve_model_and_effort("default", "low"), ("gemini-4-hydrogen-low", "low"))
                self.assertEqual(mod.resolve_model_and_effort("flash", "high"), ("gemini-4-hydrogen-high", "high"))
                self.assertEqual(mod.resolve_model_and_effort("gemini-4-hydrogen", "high"), ("gemini-4-hydrogen-high", "high"))
                self.assertEqual(mod.resolve_model_and_effort("gemini-4-hydrogen-low"), ("gemini-4-hydrogen-low", "low"))
                for bad_model in disallowed:
                    with self.subTest(bad_model=bad_model), self.assertRaises(ValueError):
                        mod.resolve_model_and_effort(bad_model)

            with patch.object(mod, "_catalog_cache", None), patch.object(mod, "load_catalog", return_value={}):
                self.assertEqual(mod.resolve_model_alias("Gemini-4-Hydrogen", {}), "gemini-4-hydrogen")
                self.assertEqual(mod.resolve_model_and_effort("Gemini-4-Hydrogen", "high"), ("gemini-4-hydrogen-high", "high"))
                self.assertEqual(mod.resolve_model_and_effort("Gemini-4-Hydrogen-Low"), ("gemini-4-hydrogen-low", "low"))

    def test_restricted_efforts_allowlist_enforced(self) -> None:
        restricted_env = {
            "HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium",
        }
        raw_catalog = "gemini-4-hydrogen-low\ngemini-4-hydrogen-medium\ngemini-4-hydrogen-high\n"
        with self._patched_loaded_plugin(restricted_env, "restricted_efforts") as (mod, init_mod):
            self.assertEqual(mod.parse_catalog(raw_catalog), {"gemini-4-hydrogen": ("low", "medium")})
            self.assertEqual(init_mod.antigravity_profile.supported_reasoning_efforts("gemini-4-hydrogen"), ("low", "medium"))
            with patch.object(mod, "_catalog_cache", None), patch.object(mod, "load_catalog", return_value={}):
                self.assertEqual(mod.resolve_model_and_effort("gemini-4-hydrogen", "low"), ("gemini-4-hydrogen-low", "low"))
                for args in (("gemini-4-hydrogen", "high"), ("gemini-4-hydrogen-high", None), ("gemini-4-hydrogen", "invalid")):
                    with self.assertRaises(ValueError):
                        mod.resolve_model_and_effort(*args)

    def test_empty_allowlist_strings_fail_loud_and_return_empty_catalog(self) -> None:
        empty_models_env = {"HERMES_AGY_ALLOWED_MODELS": "", "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high"}
        with self._patched_loaded_plugin({"HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen"}, "empty_allowlists") as (mod, init_mod):
            profile = init_mod.antigravity_profile
            with patch.dict(os.environ, empty_models_env, clear=False):
                self.assertEqual(mod.parse_catalog("gemini-4-hydrogen-high\nclaude-sonnet-5-5-high\n"), {})
                self.assertEqual(profile.fetch_models(), [])
                with self.assertRaises(ValueError):
                    mod.default_model_name()
                with self.assertRaises(ValueError):
                    mod.default_aux_model_name()
                with self.assertRaises(ValueError):
                    _ = profile.default_aux_model
                with self.assertRaises(ValueError):
                    mod.resolve_model_and_effort("gemini-4-hydrogen")

            empty_efforts_env = {"HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen", "HERMES_AGY_ALLOWED_EFFORTS": ""}
            with patch.dict(os.environ, empty_efforts_env, clear=False):
                self.assertEqual(mod.parse_catalog("gemini-4-hydrogen-low\n"), {})
                with self.assertRaises(ValueError):
                    mod.default_aux_model_name()
                with self.assertRaises(ValueError):
                    mod.resolve_model_and_effort("gemini-4-hydrogen", "low")


if __name__ == "__main__":
    unittest.main()
