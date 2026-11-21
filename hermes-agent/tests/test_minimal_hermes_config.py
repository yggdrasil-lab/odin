#!/usr/bin/env python3
"""Static regression tests for minimal single-coordinator Hermes and Kanban decommission."""

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


class TestMinimalHermesConfig(unittest.TestCase):
    """Static invariants for minimal single-coordinator Odin stack configuration."""

    def test_config_template_enforces_minimal_single_coordinator_and_routing(self) -> None:
        cfg_path = ODIN_ROOT / "config" / "hermes" / "config.yaml.template"
        text = cfg_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("argon-high", text)
        self.assertNotIn("neon-high", text)

        gateway_sec = _extract_top_level_yaml_section(text, "gateway")
        self.assertRegex(gateway_sec, r"(?m)^\s+multiplex_profiles:\s*false\s*$")

        model_sec = _extract_top_level_yaml_section(text, "model")
        self.assertIn('default: "${AGENT_MODEL}"', model_sec)

        fallback_sec = _extract_top_level_yaml_section(text, "fallback_providers")
        claude_idx = fallback_sec.find('model: "claude-sonnet-5-5-high"')
        muninn_idx = fallback_sec.find('model: "agent-loop"')
        self.assertGreater(claude_idx, -1)
        self.assertGreater(muninn_idx, claude_idx)

        delegation_sec = _extract_top_level_yaml_section(text, "delegation")
        self.assertIn('model: "gemini-4-hydrogen-high"', delegation_sec)
        d_claude_idx = delegation_sec.find('model: "claude-sonnet-5-5-high"')
        d_muninn_idx = delegation_sec.find('model: "agent-delegate"')
        self.assertGreater(d_claude_idx, -1)
        self.assertGreater(d_muninn_idx, d_claude_idx)

        plugins_sec = _extract_top_level_yaml_section(text, "plugins")
        self.assertIn('"mnemosyne"', plugins_sec)

        toolsets_sec = _extract_top_level_yaml_section(text, "platform_toolsets")
        for expected_toolset in ("clarify", "cronjob", "delegation", "discord", "file", "memory", "skills", "terminal"):
            self.assertIn(expected_toolset, toolsets_sec)

    def test_deploy_yml_sets_hydrogen_and_drops_kanban_and_profiles(self) -> None:
        deploy_path = ODIN_ROOT / ".github" / "workflows" / "deploy.yml"
        text = deploy_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertRegex(text, r"(?m)^\s*AGENT_MODEL:\s*gemini-4-hydrogen-high\s*$")
        self.assertIn("Validate Required Configuration", text)
        self.assertNotIn("KANBAN_", text)
        self.assertNotIn("render_profiles.py", text)
        self.assertNotIn("roles.json", text)
        self.assertNotIn("/opt/data/profiles", text)

    def test_docker_compose_zero_kanban_zero_defaults_and_preserves_agy_vision(self) -> None:
        compose_path = ODIN_ROOT / "docker-compose.yml"
        text = compose_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("KANBAN_", text)
        self.assertIn("agy-vision:", text)
        self.assertIn("AGY_VISION_PORT=8643", text)
        self.assertIsNone(re.search(r"\$\{[A-Za-z0-9_]+[:-][^}]*\}", text), "Found compose default expansion")

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
