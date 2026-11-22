#!/usr/bin/env python3
"""Build-time patch: fix agy catalog HOME derivation, Claude 5.5/Gemini 4 efforts, and env allowlist lock.

Problems
--------
1. ``models.load_catalog()`` spawns ``agy models`` inheriting the gateway process
   ``HOME`` (often ``/root``), while persisted ``agy`` subscription credentials live under
   ``/opt/data/home/.gemini/antigravity-cli``.
2. Upstream ``_KNOWN_EFFORTS`` omits ``claude-sonnet-5-5``, ``claude-opus-5-5``, and
   ``gemini-4-{hydrogen,neon,carbon,argon}``, while ``_MODEL_ALIASES`` downgrades 5-5 to 4-6.
3. Minimal Hermes must strictly enforce ``HERMES_AGY_ALLOWED_MODELS`` (e.g. ``gemini-4-hydrogen``)
   and ``HERMES_AGY_ALLOWED_EFFORTS`` (e.g. ``low,medium,high``) across catalog discovery,
   aliases, ``ProviderProfile.default_aux_model``, and fail-loud model/effort resolution so
   disallowed models (such as Pool 2 Claude or Pool 1 Argon/Neon/Carbon) are never invoked.

Fixes
-----
1. Derive ``HOME`` from ``process.resolve_real_token_path()`` in ``models.load_catalog()``.
2. Register efforts for ``claude-sonnet-5-5``, ``claude-opus-5-5``, and ``gemini-4-*``.
3. Enforce ``HERMES_AGY_ALLOWED_MODELS`` and ``HERMES_AGY_ALLOWED_EFFORTS`` across
   ``models.py`` (``_KNOWN_EFFORTS``, ``_FALLBACK_MODELS``, ``_MODEL_ALIASES``,
   ``parse_catalog``, ``_cached_catalog``, ``model_efforts``, ``resolve_model_alias``,
   and fail-loud ``ValueError`` in ``resolve_model_and_effort``) and ``__init__.py``
   (``default_aux_model``, ``resolve_aux_model``, ``fetch_models``,
   ``supported_reasoning_efforts``, and ``_LazyModelCapabilities``).
4. Align upstream assertions in ``tests/test_antigravity_plugin.py``.
"""
from __future__ import annotations

import argparse
import py_compile
import sys
from pathlib import Path

DEFAULT_PLUGIN_DIR = Path("/opt/hermes/plugins/model-providers/antigravity-subscription-directsdk")

MARKER_MODELS = "# odin-patch: agy catalog HOME derivation, claude-5-5/gemini-4 efforts, and env allowlist lock"
MARKER_INIT = "# odin-patch: enforce HERMES_AGY_ALLOWED_MODELS on Antigravity profile"
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

REPL_TABLES = '''_ALL_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = {
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.7-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("low", "medium", "high"),
    "gemini-3.1-pro": ("low", "high"),
    "gemini-4-hydrogen": ("low", "medium", "high"),
    "gemini-4-neon": ("low", "medium", "high"),
    "gemini-4-carbon": ("low", "medium", "high"),
    "gemini-4-argon": ("low", "medium", "high"),
    "claude-sonnet-5-5": ("low", "medium", "high"),
    "claude-opus-5-5": ("low", "medium", "high"),
    "claude-sonnet-4-6": (),
    "claude-opus-4-6-thinking": (),
    "gpt-oss-120b": ("medium",),
}

_ALL_MODEL_ALIASES: dict[str, str] = {
    "default": "gemini-3.8-flash",
    "flash": "gemini-3.8-flash",
    "gemini-flash": "gemini-3.8-flash",
    "gemini-3.8": "gemini-3.8-flash",
    "pro": "gemini-3.1-pro",
    "gemini-pro": "gemini-3.1-pro",
    "gemini-3.1": "gemini-3.1-pro",
    "sonnet": "claude-sonnet-5-5",
    "claude-sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
    "claude-opus": "claude-opus-5-5",
}

_ALLOWED_MODELS_ENV = "HERMES_AGY_ALLOWED_MODELS"
_ALLOWED_EFFORTS_ENV = "HERMES_AGY_ALLOWED_EFFORTS"
_RECOGNIZED_EFFORTS = frozenset({"none", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})


def _parse_allowed_env(var_name: str) -> tuple[str, ...] | None:
    """Parse a comma-separated allowlist env var into ordered lowercase tokens, or None if unset."""
    raw = os.environ.get(var_name)
    if raw is None or not raw.strip():
        return None
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


def allowed_models() -> tuple[str, ...] | None:
    """Return configured allowed base models from HERMES_AGY_ALLOWED_MODELS, or None if unlocked."""
    return _parse_allowed_env(_ALLOWED_MODELS_ENV)


def allowed_efforts() -> tuple[str, ...] | None:
    """Return configured allowed effort levels from HERMES_AGY_ALLOWED_EFFORTS, or None if unlocked."""
    return _parse_allowed_env(_ALLOWED_EFFORTS_ENV)


def filter_catalog(catalog: dict[str, tuple[str, ...]]) -> dict[str, tuple[str, ...]]:
    """Filter a base-model -> efforts mapping to active model and effort allowlists."""
    models_allow = allowed_models()
    efforts_allow = allowed_efforts()
    if models_allow is None and efforts_allow is None:
        return dict(catalog)
    model_set = set(models_allow) if models_allow is not None else None
    effort_set = set(efforts_allow) if efforts_allow is not None else None
    filtered: dict[str, tuple[str, ...]] = {}
    for base, efforts in catalog.items():
        if model_set is not None and base.lower() not in model_set:
            continue
        if effort_set is None:
            filtered[base] = efforts
            continue
        kept = tuple(e for e in efforts if e in effort_set)
        if efforts and not kept:
            continue
        filtered[base] = kept
    return filtered


def effective_known_efforts() -> dict[str, tuple[str, ...]]:
    """Return built-in known efforts filtered by active allowlist env vars."""
    return filter_catalog(_ALL_KNOWN_EFFORTS)


def effective_fallback_models() -> list[str]:
    """Return fallback base model names filtered by active allowlist env vars."""
    return list(effective_known_efforts())


def effective_model_aliases() -> dict[str, str]:
    """Return alias mapping adjusted for active HERMES_AGY_ALLOWED_MODELS."""
    models_allow = allowed_models()
    if models_allow is None:
        return dict(_ALL_MODEL_ALIASES)
    if not models_allow:
        return {}
    primary = models_allow[0]
    aliases: dict[str, str] = {"default": primary, "flash": primary}
    for alias, target in _ALL_MODEL_ALIASES.items():
        if alias not in ("default", "flash") and target in models_allow:
            aliases[alias] = target
    return aliases


def default_model_name() -> str:
    """Return active default base model (first allowed model when locked)."""
    models_allow = allowed_models()
    if models_allow:
        return models_allow[0]
    return "gemini-3.8-flash"


def default_aux_model_name() -> str:
    """Return default auxiliary model (e.g. gemini-4-hydrogen-low when locked)."""
    models_allow = allowed_models()
    if not models_allow:
        return "gemini-3.8-flash"
    base = models_allow[0]
    efforts_allow = allowed_efforts()
    if efforts_allow is None or "low" in efforts_allow:
        return f"{base}-low"
    if efforts_allow:
        return f"{base}-{efforts_allow[0]}"
    return base


_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = effective_known_efforts()
_FALLBACK_MODELS = effective_fallback_models()
_MODEL_ALIASES = effective_model_aliases()
'''

ANCHOR_PARSE_CATALOG_TAIL = (
    "        efforts = grouped.setdefault(base, [])\n"
    "        if effort and effort not in efforts:\n"
    "            efforts.append(effort)\n"
    "    return {\n"
    "        base: tuple(sorted(efforts, key=_EFFORT_RANK.__getitem__))\n"
    "        for base, efforts in grouped.items()\n"
    "    }\n"
)
REPL_PARSE_CATALOG_TAIL = (
    "        efforts = grouped.setdefault(base, [])\n"
    "        if effort and effort not in efforts:\n"
    "            efforts.append(effort)\n"
    "    return filter_catalog({\n"
    "        base: tuple(sorted(efforts, key=_EFFORT_RANK.__getitem__))\n"
    "        for base, efforts in grouped.items()\n"
    "    })\n"
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

ANCHOR_CACHED_AND_EFFORTS = (
    "def _cached_catalog() -> dict[str, tuple[str, ...]] | None:\n"
    "    cache = _catalog_cache\n"
    "    if cache is not None and time.monotonic() < cache[0]:\n"
    "        return cache[1]\n"
    "    return None\n\n\n"
    "def model_efforts(base_model: str, *, refresh: bool = True) -> tuple[str, ...] | None:\n"
    '    """Efforts agy accepts for `base_model`; () means bare name, None means unknown.\n\n'
    "    Order: a fresh `agy models` result, then the built-in table, then (when\n"
    "    `refresh` is true) a new `agy models` call. While a cached result is\n"
    "    fresh, a model it does not list is not looked up again until the cache\n"
    "    expires, so an unknown name costs no process spawn per request.\n"
    '    """\n'
    "    cached = _cached_catalog()\n"
    "    if cached is not None and base_model in cached:\n"
    "        return cached[base_model]\n"
    "    if base_model in _KNOWN_EFFORTS:\n"
    "        return _KNOWN_EFFORTS[base_model]\n"
    "    if refresh and cached is None:\n"
    "        return load_catalog(only_if_stale=True).get(base_model)\n"
    "    return None\n"
)
REPL_CACHED_AND_EFFORTS = '''def _cached_catalog() -> dict[str, tuple[str, ...]] | None:
    cache = _catalog_cache
    if cache is not None and time.monotonic() < cache[0]:
        return filter_catalog(cache[1])
    return None


def model_efforts(base_model: str, *, refresh: bool = True) -> tuple[str, ...] | None:
    """Efforts agy accepts for `base_model`; () means bare name, None means unknown."""
    models_allow = allowed_models()
    if models_allow is not None and base_model.lower() not in models_allow:
        return None
    efforts_allow = allowed_efforts()
    cached = _cached_catalog()
    if cached is not None and base_model in cached:
        return cached[base_model]
    known = effective_known_efforts()
    if base_model in known:
        return known[base_model]
    if refresh and cached is None:
        efforts = load_catalog(only_if_stale=True).get(base_model)
        if efforts is not None and efforts_allow is not None:
            return tuple(e for e in efforts if e in efforts_allow)
        return efforts
    return None
'''

ANCHOR_RESOLVE_MODEL = (
    "def resolve_model_alias(\n"
    "    model: str, catalog: dict[str, tuple[str, ...]] | None = None\n"
    ") -> str:\n"
    '    """Resolve user alias to concrete base model, prioritizing the live agy catalog."""\n'
    '    m = (model or "gemini-3.8-flash").strip()\n'
    "    lower_m = m.lower()\n\n"
    "    if catalog:\n"
    "        # Exact match or base match already exists in catalog\n"
    "        if lower_m in catalog:\n"
    "            return lower_m\n"
    "        base, suffix = split_effort_suffix(lower_m)\n"
    "        if base in catalog:\n"
    "            return lower_m\n\n"
    '        lookup_term = "flash" if base == "default" else base\n\n'
    "        # Dynamic family matching against whatever models agy actually returned\n"
    "        family_keywords = {\n"
    '            ("sonnet", "claude-sonnet"): "sonnet",\n'
    '            ("opus", "claude-opus"): "opus",\n'
    '            ("pro", "gemini-pro", "gemini-3.1"): "pro",\n'
    '            ("flash", "gemini-flash", "gemini-3.8"): "flash",\n'
    '            ("gpt", "gpt-oss", "oss"): "gpt",\n'
    "        }\n"
    "        for triggers, kw in family_keywords.items():\n"
    "            if lookup_term in triggers:\n"
    "                matches = [k for k in catalog if kw in k.lower()]\n"
    "                if matches:\n"
    "                    # Sort descending so newer versions win if multiple exist (5-5 before 4-6, 3.8 before 3.7)\n"
    "                    best = sorted(matches, reverse=True)[0]\n"
    '                    return f"{best}-{suffix}" if suffix else best\n\n'
    "        # General substring match against catalog\n"
    "        matches = [k for k in catalog if lookup_term in k.lower()]\n"
    "        if matches:\n"
    "            best = sorted(matches, reverse=True)[0]\n"
    '            return f"{best}-{suffix}" if suffix else best\n\n'
    "        # Check explicit fallback aliases if not matched dynamically in catalog\n"
    "        if lookup_term in _MODEL_ALIASES:\n"
    "            aliased = _MODEL_ALIASES[lookup_term]\n"
    "            if aliased in catalog:\n"
    '                return f"{aliased}-{suffix}" if suffix else aliased\n\n'
    "    base, suffix = split_effort_suffix(lower_m)\n"
    "    fallback = _MODEL_ALIASES.get(base, base)\n"
    "    if suffix and fallback != base:\n"
    '        return f"{fallback}-{suffix}"\n'
    "    return _MODEL_ALIASES.get(lower_m, m)\n\n\n"
    "def resolve_model_and_effort(\n"
    "    model: str | None,\n"
    "    reasoning_effort: str | None = None,\n"
    ") -> tuple[str, str | None]:\n"
    '    """Map user/hermes model request to concrete CLI model ID and effort level.\n\n'
    "    Returns (model id, effort). The effort is None for models that agy selects\n"
    "    by bare name; the client then omits --effort.\n"
    '    """\n'
    '    raw_model = str(model or "gemini-3.8-flash").strip()\n'
    "    catalog = _cached_catalog()\n"
    "    if catalog is None:\n"
    "        catalog = load_catalog(only_if_stale=True)\n\n"
    "    m = resolve_model_alias(raw_model, catalog)\n\n"
    "    base_model, suffix_effort = split_effort_suffix(m)\n"
    "    supported = model_efforts(base_model, refresh=False)\n"
    "    if supported is None:\n"
    "        supported = _heuristic_efforts(base_model, suffix_effort is not None)\n"
    "    if not supported:\n"
    "        return base_model, None\n\n"
    "    # Resolve effort: explicit argument > model suffix > config setting > default\n"
    "    effort = _normalize_effort(reasoning_effort)\n"
    "    if not effort and suffix_effort:\n"
    "        effort = suffix_effort\n"
    "    if not effort:\n"
    "        try:\n"
    "            from hermes_cli.config import load_config_readonly\n"
    '            cfg_effort = load_config_readonly().get("agent", {}).get("reasoning_effort")\n'
    "            effort = _normalize_effort(cfg_effort)\n"
    "        except Exception:\n"
    "            pass\n"
    "    if not effort:\n"
    '        effort = "medium"\n\n'
    "    concrete_effort = _nearest_effort(effort, supported)\n"
    '    return f"{base_model}-{concrete_effort}", concrete_effort\n'
)

REPL_RESOLVE_MODEL = '''def _match_unlocked_catalog(
    lookup_term: str, suffix: str | None, catalog: dict[str, tuple[str, ...]]
) -> str | None:
    """Apply upstream family and substring matching when no model allowlist is active."""
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
                best = sorted(matches, reverse=True)[0]
                return f"{best}-{suffix}" if suffix else best
    matches = [k for k in catalog if lookup_term in k.lower()]
    if matches:
        best = sorted(matches, reverse=True)[0]
        return f"{best}-{suffix}" if suffix else best
    return None


def resolve_model_alias(
    model: str, catalog: dict[str, tuple[str, ...]] | None = None
) -> str:
    """Resolve user alias to concrete base model, honoring active allowlists."""
    m = (model or default_model_name()).strip()
    lower_m = m.lower()
    models_allow = allowed_models()
    aliases = effective_model_aliases()
    filtered_catalog = filter_catalog(catalog) if catalog else catalog

    if filtered_catalog:
        if lower_m in filtered_catalog:
            return lower_m
        base, suffix = split_effort_suffix(lower_m)
        if base in filtered_catalog:
            return lower_m
        lookup_term = "flash" if base == "default" else base
        if models_allow is None:
            matched = _match_unlocked_catalog(lookup_term, suffix, filtered_catalog)
            if matched is not None:
                return matched
        if lookup_term in aliases:
            aliased = aliases[lookup_term]
            if aliased in filtered_catalog:
                return f"{aliased}-{suffix}" if suffix else aliased

    base, suffix = split_effort_suffix(lower_m)
    fallback = aliases.get(base, base)
    if suffix and fallback != base:
        return f"{fallback}-{suffix}"
    return aliases.get(lower_m, m)


def _validate_allowed_request(
    raw_model: str,
    base_model: str,
    explicit_effort: str | None,
    suffix_effort: str | None,
) -> None:
    """Raise ValueError when HERMES_AGY_ALLOWED_MODELS or HERMES_AGY_ALLOWED_EFFORTS rejects the request."""
    models_allow = allowed_models()
    if models_allow is not None and base_model.lower() not in models_allow:
        raise ValueError(
            f"Model {raw_model!r} (resolved base {base_model!r}) is not permitted by "
            f"{_ALLOWED_MODELS_ENV}={','.join(models_allow)}"
        )
    efforts_allow = allowed_efforts()
    if efforts_allow is None:
        return
    if suffix_effort is not None and suffix_effort not in efforts_allow:
        raise ValueError(
            f"Reasoning effort {suffix_effort!r} from model {raw_model!r} is not permitted by "
            f"{_ALLOWED_EFFORTS_ENV}={','.join(efforts_allow)}"
        )
    if explicit_effort is not None and str(explicit_effort).strip():
        raw_eff = str(explicit_effort).strip().lower()
        if raw_eff not in _RECOGNIZED_EFFORTS:
            raise ValueError(
                f"Unknown reasoning effort {explicit_effort!r} is not permitted by "
                f"{_ALLOWED_EFFORTS_ENV}={','.join(efforts_allow)}"
            )
        norm_eff = _normalize_effort(raw_eff)
        if norm_eff not in efforts_allow:
            raise ValueError(
                f"Reasoning effort {norm_eff!r} (from {explicit_effort!r}) is not permitted by "
                f"{_ALLOWED_EFFORTS_ENV}={','.join(efforts_allow)}"
            )


def _resolve_requested_effort(
    reasoning_effort: str | None,
    suffix_effort: str | None,
    efforts_allow: tuple[str, ...] | None,
) -> str:
    """Resolve effective reasoning effort from explicit arg, model suffix, config, or default."""
    effort = _normalize_effort(reasoning_effort)
    if not effort and suffix_effort:
        effort = suffix_effort
    if not effort:
        try:
            from hermes_cli.config import load_config_readonly
            cfg_effort = load_config_readonly().get("agent", {}).get("reasoning_effort")
            effort = _normalize_effort(cfg_effort)
        except Exception:
            effort = None
        if effort and efforts_allow is not None and effort not in efforts_allow:
            raise ValueError(
                f"Configured reasoning effort {effort!r} is not permitted by "
                f"{_ALLOWED_EFFORTS_ENV}={','.join(efforts_allow)}"
            )
    return effort or "medium"


def resolve_model_and_effort(
    model: str | None,
    reasoning_effort: str | None = None,
) -> tuple[str, str | None]:
    """Map user/hermes model request to concrete CLI model ID and effort level."""
    raw_model = str(model or default_model_name()).strip()
    catalog = _cached_catalog()
    if catalog is None:
        catalog = load_catalog(only_if_stale=True)

    m = resolve_model_alias(raw_model, catalog)
    base_model, suffix_effort = split_effort_suffix(m)
    _validate_allowed_request(raw_model, base_model, reasoning_effort, suffix_effort)

    efforts_allow = allowed_efforts()
    supported = model_efforts(base_model, refresh=False)
    if supported is None:
        supported = _heuristic_efforts(base_model, suffix_effort is not None)
        if efforts_allow is not None:
            supported = tuple(e for e in supported if e in efforts_allow)
    if not supported:
        if efforts_allow is not None:
            raise ValueError(
                f"Model {raw_model!r} has no permitted reasoning efforts under "
                f"{_ALLOWED_EFFORTS_ENV}={','.join(efforts_allow)}"
            )
        return base_model, None

    effort = _resolve_requested_effort(reasoning_effort, suffix_effort, efforts_allow)
    concrete_effort = _nearest_effort(effort, supported)
    return f"{base_model}-{concrete_effort}", concrete_effort
'''

ANCHOR_INIT_IMPORTS = (
    "try:\n"
    "    from .models import (\n"
    "        _FALLBACK_MODELS,\n"
    "        _MODEL_ALIASES,\n"
    "        _cached_catalog,\n"
    "        _heuristic_efforts,\n"
    "        load_catalog,\n"
    "        model_efforts,\n"
    "        resolve_model_alias,\n"
    "        split_effort_suffix,\n"
    "    )\n"
    "except ImportError:\n"
    "    # Loaded outside a package (e.g. a flat source tree under test):\n"
    "    # the absolute name is the same module.\n"
    "    from models import (\n"
    "        _FALLBACK_MODELS,\n"
    "        _MODEL_ALIASES,\n"
    "        _cached_catalog,\n"
    "        _heuristic_efforts,\n"
    "        load_catalog,\n"
    "        model_efforts,\n"
    "        resolve_model_alias,\n"
    "        split_effort_suffix,\n"
    "    )\n"
)
REPL_INIT_IMPORTS = (
    f"{MARKER_INIT}\n"
    "try:\n"
    "    from .models import (\n"
    "        _FALLBACK_MODELS,\n"
    "        _MODEL_ALIASES,\n"
    "        _cached_catalog,\n"
    "        _heuristic_efforts,\n"
    "        allowed_efforts,\n"
    "        allowed_models,\n"
    "        default_aux_model_name,\n"
    "        effective_fallback_models,\n"
    "        filter_catalog,\n"
    "        load_catalog,\n"
    "        model_efforts,\n"
    "        resolve_model_alias,\n"
    "        split_effort_suffix,\n"
    "    )\n"
    "except ImportError:\n"
    "    # Loaded outside a package (e.g. a flat source tree under test):\n"
    "    # the absolute name is the same module.\n"
    "    from models import (\n"
    "        _FALLBACK_MODELS,\n"
    "        _MODEL_ALIASES,\n"
    "        _cached_catalog,\n"
    "        _heuristic_efforts,\n"
    "        allowed_efforts,\n"
    "        allowed_models,\n"
    "        default_aux_model_name,\n"
    "        effective_fallback_models,\n"
    "        filter_catalog,\n"
    "        load_catalog,\n"
    "        model_efforts,\n"
    "        resolve_model_alias,\n"
    "        split_effort_suffix,\n"
    "    )\n"
)

ANCHOR_INIT_EFFORTS = (
    "        name = (model or \"\").strip()\n"
    "        catalog = _cached_catalog()\n"
    "        resolved_name = resolve_model_alias(name, catalog)\n"
    "        base, suffix_effort = split_effort_suffix(resolved_name)\n"
    "        # No process spawn here: use the cached catalog and the built-in table.\n"
    "        efforts = model_efforts(base, refresh=False)\n"
    "        if efforts is None:\n"
    "            efforts = _heuristic_efforts(base, suffix_effort is not None)\n"
    "        return efforts\n"
)
REPL_INIT_EFFORTS = (
    "        name = (model or \"\").strip()\n"
    "        catalog = _cached_catalog()\n"
    "        resolved_name = resolve_model_alias(name, catalog)\n"
    "        base, suffix_effort = split_effort_suffix(resolved_name)\n"
    "        models_allow = allowed_models()\n"
    "        if models_allow is not None and base.lower() not in models_allow:\n"
    "            return ()\n"
    "        # No process spawn here: use the cached catalog and the built-in table.\n"
    "        efforts = model_efforts(base, refresh=False)\n"
    "        if efforts is None:\n"
    "            efforts = _heuristic_efforts(base, suffix_effort is not None)\n"
    "        efforts_allow = allowed_efforts()\n"
    "        if efforts is not None and efforts_allow is not None:\n"
    "            return tuple(e for e in efforts if e in efforts_allow)\n"
    "        return efforts\n"
)

ANCHOR_INIT_FETCH_MODELS = (
    "        catalog = load_catalog(timeout=timeout)\n"
    "        if catalog:\n"
    "            return list(catalog)\n"
    "        return list(_FALLBACK_MODELS)\n"
)
REPL_INIT_FETCH_MODELS = (
    "        catalog = filter_catalog(load_catalog(timeout=timeout))\n"
    "        if catalog:\n"
    "            return list(catalog)\n"
    "        return effective_fallback_models()\n"
    "\n"
    "    @property\n"
    "    def default_aux_model(self) -> str:\n"
    '        """Return env-locked default auxiliary model when HERMES_AGY_ALLOWED_MODELS is active."""\n'
    "        if allowed_models() is not None:\n"
    "            return default_aux_model_name()\n"
    '        return getattr(self, "_default_aux_model", "gemini-3.8-flash")\n'
    "\n"
    "    @default_aux_model.setter\n"
    "    def default_aux_model(self, value: str) -> None:\n"
    "        self._default_aux_model = value\n"
    "\n"
    "    def resolve_aux_model(self, *, vision: bool = False) -> str:\n"
    '        """Return locked auxiliary model for title generation/aux callers when allowlist is active."""\n'
    "        if allowed_models() is not None:\n"
    "            return default_aux_model_name()\n"
    '        return ""\n'
)

ANCHOR_INIT_LAZY_CAPS = (
    "    def get(self, model: Any, default: Any = None) -> Any:\n"
    '        name = str(model or "").strip()\n'
    "        if not name:\n"
    "            return default\n"
    "        verdict = self._declare(name.lower())\n"
)
REPL_INIT_LAZY_CAPS = (
    "    def get(self, model: Any, default: Any = None) -> Any:\n"
    '        name = str(model or "").strip()\n'
    "        if not name:\n"
    "            return default\n"
    "        models_allow = allowed_models()\n"
    "        if models_allow is not None:\n"
    "            resolved = resolve_model_alias(name, _cached_catalog()).lower()\n"
    "            resolved_base, _ = split_effort_suffix(resolved)\n"
    "            if resolved_base not in models_allow:\n"
    "                return default\n"
    "            verdict = self._declare(resolved)\n"
    "            return verdict if verdict is not None else default\n"
    "        verdict = self._declare(name.lower())\n"
)

ANCHOR_INIT_DEFAULT_AUX = '    default_aux_model="gemini-3.8-flash",\n'
REPL_INIT_DEFAULT_AUX = '    default_aux_model=default_aux_model_name(),\n'

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

    replacements = (
        (ANCHOR_IMPORTS, REPL_IMPORTS, "models.py:imports"),
        (ANCHOR_TABLES, REPL_TABLES, "models.py:_KNOWN_EFFORTS_and_ALIASES"),
        (ANCHOR_PARSE_CATALOG_TAIL, REPL_PARSE_CATALOG_TAIL, "models.py:parse_catalog"),
        (ANCHOR_LOAD_CATALOG, REPL_LOAD_CATALOG, "models.py:load_catalog"),
        (ANCHOR_CACHED_AND_EFFORTS, REPL_CACHED_AND_EFFORTS, "models.py:cached_and_efforts"),
        (ANCHOR_RESOLVE_MODEL, REPL_RESOLVE_MODEL, "models.py:resolve_model_and_effort"),
    )
    for anchor, repl, label in replacements:
        src = _replace_exact(src, anchor, repl, label)
    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (catalog HOME derivation + claude-5-5/gemini-4 efforts + allowlist)")
    return True


def patch_init(plugin_dir: Path) -> bool:
    target = plugin_dir / "__init__.py"
    if not target.is_file():
        raise FileNotFoundError(f"ERROR: {target} not found")
    src = target.read_text(encoding="utf-8")
    if MARKER_INIT in src:
        print(f"[odin-patch] {target.name} already patched; skipping")
        return False

    replacements = (
        (ANCHOR_INIT_IMPORTS, REPL_INIT_IMPORTS, "__init__.py:imports"),
        (ANCHOR_INIT_EFFORTS, REPL_INIT_EFFORTS, "__init__.py:supported_reasoning_efforts"),
        (ANCHOR_INIT_FETCH_MODELS, REPL_INIT_FETCH_MODELS, "__init__.py:fetch_models_and_aux"),
        (ANCHOR_INIT_LAZY_CAPS, REPL_INIT_LAZY_CAPS, "__init__.py:lazy_capabilities"),
        (ANCHOR_INIT_DEFAULT_AUX, REPL_INIT_DEFAULT_AUX, "__init__.py:default_aux_model"),
    )
    for anchor, repl, label in replacements:
        src = _replace_exact(src, anchor, repl, label)
    target.write_text(src, encoding="utf-8")
    py_compile.compile(str(target), doraise=True)
    print(f"[odin-patch] patched {target} (profile allowlist lock)")
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
        description="Patch antigravity models.py and __init__.py for HOME derivation, efforts, and allowlist lock."
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
        patch_init(plugin_dir)
        patch_tests(plugin_dir)
    except Exception as exc:
        print(f"[odin-patch] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
