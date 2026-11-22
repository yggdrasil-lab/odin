"""Antigravity model catalog policy, allowlist enforcement, and prompt preamble rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import os
from pathlib import Path

ALLOWED_MODELS_ENV = "HERMES_AGY_ALLOWED_MODELS"
ALLOWED_EFFORTS_ENV = "HERMES_AGY_ALLOWED_EFFORTS"
DEFAULT_BASE_MODEL = "gemini-3.8-flash"

RECOGNIZED_EFFORTS: frozenset[str] = frozenset(
    {"none", "off", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}
)
EFFORT_RANK: dict[str, int] = {"low": 0, "medium": 1, "high": 2}
EFFORT_SUFFIXES: tuple[tuple[str, str], ...] = (
    ("-xhigh", "high"),
    ("-high", "high"),
    ("-medium", "medium"),
    ("-low", "low"),
)

ALL_KNOWN_EFFORTS: dict[str, tuple[str, ...]] = {
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

ALL_MODEL_ALIASES: dict[str, str] = {
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

_FAMILY_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("sonnet", "claude-sonnet"), "sonnet"),
    (("opus", "claude-opus"), "opus"),
    (("pro", "gemini-pro", "gemini-3.1"), "pro"),
    (("flash", "gemini-flash", "gemini-3.8"), "flash"),
    (("gpt", "gpt-oss", "oss"), "gpt"),
)

PREAMBLE_RULES: tuple[str, ...] = (
    "OUTPUT BUDGET & BREVITY:",
    "- Each response has a hard per-turn completion token limit covering both visible text and hidden reasoning tokens.",
    "- Stay concise, direct, and focused on the immediate turn. NEVER attempt massive multi-file changes or huge speculative diffs in a single response.",
    "- If a task requires editing multiple files or drafting large artifacts, keep each file edit focused and incremental across turns.",
    "TOOL DISCIPLINE & EXECUTION BOUNDARIES:",
    "- When calling tools, emit ONLY the tool call(s) and stop immediately. Do NOT emit duplicate tool calls, speculative code blocks, or conversational chatter alongside tool calls.",
    "- Batch independent read-only tool calls (e.g. read_file, search_files, grep) in parallel within a single turn, while keeping file edits focused and incremental.",
    "- Never assume or claim an action succeeded until you have received and inspected the actual tool output in the subsequent turn.",
    "TIMEZONE & LOCAL TIME:",
    "- The user lives in Auckland, New Zealand (Pacific/Auckland, UTC+12 / UTC+13 with DST). All relative time references ('today', 'yesterday', 'tomorrow', 'morning', 'tonight', dates) must align strictly with Auckland local time.",
    "TURN ANCHORING & ZERO REGURGITATION:",
    "- Always anchor your response strictly to the user's latest incoming message. Respond ONLY to what the user is asking right now.",
    "- After context compaction or across turns, NEVER repeat a previous response or re-deliver a past wrap-up, retrospective, or completed task summary.",
    "- If a task or wrap-up was already finished earlier, or if the user asks a follow-up question or points out a duplicate response, address their immediate question directly instead of re-summarizing previous work.",
    "- Discard stale in-flight summaries and '[STILL IN PROGRESS]' markers if newer user instructions exist or if the requested actions were already completed.",
    "FAIL-LOUD ARCHITECTURE & ZERO SILENT FALLBACKS:",
    "- Anything that can fail must fail loudly and immediately. NEVER implement silent fallbacks, speculative secondary endpoints, catch-all default values, or broad exception suppression to mask errors.",
    "- If required environment variables, credentials, network dependencies, or configuration keys are missing or invalid, raise an explicit exception or exit non-zero immediately so the root cause is resolved at the source.",
)


@dataclass(frozen=True)
class AgyPolicy:
    """Immutable Antigravity model and reasoning-effort allowlist policy."""

    allowed_models: tuple[str, ...] | None
    allowed_efforts: tuple[str, ...] | None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> AgyPolicy:
        """Build an AgyPolicy from process or explicit environment mapping."""
        source = os.environ if env is None else env
        return cls(
            allowed_models=_parse_allowlist(source.get(ALLOWED_MODELS_ENV)),
            allowed_efforts=_parse_allowlist(source.get(ALLOWED_EFFORTS_ENV)),
        )

    def filter_catalog(
        self, catalog: Mapping[str, tuple[str, ...]]
    ) -> dict[str, tuple[str, ...]]:
        """Filter a base-model -> efforts mapping against active allowlists."""
        if self.allowed_models is None and self.allowed_efforts is None:
            return dict(catalog)
        model_set = set(self.allowed_models) if self.allowed_models is not None else None
        effort_set = set(self.allowed_efforts) if self.allowed_efforts is not None else None
        return _filter_entries(catalog, model_set, effort_set)

    def known_efforts(self) -> dict[str, tuple[str, ...]]:
        """Return built-in known efforts filtered by this policy."""
        return self.filter_catalog(ALL_KNOWN_EFFORTS)

    def fallback_models(self) -> list[str]:
        """Return fallback base model names filtered by this policy."""
        return list(self.known_efforts())

    def model_aliases(self) -> dict[str, str]:
        """Return alias mapping adjusted for allowed models."""
        if self.allowed_models is None:
            return dict(ALL_MODEL_ALIASES)
        if not self.allowed_models:
            return {}
        primary = self.allowed_models[0]
        aliases: dict[str, str] = {"default": primary, "flash": primary}
        for alias, target in ALL_MODEL_ALIASES.items():
            if alias not in ("default", "flash") and target in self.allowed_models:
                aliases[alias] = target
        return aliases

    def default_model(self) -> str:
        """Return active default base model, failing loud on empty allowlist."""
        if self.allowed_models is None:
            return DEFAULT_BASE_MODEL
        if not self.allowed_models:
            raise ValueError(
                f"{ALLOWED_MODELS_ENV} is set but empty; no Antigravity models are permitted"
            )
        return self.allowed_models[0]

    def default_aux_model(self) -> str:
        """Return active default auxiliary model, failing loud on empty allowlist."""
        if self.allowed_efforts is not None and not self.allowed_efforts:
            raise ValueError(
                f"{ALLOWED_EFFORTS_ENV} is set but empty; no reasoning efforts are permitted"
            )
        if self.allowed_models is None:
            return DEFAULT_BASE_MODEL
        base = self.default_model()
        if self.allowed_efforts is None:
            return f"{base}-low"
        chosen = "low" if "low" in self.allowed_efforts else self.allowed_efforts[0]
        return f"{base}-{chosen}"

    def model_efforts(
        self,
        base_model: str,
        *,
        cached_catalog: Mapping[str, tuple[str, ...]] | None = None,
        load_catalog_fn: Callable[[], Mapping[str, tuple[str, ...]]] | None = None,
        refresh: bool = True,
    ) -> tuple[str, ...] | None:
        """Return permitted efforts for `base_model`; () means bare, None means unknown."""
        base_key = base_model.lower()
        if self.allowed_models is not None and base_key not in self.allowed_models:
            return None
        if cached_catalog is not None and base_key in cached_catalog:
            return _filter_effort_tuple(cached_catalog[base_key], self.allowed_efforts)
        known = self.known_efforts()
        if base_key in known:
            return known[base_key]
        if refresh and cached_catalog is None and load_catalog_fn is not None:
            efforts = load_catalog_fn().get(base_key)
            return _filter_effort_tuple(efforts, self.allowed_efforts)
        return None

    def resolve_model_alias(
        self,
        model: str,
        catalog: Mapping[str, tuple[str, ...]] | None = None,
    ) -> str:
        """Resolve user alias to concrete base model, honoring active allowlists."""
        cleaned = (model or self.default_model()).strip().lower()
        aliases = self.model_aliases()
        filtered = self.filter_catalog(catalog) if catalog else None
        if filtered:
            matched = _match_catalog(cleaned, filtered, aliases, self.allowed_models is None)
            if matched is not None:
                return matched
        base, suffix = split_effort_suffix(cleaned)
        fallback = aliases.get(base, base)
        if suffix and fallback != base:
            return f"{fallback}-{suffix}"
        return aliases.get(cleaned, cleaned)

    def resolve_model_and_effort(
        self,
        model: str | None,
        reasoning_effort: str | None = None,
        *,
        catalog: Mapping[str, tuple[str, ...]] | None = None,
        load_catalog_fn: Callable[[], Mapping[str, tuple[str, ...]]] | None = None,
        config_effort: str | None = None,
    ) -> tuple[str, str | None]:
        """Resolve concrete CLI model ID and effort level under allowlist policy."""
        raw_model = str(model or self.default_model()).strip()
        active_catalog = catalog
        if active_catalog is None and load_catalog_fn is not None:
            active_catalog = load_catalog_fn()
        resolved = self.resolve_model_alias(raw_model, active_catalog)
        base_model, suffix_effort = split_effort_suffix(resolved)
        self._validate_request(raw_model, base_model, reasoning_effort, suffix_effort)
        supported = self._resolve_supported_efforts(
            raw_model, base_model, suffix_effort, active_catalog
        )
        if not supported:
            return base_model, None
        effort = self._effective_effort(reasoning_effort, suffix_effort, config_effort)
        concrete = nearest_effort(effort, supported)
        return f"{base_model}-{concrete}", concrete

    def supported_reasoning_efforts(
        self,
        model: str | None,
        *,
        catalog: Mapping[str, tuple[str, ...]] | None = None,
    ) -> tuple[str, ...]:
        """Return permitted reasoning efforts for UI/model pickers."""
        name = (model or "").strip()
        resolved = self.resolve_model_alias(name, catalog)
        base, suffix_effort = split_effort_suffix(resolved)
        if self.allowed_models is not None and base.lower() not in self.allowed_models:
            return ()
        efforts = self.model_efforts(base, cached_catalog=catalog, refresh=False)
        if efforts is None:
            efforts = heuristic_efforts(base, suffix_effort is not None)
        return _filter_effort_tuple(efforts, self.allowed_efforts) or ()

    def lazy_capability_target(
        self,
        model: str,
        *,
        catalog: Mapping[str, tuple[str, ...]] | None = None,
    ) -> str | None:
        """Return target model ID for capability lookup, or None if disallowed."""
        name = str(model or "").strip()
        if not name:
            return None
        if self.allowed_models is None:
            return name.lower()
        resolved = self.resolve_model_alias(name, catalog).lower()
        resolved_base, _ = split_effort_suffix(resolved)
        return resolved if resolved_base in self.allowed_models else None

    def _validate_request(
        self,
        raw_model: str,
        base_model: str,
        explicit_effort: str | None,
        suffix_effort: str | None,
    ) -> None:
        self._validate_model_allowed(raw_model, base_model)
        self._validate_efforts_allowed(raw_model, explicit_effort, suffix_effort)

    def _validate_model_allowed(self, raw_model: str, base_model: str) -> None:
        if self.allowed_models is None:
            return
        if not self.allowed_models:
            raise ValueError(
                f"{ALLOWED_MODELS_ENV} is set but empty; no Antigravity models are permitted"
            )
        if base_model.lower() not in self.allowed_models:
            allowed_str = ",".join(self.allowed_models)
            raise ValueError(
                f"Model {raw_model!r} (resolved base {base_model!r}) is not permitted by "
                f"{ALLOWED_MODELS_ENV}={allowed_str}"
            )

    def _validate_efforts_allowed(
        self,
        raw_model: str,
        explicit_effort: str | None,
        suffix_effort: str | None,
    ) -> None:
        if self.allowed_efforts is None:
            return
        if not self.allowed_efforts:
            raise ValueError(
                f"{ALLOWED_EFFORTS_ENV} is set but empty; no reasoning efforts are permitted"
            )
        allowed_str = ",".join(self.allowed_efforts)
        if suffix_effort is not None and suffix_effort not in self.allowed_efforts:
            raise ValueError(
                f"Reasoning effort {suffix_effort!r} from model {raw_model!r} is not permitted "
                f"by {ALLOWED_EFFORTS_ENV}={allowed_str}"
            )
        if explicit_effort is not None and str(explicit_effort).strip():
            _validate_explicit_effort(explicit_effort, self.allowed_efforts, allowed_str)

    def _resolve_supported_efforts(
        self,
        raw_model: str,
        base_model: str,
        suffix_effort: str | None,
        catalog: Mapping[str, tuple[str, ...]] | None,
    ) -> tuple[str, ...]:
        supported = self.model_efforts(base_model, cached_catalog=catalog, refresh=False)
        if supported is None:
            raw_heuristic = heuristic_efforts(base_model, suffix_effort is not None)
            supported = _filter_effort_tuple(raw_heuristic, self.allowed_efforts) or ()
        if not supported and self.allowed_efforts is not None:
            raise ValueError(
                f"Model {raw_model!r} has no permitted reasoning efforts under "
                f"{ALLOWED_EFFORTS_ENV}={','.join(self.allowed_efforts)}"
            )
        return supported

    def _effective_effort(
        self,
        explicit_effort: str | None,
        suffix_effort: str | None,
        config_effort: str | None,
    ) -> str:
        effort = normalize_effort(explicit_effort) or suffix_effort
        if effort:
            return effort
        cfg_norm = normalize_effort(config_effort)
        if cfg_norm and self.allowed_efforts is not None and cfg_norm not in self.allowed_efforts:
            raise ValueError(
                f"Configured reasoning effort {cfg_norm!r} is not permitted by "
                f"{ALLOWED_EFFORTS_ENV}={','.join(self.allowed_efforts)}"
            )
        return cfg_norm or "medium"


def normalize_effort(effort: str | None) -> str | None:
    """Normalize a reasoning effort alias into low/medium/high or None."""
    if not effort:
        return None
    cleaned = str(effort).strip().lower()
    if cleaned in ("none", "off", "minimal"):
        return "low"
    if cleaned in ("xhigh", "max", "ultra"):
        return "high"
    if cleaned in ("low", "medium", "high"):
        return cleaned
    return "medium"


def split_effort_suffix(model: str) -> tuple[str, str | None]:
    """Split `<base>-<effort>` into `(base, normalized_effort)`."""
    for suffix, effort in EFFORT_SUFFIXES:
        if model.endswith(suffix):
            return model[: -len(suffix)], effort
    return model, None


def heuristic_efforts(base_model: str, has_suffix: bool) -> tuple[str, ...]:
    """Infer efforts for models outside the built-in table."""
    if base_model.startswith("gemini-") and "pro" in base_model:
        return ("low", "high")
    if base_model.startswith("gemini-") and "flash" in base_model:
        return ("low", "medium", "high")
    return ("low", "medium", "high") if has_suffix else ()


def nearest_effort(requested: str, supported: tuple[str, ...]) -> str:
    """Select closest supported effort; ties favor the stronger effort."""
    target = EFFORT_RANK[requested]
    return min(supported, key=lambda e: (abs(EFFORT_RANK[e] - target), -EFFORT_RANK[e]))


def derive_agy_home(token_path: str | Path | None) -> str | None:
    """Derive `$HOME` from `<home>/.gemini/antigravity-cli/<token>` path."""
    if token_path is None:
        return None
    tp = Path(token_path)
    if len(tp.parents) <= 2 or tp.parents[2] == Path(tp.anchor):
        return None
    return str(tp.parents[2])


def filter_catalog(
    catalog: Mapping[str, tuple[str, ...]],
    *,
    env: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, ...]]:
    """Convenience wrapper filtering `catalog` using `AgyPolicy.from_env(env)`."""
    return AgyPolicy.from_env(env).filter_catalog(catalog)


def effective_known_efforts(
    *, env: Mapping[str, str] | None = None
) -> dict[str, tuple[str, ...]]:
    """Return built-in known efforts filtered by active policy."""
    return AgyPolicy.from_env(env).known_efforts()


def effective_fallback_models(*, env: Mapping[str, str] | None = None) -> list[str]:
    """Return fallback base model names filtered by active policy."""
    return AgyPolicy.from_env(env).fallback_models()


def effective_model_aliases(*, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return alias mapping filtered by active policy."""
    return AgyPolicy.from_env(env).model_aliases()


def default_model_name(*, env: Mapping[str, str] | None = None) -> str:
    """Return active default base model from policy."""
    return AgyPolicy.from_env(env).default_model()


def default_aux_model(*, env: Mapping[str, str] | None = None) -> str:
    """Return active default auxiliary model from policy."""
    return AgyPolicy.from_env(env).default_aux_model()


def resolve_model_alias(
    model: str,
    catalog: Mapping[str, tuple[str, ...]] | None = None,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """Convenience alias resolution via `AgyPolicy.from_env(env)`."""
    return AgyPolicy.from_env(env).resolve_model_alias(model, catalog)


def resolve_model_and_effort(
    model: str | None,
    reasoning_effort: str | None = None,
    *,
    catalog: Mapping[str, tuple[str, ...]] | None = None,
    load_catalog_fn: Callable[[], Mapping[str, tuple[str, ...]]] | None = None,
    config_effort: str | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[str, str | None]:
    """Convenience model and effort resolution via `AgyPolicy.from_env(env)`."""
    return AgyPolicy.from_env(env).resolve_model_and_effort(
        model,
        reasoning_effort,
        catalog=catalog,
        load_catalog_fn=load_catalog_fn,
        config_effort=config_effort,
    )


def _parse_allowlist(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    return tuple(dict.fromkeys(part.strip().lower() for part in raw.split(",") if part.strip()))


def _filter_effort_tuple(
    efforts: tuple[str, ...] | None,
    allowed_efforts: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    if efforts is None or allowed_efforts is None:
        return efforts
    return tuple(e for e in efforts if e in allowed_efforts)


def _filter_entries(
    catalog: Mapping[str, tuple[str, ...]],
    model_set: set[str] | None,
    effort_set: set[str] | None,
) -> dict[str, tuple[str, ...]]:
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


def _match_catalog(
    lower_m: str,
    catalog: Mapping[str, tuple[str, ...]],
    aliases: Mapping[str, str],
    unlocked: bool,
) -> str | None:
    if lower_m in catalog:
        return lower_m
    base, suffix = split_effort_suffix(lower_m)
    if base in catalog:
        return lower_m
    lookup = "flash" if base == "default" else base
    if unlocked:
        matched = _match_unlocked_family(lookup, suffix, catalog)
        if matched is not None:
            return matched
    aliased = aliases.get(lookup)
    if aliased and aliased in catalog:
        return f"{aliased}-{suffix}" if suffix else aliased
    return None


def _match_unlocked_family(
    lookup_term: str,
    suffix: str | None,
    catalog: Mapping[str, tuple[str, ...]],
) -> str | None:
    for triggers, kw in _FAMILY_KEYWORDS:
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


def _validate_explicit_effort(
    explicit_effort: str,
    allowed_efforts: tuple[str, ...],
    allowed_str: str,
) -> None:
    raw_eff = str(explicit_effort).strip().lower()
    if raw_eff not in RECOGNIZED_EFFORTS:
        raise ValueError(
            f"Unknown reasoning effort {explicit_effort!r} is not permitted by "
            f"{ALLOWED_EFFORTS_ENV}={allowed_str}"
        )
    norm_eff = normalize_effort(raw_eff)
    if norm_eff not in allowed_efforts:
        raise ValueError(
            f"Reasoning effort {norm_eff!r} (from {explicit_effort!r}) is not permitted by "
            f"{ALLOWED_EFFORTS_ENV}={allowed_str}"
        )
