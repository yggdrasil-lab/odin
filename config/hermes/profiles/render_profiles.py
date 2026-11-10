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
                    "    transport: sse",
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


def _prune_broken_symlinks(target_skills_dir: str, skills_source_dir: str, target_base: str) -> None:
    """Removes only broken skill symlinks so user/agent-added symlinks survive restarts."""
    if not os.path.isdir(target_skills_dir) or not os.path.isdir(skills_source_dir):
        return
    for root, dirs, files in os.walk(target_skills_dir, topdown=False):
        for entry in list(dirs) + list(files):
            full_path = os.path.join(root, entry)
            if os.path.islink(full_path):
                raw_target = os.readlink(full_path)
                # Translate container target_base (e.g. /opt/data/skills) back to skills_source_dir when running on host
                if target_base and raw_target.startswith(target_base + "/"):
                    rel = raw_target[len(target_base) + 1 :]
                    resolved = os.path.join(skills_source_dir, rel)
                else:
                    resolved = os.path.realpath(full_path)
                if not os.path.exists(resolved):
                    os.unlink(full_path)
        if root != target_skills_dir and not os.listdir(root):
            try:
                os.rmdir(root)
            except OSError:
                pass


def provision_skill_symlinks(
    target_skills_dir: str,
    skills_source_dir: str,
    skill_rel_paths: Iterable[str],
    optional_rel_paths: Iterable[str] = (),
    symlink_target_prefix: Optional[str] = None,
) -> List[str]:
    """Non-destructively ensures baseline skill symlinks exist under target_skills_dir.

    Fails loudly if any mandatory skill in skill_rel_paths does not exist in skills_source_dir.
    Preserves any additional skill symlinks added at runtime during session-wrapup
    or continuous improvement, and only prunes broken/dangling symlinks.
    """
    if os.path.islink(target_skills_dir):
        os.unlink(target_skills_dir)
    os.makedirs(target_skills_dir, mode=0o755, exist_ok=True)
    os.chmod(target_skills_dir, 0o755)

    target_base = symlink_target_prefix or skills_source_dir
    _prune_broken_symlinks(target_skills_dir, skills_source_dir, target_base)

    mandatory_list = list(skill_rel_paths)
    optional_set = set(optional_rel_paths)
    source_mounted = os.path.isdir(skills_source_dir)

    if not source_mounted and not symlink_target_prefix:
        raise FileNotFoundError(
            f"Skills source directory '{skills_source_dir}' does not exist (fail-loud invariant)"
        )

    for rel_path in mandatory_list + [p for p in optional_rel_paths if p not in mandatory_list]:
        src = os.path.join(skills_source_dir, rel_path)
        if source_mounted and not os.path.exists(src):
            if rel_path in optional_set and rel_path not in mandatory_list:
                continue
            raise FileNotFoundError(
                f"Required skill '{rel_path}' not found in '{skills_source_dir}' (fail-loud invariant)"
            )
        if not source_mounted and rel_path in optional_set and rel_path not in mandatory_list:
            continue
        link_target = os.path.join(target_base, rel_path)
        dst = os.path.join(target_skills_dir, rel_path)
        parent = os.path.dirname(dst)
        os.makedirs(parent, mode=0o755, exist_ok=True)
        os.chmod(parent, 0o755)
        if not os.path.lexists(dst):
            os.symlink(link_target, dst)

    linked: List[str] = []
    for root, dirs, files in os.walk(target_skills_dir):
        for entry in list(dirs) + list(files):
            full_path = os.path.join(root, entry)
            if os.path.islink(full_path):
                linked.append(os.path.relpath(full_path, target_skills_dir))
    return sorted(linked)


def render_all_profiles(
    template_path: str,
    roles_path: str,
    output_profiles_dir: str,
    skills_source_dir: str,
    env_vars: Optional[Dict[str, str]] = None,
    symlink_target_prefix: Optional[str] = None,
    overwrite_soul: bool = False,
    antigravity_agents_dir: Optional[str] = None,
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

    if antigravity_agents_dir is None and output_profiles_dir in ("/opt/odin/hermes/profiles", "/opt/data/profiles"):
        for candidate in (
            "/opt/atlas/vault/.agents/plugins/court-of-odin/agents",
            "/app/vault/.agents/plugins/court-of-odin/agents",
        ):
            if os.path.isdir(os.path.dirname(candidate)):
                antigravity_agents_dir = candidate
                break

    if antigravity_agents_dir:
        os.makedirs(antigravity_agents_dir, mode=0o755, exist_ok=True)

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

        # 2. Seed SOUL.md (or overwrite when --overwrite-soul is passed)
        soul_src = os.path.join(profiles_src_dir, role_id, "SOUL.md")
        soul_dst = os.path.join(prof_dir, "SOUL.md")
        if os.path.isfile(soul_src):
            if overwrite_soul or not os.path.exists(soul_dst):
                shutil.copy2(soul_src, soul_dst)
            if antigravity_agents_dir and role_id != "swarm-worker":
                agent_md = os.path.join(antigravity_agents_dir, f"{role_id}.md")
                if os.path.islink(agent_md) or os.path.exists(agent_md):
                    os.unlink(agent_md)
                shutil.copy2(soul_src, agent_md)

        # 3. Provision symlinked skills/
        skills_dst = os.path.join(prof_dir, "skills")
        linked = provision_skill_symlinks(
            target_skills_dir=skills_dst,
            skills_source_dir=skills_source_dir,
            skill_rel_paths=role_cfg.get("skills", []),
            optional_rel_paths=role_cfg.get("optional_skills", []),
            symlink_target_prefix=symlink_target_prefix,
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
        help="Canonical skills directory to verify from (default: /opt/data/skills)",
    )
    parser.add_argument(
        "--symlink-target-prefix",
        default=None,
        help="Optional target prefix for generated symlinks inside container (e.g. /opt/data/skills)",
    )
    parser.add_argument(
        "--overwrite-soul",
        action="store_true",
        help="Overwrite existing SOUL.md files in output profiles directory",
    )
    parser.add_argument(
        "--antigravity-agents-dir",
        default=None,
        help="Optional directory to materialize Antigravity plugin agent .md files",
    )
    args = parser.parse_args()
    res = render_all_profiles(
        template_path=args.template,
        roles_path=args.roles,
        output_profiles_dir=args.output_dir,
        skills_source_dir=args.skills_source,
        symlink_target_prefix=args.symlink_target_prefix,
        overwrite_soul=args.overwrite_soul,
        antigravity_agents_dir=args.antigravity_agents_dir,
    )
    for role_id, info in res.items():
        print(
            f"[render_profiles] {role_id}: model={info['model']} mcps={info['mcps']} skills={len(info['linked_skills'])}"
        )


if __name__ == "__main__":
    main()
