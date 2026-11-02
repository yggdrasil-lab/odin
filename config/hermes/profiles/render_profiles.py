#!/usr/bin/env python3
"""render_profiles.py - Renders Court of Odin specialist profiles, SOUL.md, and symlinked skill trees.

Uses pure Python standard library so it runs on both the Gaia host (deploy.yml)
and inside the odin_huginn-gateway container (entrypoint.sh).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
from typing import Any, Dict, Iterable, List, Optional


def load_roles_spec(roles_path: str) -> Dict[str, Any]:
    with open(roles_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _expand_env(text: str, env_vars: Dict[str, str]) -> str:
    def repl(match: re.Match[str]) -> str:
        key = match.group(1)
        return env_vars.get(key, os.environ.get(key, match.group(0)))

    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", repl, text)


def _build_mcp_block(mcps: List[str], env_vars: Dict[str, str]) -> str:
    if not mcps:
        return "mcp_servers: {}\n"

    lines = ["mcp_servers:"]
    for mcp in mcps:
        if mcp == "kratos":
            domain = env_vars.get("DOMAIN_NAME", os.environ.get("DOMAIN_NAME", ""))
            token = env_vars.get("KRATOS_MCP_TOKEN", os.environ.get("KRATOS_MCP_TOKEN", ""))
            lines.extend(
                [
                    "  kratos:",
                    "    enabled: true",
                    f"    url: https://kratos.{domain}/mcp",
                    "    headers:",
                    f"      Authorization: Bearer {token}",
                ]
            )
        elif mcp == "playwright":
            lines.extend(
                [
                    "  playwright:",
                    "    enabled: true",
                    "    url: http://poseidon_playwright:8931/sse",
                ]
            )
    return "\n".join(lines) + "\n"


def _build_toolsets_block(toolsets: List[str]) -> str:
    lines = ["platform_toolsets:", "  discord:"]
    for t in sorted(toolsets):
        lines.append(f"    - {t}")
    return "\n".join(lines) + "\n"


def _build_fallback_block(fallback_models: List[str]) -> str:
    lines = ["fallback_providers:"]
    for m in fallback_models:
        lines.append('  - provider: "antigravity-subscription-directsdk"')
        lines.append(f'    model: "{m}"')
    lines.extend(
        [
            '  - provider: "custom"',
            '    model: "agent-loop"',
            '    base_url: "http://muninn:4000/v1"',
            '    key_env: "MUNINN_MASTER_KEY"',
        ]
    )
    return "\n".join(lines)


def render_profile_config(
    template_text: str,
    role_cfg: Dict[str, Any],
    env_vars: Dict[str, str],
) -> str:
    rendered = template_text

    # 1. Pin specialist default model
    model_name = role_cfg.get("model", "gemini-3.8-flash-high")
    rendered = re.sub(
        r'default:\s*"\$\{AGENT_MODEL\}"',
        f'default: "{model_name}"',
        rendered,
        count=1,
    )

    # 2. Update main fallback_providers chain for this specialist
    fallback_models = role_cfg.get("fallback_models", ["claude-sonnet-5-5-high", "gemini-4-argon-high"])
    fallback_pattern = re.compile(
        r"^fallback_providers:\n(?:  - .*\n|    .*\n)+",
        re.MULTILINE,
    )
    rendered = fallback_pattern.sub(_build_fallback_block(fallback_models) + "\n", rendered, count=1)

    # 3. Replace mcp_servers section
    mcp_block = _build_mcp_block(role_cfg.get("mcps", []), env_vars)
    rendered = re.sub(
        r"^mcp_servers:.*?(?=^# ===|\Z)",
        mcp_block + "\n",
        rendered,
        flags=re.MULTILINE | re.DOTALL,
    )

    # 4. Replace platform_toolsets section
    toolsets_block = _build_toolsets_block(role_cfg.get("toolsets", ["file", "skills", "terminal"]))
    rendered = re.sub(
        r"^platform_toolsets:.*\Z",
        toolsets_block,
        rendered,
        flags=re.MULTILINE | re.DOTALL,
    )

    return _expand_env(rendered, env_vars)


def provision_skill_symlinks(
    target_skills_dir: str,
    skills_source_dir: str,
    skill_rel_paths: Iterable[str],
    optional_rel_paths: Iterable[str] = (),
) -> List[str]:
    """Creates a clean symlinked skill tree under target_skills_dir."""
    if os.path.exists(target_skills_dir):
        shutil.rmtree(target_skills_dir)
    os.makedirs(target_skills_dir, mode=0o755, exist_ok=True)
    os.chmod(target_skills_dir, 0o755)

    linked: List[str] = []
    for rel_path in list(skill_rel_paths) + list(optional_rel_paths):
        src = os.path.join(skills_source_dir, rel_path)
        is_optional = rel_path in optional_rel_paths
        if not os.path.exists(src):
            if is_optional:
                continue
            continue
        dst = os.path.join(target_skills_dir, rel_path)
        parent = os.path.dirname(dst)
        os.makedirs(parent, mode=0o755, exist_ok=True)
        os.chmod(parent, 0o755)
        if os.path.lexists(dst):
            os.unlink(dst)
        os.symlink(src, dst)
        linked.append(rel_path)
    return linked


def render_all_profiles(
    template_path: str,
    roles_path: str,
    output_profiles_dir: str,
    skills_source_dir: str,
    env_vars: Optional[Dict[str, str]] = None,
) -> Dict[str, Dict[str, Any]]:
    env = dict(os.environ)
    if env_vars:
        env.update(env_vars)

    with open(template_path, "r", encoding="utf-8") as tf:
        template_text = tf.read()

    spec = load_roles_spec(roles_path)
    profiles_src_dir = os.path.dirname(os.path.abspath(roles_path))

    os.makedirs(output_profiles_dir, mode=0o755, exist_ok=True)
    os.chmod(output_profiles_dir, 0o755)

    results: Dict[str, Dict[str, Any]] = {}
    for role_id, role_cfg in spec.get("roles", {}).items():
        prof_dir = os.path.join(output_profiles_dir, role_id)
        os.makedirs(prof_dir, mode=0o755, exist_ok=True)
        os.chmod(prof_dir, 0o755)

        # 1. Write config.yaml
        cfg_text = render_profile_config(template_text, role_cfg, env)
        cfg_path = os.path.join(prof_dir, "config.yaml")
        with open(cfg_path, "w", encoding="utf-8") as cf:
            cf.write(cfg_text)

        # 2. Copy SOUL.md
        soul_src = os.path.join(profiles_src_dir, role_id, "SOUL.md")
        soul_dst = os.path.join(prof_dir, "SOUL.md")
        if os.path.isfile(soul_src):
            shutil.copy2(soul_src, soul_dst)

        # 3. Provision symlinked skills/
        skills_dst = os.path.join(prof_dir, "skills")
        linked = provision_skill_symlinks(
            target_skills_dir=skills_dst,
            skills_source_dir=skills_source_dir,
            skill_rel_paths=role_cfg.get("skills", []),
            optional_rel_paths=role_cfg.get("optional_skills", []),
        )

        results[role_id] = {
            "profile_dir": prof_dir,
            "model": role_cfg.get("model"),
            "mcps": role_cfg.get("mcps", []),
            "linked_skills": linked,
        }

    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Court of Odin specialist profiles and skill symlinks.")
    parser.add_argument(
        "--template",
        default="/app/vault/Forge/odin/config/hermes/config.yaml.template",
        help="Path to base config.yaml.template",
    )
    parser.add_argument(
        "--roles",
        default="/app/vault/Forge/odin/config/hermes/profiles/roles.json",
        help="Path to roles.json specification",
    )
    parser.add_argument(
        "--output-dir",
        default="/opt/data/profiles",
        help="Target profiles directory (default: /opt/data/profiles)",
    )
    parser.add_argument(
        "--skills-source",
        default="/opt/data/skills",
        help="Canonical skills directory to symlink from (default: /opt/data/skills)",
    )
    args = parser.parse_args()
    res = render_all_profiles(
        template_path=args.template,
        roles_path=args.roles,
        output_profiles_dir=args.output_dir,
        skills_source_dir=args.skills_source,
    )
    for role_id, info in res.items():
        print(
            f"[render_profiles] {role_id}: model={info['model']} mcps={info['mcps']} skills={len(info['linked_skills'])}"
        )


if __name__ == "__main__":
    main()
