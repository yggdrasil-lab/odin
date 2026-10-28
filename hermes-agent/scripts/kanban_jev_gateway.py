#!/usr/bin/env python3
"""kanban_jev_gateway.py - CLI Entrypoint for Jev Decision Gating & Discord Threaded Kanban Gateway.

Thin orchestrator for the modular `kanban_gateway` package.
Provides backward-compatible CLI commands and re-exports public API symbols.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Ensure kanban_gateway package directory is importable regardless of CWD or invocation path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kanban_gateway import (
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
    KanbanGatewayHTTPHandler,
    call_jev_raw,
    create_kanban_task,
    evaluate_auto_merge_safety,
    evaluate_debrief_acceptance,
    evaluate_friction_actionability,
    evaluate_jev_actionability,
    evaluate_task_complexity,
    list_active_tasks,
    logger,
    post_discord_thread,
    post_thread_debrief,
    post_thread_lifecycle_update,
    record_task_debrief,
    resolve_discord_env,
    resolve_muninn_endpoint,
    run_http_server,
    triage_and_intake,
    trigger_session_wrapup_chore,
    update_task_lifecycle,
)


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
    p_update.add_argument("--status", required=True, choices=["todo", "ready", "in_progress", "in_review", "done", "blocked"])
    p_update.add_argument("--message", required=True, help="Status update or progress message")

    # Debrief command
    p_debrief = subparsers.add_parser("debrief", help="Record task debrief/retro and optionally spawn a friction chore card")
    p_debrief.add_argument("task_id", help="Kanban Task ID (e.g. kb-a1b2c3d4)")
    p_debrief.add_argument("--summary", required=True, help="Summary of work delivered and testing")
    p_debrief.add_argument("--friction", default="", help="Frictions, tool errors, or failure modes encountered")
    p_debrief.add_argument("--chore-title", default="", help="Optional title for spawned follow-up chore card")
    p_debrief.add_argument("--pr-url", default="", help="Optional GitHub PR URL requiring review")

    # Serve command (HTTP API)
    p_serve = subparsers.add_parser("serve", help="Run HTTP API server for remote alert ingestion")
    p_serve.add_argument("--host", default="0.0.0.0", help="HTTP server listen host (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8643, help="HTTP server listen port (default: 8643)")

    # Complexity routing command
    p_complexity = subparsers.add_parser("complexity", help="Evaluate task complexity and model routing tier")
    p_complexity.add_argument("--title", required=True, help="Task title")
    p_complexity.add_argument("--body", default="", help="Task body")

    # Acceptance gate command
    p_agate = subparsers.add_parser("acceptance-gate", help="Evaluate debrief contract acceptance")
    p_agate.add_argument("--title", required=True, help="Task title")
    p_agate.add_argument("--contract", required=True, help="Task acceptance contract / body")
    p_agate.add_argument("--summary", required=True, help="Worker debrief summary")

    # Auto-merge gate command
    p_mgate = subparsers.add_parser("auto-merge-gate", help="Evaluate PR auto-merge safety")
    p_mgate.add_argument("--title", required=True, help="Task title")
    p_mgate.add_argument("--summary", required=True, help="Debrief summary")
    p_mgate.add_argument("--pr-url", required=True, help="PR URL")

    # Friction gate command
    p_fgate = subparsers.add_parser("friction-gate", help="Classify friction actionability")
    p_fgate.add_argument("--text", required=True, help="Friction text")

    # List command
    subparsers.add_parser("list", aliases=["ls"], help="List active Kanban cards and mapped threads")

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
        res = record_task_debrief(args.task_id, args.summary, frictions=args.friction, chore_title=args.chore_title, pr_url=args.pr_url)
        print(f"Recorded debrief for {args.task_id}")
        if res.get("chore_id"):
            print(f"Auto-spawned friction chore card: {res['chore_id']}")
    elif args.command == "complexity":
        res = evaluate_task_complexity(args.title, args.body)
        print(json.dumps(res, indent=2))
    elif args.command == "acceptance-gate":
        res = evaluate_debrief_acceptance(args.title, args.contract, args.summary)
        print(json.dumps(res, indent=2))
    elif args.command == "auto-merge-gate":
        res = evaluate_auto_merge_safety(args.title, args.summary, args.pr_url)
        print(json.dumps(res, indent=2))
    elif args.command == "friction-gate":
        res = evaluate_friction_actionability(args.text)
        print(json.dumps(res, indent=2))
    elif args.command == "serve":
        run_http_server(host=args.host, port=args.port)
    elif args.command in ("list", "ls"):
        list_active_tasks()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
