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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    "REVIEW": 15105570,       # Orange
    "REVIEW_REQUIRED": 15105570, # Orange
    "DONE": 3066993,          # Green
    "BLOCKED": 15158332,      # Red
    "DEBRIEF": 10181046,      # Purple
}

STATUS_CANONICAL = {
    "todo": "todo",
    "ready": "ready",
    "running": "running",
    "in_progress": "running",
    "review": "review",
    "review_required": "review",
    "done": "done",
    "completed": "done",
    "blocked": "blocked",
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


def post_thread_debrief(
    thread_id: str,
    task_id: str,
    summary: str,
    frictions: Optional[str] = None,
    chore_task_id: Optional[str] = None,
) -> bool:
    """Posts a structured Task Debrief embed directly into a Discord card thread."""
    if not DISCORD_WEBHOOK_URL:
        logger.warning("KANBAN_DISCORD_WEBHOOK_URL is not set; skipping Discord thread update.")
        return False

    url = f"{DISCORD_WEBHOOK_URL}?thread_id={thread_id}&wait=true"
    color = STATUS_COLORS.get("DEBRIEF", 10181046)

    fields = [
        {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
    ]

    has_friction = bool(frictions and frictions.strip() and frictions.strip().lower() != "none")
    if has_friction:
        fields.append({"name": "Frictions / Learnings", "value": frictions[:1024], "inline": False})
    else:
        fields.append({"name": "Execution Quality", "value": "Clean execution (zero frictions recorded)", "inline": False})

    if chore_task_id:
        fields.append({"name": "Auto-Spawned Chore", "value": f"`{chore_task_id}` (enqueued to backlog)", "inline": True})

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
    canonical_status = STATUS_CANONICAL.get(new_status.lower(), new_status.lower())
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    now = int(time.time())
    extra_clause = ""
    if canonical_status == "running":
        extra_clause = ", started_at = ?"
    elif canonical_status == "done":
        extra_clause = ", completed_at = ?"

    query = f"UPDATE tasks SET status = ?{extra_clause} WHERE id = ?"
    params = [canonical_status, now, task_id] if extra_clause else [canonical_status, task_id]
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


def record_task_debrief(
    task_id: str,
    summary: str,
    frictions: Optional[str] = None,
    chore_title: Optional[str] = None,
) -> Dict[str, Any]:
    """Records a task debrief / retrospective receipt and optionally spawns a follow-up chore card."""
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    now = int(time.time())

    # 1. Verify task exists
    cursor.execute("SELECT id, title FROM tasks WHERE id = ?", (task_id,))
    task_row = cursor.fetchone()
    if not task_row:
        conn.close()
        raise ValueError(f"Task {task_id} not found in kanban database")

    # 2. Record debrief event
    payload = {
        "summary": summary,
        "frictions": frictions or "None",
        "timestamp": now,
    }
    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'debrief', ?, ?)
        """,
        (task_id, json.dumps(payload), now),
    )

    # 3. If friction or chore_title is specified, auto-spawn a chore card
    chore_id = None
    chore_thread_id = None
    if chore_title or (frictions and frictions.strip() and frictions.strip().lower() != "none"):
        ctitle = chore_title or f"Address friction in {task_id}"
        cbody = (
            f"Auto-spawned from task debrief for {task_id}:\n\n"
            f"**Friction Report:**\n{frictions}\n\n"
            f"**Origin Task:** {task_row[1]} ({task_id})"
        )
        conn.commit()
        conn.close()
        chore_id, chore_thread_id = create_kanban_task(
            title=ctitle,
            body=cbody,
            priority=1,
            assignee=DEFAULT_ASSIGNEE,
        )
        conn = sqlite3.connect(KANBAN_DB_PATH)
        cursor = conn.cursor()

    # 4. Find Discord thread subscription for parent task
    cursor.execute(
        """
        SELECT thread_id FROM kanban_notify_subs
        WHERE task_id = ? AND platform = 'discord' LIMIT 1
        """,
        (task_id,),
    )
    sub_row = cursor.fetchone()
    thread_id = sub_row[0] if sub_row else None

    conn.commit()
    conn.close()

    # 5. Post Discord embed
    if thread_id:
        post_thread_debrief(
            thread_id=thread_id,
            task_id=task_id,
            summary=summary,
            frictions=frictions,
            chore_task_id=chore_id,
        )

    return {
        "task_id": task_id,
        "summary": summary,
        "frictions": frictions,
        "chore_id": chore_id,
        "chore_thread_id": chore_thread_id,
        "thread_id": thread_id,
    }


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


class KanbanGatewayHTTPHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP API handler for remote triage, task creation, and debrief."""

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path == "/health":
            self._send_json(200, {"status": "ok", "service": "kanban-gateway"})
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        post_body = self.rfile.read(content_length)
        try:
            payload = json.loads(post_body.decode("utf-8")) if post_body else {}
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON payload: {e}"})
            return

        if self.path == "/triage":
            text = payload.get("text", "")
            title = payload.get("title")
            context = payload.get("context", "")
            if not text:
                self._send_json(400, {"error": "Missing required field: text"})
                return
            res = triage_and_intake(text, title=title, context=context)
            self._send_json(200, res or {})

        elif self.path == "/create":
            title = payload.get("title", "")
            body = payload.get("body", "")
            priority = payload.get("priority", 1)
            assignee = payload.get("assignee", DEFAULT_ASSIGNEE)
            if not title or not body:
                self._send_json(400, {"error": "Missing title or body"})
                return
            task_id, thread_id = create_kanban_task(title, body, priority=priority, assignee=assignee)
            self._send_json(200, {"task_id": task_id, "thread_id": thread_id})

        elif self.path == "/debrief":
            task_id = payload.get("task_id", "")
            summary = payload.get("summary", "")
            friction = payload.get("friction", "")
            chore_title = payload.get("chore_title", "")
            if not task_id or not summary:
                self._send_json(400, {"error": "Missing task_id or summary"})
                return
            res = record_task_debrief(task_id, summary, frictions=friction, chore_title=chore_title)
            self._send_json(200, res)

        elif self.path == "/update":
            task_id = payload.get("task_id", "")
            status = payload.get("status", "")
            message = payload.get("message", "")
            if not task_id or not status:
                self._send_json(400, {"error": "Missing task_id or status"})
                return
            update_task_lifecycle(task_id, status, message)
            self._send_json(200, {"status": "updated", "task_id": task_id, "new_status": status})

        else:
            self._send_json(404, {"error": f"Endpoint {self.path} not found"})


def run_http_server(host: str = "0.0.0.0", port: int = 8643) -> None:
    """Runs the ThreadingHTTPServer for remote Kanban gateway intake."""
    server = ThreadingHTTPServer((host, port), KanbanGatewayHTTPHandler)
    logger.info("Serving Kanban Gateway HTTP API on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Kanban Gateway HTTP API...")
        server.server_close()


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

    # Debrief command
    p_debrief = subparsers.add_parser("debrief", help="Record task debrief/retro and optionally spawn a friction chore card")
    p_debrief.add_argument("task_id", help="Kanban Task ID (e.g. kb-a1b2c3d4)")
    p_debrief.add_argument("--summary", required=True, help="Summary of work delivered and testing")
    p_debrief.add_argument("--friction", default="", help="Frictions, tool errors, or failure modes encountered")
    p_debrief.add_argument("--chore-title", default="", help="Optional title for spawned follow-up chore card")

    # Serve command (HTTP API)
    p_serve = subparsers.add_parser("serve", help="Run HTTP API server for remote alert ingestion")
    p_serve.add_argument("--host", default="0.0.0.0", help="HTTP server listen host (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8643, help="HTTP server listen port (default: 8643)")

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
    elif args.command == "debrief":
        res = record_task_debrief(args.task_id, args.summary, frictions=args.friction, chore_title=args.chore_title)
        print(f"Recorded debrief for {args.task_id}")
        if res.get("chore_id"):
            print(f"Auto-spawned friction chore card: {res['chore_id']}")
    elif args.command == "serve":
        run_http_server(host=args.host, port=args.port)
    elif args.command == "list":
        list_active_tasks()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
