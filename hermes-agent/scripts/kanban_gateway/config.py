"""config.py - Configuration, environment resolution, and constants for Kanban Gateway."""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Tuple

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


def resolve_muninn_endpoint() -> str:
    """Resolves Muninn endpoint strictly via Swarm overlay network (aether-net) or explicit MUNINN_API_URL."""
    if os.environ.get("MUNINN_API_URL"):
        return os.environ["MUNINN_API_URL"]

    try:
        import socket
        socket.gethostbyname("muninn")
        return "http://muninn:4000/v1/chat/completions"
    except Exception as e:
        raise RuntimeError(
            "Cannot reach Muninn on Swarm overlay network (aether-net): host 'muninn' resolution failed. "
            "Internal overlay access is required. Set MUNINN_API_URL explicitly if routing via custom endpoint."
        ) from e


try:
    MUNINN_API_URL = resolve_muninn_endpoint()
except Exception:
    MUNINN_API_URL = ""

MUNINN_API_KEY = (
    os.environ.get("MUNINN_API_KEY")
    or os.environ.get("MUNINN_MASTER_KEY")
    or os.environ.get("OPENAI_API_KEY")
    or os.environ.get("LITELLM_MASTER_KEY")
    or ""
)
JEV_MODEL = os.environ.get("JEV_MODEL", "jev-decision")


def resolve_discord_env() -> Tuple[str, str, str]:
    """Resolves Discord webhook credentials from process env or odin_huginn-gateway container."""
    webhook = os.environ.get("KANBAN_DISCORD_WEBHOOK_URL", "")
    channel = os.environ.get("KANBAN_DISCORD_CHANNEL_ID", "")
    uid = os.environ.get("DISCORD_USER_ID") or os.environ.get("DISCORD_ALLOWED_USERS", "").split(",")[0].strip()
    if webhook and channel and uid:
        return webhook, channel, uid
    try:
        res = subprocess.run(
            [
                "docker",
                "service",
                "inspect",
                "--format",
                "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}",
                "odin_huginn-gateway",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if line.startswith("KANBAN_DISCORD_WEBHOOK_URL=") and not webhook:
                    webhook = line.split("=", 1)[1]
                elif line.startswith("KANBAN_DISCORD_CHANNEL_ID=") and not channel:
                    channel = line.split("=", 1)[1]
                elif line.startswith("DISCORD_USER_ID=") and not uid:
                    uid = line.split("=", 1)[1]
    except Exception:
        pass
    return webhook, channel, uid


DISCORD_WEBHOOK_URL, DISCORD_CHANNEL_ID, DISCORD_USER_ID = resolve_discord_env()
DEFAULT_ASSIGNEE = os.environ.get("KANBAN_DEFAULT_ASSIGNEE", "swarm-worker")
USER_AGENT = "Yggdrasil-KanbanGateway/1.0 (DiscordBot)"

# Severity / Status colors for Discord embeds
STATUS_COLORS = {
    "TODO": 3447003,          # Blue
    "READY": 3447003,         # Blue
    "IN_PROGRESS": 15844351,  # Amber
    "RUNNING": 15844351,      # Amber
    "IN_REVIEW": 15105570,    # Orange
    "DONE": 3066993,          # Green
    "BLOCKED": 15158332,      # Red
    "DEBRIEF": 10181046,      # Purple
}

STATUS_CANONICAL = {
    "todo": "todo",
    "ready": "ready",
    "running": "in_progress",
    "in_progress": "in_progress",
    "in_review": "in_review",
    "done": "done",
    "completed": "done",
    "blocked": "blocked",
}
