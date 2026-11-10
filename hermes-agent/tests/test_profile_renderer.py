import os
import re
import tempfile
import unittest


def _parse_toolsets(cfg_text: str) -> set[str]:
    m = re.search(r"^platform_toolsets:\s*\n\s+discord:\s*\n((?:\s+-\s+\S+\n)+)", cfg_text, re.MULTILINE)
    if not m:
        return set()
    return {line.strip().lstrip("- ").strip() for line in m.group(1).strip().splitlines()}


class TestProfileRenderer(unittest.TestCase):
    def test_render_all_profiles_and_skill_symlinks(self):
        from render_profiles import load_roles_spec, provision_skill_symlinks, render_all_profiles

        vault_root = os.environ.get("VAULT", "/opt/atlas/vault" if os.path.exists("/opt/atlas/vault") else "/app/vault")
        roles_path = os.path.join(vault_root, "Forge/odin/config/hermes/profiles/roles.json")
        template_path = os.path.join(vault_root, "Forge/odin/config/hermes/config.yaml.template")
        spec = load_roles_spec(roles_path)

        # Verify main coordinator template has 0 MCP servers and lean toolsets
        with open(template_path, "r", encoding="utf-8") as f:
            main_cfg = f.read()
        self.assertIn("mcp_servers: {}", main_cfg)
        self.assertEqual(
            _parse_toolsets(main_cfg),
            {"delegation", "terminal", "file", "memory", "discord", "clarify", "cronjob", "skills"},
        )

        # Verify all 53 canonical Hermes skills are mapped across Coordinator + 5 Specialists
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

            self.assertEqual(len(all_canonical_skills), 53)

            # Verify fail-loud invariant on missing mandatory skill
            with self.assertRaises(FileNotFoundError):
                provision_skill_symlinks(
                    target_skills_dir=os.path.join(tmpdir, "fail_test"),
                    skills_source_dir=skills_src,
                    skill_rel_paths=["nonexistent/missing-skill"],
                )

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
                    "AGENT_MODEL": "gemini-3.8-flash-high",
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

                if specialist == "sindri":
                    self.assertIn('default: "gemini-4-argon-high"', cfg_text)
                else:
                    self.assertIn('default: "gemini-3.8-flash-high"', cfg_text)

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
                env_vars={"AGENT_MODEL": "gemini-3.8-flash-high", "DOMAIN_NAME": "tienzo.net"},
            )
            with open(vor_soul, "r", encoding="utf-8") as sf:
                self.assertIn("# Runtime wrap-up improvement", sf.read())
            self.assertIn("custom/learned-skill", re_rendered["vor"]["linked_skills"])
            self.assertFalse(os.path.lexists(os.path.join(vor_custom_dir, "broken-skill")))


if __name__ == "__main__":
    unittest.main()
