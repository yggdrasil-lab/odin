"""jev.py - TypeSafe Jev System One decision gating and model routing."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional, Tuple

from .config import (
    ASSIGNEE_ALIASES,
    JEV_MODEL,
    MUNINN_API_KEY,
    SPECIALIST_MODELS,
    USER_AGENT,
    logger,
    normalize_assignee,
    resolve_muninn_endpoint,
)


def call_jev_raw(
    system_prompt: str,
    user_content: str,
    model: str = JEV_MODEL,
    timeout: float = 15.0,
) -> Tuple[str, float]:
    """Sends a System One query to Jev via Muninn and returns (content, elapsed_seconds)."""
    endpoint = resolve_muninn_endpoint()
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.0,
    }
    if not MUNINN_API_KEY:
        logger.error("MUNINN_API_KEY (or LITELLM_MASTER_KEY) is not set in environment.")
        return "fallback_error: missing_muninn_api_key", 0.0

    req = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {MUNINN_API_KEY}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = time.time() - start
            body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"].strip()
            return content, elapsed
    except Exception as e:
        elapsed = time.time() - start
        logger.error("Jev evaluation failed through Muninn (%s): %s", endpoint, e)
        return f"fallback_error: {e}", elapsed


def evaluate_jev_actionability(event_text: str, context: str = "") -> Tuple[bool, float, str]:
    """Evaluates whether an event requires multi-step engineering follow-up via Muninn Jev."""
    system_prompt = (
        "Evaluate whether the following operational event, incident, or triage item "
        "represents an actionable engineering task requiring Kanban tracking and asynchronous "
        "worker execution. Answer 'true' if it is an actionable multi-step task, bugfix, "
        "infrastructure repair, or maintenance job. Answer 'false' if it is routine telemetry, "
        "a transient health ping, a one-off query, or non-actionable noise."
    )
    user_content = f"Context:\n{context}\n\nEvent to evaluate:\n{event_text}" if context else f"Event to evaluate:\n{event_text}"
    content, elapsed = call_jev_raw(system_prompt, user_content)
    is_actionable = "true" in content.lower() or "fallback_error" in content
    logger.info("Jev System One evaluated event in %.2fs -> Actionable: %s (Raw: '%s')", elapsed, is_actionable, content)
    return is_actionable, elapsed, content


def _infer_specialist_from_text(title: str, body: str, tier: str) -> str:
    """Infers canonical Court of Odin specialist when Jev response omits an explicit role."""
    combined = f"{title}\n{body}".lower()
    if any(k in combined for k in ("eir", "kratos", "workout", "biometric", "hrv", "daily journal", "badminton", "00-health", "10-wealth", "20-career", "30-relationships")):
        return "saga"
    if any(k in combined for k in ("wrap-up", "wrapup", "mnemosyne", "skill", "areas/90-infrastructure", "efforts/", "chore/friction", "address friction")):
        return "vor"
    if any(k in combined for k in ("playwright", "visual gate", "visual review", "iphone 14", "pixel 7", "qa", "schema validation")):
        return "forseti"
    if any(k in combined for k in ("forge/", "docker-compose", "dockerfile", "deploy.yml", "open a pr", "pull request")) or tier == "frontier_deep":
        return "sindri"
    return "huginn"


def evaluate_task_complexity(title: str, body: str) -> Dict[str, Any]:
    """Evaluates task complexity and routes to Court of Odin specialist role and model tier."""
    system_prompt = (
        "Evaluate the technical complexity and specialist domain of the following task for the Court of Odin autonomous workers. "
        "Classify both tier and role:\n"
        "```json\n"
        "{\n"
        '  "questions": {\n'
        '    "tier": {\n'
        '      "type": "choice",\n'
        '      "instructions": "Classify task complexity tier into flash_fast, frontier_deep, or human_pairing",\n'
        '      "criteria": {\n'
        '        "flash_fast": "Routine chores, documentation updates, log/web investigation, biometric/journal entries, QA checks, or minor script tweaks. Uses gemini-3.8-flash-high.",\n'
        '        "frontier_deep": "Architectural additions, multi-service Docker Swarm compose changes, database migrations, security hardening, or complex Forge PR implementation. Uses gemini-4-argon-high.",\n'
        '        "human_pairing": "Architectural forks, destructive deletions, subjective product decisions, or fundamental infrastructure trade-offs requiring pairing with Raymund."\n'
        "      }\n"
        "    },\n"
        '    "role": {\n'
        '      "type": "choice",\n'
        '      "instructions": "Assign the task to the single best Court of Odin specialist: huginn, sindri, forseti, vor, or saga",\n'
        '      "criteria": {\n'
        '        "huginn": "Scout Raven: log triage, root-cause diagnostics, host/Swarm telemetry inspection, and web research.",\n'
        '        "sindri": "Forge Builder: code changes, Docker Swarm compose stacks, Dockerfile/CI updates, and PR creation in Forge/<repo>.",\n'
        '        "forseti": "QA & Visual Gate: Playwright visual verification (iPhone 14 / Pixel 7), PR diff invariant audits, and schema validation.",\n'
        '        "vor": "Vault & Memory Curator: session wrap-ups, skill maintenance, Mnemosyne hygiene, and Areas/90-Infrastructure documentation.",\n'
        '        "saga": "Journal & Health Scribe: Daily Journal stories, life areas (00-30), Eir biometrics analysis, and Kratos workout autoregulation."\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "```"
    )
    content, elapsed = call_jev_raw(system_prompt, f"Task Title: {title}\n\nTask Description:\n{body}")
    content_lower = content.lower()
    tier = "flash_fast"
    recommended_assignee = ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            if parsed.get("tier") in ("flash_fast", "frontier_deep", "human_pairing"):
                tier = str(parsed["tier"])
            if parsed.get("role"):
                recommended_assignee = normalize_assignee(str(parsed["role"]))
    except Exception:
        if "human_pairing" in content_lower:
            tier = "human_pairing"
        elif "frontier_deep" in content_lower:
            tier = "frontier_deep"
        elif "flash_fast" in content_lower:
            tier = "flash_fast"

    if not recommended_assignee or recommended_assignee == "swarm-worker":
        for candidate in ("huginn", "sindri", "forseti", "vor", "saga", *ASSIGNEE_ALIASES.keys()):
            if f'"{candidate}"' in content_lower or f"'{candidate}'" in content_lower or f"role: {candidate}" in content_lower:
                recommended_assignee = normalize_assignee(candidate)
                break

    if tier == "human_pairing":
        recommended_assignee = "raymund"
        recommended_model = "human/raymund"
    else:
        if not recommended_assignee or recommended_assignee == "swarm-worker":
            recommended_assignee = _infer_specialist_from_text(title, body, tier)
        if tier == "frontier_deep":
            recommended_model = "gemini-4-argon-high"
        elif tier == "flash_fast":
            recommended_model = "gemini-3.8-flash-high"
        else:
            recommended_model = SPECIALIST_MODELS.get(recommended_assignee, "gemini-3.8-flash-high")

    logger.info(
        "Jev System One task complexity in %.2fs -> Tier: %s | Assignee: %s (%s)",
        elapsed,
        tier,
        recommended_assignee,
        recommended_model,
    )
    return {
        "tier": tier,
        "recommended_assignee": recommended_assignee,
        "recommended_model": recommended_model,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }


def evaluate_triage_and_complexity(event_text: str, title: str, context: str = "") -> Dict[str, Any]:
    """Batches event actionability, task complexity tier, and specialist role in 1 Jev round-trip."""
    schema = {
        "questions": {
            "actionable": {
                "type": "noul",
                "instructions": (
                    "True if the event represents an actionable multi-step engineering task, bugfix, "
                    "infrastructure repair, or maintenance job requiring Kanban tracking; False if routine "
                    "telemetry, transient health ping, or non-actionable noise."
                ),
            },
            "tier": {
                "type": "choice",
                "instructions": "Classify task complexity tier into flash_fast, frontier_deep, or human_pairing",
                "criteria": {
                    "flash_fast": "Routine chores, documentation updates, log/web investigation, biometric/journal entries, QA checks, or minor script tweaks.",
                    "frontier_deep": "Architectural additions, multi-service Docker Swarm compose changes, database migrations, or complex Forge PR implementation.",
                    "human_pairing": "Architectural forks, destructive deletions, or fundamental trade-offs requiring pairing with Raymund.",
                },
            },
            "role": {
                "type": "choice",
                "instructions": "Assign the task to the single best Court of Odin specialist: huginn, sindri, forseti, vor, or saga",
                "criteria": {
                    "huginn": "Scout Raven: log triage, root-cause diagnostics, host/Swarm telemetry inspection, and web research.",
                    "sindri": "Forge Builder: code changes, Docker Swarm compose stacks, Dockerfile/CI updates, and PR creation in Forge/<repo>.",
                    "forseti": "QA & Visual Gate: Playwright visual verification, PR diff invariant audits, and schema validation.",
                    "vor": "Vault & Memory Curator: session wrap-ups, skill maintenance, Mnemosyne hygiene, and Areas/90-Infrastructure documentation.",
                    "saga": "Journal & Health Scribe: Daily Journal stories, life areas (00-30), Eir biometrics analysis, and Kratos workout autoregulation.",
                },
            },
        }
    }
    system_prompt = (
        "Evaluate event actionability, complexity tier, and specialist role for the Court of Odin:\n"
        f"```json\n{json.dumps(schema)}\n```"
    )
    user_content = (
        f"Context:\n{context}\n\nTitle: {title}\n\nEvent:\n{event_text}"
        if context
        else f"Title: {title}\n\nEvent:\n{event_text}"
    )
    content, elapsed = call_jev_raw(system_prompt, user_content)

    is_actionable = "fallback_error" in content
    tier = "flash_fast"
    recommended_assignee = ""
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            is_actionable = str(parsed.get("actionable", "true")).lower() == "true"
            if parsed.get("tier") in ("flash_fast", "frontier_deep", "human_pairing"):
                tier = str(parsed["tier"])
            if parsed.get("role"):
                recommended_assignee = normalize_assignee(str(parsed["role"]))
    except Exception:
        is_actionable = "true" in content.lower() or "fallback_error" in content

    if tier == "human_pairing":
        recommended_assignee = "raymund"
        recommended_model = "human/raymund"
    else:
        if not recommended_assignee or recommended_assignee == "swarm-worker":
            recommended_assignee = _infer_specialist_from_text(title, event_text, tier)
        if tier == "frontier_deep":
            recommended_model = "gemini-4-argon-high"
        elif tier == "flash_fast":
            recommended_model = "gemini-3.8-flash-high"
        else:
            recommended_model = SPECIALIST_MODELS.get(recommended_assignee, "gemini-3.8-flash-high")

    return {
        "actionable": is_actionable,
        "tier": tier,
        "recommended_assignee": recommended_assignee,
        "recommended_model": recommended_model,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }


def _classify_friction_tier_fallback(friction_text: str) -> str:
    """Deterministic fallback classifying whether a friction is an existing failing script (Tier 2 auto-fix)
    or a structural/rule/new-skill proposal (Tier 3 human review).
    """
    lower = (friction_text or "").lower()
    if not lower or lower == "none":
        return "ignore_transient"
    tier3_markers = ("new skill", "create skill", "agents.md", ".agents/rules", "new rule", "redesign", "architectural")
    if any(m in lower for m in tier3_markers):
        return "propose_review"
    script_fail_markers = (
        "traceback",
        "syntaxerror",
        "filenotfounderror",
        "modulenotfounderror",
        "typeerror",
        "valueerror",
        "keyerror",
        "attributeerror",
        "calledprocesserror",
        "unrecognized arguments",
        "no such file",
        "broken symlink",
        "failing script",
        "script failed",
        "script crash",
        "exit code 1",
        "exit code 2",
        ".py",
        ".sh",
    )
    if any(m in lower for m in script_fail_markers):
        return "auto_fix_script"
    return "propose_review"


def evaluate_debrief_gates_batch(
    task_title: str,
    task_body: str,
    debrief_summary: str,
    pr_url: Optional[str] = None,
    friction_text: Optional[str] = None,
) -> Dict[str, Any]:
    """Evaluates debrief contract acceptance, PR auto-merge safety, and friction routing in 1 Jev round-trip."""
    has_pr = bool(pr_url and pr_url.strip())
    has_friction = bool(friction_text and friction_text.strip() and friction_text.strip().lower() != "none")

    questions: Dict[str, Any] = {
        "accepted": {
            "type": "noul",
            "instructions": (
                "True if the worker's debrief summary demonstrates that the stated task acceptance criteria "
                "and deliverables were fulfilled; False if prematurely aborted or missing core deliverables."
            ),
        }
    }
    if has_pr:
        questions["auto_merge_eligible"] = {
            "type": "noul",
            "instructions": (
                "True if the PR consists strictly of non-critical documentation, typo fixes, comment cleanups, "
                "minor chore scripts, or safe lint patches with passing tests. False if it alters core "
                "infrastructure, container entrypoints, compose networks, database schemas, or service runtime configs."
            ),
        }
    if has_friction:
        questions["friction_action"] = {
            "type": "choice",
            "instructions": "Classify friction actionability into auto_fix_script, propose_review, ignore_transient, or alert_channel",
            "criteria": {
                "auto_fix_script": "A failing or broken existing script (Python/bash traceback, SyntaxError, FileNotFoundError, broken path/symlink, missing CLI flag, unhandled exception) or a stale path/flag in an existing skill doc that should be repaired directly and autonomously without waiting for human decision.",
                "propose_review": "A brand-new skill, brand-new script, canonical rule change (AGENTS.md / .agents/rules/), workflow redesign, or architectural friction that requires human review and decision in a #info proposal thread before execution.",
                "ignore_transient": "A one-off transient network retry, minor timing delay, benign warning, or non-actionable observation. Safe to ignore.",
                "alert_channel": "A critical blocking outage, credential revocation, or fatal system error requiring immediate attention.",
            },
        }
        questions["friction_assignee"] = {
            "type": "choice",
            "instructions": "Assign the follow-up friction chore to vor or sindri",
            "criteria": {
                "vor": "Skills, vault rules, documentation in Areas/90-Infrastructure, or Mnemosyne hygiene",
                "sindri": "Forge stack repositories, Docker Swarm compose, Dockerfile, or CI workflows",
            },
        }

    schema = {"questions": questions}
    system_prompt = (
        "Evaluate task completion debrief gates for the Court of Odin:\n"
        f"```json\n{json.dumps(schema)}\n```"
    )
    user_parts = [
        f"Task Title: {task_title}",
        f"Task Contract / Description:\n{task_body}",
        f"Worker Debrief Summary:\n{debrief_summary}",
    ]
    if has_pr:
        user_parts.append(f"PR URL: {pr_url}")
    if has_friction:
        user_parts.append(f"Reported Friction:\n{friction_text}")

    content, elapsed = call_jev_raw(system_prompt, "\n\n".join(user_parts))
    parsed: Dict[str, Any] = {}
    try:
        loaded = json.loads(content)
        if isinstance(loaded, dict):
            parsed = loaded
    except Exception:
        if not has_pr and not has_friction:
            parsed = {"accepted": "true" if ("true" in content.lower() or "fallback_error" in content) else "false"}

    is_accepted = str(parsed.get("accepted", "true")).lower() == "true" or "fallback_error" in content
    acceptance_gate = {
        "accepted": is_accepted,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }

    if has_pr:
        is_auto_merge = str(parsed.get("auto_merge_eligible", "false")).lower() == "true" and "fallback_error" not in content
        auto_merge_gate = {
            "auto_merge_eligible": is_auto_merge,
            "raw_verdict": content,
            "latency_seconds": round(elapsed, 3),
        }
    else:
        auto_merge_gate = {"auto_merge_eligible": False, "raw_verdict": "no_pr"}

    friction_gate = None
    friction_assignee = "vor"
    if has_friction:
        raw_action = str(parsed.get("friction_action", ""))
        if raw_action in ("auto_fix_script", "propose_review", "spawn_chore", "ignore_transient", "alert_channel"):
            action = raw_action
        else:
            action = _classify_friction_tier_fallback(friction_text or "")
        if action == "spawn_chore":
            action = _classify_friction_tier_fallback(friction_text or "")
        if parsed.get("friction_assignee") in ("vor", "sindri"):
            friction_assignee = str(parsed["friction_assignee"])
        else:
            friction_lower = (friction_text or "").lower()
            friction_assignee = (
                "sindri"
                if any(k in friction_lower for k in ("forge/", "docker-compose", "dockerfile", "deploy.yml"))
                else "vor"
            )
        friction_gate = {
            "action": action,
            "assignee": friction_assignee,
            "raw_verdict": content,
            "latency_seconds": round(elapsed, 3),
        }

    return {
        "acceptance_gate": acceptance_gate,
        "auto_merge_gate": auto_merge_gate,
        "friction_gate": friction_gate,
        "friction_assignee": friction_assignee,
    }


def evaluate_debrief_acceptance(task_title: str, task_body: str, debrief_summary: str) -> Dict[str, Any]:
    """Verifies worker debrief against completion contract (Noul)."""
    system_prompt = (
        "You are an automated contract verification gate for an autonomous software agent. "
        "Evaluate whether the worker's debrief summary demonstrates that the stated task acceptance criteria and deliverables were fulfilled. "
        "```json\n"
        "{\n"
        '  "questions": {\n'
        '    "accepted": {\n'
        '      "type": "noul",\n'
        '      "instructions": "True if the deliverable is completed and verified; False if the task was prematurely aborted, missing core deliverables, or failed its verification contract."\n'
        "    }\n"
        "  }\n"
        "}\n"
        "```"
    )
    user_content = f"Task Title: {task_title}\n\nTask Contract / Description:\n{task_body}\n\nWorker Debrief Summary:\n{debrief_summary}"
    content, elapsed = call_jev_raw(system_prompt, user_content)
    is_accepted = "true" in content.lower() or "fallback_error" in content
    logger.info("Jev System One contract acceptance in %.2fs -> Accepted: %s (Raw: '%s')", elapsed, is_accepted, content)
    return {
        "accepted": is_accepted,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }


def evaluate_auto_merge_safety(task_title: str, debrief_summary: str, pr_url: str) -> Dict[str, Any]:
    """Evaluates whether a PR is safe for autonomous merging without human sign-off (Noul)."""
    system_prompt = (
        "Evaluate whether this completed pull request is safe to auto-merge without human review. "
        "```json\n"
        "{\n"
        '  "questions": {\n'
        '    "auto_merge_eligible": {\n'
        '      "type": "noul",\n'
        '      "instructions": "True if the PR consists strictly of non-critical documentation, typo fixes, comment cleanups, minor chore scripts, or safe lint patches with passing tests. False if it alters core infrastructure, container entrypoints, compose network definitions, database schemas, secrets, or service runtime configurations that require human sign-off."\n'
        "    }\n"
        "  }\n"
        "}\n"
        "```"
    )
    user_content = f"PR URL: {pr_url}\nTask: {task_title}\nDebrief: {debrief_summary}"
    content, elapsed = call_jev_raw(system_prompt, user_content)
    is_auto_merge = "true" in content.lower() and "fallback_error" not in content
    logger.info("Jev System One auto-merge safety in %.2fs -> Eligible: %s (Raw: '%s')", elapsed, is_auto_merge, content)
    return {
        "auto_merge_eligible": is_auto_merge,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }


def evaluate_friction_actionability(friction_text: str) -> Dict[str, Any]:
    """Classifies reported frictions into auto_fix_script (Tier 2), propose_review (Tier 3), ignore_transient, or alert_channel."""
    system_prompt = (
        "Classify the reported task execution friction into exactly one category:\n"
        "```json\n"
        "{\n"
        '  "questions": {\n'
        '    "action": {\n'
        '      "type": "choice",\n'
        '      "instructions": "Classify friction actionability into auto_fix_script, propose_review, ignore_transient, or alert_channel",\n'
        '      "criteria": {\n'
        '        "auto_fix_script": "A failing or broken existing script (Python/bash traceback, SyntaxError, FileNotFoundError, broken path/symlink, missing CLI flag, unhandled exception) or a stale path/flag in an existing skill doc that should be repaired directly and autonomously without waiting for human decision.",\n'
        '        "propose_review": "A brand-new skill, brand-new script, canonical rule change (AGENTS.md / .agents/rules/), workflow redesign, or architectural friction that requires human review and decision in a #info proposal thread before execution.",\n'
        '        "ignore_transient": "A one-off transient network retry, minor timing delay, benign warning, or non-actionable observation (e.g. 0 frictions, clean execution). Safe to ignore without spawning a card.",\n'
        '        "alert_channel": "A critical blocking outage, credential revocation, or fatal system error requiring immediate attention."\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "```"
    )
    content, elapsed = call_jev_raw(system_prompt, f"Reported Friction:\n{friction_text}")
    content_lower = content.lower()
    action = _classify_friction_tier_fallback(friction_text)
    if "ignore_transient" in content_lower:
        action = "ignore_transient"
    elif "alert_channel" in content_lower:
        action = "alert_channel"
    elif "auto_fix_script" in content_lower:
        action = "auto_fix_script"
    elif "propose_review" in content_lower or "spawn_chore" in content_lower:
        action = "propose_review"

    logger.info("Jev System One friction classification in %.2fs -> Action: %s (Raw: '%s')", elapsed, action, content)
    return {
        "action": action,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }
