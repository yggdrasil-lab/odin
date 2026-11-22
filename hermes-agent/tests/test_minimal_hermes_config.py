#!/usr/bin/env python3
"""Static regression tests for minimal single-coordinator Hermes and Hydrogen model lock."""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ODIN_ROOT = Path(__file__).resolve().parents[2]
PATCHES_DIR = ODIN_ROOT / "hermes-agent" / "patches"
if str(PATCHES_DIR) not in sys.path:
    sys.path.insert(0, str(PATCHES_DIR))

import enhance_agy_prompt_preamble as preamble_patch  # noqa: E402


def _extract_top_level_yaml_section(text: str, section_name: str) -> str:
    """Return text from a top-level YAML key until the next top-level key."""
    lines = text.splitlines()
    captured: list[str] = []
    in_section = False
    prefix = f"{section_name}:"
    for line in lines:
        if not in_section:
            if line.startswith(prefix):
                in_section = True
                captured.append(line)
            continue
        if line and not line.startswith((" ", "\t", "#")):
            break
        captured.append(line)
    return "\n".join(captured)


def _extract_indented_yaml_block(text: str, key_name: str, indent: int = 2) -> str:
    """Return an indented YAML subsection (e.g. a service under services: or task under auxiliary:)."""
    lines = text.splitlines()
    captured: list[str] = []
    in_block = False
    target_prefix = f"{' ' * indent}{key_name}:"
    for line in lines:
        if not in_block:
            if line.startswith(target_prefix):
                in_block = True
                captured.append(line)
            continue
        stripped = line.lstrip(" ")
        leading = len(line) - len(stripped)
        if stripped and not stripped.startswith("#") and leading <= indent:
            break
        captured.append(line)
    return "\n".join(captured)


class TestMinimalHermesConfig(unittest.TestCase):
    """Static invariants for minimal single-coordinator Odin stack configuration."""

    def test_config_template_enforces_hydrogen_lock_and_zero_claude(self) -> None:
        cfg_path = ODIN_ROOT / "config" / "hermes" / "config.yaml.template"
        text = cfg_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("claude-", text.lower())
        self.assertNotIn("argon-high", text)
        self.assertNotIn("neon-high", text)
        self.assertNotIn("multiplex_profiles", text)
        self.assertEqual(_extract_top_level_yaml_section(text, "gateway"), "")

        model_sec = _extract_top_level_yaml_section(text, "model")
        self.assertIn('default: "${AGENT_MODEL}"', model_sec)

        fallback_sec = _extract_top_level_yaml_section(text, "fallback_providers")
        self.assertIn('model: "agent-loop"', fallback_sec)
        self.assertIn('base_url: "http://muninn:4000/v1"', fallback_sec)

        delegation_sec = _extract_top_level_yaml_section(text, "delegation")
        self.assertIn('model: "gemini-4-hydrogen-high"', delegation_sec)
        self.assertIn('model: "agent-delegate"', delegation_sec)

        aux_sec = _extract_top_level_yaml_section(text, "auxiliary")
        for task_name in ("compression", "memory", "background_review"):
            task_sec = _extract_indented_yaml_block(aux_sec, task_name, indent=2)
            self.assertIn('model: "gemini-4-hydrogen-medium"', task_sec, task_name)

        title_sec = _extract_indented_yaml_block(aux_sec, "title_generation", indent=2)
        self.assertIn('provider: "custom"', title_sec)
        self.assertIn('model: "compress"', title_sec)
        self.assertIn('base_url: "http://muninn:4000/v1"', title_sec)
        self.assertIn('key_env: "MUNINN_MASTER_KEY"', title_sec)
        self.assertIn('model: "gemini-4-hydrogen-low"', title_sec)

        plugins_sec = _extract_top_level_yaml_section(text, "plugins")
        self.assertIn('"mnemosyne"', plugins_sec)

        toolsets_sec = _extract_top_level_yaml_section(text, "platform_toolsets")
        for expected_toolset in ("clarify", "cronjob", "delegation", "discord", "file", "memory", "skills", "terminal"):
            self.assertIn(expected_toolset, toolsets_sec)

    def test_deploy_yml_sets_hydrogen_allowlist_and_validates_config(self) -> None:
        deploy_path = ODIN_ROOT / ".github" / "workflows" / "deploy.yml"
        text = deploy_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertRegex(text, r"(?m)^\s*AGENT_MODEL:\s*gemini-4-hydrogen-high\s*$")
        self.assertRegex(text, r"(?m)^\s*HERMES_AGY_ALLOWED_MODELS:\s*gemini-4-hydrogen\s*$")
        self.assertRegex(text, r"(?m)^\s*HERMES_AGY_ALLOWED_EFFORTS:\s*low,medium,high\s*$")
        self.assertIn("Validate Required Configuration", text)
        self.assertIn('[ -z "$HERMES_AGY_ALLOWED_MODELS" ]', text)
        self.assertIn('[ -z "$HERMES_AGY_ALLOWED_EFFORTS" ]', text)
        self.assertNotIn("KANBAN_", text)
        self.assertNotIn("render_profiles.py", text)
        self.assertNotIn("roles.json", text)
        self.assertNotIn("/opt/data/profiles", text)

    def test_docker_compose_passes_allowlist_zero_defaults_and_no_muninn_ports(self) -> None:
        compose_path = ODIN_ROOT / "docker-compose.yml"
        text = compose_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("KANBAN_", text)
        self.assertIsNone(re.search(r"\$\{[A-Za-z0-9_]+[:-][^}]*\}", text), "Found compose default expansion")

        services_sec = _extract_top_level_yaml_section(text, "services")
        for svc_name in ("agy-vision", "huginn-gateway", "huginn-dashboard"):
            svc_sec = _extract_indented_yaml_block(services_sec, svc_name, indent=2)
            self.assertIn("HERMES_AGY_ALLOWED_MODELS=${HERMES_AGY_ALLOWED_MODELS}", svc_sec, svc_name)
            self.assertIn("HERMES_AGY_ALLOWED_EFFORTS=${HERMES_AGY_ALLOWED_EFFORTS}", svc_sec, svc_name)

        self.assertIn("AGY_VISION_PORT=8643", _extract_indented_yaml_block(services_sec, "agy-vision", indent=2))
        muninn_sec = _extract_indented_yaml_block(services_sec, "muninn", indent=2)
        self.assertIsNone(re.search(r"(?m)^\s+ports:\s*$", muninn_sec), "muninn must not publish Swarm ports")
        self.assertIn("endpoint_mode: dnsrr", muninn_sec)

    def test_retired_kanban_and_profile_files_are_removed(self) -> None:
        retired_paths = [
            ODIN_ROOT / "hermes-agent" / "scripts" / "kanban_jev_gateway.py",
            ODIN_ROOT / "hermes-agent" / "scripts" / "kanban_gateway",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_kanban_dependencies_and_wrapup.py",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_kanban_ide_ownership.py",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_kanban_role_routing.py",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_kanban_verification_pipeline.py",
            ODIN_ROOT / "config" / "hermes" / "profiles",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_profile_renderer.py",
        ]
        for path in retired_paths:
            self.assertFalse(path.exists(), f"Retired path still exists: {path}")

        dockerfile_text = (ODIN_ROOT / "hermes-agent" / "Dockerfile").read_text(encoding="utf-8")
        self.assertNotIn("kanban-gateway", dockerfile_text)

        entrypoint_text = (ODIN_ROOT / "hermes-agent" / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertNotIn("kanban-gateway", entrypoint_text)
        self.assertNotIn("render_profiles.py", entrypoint_text)
        self.assertIn("chmod 755 /opt/data", entrypoint_text)
        self.assertIn("chmod 600 /opt/data/gateway.sock /opt/data/gateway.lock", entrypoint_text)

    def test_enhance_agy_prompt_preamble_batches_readonly_tools(self) -> None:
        self.assertNotIn("one file or step at a time", preamble_patch.INJECTION)
        self.assertNotIn("Focus on one tool or file edit per turn", preamble_patch.INJECTION)
        self.assertIn("Batch independent read-only tool calls", preamble_patch.INJECTION)
        self.assertIn("Batch independent read-only tool calls", preamble_patch.TEST_INJECTION)
        self.assertNotIn("\u2014", preamble_patch.INJECTION)
        self.assertNotIn("\u2013", preamble_patch.INJECTION)


if __name__ == "__main__":
    unittest.main()
