"""server.py - HTTP API Server for remote Kanban task ingestion, triage, and debrief."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any, Dict

from .config import DEFAULT_ASSIGNEE, logger
from .engine import (
    create_kanban_task,
    record_task_debrief,
    triage_and_intake,
    update_task_lifecycle,
)


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
            pr_url = payload.get("pr_url", "")
            if not task_id or not summary:
                self._send_json(400, {"error": "Missing task_id or summary"})
                return
            res = record_task_debrief(task_id, summary, frictions=friction, chore_title=chore_title, pr_url=pr_url)
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
