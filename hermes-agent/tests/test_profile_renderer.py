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
        from render_profiles import load_roles_spec, render_all_profiles

        roles_path = "/opt/atlas/vault/Forge/odin/config/hermes/profiles/roles.json"
        template_path = "/opt/atlas/vault/Forge/odin/config/hermes/config.yaml.template"
        spec = load_roles_spec(roles_path)

        # Verify main coordinator template has 0 MCP servers and lean toolsets
        with open(template_path, "r", encoding="utf-8") as f:
            main_cfg = f.read()
        self.assertIn("mcp_servers: {}", main_cfg)
        self.assertEqual(
            _parse_toolsets(main_cfg),
            {"delegation", "terminal", "file", "memory", "discord", "clarify", "cronjob", "skills"},
        )

        # Verify all 49 canonical Hermes skills are mapped across Coordinator + 5 Specialists
        with tempfile.TemporaryDirectory() as tmpdir:
            skills_src = os.path.join(tmpdir, "skills_src")
            profiles_dst = os.path.join(tmpdir, "profiles")

            all_49_skills = set(spec["coordinator"]["skills"])
            for role_id, role_cfg in spec["roles"].items():
                for rel_skill in role_cfg.get("skills", []):
                    all_49_skills.add(rel_skill)
                    skill_dir = os.path.join(skills_src, rel_skill)
                    os.makedirs(skill_dir, exist_ok=True)
                    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as sf:
                        sf.write(f"---\nname: {os.path.basename(rel_skill)}\ndescription: test\n---\n")

            for rel_skill in spec["coordinator"]["skills"]:
                skill_dir = os.path.join(skills_src, rel_skill)
                os.makedirs(skill_dir, exist_ok=True)
                with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as sf:
                    sf.write(f"---\nname: {os.path.basename(rel_skill)}\ndescription: test\n---\n")

            self.assertEqual(len(all_49_skills), 49)

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
            )

            for specialist in ("huginn", "sindri", "forseti", "vor", "saga"):
                self.assertIn(specialist, rendered)
                prof_dir = os.path.join(profiles_dst, specialist)
                self.assertTrue(os.path.isfile(os.path.join(prof_dir, "config.yaml")))
                self.assertTrue(os.path.isfile(os.path.join(prof_dir, "SOUL.md")))

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


if __name__ == "__main__":
    unittest.main()
