#!/usr/bin/env python3
"""Direct interface unit tests for odin_runtime.agy_policy."""

from __future__ import annotations

from pathlib import Path
import sys
import unittest

HERMES_AGENT_DIR = Path(__file__).resolve().parent.parent
if str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

from odin_runtime.agy_policy import (  # noqa: E402
    PREAMBLE_RULES,
    AgyPolicy,
    default_aux_model,
    default_model_name,
    derive_agy_home,
    effective_known_efforts,
    effective_model_aliases,
    filter_catalog,
    resolve_model_alias,
    resolve_model_and_effort,
)


class TestOdinRuntimeAgyPolicy(unittest.TestCase):
    """Tests for allowlist enforcement, catalog filtering, HOME derivation, and preamble."""

    def test_unlocked_policy_exposes_claude55_and_gemini4_efforts(self) -> None:
        env: dict[str, str] = {}
        known = effective_known_efforts(env=env)
        aliases = effective_model_aliases(env=env)
        self.assertEqual(known["claude-sonnet-5-5"], ("low", "medium", "high"))
        self.assertEqual(known["claude-opus-5-5"], ("low", "medium", "high"))
        self.assertEqual(known["gemini-4-hydrogen"], ("low", "medium", "high"))
        self.assertEqual(aliases["sonnet"], "claude-sonnet-5-5")
        self.assertEqual(aliases["opus"], "claude-opus-5-5")
        self.assertEqual(default_aux_model(env=env), "gemini-3.8-flash")
        self.assertEqual(
            resolve_model_and_effort("sonnet", "medium", catalog={}, env=env),
            ("claude-sonnet-5-5-medium", "medium"),
        )

    def test_pool2_claude_lock_filters_catalog_and_rejects_pool1_gemini(self) -> None:
        env = {
            "HERMES_AGY_ALLOWED_MODELS": "claude-sonnet-5-5",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high",
        }
        policy = AgyPolicy.from_env(env)
        raw_catalog = {
            "claude-sonnet-5-5": ("low", "medium", "high"),
            "gemini-4-neon": ("low", "medium", "high"),
            "gemini-4-argon": ("low", "medium", "high"),
            "gemini-4-hydrogen": ("low", "medium", "high"),
            "claude-opus-5-5": ("low", "medium", "high"),
        }
        self.assertEqual(
            filter_catalog(raw_catalog, env=env),
            {"claude-sonnet-5-5": ("low", "medium", "high")},
        )
        self.assertEqual(policy.default_model(), "claude-sonnet-5-5")
        self.assertEqual(policy.default_aux_model(), "claude-sonnet-5-5-low")
        self.assertEqual(
            policy.resolve_model_and_effort("claude-sonnet-5-5", "high", catalog=raw_catalog),
            ("claude-sonnet-5-5-high", "high"),
        )
        self.assertEqual(
            policy.supported_reasoning_efforts("claude-sonnet-5-5", catalog=raw_catalog),
            ("low", "medium", "high"),
        )
        self.assertEqual(
            policy.supported_reasoning_efforts("gemini-4-neon", catalog=raw_catalog),
            (),
        )
        self.assertIsNone(policy.lazy_capability_target("gemini-4-neon", catalog=raw_catalog))
        self.assertEqual(
            policy.lazy_capability_target("claude-sonnet-5-5-high", catalog=raw_catalog),
            "claude-sonnet-5-5-high",
        )
        for blocked in (
            "gemini-4-neon-high",
            "gemini-4-argon-high",
            "gemini-4-hydrogen-high",
            "claude-opus-5-5-high",
            "gemini-3.8-flash",
        ):
            with self.assertRaises(ValueError):
                policy.resolve_model_and_effort(blocked, catalog=raw_catalog)

    def test_restricted_efforts_and_alias_normalization(self) -> None:
        env = {
            "HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen",
            "HERMES_AGY_ALLOWED_EFFORTS": "medium,high",
        }
        policy = AgyPolicy.from_env(env)
        self.assertEqual(policy.default_aux_model(), "gemini-4-hydrogen-medium")
        self.assertEqual(resolve_model_alias("default", env=env), "gemini-4-hydrogen")
        self.assertEqual(
            policy.resolve_model_and_effort("gemini-4-hydrogen", "xhigh", catalog={}),
            ("gemini-4-hydrogen-high", "high"),
        )
        for bad in (("gemini-4-hydrogen", "low"), ("gemini-4-hydrogen-low", None), ("gemini-4-hydrogen", "bogus")):
            with self.assertRaises(ValueError):
                policy.resolve_model_and_effort(*bad, catalog={})
        with self.assertRaises(ValueError):
            policy.resolve_model_and_effort("gemini-4-hydrogen", config_effort="low", catalog={})

    def test_empty_allowlists_return_empty_catalog_and_fail_loud_on_resolve(self) -> None:
        empty_models = {"HERMES_AGY_ALLOWED_MODELS": "", "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high"}
        self.assertEqual(filter_catalog({"claude-sonnet-5-5": ("low",)}, env=empty_models), {})
        with self.assertRaises(ValueError):
            default_model_name(env=empty_models)
        with self.assertRaises(ValueError):
            default_aux_model(env=empty_models)
        with self.assertRaises(ValueError):
            resolve_model_and_effort("claude-sonnet-5-5", env=empty_models)

        empty_efforts = {"HERMES_AGY_ALLOWED_MODELS": "claude-sonnet-5-5", "HERMES_AGY_ALLOWED_EFFORTS": ""}
        self.assertEqual(filter_catalog({"claude-sonnet-5-5": ("low",)}, env=empty_efforts), {})
        with self.assertRaises(ValueError):
            default_aux_model(env=empty_efforts)
        with self.assertRaises(ValueError):
            resolve_model_and_effort("claude-sonnet-5-5", "low", env=empty_efforts)

    def test_derive_agy_home_and_preamble_rules(self) -> None:
        self.assertEqual(
            derive_agy_home("/opt/data/home/.gemini/antigravity-cli/jetski-standalone-oauth-token"),
            "/opt/data/home",
        )
        self.assertIsNone(derive_agy_home(None))
        self.assertIsNone(derive_agy_home("/a/b"))
        preamble_str = " ".join(PREAMBLE_RULES)
        self.assertIn("OUTPUT BUDGET & BREVITY:", preamble_str)
        self.assertIn("TOOL DISCIPLINE & EXECUTION BOUNDARIES:", preamble_str)
        self.assertIn("Batch independent read-only tool calls", preamble_str)
        self.assertIn("Pacific/Auckland", preamble_str)
        self.assertIn("TURN ANCHORING & ZERO REGURGITATION:", preamble_str)
        self.assertIn("FAIL-LOUD ARCHITECTURE & ZERO SILENT FALLBACKS:", preamble_str)
        self.assertNotIn("one file or step at a time", preamble_str)
        self.assertNotIn("\u2014", preamble_str)
        self.assertNotIn("\u2013", preamble_str)


if __name__ == "__main__":
    unittest.main()
