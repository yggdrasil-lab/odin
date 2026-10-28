"""kanban_gateway - Modular package for Jev Decision Gating & Discord Threaded Kanban Gateway."""

from __future__ import annotations

from .config import (
    DEFAULT_ASSIGNEE,
    DISCORD_CHANNEL_ID,
    DISCORD_USER_ID,
    DISCORD_WEBHOOK_URL,
    JEV_MODEL,
    KANBAN_DB_PATH,
    MUNINN_API_KEY,
    MUNINN_API_URL,
    STATUS_CANONICAL,
    STATUS_COLORS,
    USER_AGENT,
    logger,
    resolve_discord_env,
    resolve_muninn_endpoint,
)
from .discord import (
    post_discord_thread,
    post_thread_debrief,
    post_thread_lifecycle_update,
)
from .engine import (
    create_kanban_task,
    list_active_tasks,
    record_task_debrief,
    triage_and_intake,
    trigger_session_wrapup_chore,
    update_task_lifecycle,
)
from .jev import (
    call_jev_raw,
    evaluate_auto_merge_safety,
    evaluate_debrief_acceptance,
    evaluate_friction_actionability,
    evaluate_jev_actionability,
    evaluate_task_complexity,
)
from .server import (
    KanbanGatewayHTTPHandler,
    run_http_server,
)

__all__ = [
    "DEFAULT_ASSIGNEE",
    "DISCORD_CHANNEL_ID",
    "DISCORD_USER_ID",
    "DISCORD_WEBHOOK_URL",
    "JEV_MODEL",
    "KANBAN_DB_PATH",
    "MUNINN_API_KEY",
    "MUNINN_API_URL",
    "STATUS_CANONICAL",
    "STATUS_COLORS",
    "USER_AGENT",
    "KanbanGatewayHTTPHandler",
    "call_jev_raw",
    "create_kanban_task",
    "evaluate_auto_merge_safety",
    "evaluate_debrief_acceptance",
    "evaluate_friction_actionability",
    "evaluate_jev_actionability",
    "evaluate_task_complexity",
    "list_active_tasks",
    "logger",
    "post_discord_thread",
    "post_thread_debrief",
    "post_thread_lifecycle_update",
    "record_task_debrief",
    "resolve_discord_env",
    "resolve_muninn_endpoint",
    "run_http_server",
    "triage_and_intake",
    "trigger_session_wrapup_chore",
    "update_task_lifecycle",
]
