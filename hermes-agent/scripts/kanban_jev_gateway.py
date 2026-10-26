#!/usr/bin/env python3
"""kanban_jev_gateway.py - Jev Decision Gating & Discord Threaded Kanban Gateway.

Wires TypeSafe Jev System One decision gating through Muninn LiteLLM proxy
to evaluate incoming alerts, triage items, and operational events. If deemed
actionable, provisions a task in Hermes kanban.db and spins up an isolated
thread in the Discord forum channel (one thread per card). Subsequent
lifecycle events (in_progress, review_required, done) post directly into that thread.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [kanban-gateway] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kanban-gateway")

# Configuration Defaults (injected via environment variables / secrets)
KANBAN_DB_PATH = os.environ.get(
    "KANBAN_DB_PATH",
    "/opt/data/kanban.db" if os.path.exists("/opt/data/kanban.db") else "/opt/odin/hermes/kanban.db",
)
DOMAIN_NAME = os.environ.get("DOMAIN_NAME", "")


def resolve_muninn_endpoint() -> str:
    """Prioritizes internal Docker Swarm overlay network (zero public exposure).

    In-container (Hermes, workers, cron): resolves 'muninn' via Docker embedded DNS -> http://muninn:4000/v1.
    Host execution (Antigravity/CLI): falls back to DOMAIN_NAME if internal DNS is unavailable.
    """
    if os.environ.get("MUNINN_API_URL"):
        return os.environ["MUNINN_API_URL"]

    try:
        import socket
        socket.gethostbyname("muninn")
        return "http://muninn:4000/v1/chat/completions"
    except Exception:
        pass

    if DOMAIN_NAME:
        return f"https://muninn.{DOMAIN_NAME}/v1/chat/completions"

    return "http://muninn:4000/v1/chat/completions"


MUNINN_API_URL = resolve_muninn_endpoint()
MUNINN_API_KEY = os.environ.get("MUNINN_API_KEY") or os.environ.get("LITELLM_MASTER_KEY", "")
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-decision")

DISCORD_WEBHOOK_URL = os.environ.get("KANBAN_DISCORD_WEBHOOK_URL", "")
DISCORD_CHANNEL_ID = os.environ.get("KANBAN_DISCORD_CHANNEL_ID", "")
DISCORD_USER_ID = os.environ.get("DISCORD_USER_ID", "")
DEFAULT_ASSIGNEE = os.environ.get("KANBAN_DEFAULT_ASSIGNEE", "swarm-worker")
USER_AGENT = "Yggdrasil-KanbanGateway/1.0 (DiscordBot)"

# Severity / Status colors for Discord embeds
STATUS_COLORS = {
    "TODO": 3447003,          # Blue
    "READY": 3447003,         # Blue
    "IN_PROGRESS": 15844351,  # Amber
    "RUNNING": 15844351,      # Amber
    "REVIEW_REQUIRED": 15105570, # Orange
    "DONE": 3066993,          # Green
    "BLOCKED": 15158332,      # Red
}


def evaluate_jev_actionability(event_text: str, context: str = "") -> Tuple[bool, float, str]:
    """Evaluates whether an event requires multi-step engineering follow-up via Muninn Jev.

    Returns:
        (is_actionable, latency_seconds, raw_verdict)
    """
    system_prompt = (
        "Evaluate whether the following operational event, incident, or triage item "
        "represents an actionable engineering task requiring Kanban tracking and asynchronous "
        "worker execution. Answer 'true' if it is an actionable multi-step task, bugfix, "
        "infrastructure repair, or maintenance job. Answer 'false' if it is routine telemetry, "
        "a transient health ping, a one-off query, or non-actionable noise."
    )

    messages = [
        {"role": "system", "content": system_prompt},
    ]
    if context:
        messages.append({"role": "user", "content": f"Context:\n{context}"})
    messages.append({"role": "user", "content": f"Event to evaluate:\n{event_text}"})

    payload = {
        "model": JEV_MODEL,
        "messages": messages,
        "temperature": 0.0,
    }

    if not MUNINN_API_KEY:
        logger.error("MUNINN_API_KEY (or LITELLM_MASTER_KEY) is not set in environment.")
        return True, 0.0, "fallback_error: missing_muninn_api_key"

    req = urllib.request.Request(
        MUNINN_API_URL,
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
        with urllib.request.urlopen(req, timeout=15) as resp:
            elapsed = time.time() - start
            body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"].strip().lower()
            is_actionable = "true" in content
            logger.info(
                "Jev System One evaluated event in %.2fs -> Actionable: %s (Raw: '%s')",
                elapsed,
                is_actionable,
                content,
            )
            return is_actionable, elapsed, content
    except Exception as e:
        elapsed = time.time() - start
        logger.error("Jev evaluation failed through Muninn (%s): %s", MUNINN_API_URL, e)
        # Safe fallback: if Jev is unreachable, do not silently lose potentially critical alerts
        return True, elapsed, f"fallback_error: {e}"


def post_discord_thread(title: str, thread_name: str, description: str, fields: Optional[list] = None) -> str:
    """Creates a new topic thread in the Discord forum channel via webhook."""
    if not DISCORD_WEBHOOK_URL:
        raise ValueError("KANBAN_DISCORD_WEBHOOK_URL is not set in environment.")

    url = f"{DISCORD_WEBHOOK_URL}?wait=true"

    embed_fields = fields or []
    mention = f"<@{DISCORD_USER_ID}>" if DISCORD_USER_ID else ""
    payload = {
        "username": "Huginn Kanban",
        "thread_name": thread_name,
        "content": mention,
        "embeds": [
            {
                "title": f"📋 {title}",
                "color": STATUS_COLORS["TODO"],
                "description": description,
                "fields": embed_fields,
                "footer": {"text": "Yggdrasil Kanban · One Thread Per Card"},
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

    with urllib.request.urlopen(req, timeout=15) as resp:
        res = json.loads(resp.read().decode("utf-8"))
        thread_id = str(res.get("channel_id") or res.get("id"))
        logger.info("Created Discord forum thread '%s' (ID: %s)", thread_name, thread_id)
        return thread_id


def post_thread_lifecycle_update(thread_id: str, status: str, message: str, fields: Optional[list] = None) -> bool:
    """Posts a status or progress update directly into an existing Discord card thread."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("KANBAN_DISCORD_WEBHOOK_URL is not set; skipping Discord thread update.")
        return False

    url = f"{DISCORD_WEBHOOK_URL}?thread_id={thread_id}&wait=true"
    color = STATUS_COLORS.get(status.upper(), 3447003)

    embed_fields = fields or []
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


def create_kanban_task(
    title: str,
    body: str,
    priority: int = 1,
    assignee: Optional[str] = None,
    workspace_kind: str = "scratch",
    notify_discord: bool = True,
) -> Tuple[str, Optional[str]]:
    """Inserts a new task into kanban.db and provisions its Discord thread subscription.

    Returns:
        (task_id, discord_thread_id)
    """
    resolved_assignee = assignee or DEFAULT_ASSIGNEE
    short_id = uuid.uuid4().hex[:8]
    task_id = f"kb-{short_id}"
    full_title = f"[KB-{short_id}] {title}"
    now = int(time.time())

    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    # 1. Insert into tasks
    cursor.execute(
        """
        INSERT INTO tasks (
            id, title, body, assignee, status, priority, created_by,
            created_at, workspace_kind, consecutive_failures, goal_mode, block_recurrences
        ) VALUES (?, ?, ?, ?, 'ready', ?, 'jev-gateway', ?, ?, 0, 0, 0)
        """,
        (task_id, full_title, body, resolved_assignee, priority, now, workspace_kind),
    )

    # 2. Record created event
    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'created', ?, ?)
        """,
        (task_id, json.dumps({"source": "jev-gateway", "title": full_title}), now),
    )

    thread_id = None
    if notify_discord and DISCORD_WEBHOOK_URL:
        try:
            fields = [
                {"name": "Status", "value": "`READY`", "inline": True},
                {"name": "Priority", "value": f"`P{priority}`", "inline": True},
                {"name": "Assignee", "value": f"`{resolved_assignee}`", "inline": True},
            ]
            thread_id = post_discord_thread(
                title=full_title,
                thread_name=full_title,
                description=body,
                fields=fields,
            )

            # 3. Store notification subscription in kanban_notify_subs
            cursor.execute(
                """
                INSERT OR REPLACE INTO kanban_notify_subs (
                    task_id, platform, chat_id, thread_id, user_id, notifier_profile,
                    created_at, last_event_id, delivery_mode
                ) VALUES (?, 'discord', ?, ?, ?, 'kanban-gateway', ?, 0, 'notify')
                """,
                (task_id, DISCORD_CHANNEL_ID, thread_id, DISCORD_USER_ID, now),
            )
        except Exception as e:
            logger.error("Failed to provision Discord thread for %s: %s", task_id, e)

    conn.commit()
    conn.close()

    logger.info("Created Kanban task %s (Discord Thread: %s)", task_id, thread_id)
    return task_id, thread_id


def update_task_lifecycle(task_id: str, new_status: str, message: str) -> bool:
    """Updates task status in kanban.db and notifies its assigned Discord thread."""
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    now = int(time.time())
    extra_clause = ""
    if new_status.lower() == "in_progress":
        extra_clause = ", started_at = ?"
    elif new_status.lower() in ("done", "completed"):
        extra_clause = ", completed_at = ?"

    query = f"UPDATE tasks SET status = ?{extra_clause} WHERE id = ?"
    params = [new_status, now, task_id] if extra_clause else [new_status, task_id]
    cursor.execute(query, params)

    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'status_changed', ?, ?)
        """,
        (task_id, json.dumps({"status": new_status, "message": message}), now),
    )

    # Find registered thread subscription
    cursor.execute(
        """
        SELECT thread_id FROM kanban_notify_subs
        WHERE task_id = ? AND platform = 'discord' LIMIT 1
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    thread_id = row[0] if row else None

    conn.commit()
    conn.close()

    if thread_id:
        fields = [
            {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
            {"name": "New Status", "value": f"`{new_status.upper()}`", "inline": True},
        ]
        post_thread_lifecycle_update(thread_id, new_status, message, fields=fields)

    return True


def triage_and_intake(event_text: str, title: Optional[str] = None, context: str = "") -> Optional[Dict[str, Any]]:
    """Complete entrypoint: evaluates with Jev and provisions task if actionable."""
    logger.info("Running Jev triage on event: %s", event_text[:80])
    is_actionable, elapsed, verdict = evaluate_jev_actionability(event_text, context=context)

    if not is_actionable:
        logger.info("Event classified as NON-ACTIONABLE by Jev (suppressed from Kanban).")
        return {
            "actionable": False,
            "verdict": verdict,
            "elapsed_seconds": elapsed,
            "task_id": None,
            "thread_id": None,
        }

    # Extract short title if not provided
    if not title:
        first_line = event_text.strip().split("\n")[0]
        title = first_line[:60].strip()
        if len(first_line) > 60:
            title += "..."

    task_id, thread_id = create_kanban_task(
        title=title,
        body=event_text,
        priority=1,
        assignee=DEFAULT_ASSIGNEE,
    )

    return {
        "actionable": True,
        "verdict": verdict,
        "elapsed_seconds": elapsed,
        "task_id": task_id,
        "thread_id": thread_id,
    }


def list_active_tasks() -> None:
    """Prints active tasks and their mapped Discord thread IDs."""
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT t.id, t.title, t.status, t.priority, s.thread_id
        FROM tasks t
        LEFT JOIN kanban_notify_subs s ON t.id = s.task_id
        ORDER BY t.created_at DESC LIMIT 20
        """
    )
    rows = cursor.fetchall()
    conn.close()

    print(f"\n{'TASK ID':14} {'STATUS':12} {'DISCORD THREAD':20} {'TITLE'}")
    print("-" * 75)
    for row in rows:
        tid, title, status, prio, thread = row
        thread_display = thread or "none"
        print(f"{tid:14} {status:12} {thread_display:20} {title}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Jev Decision Gating & Discord Threaded Kanban Gateway")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Triage command
    p_triage = subparsers.add_parser("triage", help="Triage event with Jev; if actionable, create card + thread")
    p_triage.add_argument("text", help="Event or alert text to evaluate")
    p_triage.add_argument("--title", help="Optional explicit title")
    p_triage.add_argument("--context", default="", help="Optional system context")

    # Create command (bypass Jev)
    p_create = subparsers.add_parser("create", help="Directly create Kanban card and Discord thread")
    p_create.add_argument("--title", required=True, help="Task title")
    p_create.add_argument("--body", required=True, help="Task description")
    p_create.add_argument("--priority", type=int, default=1, help="Task priority (0-3)")
    p_create.add_argument("--assignee", default=DEFAULT_ASSIGNEE, help="Assignee profile")

    # Update command
    p_update = subparsers.add_parser("update", help="Update task lifecycle status and notify thread")
    p_update.add_argument("--task-id", required=True, help="Task ID (e.g. kb-a1b2c3d4)")
    p_update.add_argument("--status", required=True, choices=["todo", "ready", "running", "in_progress", "review_required", "done", "blocked"])
    p_update.add_argument("--message", required=True, help="Status update or progress message")

    # List command
    subparsers.add_parser("list", help="List active Kanban cards and mapped threads")

    args = parser.parse_args()

    if args.command == "triage":
        res = triage_and_intake(args.text, title=args.title, context=args.context)
        print(json.dumps(res, indent=2))
    elif args.command == "create":
        task_id, thread_id = create_kanban_task(args.title, args.body, priority=args.priority, assignee=args.assignee)
        print(f"Created task {task_id} with thread ID {thread_id}")
    elif args.command == "update":
        update_task_lifecycle(args.task_id, args.status, args.message)
        print(f"Updated task {args.task_id} to status '{args.status}'")
    elif args.command == "list":
        list_active_tasks()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
