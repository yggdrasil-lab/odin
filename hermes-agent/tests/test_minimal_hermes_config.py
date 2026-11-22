#!/usr/bin/env python3
"""Static regression tests for minimal single-coordinator Hermes and stack cleanup."""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import textwrap
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


def _extract_step_run_script(step_text: str) -> str:
    """Return dedented shell script from a workflow step's `run: |` block."""
    lines = step_text.splitlines()
    captured: list[str] = []
    in_run = False
    for line in lines:
        if not in_run:
            if line.strip() == "run: |":
                in_run = True
            continue
        if line and not line.startswith("          "):
            break
        captured.append(line)
    return textwrap.dedent("\n".join(captured))


def _write_exec(path: Path, content: str) -> None:
    """Write an executable script file with 0755 permissions."""
    path.write_text(content, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _run_stability_gate_script(
    script: str,
    docker_script: str,
    date_script: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Execute extracted convergence step script against stubbed docker/sleep/date CLIs."""
    with tempfile.TemporaryDirectory() as tmp:
        bin_dir = Path(tmp) / "bin"
        bin_dir.mkdir()
        _write_exec(bin_dir / "docker", docker_script)
        _write_exec(bin_dir / "sleep", "#!/bin/sh\nexit 0\n")
        if date_script is not None:
            _write_exec(bin_dir / "date", date_script)
        env = os.environ.copy()
        env["PATH"] = f"{bin_dir}:{env.get('PATH', '')}"
        env["STATE_DIR"] = tmp
        return subprocess.run(
            ["bash", "-c", script],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )


def _build_dwell_docker_stub(second_gw_line: str) -> str:
    """Build a fake docker CLI that passes baseline then emits second_gw_line on dwell."""
    return textwrap.dedent(
        f"""\
        #!/bin/sh
        case "$1 $2" in
          "service ls") printf "odin_huginn-gateway|1/1\\nodin_huginn-dashboard|1/1\\n" ;;
          "service inspect") echo "" ;;
          "service ps")
            case "$*" in
              *"--filter desired-state=running"*)
                if [ "$3" = "odin_huginn-gateway" ]; then
                  n=$(cat "$STATE_DIR/gw_n" 2>/dev/null || echo 0)
                  n=$((n + 1))
                  echo "$n" > "$STATE_DIR/gw_n"
                  if [ "$n" -le 1 ]; then
                    echo "gw-task-1|Running 3 seconds ago|"
                  else
                    echo "{second_gw_line}"
                  fi
                else
                  echo "dash-task-1|Running 10 seconds ago|"
                fi
                ;;
              *) echo "DIAG_PS:$3" ;;
            esac
            ;;
          "service logs") echo "DIAG_LOGS:$*" ;;
        esac
        """
    )


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
        for required in (
            "odin_huginn-gateway",
            "odin_huginn-dashboard",
            'docker service ls --filter name=odin_ --format "{{.Name}}|{{.Replicas}}"',
            'docker service ps "$svc" --filter desired-state=running --no-trunc --format "{{.ID}}|{{.CurrentState}}|{{.Error}}"',
            "DWELL_CHECKS=3",
            "DWELL_INTERVAL=10",
            "docker service ps",
            "docker service logs --tail 80",
            "exit 1",
        ):
            self.assertIn(required, step_text)

    def test_post_deploy_convergence_gate_passes_when_tasks_stay_stable(self) -> None:
        text = (ODIN_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        script = _extract_step_run_script(_extract_workflow_step(text, "Verify Post-Deploy Swarm Convergence"))
        self.assertTrue(script.strip())

        docker_stub = textwrap.dedent(
            """\
            #!/bin/sh
            case "$1 $2" in
              "service ls")
                n=$(cat "$STATE_DIR/ls_n" 2>/dev/null || echo 0)
                n=$((n + 1))
                echo "$n" > "$STATE_DIR/ls_n"
                if [ "$n" -eq 1 ]; then
                  printf "odin_huginn-gateway|0/1\\nodin_huginn-dashboard|1/1\\n"
                else
                  printf "odin_huginn-gateway|1/1\\nodin_huginn-dashboard|1/1\\n"
                fi
                ;;
              "service inspect") echo "completed" ;;
              "service ps")
                case "$3" in
                  odin_huginn-gateway) echo "gw-task-1|Running 10 seconds ago|" ;;
                  odin_huginn-dashboard) echo "dash-task-1|Running 10 seconds ago|" ;;
                esac
                ;;
            esac
            """
        )
        proc = _run_stability_gate_script(script, docker_stub)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Dwell check 3/3 passed", proc.stdout)

    def test_post_deploy_convergence_gate_fails_on_task_id_churn(self) -> None:
        text = (ODIN_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        script = _extract_step_run_script(_extract_workflow_step(text, "Verify Post-Deploy Swarm Convergence"))
        self.assertTrue(script.strip())

        proc = _run_stability_gate_script(script, _build_dwell_docker_stub("gw-task-2|Running 1 second ago|"))
        self.assertEqual(proc.returncode, 1)
        self.assertIn("Task ID churn detected", proc.stderr)
        self.assertIn("DIAG_PS:odin_huginn-gateway", proc.stderr)
        self.assertIn("DIAG_LOGS:service logs --tail 80 odin_huginn-gateway", proc.stderr)

    def test_post_deploy_convergence_gate_fails_on_task_error_and_non_running_state(self) -> None:
        text = (ODIN_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        script = _extract_step_run_script(_extract_workflow_step(text, "Verify Post-Deploy Swarm Convergence"))
        self.assertTrue(script.strip())

        for dwell_line in ("gw-task-1|Running 2 seconds ago|task: non-zero exit (1)", "gw-task-1|Starting 1 second ago|"):
            proc = _run_stability_gate_script(script, _build_dwell_docker_stub(dwell_line))
            self.assertEqual(proc.returncode, 1, dwell_line)
            self.assertIn("Stability dwell check", proc.stderr)
            self.assertIn("DIAG_LOGS:service logs --tail 80 odin_huginn-gateway", proc.stderr)

    def test_post_deploy_convergence_gate_fails_on_convergence_timeout(self) -> None:
        text = (ODIN_ROOT / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")
        script = _extract_step_run_script(_extract_workflow_step(text, "Verify Post-Deploy Swarm Convergence"))
        self.assertTrue(script.strip())

        docker_stub = textwrap.dedent(
            """\
            #!/bin/sh
            case "$1 $2" in
              "service ls") printf "odin_huginn-gateway|0/1\\nodin_huginn-dashboard|1/1\\n" ;;
              "service ps") echo "DIAG_PS:$3" ;;
              "service logs") echo "DIAG_LOGS:$*" ;;
            esac
            """
        )
        date_stub = textwrap.dedent(
            """\
            #!/bin/sh
            n=$(cat "$STATE_DIR/date_n" 2>/dev/null || echo 0)
            n=$((n + 1))
            echo "$n" > "$STATE_DIR/date_n"
            if [ "$n" -eq 1 ]; then
              echo 1000
            else
              echo 1200
            fi
            """
        )
        proc = _run_stability_gate_script(script, docker_stub, date_stub)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("timed out", proc.stderr)
        self.assertIn("DIAG_PS:odin_huginn-gateway", proc.stderr)
        self.assertIn("DIAG_LOGS:service logs --tail 80 odin_huginn-gateway", proc.stderr)


if __name__ == "__main__":
    unittest.main()
