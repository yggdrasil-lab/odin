#!/usr/bin/env python3
"""Build-time patch: route Mnemosyne host LLM operations to auxiliary.memory task.

Problem
-------
By default, Mnemosyne memory consolidation (summarize_memories) and structured fact
extraction (extract_facts) run against an OpenAI-compatible HTTP API configured via
MNEMOSYNE_LLM_BASE_URL. In Odin, this pointed to Muninn (http://muninn:4000/v1) with
MNEMOSYNE_LLM_MODEL=memory-extract, directly consuming DeepSeek V4-Flash tokens.

Furthermore, HermesAuxLLMBackend in hermes_memory_provider was wired to task="compression",
which uses auxiliary.compression and falls back to Muninn compress (Gemini 2.5 Flash on Google API),
skipping the user's Antigravity (agy) subscription quotas (Claude Sonnet 5.5 and Gemini 4 Argon)
and the main loop fallback chain. Additionally, standalone / MCP invocations of Mnemosyne
never registered the host LLM backend lazily.

Fix
---
1. Patch hermes_memory_provider.hermes_llm_adapter so HermesAuxLLMBackend uses task="memory".
   This allows config.yaml to define auxiliary.memory on the Antigravity subscription
   (antigravity-subscription-directsdk: gemini-3.8-flash-high) with the exact same
   fallback chain as the main agent loop (claude-sonnet-5-5-high -> gemini-4-argon-high -> agent-loop).
2. Patch mnemosyne.core.local_llm._try_host_llm to lazily call register_hermes_host_llm()
   if get_host_llm_backend() is None, ensuring standalone CLI and MCP tool executions
   automatically connect to the Hermes host LLM when MNEMOSYNE_HOST_LLM_ENABLED=true.
3. Patch mnemosyne.install._configure_hermes to recognize quoted provider settings
   and anchor the memory regex to ^memory: so nested auxiliary.memory is never corrupted.
"""
from __future__ import annotations

import sys
from pathlib import Path

MARKER_ADAPTER = "# odin-patch: route host LLM to auxiliary.memory task"
MARKER_LOCAL_LLM = "# odin-patch: lazy register hermes host LLM backend"
MARKER_INSTALL = "# odin-patch: harden mnemosyne install config detection and anchor regex"
MARKER_PM_ENV = "# odin-patch: preserve base_venv site-packages across PM generations"
MARKER_PM_PKG = "# odin-patch: stamp and link base_venv site-packages in PM generations"


def patch_adapter() -> bool:
    targets: list[Path] = []
    try:
        import mnemosyne_hermes.hermes_llm_adapter as modern_adapter
        targets.append(Path(modern_adapter.__file__))
    except Exception:
        candidates = list(Path("/opt/hermes/.venv/lib").glob("python*/site-packages/mnemosyne_hermes/hermes_llm_adapter.py"))
        if candidates:
            targets.extend(candidates)

    try:
        import hermes_memory_provider.hermes_llm_adapter as legacy_adapter
        targets.append(Path(legacy_adapter.__file__))
    except Exception:
        candidates = list(Path("/opt/hermes/.venv/lib").glob("python*/site-packages/hermes_memory_provider/hermes_llm_adapter.py"))
        if candidates:
            targets.extend(candidates)

    if not targets:
        print("[odin-patch] ERROR: hermes_llm_adapter.py not found in modern or legacy path", file=sys.stderr)
        return False

    old_task = '    task = "compression"\n'
    new_task = f'    {MARKER_ADAPTER}\n    task = "memory"\n'

    patched_any = False
    for target in set(targets):
        src = target.read_text(encoding="utf-8")
        if MARKER_ADAPTER in src:
            print(f"[odin-patch] {target} already patched; skipping")
            patched_any = True
            continue
        if old_task in src:
            target.write_text(src.replace(old_task, new_task, 1), encoding="utf-8")
            print(f"[odin-patch] patched {target} (routed task to memory)")
            patched_any = True
        else:
            print(f"[odin-patch] WARNING: task = \"compression\" not found in {target}", file=sys.stderr)

    return patched_any


def patch_local_llm() -> bool:
    try:
        import mnemosyne.core.local_llm as local_llm_mod
        target = Path(local_llm_mod.__file__)
    except Exception:
        candidate = Path("/opt/hermes/.venv/lib/python3.14/site-packages/mnemosyne/core/local_llm.py")
        if candidate.is_file():
            target = candidate
        else:
            print("[odin-patch] ERROR: local_llm.py not found", file=sys.stderr)
            return False

    src = target.read_text(encoding="utf-8")
    if MARKER_LOCAL_LLM in src:
        print("[odin-patch] local_llm already patched; skipping")
        return True

    old_check = (
        "    if get_host_llm_backend() is None:\n"
        "        return (False, None)\n"
    )
    new_check = (
        f"    {MARKER_LOCAL_LLM}\n"
        "    if get_host_llm_backend() is None:\n"
        "        try:\n"
        "            try:\n"
        "                from mnemosyne_hermes.hermes_llm_adapter import register_hermes_host_llm\n"
        "            except ImportError:\n"
        "                from hermes_memory_provider.hermes_llm_adapter import register_hermes_host_llm\n"
        "            register_hermes_host_llm()\n"
        "        except Exception:\n"
        "            pass\n"
        "    if get_host_llm_backend() is None:\n"
        "        return (False, None)\n"
    )

    if old_check not in src:
        print("[odin-patch] ERROR: get_host_llm_backend() check not found in local_llm.py", file=sys.stderr)
        return False

    target.write_text(src.replace(old_check, new_check, 1), encoding="utf-8")
    print(f"[odin-patch] patched {target} (lazy registration of host LLM)")
    return True


def patch_mnemosyne_install() -> bool:
    try:
        import mnemosyne.install as install_mod
        target = Path(install_mod.__file__)
    except Exception:
        candidate = Path("/opt/hermes/.venv/lib/python3.14/site-packages/mnemosyne/install.py")
        if candidate.is_file():
            target = candidate
        else:
            print("[odin-patch] ERROR: mnemosyne/install.py not found", file=sys.stderr)
            return False

    src = target.read_text(encoding="utf-8")
    if MARKER_INSTALL in src:
        print("[odin-patch] mnemosyne.install already patched; skipping")
        return True

    legacy_check = '    if "provider: mnemosyne" in config_text:\n'
    modern_check = '    if _config_selects_mnemosyne(config_text):\n'
    if legacy_check in src:
        new_check = (
            f"    {MARKER_INSTALL}\n"
            '    if "provider: mnemosyne" in config_text or \'provider: "mnemosyne"\' in config_text or "provider: \'mnemosyne\'" in config_text:\n'
        )
        src = src.replace(legacy_check, new_check, 1)
    elif modern_check in src:
        new_check = (
            f"    {MARKER_INSTALL}\n"
            f"{modern_check}"
        )
        src = src.replace(modern_check, new_check, 1)
    else:
        print("[odin-patch] ERROR: neither legacy nor modern mnemosyne config check found in mnemosyne/install.py", file=sys.stderr)
        return False

    old_sub = (
        "        new_config = re.sub(\n"
        "            r'(memory:\\s*)\\n(\\s*provider:\\s*\\S+)?',\n"
        "            r'\\1\\n  provider: mnemosyne\\n',\n"
        "            config_text,\n"
        "            count=1,\n"
        "        )\n"
    )
    new_sub = (
        "        new_config = re.sub(\n"
        "            r'(^memory:\\s*)\\n(\\s*provider:\\s*\\S+)?',\n"
        "            r'\\1\\n  provider: mnemosyne\\n',\n"
        "            config_text,\n"
        "            count=1,\n"
        "            flags=re.MULTILINE,\n"
        "        )\n"
    )
    if old_sub not in src:
        print("[odin-patch] ERROR: re.sub pattern not found in mnemosyne/install.py", file=sys.stderr)
        return False

    src = src.replace(old_sub, new_sub, 1)
    target.write_text(src, encoding="utf-8")
    print(f"[odin-patch] patched {target} (hardened mnemosyne install config detection)")
    return True


def patch_pm_base_venv_bridge() -> bool:
    """Ensure PM dependency generations retain access to image-baked /opt/hermes/.venv packages.

    Upstream Hermes s6 stage2 rebuilds a managed uv virtualenv under /opt/data/installs/*/environments/*/venv
    and strips /opt/hermes/.venv/lib/python3.*/site-packages from sys.path in activate_dependencies().
    Because mnemosyne-memory, mnemosyne-hermes, sqlite-vec, and playwright are pre-installed in
    /opt/hermes/.venv at Docker build time, stripping base_venv site-packages causes
    ModuleNotFoundError: No module named 'mnemosyne' at runtime.
    """
    env_target = Path("/opt/hermes/pm/environments.py")
    pkg_target = Path("/opt/hermes/pm/packages.py")
    if not env_target.is_file() or not pkg_target.is_file():
        print("[odin-patch] PM modules not present; skipping PM base_venv bridge")
        return True

    env_src = env_target.read_text(encoding="utf-8")
    if MARKER_PM_ENV not in env_src:
        old_activate = (
            "    # uv editable members are activated by .pth files, not by sys.path alone.\n"
            "    site.addsitedir(str(selected))\n"
            "    sys.path[:] = [str(project_root.resolve()), str(selected),\n"
            "                   *[entry for entry in sys.path if Path(entry).resolve() != selected.resolve()]]\n"
            "    os.environ[\"PYTHONPATH\"] = os.pathsep.join([str(project_root.resolve()), str(selected)])\n"
        )
        new_activate = (
            f"    {MARKER_PM_ENV}\n"
            "    # uv editable members are activated by .pth files, not by sys.path alone.\n"
            "    site.addsitedir(str(selected))\n"
            "    base_site = site_packages(base_venv(project_root))\n"
            "    extra_sites = [str(base_site)] if base_site.is_dir() and base_site.resolve() != selected.resolve() else []\n"
            "    for extra_site in extra_sites:\n"
            "        try:\n"
            "            pth_file = selected / \"odin-base-site.pth\"\n"
            "            if not pth_file.is_file():\n"
            "                pth_file.write_text(f\"{extra_site}\\n\", encoding=\"utf-8\")\n"
            "        except OSError:\n"
            "            pass\n"
            "        site.addsitedir(extra_site)\n"
            "    exclude_resolved = {selected.resolve(), *(Path(s).resolve() for s in extra_sites)}\n"
            "    sys.path[:] = [str(project_root.resolve()), str(selected), *extra_sites,\n"
            "                   *[entry for entry in sys.path if Path(entry).resolve() not in exclude_resolved]]\n"
            "    os.environ[\"PYTHONPATH\"] = os.pathsep.join([str(project_root.resolve()), str(selected), *extra_sites])\n"
        )
        if old_activate not in env_src:
            print("[odin-patch] ERROR: activate_dependencies anchor not found in pm/environments.py", file=sys.stderr)
            return False
        env_src = env_src.replace(old_activate, new_activate, 1)

        old_act_env = (
            "    env[\"PYTHONPATH\"] = os.pathsep.join([str(project_root.resolve()),\n"
            "                                         *([str(site_packages(environment))] if environment else [])])\n"
        )
        new_act_env = (
            "    base_site = site_packages(base_venv(project_root))\n"
            "    extra_sites = ([str(base_site)] if environment and base_site.is_dir()\n"
            "                   and base_site.resolve() != site_packages(environment).resolve() else [])\n"
            "    env[\"PYTHONPATH\"] = os.pathsep.join([str(project_root.resolve()),\n"
            "                                         *([str(site_packages(environment)), *extra_sites] if environment else [])])\n"
        )
        if old_act_env not in env_src:
            print("[odin-patch] ERROR: activation_environment anchor not found in pm/environments.py", file=sys.stderr)
            return False
        env_src = env_src.replace(old_act_env, new_act_env, 1)
        env_target.write_text(env_src, encoding="utf-8")
        print(f"[odin-patch] patched {env_target} (preserved base_venv site-packages)")

    pkg_src = pkg_target.read_text(encoding="utf-8")
    if MARKER_PM_PKG not in pkg_src:
        old_stamp = (
            "        h = hashlib.sha256()\n"
            "        h.update(_uv_lock_digest(self.project_root() / \"uv.lock\"))\n"
        )
        new_stamp = (
            f"        {MARKER_PM_PKG}\n"
            "        h = hashlib.sha256()\n"
            "        h.update(_uv_lock_digest(self.project_root() / \"uv.lock\"))\n"
            "        h.update(_uv_lock_digest(self.project_root() / \"pm\" / \"environments.py\"))\n"
        )
        if old_stamp not in pkg_src:
            print("[odin-patch] ERROR: expected_stamp anchor not found in pm/packages.py", file=sys.stderr)
            return False
        pkg_src = pkg_src.replace(old_stamp, new_stamp, 1)

        old_check = (
            "            resolved_lock = generation / \"workspace\" / \"uv.lock\"\n"
            "            environment.check(resolved_lock)\n"
        )
        new_check = (
            "            resolved_lock = generation / \"workspace\" / \"uv.lock\"\n"
            "            environment.check(resolved_lock)\n"
            "            from pm.environments import base_venv, site_packages\n"
            "            base_site = site_packages(base_venv(project))\n"
            "            cand_site = site_packages(candidate)\n"
            "            if base_site.is_dir() and cand_site.is_dir() and base_site.resolve() != cand_site.resolve():\n"
            "                (cand_site / \"odin-base-site.pth\").write_text(f\"{base_site}\\n\", encoding=\"utf-8\")\n"
        )
        if old_check not in pkg_src:
            print("[odin-patch] ERROR: environment.check anchor not found in pm/packages.py", file=sys.stderr)
            return False
        pkg_src = pkg_src.replace(old_check, new_check, 1)
        pkg_target.write_text(pkg_src, encoding="utf-8")
        print(f"[odin-patch] patched {pkg_target} (stamped and linked base_venv site-packages)")

    return True


def main() -> int:
    ok = (
        patch_adapter()
        and patch_local_llm()
        and patch_mnemosyne_install()
        and patch_pm_base_venv_bridge()
    )
    return 0 if ok else 1



if __name__ == "__main__":
    sys.exit(main())
