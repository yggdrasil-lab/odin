"""server.py - HTTP API Server for remote Kanban task ingestion, triage, linking, and debrief."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from typing import Any, Callable, Dict

from .config import DEFAULT_ASSIGNEE, logger
from .engine import (
    create_kanban_task,
    link_kanban_tasks,
    record_task_debrief,
    triage_and_intake,
    update_task_lifecycle,
)


def _coerce_depends_on(raw: Any) -> list[str] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(item, str) for item in raw):
        return list(raw)
    raise ValueError("depends_on/blocked_by must be a string or list of strings")


class KanbanGatewayHTTPHandler(BaseHTTPRequestHandler):
    """Lightweight HTTP API handler for remote triage, task creation, linking, and debrief."""

    def log_message(self, format: str, *args: Any) -> None:
        logger.info("%s - - [%s] %s", self.client_address[0], self.log_date_time_string(), format % args)

    def _send_json(self, status_code: int, data: Dict[str, Any]) -> None:
        body = json.dumps(data).encode("utf-8")
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _handle_triage(self, payload: Dict[str, Any]) -> None:
        text = payload.get("text", "")
        if not text:
            self._send_json(400, {"error": "Missing required field: text"})
            return
        res = triage_and_intake(text, title=payload.get("title"), context=payload.get("context", ""))
        self._send_json(200, res or {})

    def _handle_create(self, payload: Dict[str, Any]) -> None:
        title = payload.get("title", "")
        body = payload.get("body", "")
        if not title or not body:
            self._send_json(400, {"error": "Missing title or body"})
            return
        try:
            deps: list[str] = []
            for key in ("depends_on", "blocked_by"):
                parsed = _coerce_depends_on(payload.get(key))
                if parsed:
                    deps.extend(parsed)
            task_id, thread_id = create_kanban_task(
                title,
                body,
                priority=payload.get("priority", 1),
                assignee=payload.get("assignee", DEFAULT_ASSIGNEE),
                tier=payload.get("tier"),
                await_approval=bool(payload.get("await_approval", False)),
                depends_on=deps or None,
                status=payload.get("status"),
            )
            self._send_json(200, {"task_id": task_id, "thread_id": thread_id})
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_link(self, payload: Dict[str, Any]) -> None:
        parent_id = payload.get("parent_id", "")
        child_id = payload.get("child_id", "")
        if not parent_id or not child_id:
            self._send_json(400, {"error": "Missing parent_id or child_id"})
            return
        try:
            res = link_kanban_tasks(parent_id, child_id)
            self._send_json(200, res)
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})

    def _handle_debrief(self, payload: Dict[str, Any]) -> None:
        task_id = payload.get("task_id", "")
        summary = payload.get("summary", "")
        if not task_id or not summary:
            self._send_json(400, {"error": "Missing task_id or summary"})
            return
        res = record_task_debrief(
            task_id,
            summary,
            frictions=payload.get("friction", ""),
            chore_title=payload.get("chore_title", ""),
            pr_url=payload.get("pr_url", ""),
        )
        self._send_json(200, res)

    def _handle_update(self, payload: Dict[str, Any]) -> None:
        task_id = payload.get("task_id", "")
        status = payload.get("status", "")
        if not task_id or not status:
            self._send_json(400, {"error": "Missing task_id or status"})
            return
        ok = update_task_lifecycle(
            task_id,
            status,
            payload.get("message", ""),
            force=bool(payload.get("force", False)),
        )
        if not ok:
            self._send_json(409, {"status": "rejected", "task_id": task_id, "new_status": status})
            return
        self._send_json(200, {"status": "updated", "task_id": task_id, "new_status": status})

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

        handlers: dict[str, Callable[[Dict[str, Any]], None]] = {
            "/triage": self._handle_triage,
            "/create": self._handle_create,
            "/link": self._handle_link,
            "/debrief": self._handle_debrief,
            "/update": self._handle_update,
        }
        handler = handlers.get(self.path)
        if handler is None:
            self._send_json(404, {"error": f"Endpoint {self.path} not found"})
            return
        handler(payload)


def run_http_server(host: str = "0.0.0.0", port: int = 8643) -> None:
    """Runs the ThreadingHTTPServer for remote Kanban gateway intake."""
    server = ThreadingHTTPServer((host, port), KanbanGatewayHTTPHandler)
    logger.info("Serving Kanban Gateway HTTP API on http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Kanban Gateway HTTP API...")
        server.server_close()
