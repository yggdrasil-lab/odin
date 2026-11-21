import os
import re
import tempfile
import unittest


def _parse_platform_toolsets(cfg_text: str, platform: str) -> set[str]:
    section_match = re.search(r"^platform_toolsets:\s*\n((?:  .*\n|\n)+)", cfg_text, re.MULTILINE)
    if not section_match:
        return set()
    block = section_match.group(1)
    m = re.search(rf"^  {re.escape(platform)}:\s*\n((?:    -\s+\S+\n)+)", block, re.MULTILINE)
    if not m:
        return set()
    return {line.strip().lstrip("- ").strip() for line in m.group(1).strip().splitlines()}


def _parse_mnemosyne_tools(cfg_text: str) -> list[str]:
    m = re.search(r"^    tools:\s*\n((?:      -\s+\S+\n)+)", cfg_text, re.MULTILINE)
    if not m:
        return []
    return [line.strip().lstrip("- ").strip().strip('"') for line in m.group(1).strip().splitlines()]


class TestProfileRenderer(unittest.TestCase):
    def test_render_all_profiles_and_skill_symlinks(self):
        from render_profiles import load_roles_spec, provision_skill_symlinks, render_all_profiles

        vault_root = os.environ.get("VAULT", "/opt/atlas/vault" if os.path.exists("/opt/atlas/vault") else "/app/vault")
        roles_path = os.path.join(vault_root, "Forge/odin/config/hermes/profiles/roles.json")
        template_path = os.path.join(vault_root, "Forge/odin/config/hermes/config.yaml.template")
        spec = load_roles_spec(roles_path)

        # Verify main coordinator template has 0 MCP servers, lean toolsets, and zero orchestrator models
        self.assertEqual(spec["coordinator"]["model"], "claude-sonnet-5-5-high")
        with open(template_path, "r", encoding="utf-8") as f:
            main_cfg = f.read()
        self.assertIn("mcp_servers: {}", main_cfg)
        self.assertNotIn("gemini-pro-orchestrator", main_cfg)
        main_aux_block = main_cfg.split("# Fallback chain (main agent loop)", 1)[0].split("auxiliary:", 1)[1]
        for expected_aux_model in (
            "compress",
            "memory-extract",
            "app-synthesize",
            "claude-sonnet-5-5-medium",
            "gemini-4-hydrogen-medium",
        ):
            self.assertIn(f'model: "{expected_aux_model}"', main_aux_block)
        self.assertNotIn("gemini-4-argon-high", main_aux_block)
        self.assertIn('model: "gemini-4-neon-high"', main_cfg)
        main_fb = main_cfg.split("# Fallback chain (main agent loop)", 1)[1].split("# Delegation Configuration", 1)[0]
        self.assertEqual(re.findall(r'model:\s*"([^"]+)"', main_fb), ["gemini-4-hydrogen-high", "agent-loop"])
        deleg_block = main_cfg.split("delegation:", 1)[1].split("# Safety & Approvals", 1)[0]
        self.assertEqual(
            re.findall(r'model:\s*"([^"]+)"', deleg_block),
            ["gemini-4-neon-high", "claude-sonnet-5-5-high", "agent-loop"],
        )
        self.assertEqual(
            _parse_platform_toolsets(main_cfg, "discord"),
            {"delegation", "terminal", "file", "memory", "discord", "clarify", "cronjob", "skills"},
        )
        self.assertEqual(
            _parse_platform_toolsets(main_cfg, "cli"),
            {"delegation", "terminal", "file", "memory", "cronjob", "skills"},
        )
        expected_core_mnemosyne_tools = [
            "mnemosyne_recall",
            "mnemosyne_remember",
            "mnemosyne_get",
            "mnemosyne_update",
            "mnemosyne_forget",
            "mnemosyne_invalidate",
            "mnemosyne_validate",
            "mnemosyne_stats",
        ]
        self.assertEqual(_parse_mnemosyne_tools(main_cfg), expected_core_mnemosyne_tools)
        comp_block = main_aux_block.split("memory:", 1)[0]
        bg_block = main_aux_block.split("background_review:", 1)[1].split("approval:", 1)[0]
        self.assertIn("fallback_chain:", comp_block)
        self.assertIn("fallback_providers:", comp_block)
        self.assertNotIn("context_length: 200000", comp_block)
        self.assertIn("fallback_chain:", bg_block)
        self.assertIn("fallback_providers:", bg_block)

        # Verify all 50 canonical Hermes skills are mapped across Coordinator + 5 Specialists
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_src = os.path.join(tmpdir, "skills_src")
            profiles_dst = os.path.join(tmpdir, "profiles")
            agy_agents_dst = os.path.join(tmpdir, "agents")

            all_canonical_skills = set(spec["coordinator"]["skills"])
            for role_id, role_cfg in spec["roles"].items():
                for rel_skill in role_cfg.get("skills", []):
                    all_canonical_skills.add(rel_skill)
                    skill_dir = os.path.join(skills_src, rel_skill)
                    os.makedirs(skill_dir, exist_ok=True)
                    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as sf:
                        sf.write(f"---\nname: {os.path.basename(rel_skill)}\ndescription: test\n---\n")

            for rel_skill in spec["coordinator"]["skills"]:
                skill_dir = os.path.join(skills_src, rel_skill)
                os.makedirs(skill_dir, exist_ok=True)
                with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as sf:
                    sf.write(f"---\nname: {os.path.basename(rel_skill)}\ndescription: test\n---\n")

            self.assertEqual(len(all_canonical_skills), 50)

            # Verify fail-loud invariant on missing mandatory skill when skills_source_dir is mounted
            with self.assertRaises(FileNotFoundError):
                provision_skill_symlinks(
                    target_skills_dir=os.path.join(tmpdir, "fail_test"),
                    skills_source_dir=skills_src,
                    skill_rel_paths=["nonexistent/missing-skill"],
                )

            # Verify cross-environment CI runner mode (unmounted skills_source_dir with symlink_target_prefix)
            ci_links = provision_skill_symlinks(
                target_skills_dir=os.path.join(tmpdir, "ci_runner_test"),
                skills_source_dir=os.path.join(tmpdir, "unmounted_host_skills"),
                skill_rel_paths=["devops/odin-stack"],
                symlink_target_prefix="/opt/data/skills",
            )
            self.assertEqual(ci_links, ["devops/odin-stack"])

            # Simulate legacy top-level symlink for swarm-worker/skills to verify safe replacement
            legacy_worker_dir = os.path.join(profiles_dst, "swarm-worker")
            os.makedirs(legacy_worker_dir, exist_ok=True)
            os.symlink(skills_src, os.path.join(legacy_worker_dir, "skills"))

            rendered = render_all_profiles(
                template_path=template_path,
                roles_path=roles_path,
                output_profiles_dir=profiles_dst,
                skills_source_dir=skills_src,
                env_vars={
                    "AGENT_MODEL": "claude-sonnet-5-5-high",
                    "DOMAIN_NAME": "tienzo.net",
                    "KRATOS_MCP_TOKEN": "test-token",
                },
                antigravity_agents_dir=agy_agents_dst,
            )

            for specialist in ("huginn", "sindri", "forseti", "vor", "saga"):
                self.assertIn(specialist, rendered)
                prof_dir = os.path.join(profiles_dst, specialist)
                self.assertTrue(os.path.isfile(os.path.join(prof_dir, "config.yaml")))
                self.assertTrue(os.path.isfile(os.path.join(prof_dir, "SOUL.md")))

                # Verify Antigravity agent file materialized as real file with subagent frontmatter
                agy_md = os.path.join(agy_agents_dst, f"{specialist}.md")
                self.assertTrue(os.path.isfile(agy_md))
                self.assertFalse(os.path.islink(agy_md))
                with open(agy_md, "r", encoding="utf-8") as af:
                    agy_text = af.read()
                self.assertIn("subagent: true", agy_text)
                self.assertIn("mainAgent: false", agy_text)

                # Check directory mode is 0755
                mode = oct(os.stat(prof_dir).st_mode & 0o777)
                self.assertEqual(mode, "0o755")

                with open(os.path.join(prof_dir, "config.yaml"), "r", encoding="utf-8") as cf:
                    cfg_text = cf.read()

                if specialist in ("huginn", "sindri", "vor"):
                    self.assertIn("mcp_servers: {}", cfg_text)
                    self.assertNotIn("  playwright:", cfg_text)
                    self.assertNotIn("  kratos:", cfg_text)
                elif specialist == "forseti":
                    self.assertIn("  playwright:", cfg_text)
                    self.assertIn("    transport: sse", cfg_text)
                    self.assertNotIn("  kratos:", cfg_text)
                elif specialist == "saga":
                    self.assertIn("  kratos:", cfg_text)
                    self.assertIn("https://kratos.tienzo.net/mcp", cfg_text)
                    self.assertNotIn("  playwright:", cfg_text)

                self.assertNotIn("gemini-pro-orchestrator", cfg_text)
                aux_block = cfg_text.split("# Fallback chain (main agent loop)", 1)[0].split("auxiliary:", 1)[1]
                for expected_aux_model in (
                    "compress",
                    "memory-extract",
                    "app-synthesize",
                    "claude-sonnet-5-5-medium",
                    "gemini-4-hydrogen-medium",
                ):
                    self.assertIn(f'model: "{expected_aux_model}"', aux_block)
                self.assertNotIn("gemini-4-argon-high", aux_block)

                expected_models = {
                    "sindri": ("gemini-4-neon-high", ["claude-sonnet-5-5-high", "agent-loop"]),
                    "huginn": ("gemini-4-neon-high", ["claude-sonnet-5-5-high", "agent-loop"]),
                    "forseti": ("claude-sonnet-5-5-high", ["gemini-4-neon-high", "agent-loop"]),
                    "vor": ("claude-sonnet-5-5-high", ["gemini-4-hydrogen-high", "agent-loop"]),
                    "saga": ("claude-sonnet-5-5-high", ["gemini-4-hydrogen-high", "agent-loop"]),
                }
                primary_model, expected_fallbacks = expected_models[specialist]
                self.assertIn(f'default: "{primary_model}"', cfg_text)
                fb_block = cfg_text.split("# Fallback chain (main agent loop)", 1)[1].split("# Delegation Configuration", 1)[0]
                fb_models = re.findall(r'model:\s*"([^"]+)"', fb_block)
                self.assertEqual(fb_models, expected_fallbacks)
                if primary_model.startswith("gemini-"):
                    self.assertFalse(any(m.startswith("gemini-") for m in fb_models))
                # Ensure no redundant intra-pool Gemini -> Gemini consecutive fallback legs
                for left, right in zip(fb_models, fb_models[1:]):
                    self.assertFalse(left.startswith("gemini-") and right.startswith("gemini-"))

                # Verify SOUL.md contains pipeline handoff instruction
                with open(os.path.join(prof_dir, "SOUL.md"), "r", encoding="utf-8") as sf:
                    soul_text = sf.read()
                self.assertIn("kanban-gateway handoff", soul_text)

                # Verify symlinked skills count
                expected_skills = spec["roles"][specialist]["skills"]
                linked = []
                for root, dirs, _ in os.walk(os.path.join(prof_dir, "skills")):
                    for d in dirs:
                        p = os.path.join(root, d)
                        if os.path.islink(p):
                            linked.append(d)
                self.assertEqual(len(linked), len(expected_skills))

            # Verify non-destructive behavior on re-render:
            # 1. Runtime edits to SOUL.md survive re-render
            vor_soul = os.path.join(profiles_dst, "vor", "SOUL.md")
            with open(vor_soul, "a", encoding="utf-8") as sf:
                sf.write("\n# Runtime wrap-up improvement\n")

            # 2. Newly added valid skill symlink survives re-render, while broken symlink is pruned
            extra_skill_src = os.path.join(skills_src, "custom", "learned-skill")
            os.makedirs(extra_skill_src, exist_ok=True)
            vor_custom_dir = os.path.join(profiles_dst, "vor", "skills", "custom")
            os.makedirs(vor_custom_dir, exist_ok=True)
            os.symlink(extra_skill_src, os.path.join(vor_custom_dir, "learned-skill"))
            os.symlink(os.path.join(skills_src, "custom", "nonexistent-skill"), os.path.join(vor_custom_dir, "broken-skill"))

            re_rendered = render_all_profiles(
                template_path=template_path,
                roles_path=roles_path,
                output_profiles_dir=profiles_dst,
                skills_source_dir=skills_src,
                env_vars={"AGENT_MODEL": "claude-sonnet-5-5-high", "DOMAIN_NAME": "tienzo.net"},
            )
            with open(os.path.join(profiles_dst, "swarm-worker", "config.yaml"), "r", encoding="utf-8") as swf:
                sw_cfg = swf.read()
                self.assertIn('default: "gemini-4-hydrogen-high"', sw_cfg)
                self.assertNotIn("gemini-pro-orchestrator", sw_cfg)
                sw_fb_block = sw_cfg.split("# Fallback chain (main agent loop)", 1)[1].split("# Delegation Configuration", 1)[0]
                self.assertEqual(re.findall(r'model:\s*"([^"]+)"', sw_fb_block), ["claude-sonnet-5-5-high", "agent-loop"])

            with open(vor_soul, "r", encoding="utf-8") as sf:
                self.assertIn("# Runtime wrap-up improvement", sf.read())
            self.assertIn("custom/learned-skill", re_rendered["vor"]["linked_skills"])
            self.assertFalse(os.path.lexists(os.path.join(vor_custom_dir, "broken-skill")))

    def test_cli_toolsets_mnemosyne_slimming_and_context_ceiling(self):
        from render_profiles import load_roles_spec, render_profile_config

        vault_root = os.environ.get("VAULT", "/opt/atlas/vault" if os.path.exists("/opt/atlas/vault") else "/app/vault")
        odin_dir = os.path.join(vault_root, "Forge/odin")
        roles_path = os.path.join(odin_dir, "config/hermes/profiles/roles.json")
        template_path = os.path.join(odin_dir, "config/hermes/config.yaml.template")
        compose_path = os.path.join(odin_dir, "docker-compose.yml")
        deploy_path = os.path.join(odin_dir, ".github/workflows/deploy.yml")

        spec = load_roles_spec(roles_path)
        with open(template_path, "r", encoding="utf-8") as tf:
            template_text = tf.read()
        env_vars = {"AGENT_MODEL": "claude-sonnet-5-5-high", "DOMAIN_NAME": "example.net", "KRATOS_MCP_TOKEN": "tok"}

        core_tools = [
            "mnemosyne_recall", "mnemosyne_remember", "mnemosyne_get", "mnemosyne_update",
            "mnemosyne_forget", "mnemosyne_invalidate", "mnemosyne_validate", "mnemosyne_stats",
        ]
        vor_tools = core_tools + [
            "mnemosyne_sleep", "mnemosyne_diagnose", "mnemosyne_resolve_conflicts", "mnemosyne_persona_promote",
        ]

        for role_id, role_cfg in spec["roles"].items():
            rendered = render_profile_config(template_text, role_cfg, env_vars)
            expected_toolsets = set(role_cfg["toolsets"])
            discord_ts = _parse_platform_toolsets(rendered, "discord")
            cli_ts = _parse_platform_toolsets(rendered, "cli")
            self.assertEqual(discord_ts, expected_toolsets, role_id)
            self.assertEqual(cli_ts, expected_toolsets, role_id)
            self.assertNotIn("execute_code", cli_ts)
            if role_id in ("sindri", "forseti"):
                self.assertNotIn("memory", discord_ts)
                self.assertNotIn("memory", cli_ts)
            if role_id == "vor":
                self.assertIn("session_search", cli_ts)
                self.assertEqual(_parse_mnemosyne_tools(rendered), vor_tools)
            else:
                self.assertEqual(_parse_mnemosyne_tools(rendered), core_tools)
            comp_aux = rendered.split("compression:", 1)[1].split("memory:", 1)[0]
            bg_aux = rendered.split("background_review:", 1)[1].split("approval:", 1)[0]
            self.assertIn("fallback_chain:", comp_aux)
            self.assertIn("fallback_chain:", bg_aux)

        with open(deploy_path, "r", encoding="utf-8") as df:
            self.assertIn("HERMES_ANTIGRAVITY_CONTEXT_LENGTH: 200000", df.read())
        with open(compose_path, "r", encoding="utf-8") as cf:
            compose_text = cf.read()
        self.assertEqual(
            compose_text.count("HERMES_ANTIGRAVITY_CONTEXT_LENGTH=${HERMES_ANTIGRAVITY_CONTEXT_LENGTH}"),
            2,
        )
        self.assertNotRegex(compose_text, r"\$\{[A-Za-z0-9_]+:-")

    def test_entrypoint_nonroot_agy_persistence_hygiene(self):
        vault_root = os.environ.get("VAULT", "/opt/atlas/vault" if os.path.exists("/opt/atlas/vault") else "/app/vault")
        entrypoint_path = os.path.join(vault_root, "Forge/odin/hermes-agent/entrypoint.sh")
        with open(entrypoint_path, "r", encoding="utf-8") as f:
            entrypoint_text = f.read()
        self.assertIn('export PATH="$HOME/.local/bin:$PATH"', entrypoint_text)
        self.assertNotIn("ln -sf \"$HOME/.local/bin/agy\" /usr/local/bin/agy", entrypoint_text)

    def test_entrypoint_render_profiles_fail_loud(self):
        vault_root = os.environ.get("VAULT", "/opt/atlas/vault" if os.path.exists("/opt/atlas/vault") else "/app/vault")
        entrypoint_path = os.path.join(vault_root, "Forge/odin/hermes-agent/entrypoint.sh")
        with open(entrypoint_path, "r", encoding="utf-8") as f:
            entrypoint_text = f.read()
        self.assertIn("set -e", entrypoint_text)
        match = re.search(r"render_profiles\.py.*?\n\s*fi\b", entrypoint_text, re.DOTALL)
        self.assertIsNotNone(match, "render_profiles.py invocation block not found in entrypoint.sh")
        render_block = match.group(0)
        self.assertIn("--skills-source /opt/data/skills", render_block)
        self.assertNotIn("|| true", render_block)


if __name__ == "__main__":
    unittest.main()
