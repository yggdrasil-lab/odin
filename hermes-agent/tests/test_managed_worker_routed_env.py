#!/usr/bin/env python3
"""Standalone tests for fix_managed_worker_routed_env build-time patch."""

from __future__ import annotations

import ast
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

import fix_managed_worker_routed_env as worker_env_patch  # noqa: E402


def _build_session_managed_worker_fixture(*, include_anchor: bool = True) -> str:
    if not include_anchor:
        return "import os\n\ndef _worker_env(authority):\n    return {}\n"
    return '''import os
from pathlib import Path

def _launch_env_only_credentials(home, env):
    return {"LAUNCH_ONLY": "launch-secret"}

def _worker_env(authority):
    from pathlib import Path
    from agent.secret_scope import is_multiplex_active
    from tools.environments.local import _is_routed_home
    home = Path(str(authority.profile_id))
    if not home.is_absolute():
        return None
    routed = _is_routed_home(home)
    if not routed and not is_multiplex_active():
        return None
    from agent.secret_scope import build_profile_secret_scope
    from tools.environments.local import _scrub_credentials, build_subprocess_env, strip_launch_profile_env
    env = build_subprocess_env(base=strip_launch_profile_env(os.environ.copy(), home), scrub_secrets=True)
    secrets = build_profile_secret_scope(home)
    if routed:
        _scrub_credentials(env, inherit_credentials=False)
    else:
        secrets = {**_launch_env_only_credentials(home, env), **secrets}
    env.update({k: v for k, v in secrets.items() if v is not None})
    env['HERMES_HOME'] = str(home)
    from hermes_constants import apply_subprocess_home_env
    apply_subprocess_home_env(env)
    return env
'''


def _fake_worker_deps(routed_value: bool, profile_secrets: dict[str, str]) -> dict[str, types.ModuleType]:
    scrub_keys = {
        "MUNINN_MASTER_KEY",
        "TYPESAFE_API_KEY",
        "TAVILY_API_KEY",
        "SEARXNG_URL",
        "CAMOFOX_URL",
        "CUSTOM_SVC_TOKEN",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_USERS",
        "GATEWAY_ALLOW_ALL_USERS",
        "API_SERVER_KEY",
        "API_SERVER_PORT",
        "OPENAI_API_KEY",
    }

    def fake_scrub(env: dict[str, str], *, inherit_credentials: bool = False) -> dict[str, str]:
        if not inherit_credentials:
            for key in list(env):
                if key in scrub_keys or key.startswith("API_SERVER_"):
                    del env[key]
        return env

    def fake_build_subprocess_env(*, base=None, scrub_secrets=True):
        out = dict(base or {})
        if scrub_secrets:
            fake_scrub(out, inherit_credentials=False)
        return out

    mod_agent = types.ModuleType("agent")
    mod_secret_scope = types.ModuleType("agent.secret_scope")
    mod_secret_scope.is_multiplex_active = lambda: True  # type: ignore[attr-defined]
    mod_secret_scope.build_profile_secret_scope = lambda _home: dict(profile_secrets)  # type: ignore[attr-defined]

    mod_tools = types.ModuleType("tools")
    mod_envs = types.ModuleType("tools.environments")
    mod_local = types.ModuleType("tools.environments.local")
    mod_local._is_routed_home = lambda _home: routed_value  # type: ignore[attr-defined]
    mod_local._scrub_credentials = fake_scrub  # type: ignore[attr-defined]
    mod_local.build_subprocess_env = fake_build_subprocess_env  # type: ignore[attr-defined]
    mod_local.strip_launch_profile_env = lambda env, _home: env  # type: ignore[attr-defined]

    mod_consts = types.ModuleType("hermes_constants")
    mod_consts.apply_subprocess_home_env = lambda env: env.setdefault("HOME", env["HERMES_HOME"])  # type: ignore[attr-defined]

    return {
        "agent": mod_agent,
        "agent.secret_scope": mod_secret_scope,
        "tools": mod_tools,
        "tools.environments": mod_envs,
        "tools.environments.local": mod_local,
        "hermes_constants": mod_consts,
    }


def _load_patched_worker_module(target: Path, mod_name: str) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(mod_name, str(target))
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


class TestManagedWorkerRoutedEnv(unittest.TestCase):
    def test_patch_applies_idempotently_and_fails_loudly_on_missing_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "session_managed_worker.py"
            target.write_text(_build_session_managed_worker_fixture(include_anchor=True), encoding="utf-8")

            self.assertEqual(worker_env_patch.main(["--target", str(target)]), 0)
            after_first = target.read_text(encoding="utf-8")
            self.assertEqual(worker_env_patch.main(["--target", str(target)]), 0)
            after_second = target.read_text(encoding="utf-8")

            self.assertEqual(after_first, after_second)
            self.assertEqual(after_first.count(worker_env_patch.MARKER), 1)
            ast.parse(after_first, filename=str(target))

        with tempfile.TemporaryDirectory() as tmpdir:
            bad_target = Path(tmpdir) / "session_managed_worker.py"
            bad_target.write_text(_build_session_managed_worker_fixture(include_anchor=False), encoding="utf-8")
            self.assertNotEqual(worker_env_patch.main(["--target", str(bad_target)]), 0)

    def test_routed_worker_forwards_allowlist_and_keeps_gateway_secrets_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            target = Path(tmpdir) / "session_managed_worker.py"
            target.write_text(_build_session_managed_worker_fixture(include_anchor=True), encoding="utf-8")
            self.assertTrue(worker_env_patch.patch_file(target))

            mod_name = "test_patched_session_managed_worker_mod"
            try:
                mod = _load_patched_worker_module(target, mod_name)
                fake_env = {
                    "MUNINN_MASTER_KEY": "muninn-host-key",
                    "TYPESAFE_API_KEY": "jev-host-key",
                    "TAVILY_API_KEY": "tavily-host-key",
                    "SEARXNG_URL": "http://poseidon_searxng:8080",
                    "CAMOFOX_URL": "",
                    "CUSTOM_SVC_TOKEN": "custom-host-token",
                    "DISCORD_BOT_TOKEN": "discord-secret",
                    "DISCORD_ALLOWED_USERS": "12345",
                    "GATEWAY_ALLOW_ALL_USERS": "true",
                    "API_SERVER_KEY": "api-secret",
                    "API_SERVER_PORT": "8642",
                    "OPENAI_API_KEY": "openai-secret",
                    "HERMES_ROUTED_WORKER_ENV_ALLOWLIST": "CUSTOM_SVC_TOKEN,DISCORD_BOT_TOKEN,API_SERVER_KEY",
                }
                deps = _fake_worker_deps(routed_value=True, profile_secrets={"TAVILY_API_KEY": "profile-tavily-override"})
                authority = SimpleNamespace(profile_id="/opt/data/profiles/huginn")

                with patch.dict(os.environ, fake_env, clear=True), patch.dict(sys.modules, deps):
                    env = mod._worker_env(authority)

                self.assertEqual(env["MUNINN_MASTER_KEY"], "muninn-host-key")
                self.assertEqual(env["TYPESAFE_API_KEY"], "jev-host-key")
                self.assertEqual(env["TAVILY_API_KEY"], "profile-tavily-override")
                self.assertEqual(env["SEARXNG_URL"], "http://poseidon_searxng:8080")
                self.assertNotIn("CAMOFOX_URL", env)
                self.assertEqual(env["CUSTOM_SVC_TOKEN"], "custom-host-token")

                for scrubbed in (
                    "DISCORD_BOT_TOKEN",
                    "DISCORD_ALLOWED_USERS",
                    "GATEWAY_ALLOW_ALL_USERS",
                    "API_SERVER_KEY",
                    "API_SERVER_PORT",
                    "OPENAI_API_KEY",
                ):
                    self.assertNotIn(scrubbed, env)
            finally:
                sys.modules.pop(mod_name, None)


if __name__ == "__main__":
    unittest.main()
