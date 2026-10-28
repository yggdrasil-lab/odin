"""jev.py - TypeSafe Jev System One decision gating and model routing."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Tuple

from .config import (
    JEV_MODEL,
    MUNINN_API_KEY,
    USER_AGENT,
    logger,
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
        logger.error("Jev evaluation failed through Muninn (%s): %s", MUNINN_API_URL, e)
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


def evaluate_task_complexity(title: str, body: str) -> Dict[str, Any]:
    """Evaluates task complexity and routes to flash_fast, frontier_deep, or human_pairing (Choice)."""
    system_prompt = (
        "Evaluate the technical complexity of the following software/infrastructure task for an autonomous agent worker. "
        "Classify into exactly one tier:\n"
        "```json\n"
        "{\n"
        '  "questions": {\n'
        '    "tier": {\n'
        '      "type": "choice",\n'
        '      "instructions": "Classify task complexity tier into flash_fast, frontier_deep, or human_pairing",\n'
        '      "criteria": {\n'
        '        "flash_fast": "Routine chores, documentation updates, typo fixes, script parameter tweaks, lint errors, minor path adjustments. Suitable for fast subscription models (gemini-3.8-flash-high).",\n'
        '        "frontier_deep": "Architectural additions, multi-service Docker Swarm compose changes, database migrations, security hardening, multi-step refactoring, or critical reliability fixes. Requires frontier reasoning models (gemini-4-argon-high).",\n'
        '        "human_pairing": "Architectural forks, destructive deletions, subjective product decisions, or fundamental infrastructure trade-offs requiring pairing with Raymund."\n'
        "      }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "```"
    )
    content, elapsed = call_jev_raw(system_prompt, f"Task Title: {title}\n\nTask Description:\n{body}")
    content_lower = content.lower()
    tier = "flash_fast"
    recommended_model = "gemini-3.8-flash-high"
    if "human_pairing" in content_lower:
        tier = "human_pairing"
        recommended_model = "human/raymund"
    elif "frontier_deep" in content_lower:
        tier = "frontier_deep"
        recommended_model = "gemini-4-argon-high"
    elif "flash_fast" in content_lower:
        tier = "flash_fast"
        recommended_model = "gemini-3.8-flash-high"

    logger.info("Jev System One task complexity in %.2fs -> Tier: %s (%s)", elapsed, tier, recommended_model)
    return {
        "tier": tier,
        "recommended_model": recommended_model,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
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
    """Classifies reported frictions to avoid spawning redundant chore cards for transient noise (Choice)."""
    system_prompt = (
        "Classify the reported task execution friction into exactly one category:\n"
        "```json\n"
        "{\n"
        '  "questions": {\n'
        '    "action": {\n'
        '      "type": "choice",\n'
        '      "instructions": "Classify friction actionability into spawn_chore, ignore_transient, or alert_channel",\n'
        '      "criteria": {\n'
        '        "spawn_chore": "A concrete, reproducible operational bug, missing tool, bad configuration, or recurring bottleneck that warrants creating an autonomous follow-up [Chore/Friction] Kanban card.",\n'
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
    action = "spawn_chore"  # Safe default
    if "ignore_transient" in content_lower:
        action = "ignore_transient"
    elif "alert_channel" in content_lower:
        action = "alert_channel"
    elif "spawn_chore" in content_lower:
        action = "spawn_chore"

    logger.info("Jev System One friction classification in %.2fs -> Action: %s (Raw: '%s')", elapsed, action, content)
    return {
        "action": action,
        "raw_verdict": content,
        "latency_seconds": round(elapsed, 3),
    }
