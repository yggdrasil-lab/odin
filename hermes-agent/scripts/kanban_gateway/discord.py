"""discord.py - Discord forum channel thread creation and embed notifications."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

from .config import (
    DISCORD_CHANNEL_ID,
    DISCORD_USER_ID,
    DISCORD_WEBHOOK_URL,
    STATUS_CANONICAL,
    STATUS_COLORS,
    USER_AGENT,
    logger,
)


def post_discord_thread(title: str, thread_name: str, description: str, fields: Optional[list] = None) -> str:
    """Creates a new topic thread in the Discord forum channel via webhook."""
    if not DISCORD_WEBHOOK_URL:
        raise ValueError("KANBAN_DISCORD_WEBHOOK_URL is not set in environment.")

    url = f"{DISCORD_WEBHOOK_URL}?wait=true"

    embed_fields = fields or []
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
    payload = {
        "username": "Huginn Kanban",
        "thread_name": thread_name[:100],
        "content": mention,
        "embeds": [
            {
                "title": f"📋 {title[:256]}",
                "color": STATUS_COLORS["READY"],
                "description": description[:4096],
                "fields": embed_fields,
                "footer": {"text": "Yggdrasil Autonomous Kanban • Huginn"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ],
    }

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            thread_id = data.get("channel_id")
            if not thread_id:
                raise ValueError(f"Discord response missing channel_id: {data}")
            logger.info("Successfully provisioned Discord thread %s for '%s'", thread_id, title)
            return str(thread_id)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("Discord webhook returned HTTP %d: %s", e.code, body)
        raise RuntimeError(f"Discord API HTTP {e.code}: {body}") from e


def post_thread_lifecycle_update(
    thread_id: str,
    status: str,
    message: str,
    fields: Optional[list] = None,
) -> bool:
    """Posts a status transition embed directly to an existing Discord card thread."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("KANBAN_DISCORD_WEBHOOK_URL is not set; skipping Discord thread update.")
        return False

    url = f"{DISCORD_WEBHOOK_URL}?thread_id={thread_id}&wait=true"
    color = STATUS_COLORS.get(status.upper(), 3447003)

    embed_fields = fields or []
    canonical = STATUS_CANONICAL.get(status.lower(), status.lower())
    mention = f"<@{DISCORD_USER_ID}> 🔔 Review Required" if (canonical == "in_review" and DISCORD_USER_ID) else ""

    payload = {
        "username": "Huginn Kanban",
        "embeds": [
            {
                "title": f"🔄 Task Lifecycle: {status.upper()}",
                "color": color,
                "description": message,
                "fields": embed_fields,
                "footer": {"text": f"Thread ID: {thread_id}"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ],
    }
    if mention:
        payload["content"] = mention

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Posted update '%s' to thread %s (HTTP %d)", status, thread_id, resp.status)
            return True
    except Exception as e:
        logger.error("Failed posting update to thread %s: %s", thread_id, e)
        return False


def post_thread_debrief(
    thread_id: str,
    task_id: str,
    summary: str,
    frictions: Optional[str] = None,
    chore_task_id: Optional[str] = None,
    pr_url: Optional[str] = None,
    acceptance_gate: Optional[Dict[str, Any]] = None,
    auto_merge_gate: Optional[Dict[str, Any]] = None,
    friction_gate: Optional[Dict[str, Any]] = None,
) -> bool:
    """Posts a structured Task Debrief embed directly into a Discord card thread."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("KANBAN_DISCORD_WEBHOOK_URL is not set; skipping Discord thread update.")
        return False

    url = f"{DISCORD_WEBHOOK_URL}?thread_id={thread_id}&wait=true"
    color = STATUS_COLORS.get("DEBRIEF", 10181046)

    # Auto-extract PR URL from summary if not explicitly provided
    extracted_pr = pr_url
    if not extracted_pr and ("github.com/" in summary and "/pull/" in summary):
        import re
        m = re.search(r"https://github\.com/[^\s)]+/pull/\d+", summary)
        if m:
            extracted_pr = m.group(0)

    mention = ""
    if extracted_pr and DISCORD_USER_ID:
        if auto_merge_gate and auto_merge_gate.get("auto_merge_eligible"):
            mention = f"🤖 Pull Request Auto-Merge Eligible (Routine Chore/Docs): {extracted_pr}"
        else:
            mention = f"<@{DISCORD_USER_ID}> 🔔 Pull Request Review Required: {extracted_pr}"

    fields = [
        {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
    ]

    if extracted_pr:
        fields.append({"name": "Pull Request", "value": f"[View on GitHub]({extracted_pr})", "inline": True})

    if acceptance_gate:
        acc_text = "✅ Verified Criteria Met" if acceptance_gate.get("accepted") else f"⚠️ Contract Warning: {acceptance_gate.get('raw_verdict', '')[:100]}"
        fields.append({"name": "Contract Acceptance", "value": acc_text, "inline": True})

    if auto_merge_gate and extracted_pr:
        merge_text = "🤖 Auto-Merge Eligible" if auto_merge_gate.get("auto_merge_eligible") else "👤 Human Review Required"
        fields.append({"name": "Merge Safety Gate", "value": merge_text, "inline": True})

    has_friction = bool(frictions and frictions.strip() and frictions.strip().lower() != "none")
    if has_friction:
        fields.append({"name": "Frictions / Learnings", "value": frictions[:1024], "inline": False})
    else:
        fields.append({"name": "Execution Quality", "value": "Clean execution (zero frictions recorded)", "inline": False})

    if chore_task_id:
        fields.append({"name": "Auto-Spawned Chore", "value": f"`{chore_task_id}` (enqueued to backlog)", "inline": True})
    elif friction_gate and friction_gate.get("action") == "ignore_transient":
        fields.append({"name": "Friction Triage", "value": "Transient noise suppressed (no chore card spawned)", "inline": True})

    payload = {
        "username": "Huginn Kanban",
        "embeds": [
            {
                "title": "Task Debrief & Retrospective",
                "color": color,
                "description": summary,
                "fields": fields,
                "footer": {"text": f"Thread ID: {thread_id} • Continuous Improvement"},
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        ],
    }
    if mention:
        payload["content"] = mention

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logger.info("Posted debrief for %s to thread %s (HTTP %d)", task_id, thread_id, resp.status)
            return True
    except Exception as e:
        logger.error("Failed posting debrief for %s to thread %s: %s", task_id, thread_id, e)
        return False
