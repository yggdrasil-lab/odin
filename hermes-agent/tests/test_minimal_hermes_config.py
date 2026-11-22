#!/usr/bin/env python3
"""Static regression tests for minimal single-coordinator Hermes and stack cleanup."""

from __future__ import annotations

from dataclasses import dataclass
import re
import sys
import unittest
from pathlib import Path

ODIN_ROOT = Path(__file__).resolve().parents[2]
HERMES_AGENT_DIR = ODIN_ROOT / "hermes-agent"
if str(HERMES_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT_DIR))

from odin_runtime.agy_policy import PREAMBLE_RULES  # noqa: E402


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


def _extract_workflow_step(text: str, step_name: str) -> str:
    """Return text for a GitHub Actions workflow step block by name."""
    lines = text.splitlines()
    captured: list[str] = []
    in_step = False
    target = f"      - name: {step_name}"
    for line in lines:
        if not in_step:
            if line.strip() == target.strip() and line.startswith("      - name:"):
                in_step = True
                captured.append(line)
            continue
        if line.startswith("      - name:") or (
            line and not line.startswith(("        ", "\t", "#"))
        ):
            break
        captured.append(line)
    return "\n".join(captured)


@dataclass(frozen=True)
class _EntrypointBranches:
    dashboard: str
    gateway: str


def _extract_entrypoint_branches(text: str) -> _EntrypointBranches:
    """Split entrypoint.sh into dashboard and gateway mode blocks."""
    match = re.search(
        r'(?ms)^if \[ "\$MODE" = "dashboard" \]; then\n(.*?)\nelse\n(.*?)\nfi\s*\Z',
        text,
    )
    if match is None:
        raise AssertionError("entrypoint.sh missing top-level dashboard/gateway branch")
    return _EntrypointBranches(dashboard=match.group(1), gateway=match.group(2))


class TestMinimalHermesConfig(unittest.TestCase):
    """Static invariants for minimal single-coordinator Odin stack configuration."""

    def test_config_template_enforces_pool2_claude_lock_and_zero_pool1(self) -> None:
        text = (ODIN_ROOT / "config" / "hermes" / "config.yaml.template").read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("gemini-4-", text.lower())
        self.assertNotIn("argon-high", text)
        self.assertNotIn("neon-high", text)
        self.assertNotIn("multiplex_profiles", text)
        self.assertEqual(_extract_top_level_yaml_section(text, "gateway"), "")

        model_sec = _extract_top_level_yaml_section(text, "model")
        self.assertIn('default: "${AGENT_MODEL}"', model_sec)
        self.assertIn('provider: "antigravity-subscription-directsdk"', model_sec)

        fallback_sec = _extract_top_level_yaml_section(text, "fallback_providers")
        self.assertIn('model: "agent-loop"', fallback_sec)
        self.assertIn('base_url: "http://muninn:4000/v1"', fallback_sec)
        self.assertIn('key_env: "MUNINN_MASTER_KEY"', fallback_sec)

        plugins_sec = _extract_top_level_yaml_section(text, "plugins")
        self.assertIn('"mnemosyne"', plugins_sec)

        toolsets_sec = _extract_top_level_yaml_section(text, "platform_toolsets")
        for expected_toolset in ("clarify", "cronjob", "delegation", "discord", "file", "memory", "skills", "terminal"):
            self.assertIn(expected_toolset, toolsets_sec)

    def test_config_template_delegation_and_named_muninn_provider(self) -> None:
        text = (ODIN_ROOT / "config" / "hermes" / "config.yaml.template").read_text(encoding="utf-8")

        delegation_sec = _extract_top_level_yaml_section(text, "delegation")
        self.assertIn('provider: "muninn"', delegation_sec)
        self.assertIn('model: "agent-delegate"', delegation_sec)
        self.assertIn('provider: "antigravity-subscription-directsdk"', delegation_sec)
        self.assertIn('model: "claude-sonnet-5-5-high"', delegation_sec)

        providers_sec = _extract_top_level_yaml_section(text, "providers")
        muninn_sec = _extract_indented_yaml_block(providers_sec, "muninn", indent=2)
        self.assertIn('base_url: "http://muninn:4000/v1"', muninn_sec)
        self.assertIn('key_env: "MUNINN_MASTER_KEY"', muninn_sec)

    def test_config_template_auxiliary_chains_and_title_generation(self) -> None:
        text = (ODIN_ROOT / "config" / "hermes" / "config.yaml.template").read_text(encoding="utf-8")
        aux_sec = _extract_top_level_yaml_section(text, "auxiliary")
        for task_name in ("compression", "memory", "background_review"):
            task_sec = _extract_indented_yaml_block(aux_sec, task_name, indent=2)
            self.assertIn('model: "claude-sonnet-5-5-medium"', task_sec, task_name)

        title_sec = _extract_indented_yaml_block(aux_sec, "title_generation", indent=2)
        self.assertIn('provider: "custom"', title_sec)
        self.assertIn('model: "compress"', title_sec)
        self.assertIn('base_url: "http://muninn:4000/v1"', title_sec)
        self.assertIn('key_env: "MUNINN_MASTER_KEY"', title_sec)
        self.assertIn('model: "claude-sonnet-5-5-low"', title_sec)

    def test_deploy_yml_sets_claude_allowlist_and_retires_dead_vars(self) -> None:
        deploy_path = ODIN_ROOT / ".github" / "workflows" / "deploy.yml"
        text = deploy_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertRegex(text, r"(?m)^\s*AGENT_MODEL:\s*claude-sonnet-5-5-high\s*$")
        self.assertRegex(text, r"(?m)^\s*HERMES_AGY_ALLOWED_MODELS:\s*claude-sonnet-5-5\s*$")
        self.assertRegex(text, r"(?m)^\s*HERMES_AGY_ALLOWED_EFFORTS:\s*low,medium,high\s*$")
        self.assertIn("Validate Required Configuration", text)
        self.assertIn('[ -z "$DISCORD_CHANNEL_INFO_ID" ]', text)
        self.assertIn('[ -z "$HERMES_AGY_ALLOWED_MODELS" ]', text)
        self.assertIn('[ -z "$HERMES_AGY_ALLOWED_EFFORTS" ]', text)
        for retired_var in (
            "KANBAN_",
            "OLLAMA_",
            "CHAT_MODEL",
            "LLDAP_LDAP_",
            "WEBUI_SECRET_KEY",
            "KRATOS_MCP_TOKEN",
            "MCP_KRATOS_API_KEY",
            "render_profiles.py",
            "roles.json",
            "/opt/data/profiles",
        ):
            self.assertNotIn(retired_var, text)

    def test_muninn_config_routes_free_gemini38_and_retires_ollama_and_agent_auto(self) -> None:
        muninn_path = ODIN_ROOT / "config" / "muninn" / "config.yaml.template"
        text = muninn_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("agy-vision", text.lower())
        self.assertNotIn("ollama", text.lower())
        self.assertNotIn("agent-auto", text)
        self.assertIn("&gemini_38_flash gemini/gemini-3.8-flash", text)
        self.assertIn("&gemini_35_flash_lite gemini/gemini-3.5-flash-lite", text)
        self.assertIn("budget_limit: 35.0", text)

        for alias, expected_anchor in (
            ("memory-extract", "*gemini_38_flash"),
            ("compress", "&gemini_35_flash_lite gemini/gemini-3.5-flash-lite"),
            ("app-synthesize", "*gemini_38_flash"),
            ("gemini-4-hydrogen", "*gemini_38_flash"),
        ):
            match = re.search(rf"(?ms)^\s*-\s*model_name:\s*{re.escape(alias)}\b.*?(?=^\s*-\s*model_name:|\Z)", text)
            self.assertIsNotNone(match, alias)
            block = match.group(0) if match else ""
            self.assertIn(f"model: {expected_anchor}", block, alias)
            self.assertIn("api_key: os.environ/GEMINI_API_KEY", block, alias)

        router_sec = _extract_top_level_yaml_section(text, "router_settings")
        self.assertRegex(router_sec, r"(?ms)gemini-4-hydrogen:\s*-\s*gemini-3\.5-flash-lite\s*-\s*agent-loop-deepseek")

    def test_docker_compose_uses_common_env_anchor_and_retires_dead_services(self) -> None:
        compose_path = ODIN_ROOT / "docker-compose.yml"
        text = compose_path.read_text(encoding="utf-8")

        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)
        self.assertNotIn("KANBAN_", text)
        self.assertNotIn("KRATOS_MCP_TOKEN", text)
        self.assertNotIn("MCP_KRATOS_API_KEY", text)
        self.assertIsNone(re.search(r"\$\{[A-Za-z0-9_]+[:-][^}]*\}", text), "Found compose default expansion")

        anchor_sec = _extract_top_level_yaml_section(text, "x-hermes-common-env")
        self.assertIn("x-hermes-common-env: &hermes-common-env", anchor_sec)
        self.assertIn("HERMES_AGY_ALLOWED_MODELS: ${HERMES_AGY_ALLOWED_MODELS}", anchor_sec)
        self.assertIn("HERMES_AGY_ALLOWED_EFFORTS: ${HERMES_AGY_ALLOWED_EFFORTS}", anchor_sec)

        services_sec = _extract_top_level_yaml_section(text, "services")
        for retired_svc in ("ollama", "agy-vision", "open-webui"):
            self.assertEqual(_extract_indented_yaml_block(services_sec, retired_svc, indent=2), "", retired_svc)

        for svc_name in ("huginn-gateway", "huginn-dashboard"):
            svc_sec = _extract_indented_yaml_block(services_sec, svc_name, indent=2)
            self.assertIn("<<: *hermes-common-env", svc_sec, svc_name)

        muninn_sec = _extract_indented_yaml_block(services_sec, "muninn", indent=2)
        self.assertIsNone(re.search(r"(?m)^\s+ports\s*:", muninn_sec), "muninn must not publish Swarm ports")
        self.assertIn("endpoint_mode: dnsrr", muninn_sec)
        self.assertIn("homepage.description=LiteLLM Prompt & Response Observability", muninn_sec)

        volumes_sec = _extract_top_level_yaml_section(text, "volumes")
        self.assertNotIn("ollama_data", volumes_sec)
        self.assertNotIn("open-webui_data", volumes_sec)

        secrets_sec = _extract_top_level_yaml_section(text, "secrets")
        self.assertNotIn("odin_lldap_user_pass", secrets_sec)
        self.assertNotIn("odin_webui_secret_key", secrets_sec)

        configs_sec = _extract_top_level_yaml_section(text, "configs")
        self.assertNotIn("ollama_init_script", configs_sec)

    def test_retired_files_removed_and_odin_runtime_plus_patch_runner_present(self) -> None:
        retired_paths = [
            ODIN_ROOT / "config" / "ollama-init" / "init.sh",
            ODIN_ROOT / "hermes-agent" / "scripts" / "agy_vision_proxy.py",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_agy_vision_proxy.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "fix_managed_worker_routed_env.py",
            ODIN_ROOT / "hermes-agent" / "tests" / "test_managed_worker_routed_env.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "fix_agy_output_cap_detection.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "fix_agy_v1029_worker_usage_and_native_tool.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "fix_agy_catalog_and_claude55_aliases.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "enhance_agy_prompt_preamble.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "hook_pre_compress_wrapup.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "route_mnemosyne_to_memory_task.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "enhance_gateway_retry_and_single_query_safety.py",
            ODIN_ROOT / "hermes-agent" / "patches" / "fix_startup_api_media_sweep_offload.py",
        ]
        for path in retired_paths:
            self.assertFalse(path.exists(), f"Retired path still exists: {path}")

        self.assertTrue((HERMES_AGENT_DIR / "odin_runtime" / "__init__.py").is_file())
        self.assertTrue((HERMES_AGENT_DIR / "patches" / "patch_runner.py").is_file())
        self.assertTrue((HERMES_AGENT_DIR / "patches" / "specs.py").is_file())

        handover_text = (HERMES_AGENT_DIR / "odin_runtime" / "pre_compress_handover.py").read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"\b\d{17,20}\b", handover_text))
        self.assertNotIn("/opt/data/.env", handover_text)
        self.assertNotIn("/opt/data/hooks", handover_text)

        dockerfile_text = (HERMES_AGENT_DIR / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY odin_runtime /opt/hermes/.venv/lib/python3.14/site-packages/odin_runtime", dockerfile_text)
        self.assertIn("patch_runner.py apply", dockerfile_text)

        entrypoint_text = (HERMES_AGENT_DIR / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertNotIn("kanban-gateway", entrypoint_text)
        self.assertNotIn("render_profiles.py", entrypoint_text)
        self.assertIn("chmod 755 /opt/data", entrypoint_text)
        self.assertIn("chmod 600 /opt/data/gateway.sock /opt/data/gateway.lock", entrypoint_text)

    def test_odin_runtime_preamble_batches_readonly_tools(self) -> None:
        joined = "\n".join(PREAMBLE_RULES)
        self.assertNotIn("one file or step at a time", joined)
        self.assertNotIn("Focus on one tool or file edit per turn", joined)
        self.assertIn("Batch independent read-only tool calls", joined)
        self.assertNotIn("\u2014", joined)
        self.assertNotIn("\u2013", joined)

    def test_deploy_yml_has_post_deploy_swarm_stability_gate(self) -> None:
        text = (ODIN_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        deploy_idx = text.find('/tmp/deploy.sh "odin" docker-compose.yml')
        gate_idx = text.find("- name: Verify Post-Deploy Swarm Convergence")
        self.assertGreater(deploy_idx, -1)
        self.assertGreater(gate_idx, deploy_idx)

        step_text = _extract_workflow_step(text, "Verify Post-Deploy Swarm Convergence")
        self.assertNotIn("\u2014", step_text)
        self.assertNotIn("\u2013", step_text)

        curl_cmd = (
            "curl -fsSL "
            "https://raw.githubusercontent.com/yggdrasil-lab/yggdrasil-os/main/deploy/verify_convergence.sh "
            "-o /tmp/verify_convergence.sh"
        )
        chmod_cmd = "chmod +x /tmp/verify_convergence.sh"
        run_cmd = '/tmp/verify_convergence.sh "odin" odin_huginn-gateway odin_huginn-dashboard'
        curl_idx = step_text.find(curl_cmd)
        chmod_idx = step_text.find(chmod_cmd)
        run_idx = step_text.find(run_cmd)
        self.assertGreater(curl_idx, -1)
        self.assertGreater(chmod_idx, curl_idx)
        self.assertGreater(run_idx, chmod_idx)

    def test_entrypoint_opts_out_of_upstream_bundled_skills(self) -> None:
        text = (HERMES_AGENT_DIR / "entrypoint.sh").read_text(encoding="utf-8")
        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u2013", text)

        branches = _extract_entrypoint_branches(text)
        marker_cmd = "touch /opt/data/.no-bundled-skills"

        root_chown_match = re.search(
            r'(?ms)^  if \[ "\$\(id -u\)" = "0" \]; then\n(.*?)^  fi\n',
            branches.dashboard,
        )
        self.assertIsNotNone(root_chown_match, "dashboard root chown block missing")
        root_block = root_chown_match.group(1) if root_chown_match else ""
        root_touch = root_block.find(marker_cmd)
        root_chown = root_block.find('chown -R "$TARGET_UID:$TARGET_GID" /opt/data')
        self.assertGreater(root_touch, -1, "dashboard root branch must create .no-bundled-skills")
        self.assertGreater(root_chown, root_touch, "marker must be created before chown -R")

        non_root_match = re.search(
            r'(?ms)^  if \[ "\$\(id -u\)" = "0" \]; then\n(?:(?!^  fi\b).)*?^  else\n(.*?)^  fi',
            branches.dashboard,
        )
        self.assertIsNotNone(non_root_match, "dashboard non-root else block missing")
        non_root_block = non_root_match.group(1) if non_root_match else ""
        non_root_touch = non_root_block.find(marker_cmd)
        dash_exec = non_root_block.find("hermes dashboard")
        self.assertGreater(non_root_touch, -1, "dashboard else branch must create .no-bundled-skills")
        self.assertGreater(dash_exec, non_root_touch, "marker must be created before hermes dashboard")

        gw_chmod = branches.gateway.find("chmod 755 /opt/data")
        gw_touch = branches.gateway.find(marker_cmd)
        gw_exec = branches.gateway.find("exec /opt/hermes/.venv/bin/hermes gateway run")
        self.assertGreater(gw_chmod, -1)
        self.assertGreater(gw_touch, gw_chmod, "gateway branch must create .no-bundled-skills after chmod 755")
        self.assertGreater(gw_exec, gw_touch, "gateway marker must precede hermes gateway run")


if __name__ == "__main__":
    unittest.main()
