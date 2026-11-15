#!/usr/bin/env python3
"""kanban_jev_gateway.py - CLI Entrypoint for Jev Decision Gating & Discord Threaded Kanban Gateway.

Thin orchestrator for the modular `kanban_gateway` package following the 5-Section
SOLID Newspaper Layout and Table-Driven Dispatch standard.
Provides backward-compatible CLI commands and re-exports public API symbols.
"""

# ============================================================================
# 1. IMPORTS & CONSTANTS
# ============================================================================
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
import sys
from typing import Callable, Sequence

# Ensure kanban_gateway package directory is importable regardless of CWD or invocation path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from kanban_gateway import (
    DEFAULT_ASSIGNEE,
    DISCORD_CHANNEL_ID,
    DISCORD_USER_ID,
    JEV_MODEL,
    KANBAN_DB_PATH,
    MUNINN_API_KEY,
    MUNINN_API_URL,
    STATUS_CANONICAL,
    STATUS_COLORS,
    USER_AGENT,
    KanbanGatewayHTTPHandler,
    call_jev_raw,
    create_improvement_proposal,
    create_kanban_task,
    evaluate_auto_merge_safety,
    evaluate_debrief_acceptance,
    evaluate_friction_actionability,
    evaluate_jev_actionability,
    evaluate_task_complexity,
    handoff_kanban_task,
    list_active_tasks,
    logger,
    post_discord_thread,
    post_thread_debrief,
    post_thread_lifecycle_update,
    record_task_debrief,
    resolve_discord_env,
    resolve_muninn_endpoint,
    run_http_server,
    show_kanban_task,
    triage_and_intake,
    trigger_session_wrapup_chore,
    update_task_lifecycle,
    verify_pipeline_stage,
)


# ============================================================================
# 2. DOMAIN MODELS (Pure Data Contracts - Zero I/O)
# ============================================================================
@dataclass(frozen=True)
class VerifyCommandRequest:
    task_id: str
    passed: bool
    summary: str
    feedback: str
    pr_url: str
    max_retries: int


# ============================================================================
# 3. GATEWAYS / ADAPTERS (Delegated to kanban_gateway Package)
# ============================================================================
class KanbanCliGateway:
    """Thin adapter delegating CLI actions to the kanban_gateway package."""

    def execute_verify(self, req: VerifyCommandRequest) -> dict:
        return verify_pipeline_stage(
            task_id=req.task_id,
            passed=req.passed,
            summary=req.summary,
            feedback=req.feedback,
            pr_url=req.pr_url,
            max_retries=req.max_retries,
        )


# ============================================================================
# 4. PURE BUSINESS LOGIC & EVALUATORS (Zero I/O, Zero Print)
# ============================================================================
def resolve_verify_request(
    task_id: str,
    passed_flag: bool,
    failed_flag: bool,
    summary: str,
    feedback: str,
    pr_url: str,
    max_retries: int,
) -> VerifyCommandRequest | None:
    if not passed_flag and not failed_flag:
        return None
    return VerifyCommandRequest(
        task_id=task_id,
        passed=bool(passed_flag and not failed_flag),
        summary=summary,
        feedback=feedback,
        pr_url=pr_url,
        max_retries=max_retries,
    )


def format_debrief_output(task_id: str, result: dict, force_auto_fix: bool) -> str:
    lines = [f"Recorded debrief for {task_id}"]
    chore_id = result.get("chore_id")
    if chore_id:
        fg = result.get("friction_gate") or {}
        if force_auto_fix or fg.get("action") == "auto_fix_script":
            lines.append(f"Auto-spawned Tier 2 direct script fix (ready): {chore_id}")
        else:
            lines.append(f"Created Tier 3 improvement proposal (in_review): {chore_id}")
    return "\n".join(lines)


# ============================================================================
# 5. PRESENTATION & CLI ENTRYPOINT (Argparse & Table-Driven Dispatch)
# ============================================================================
def cmd_triage(args: argparse.Namespace) -> int:
    res = triage_and_intake(args.text, title=args.title, context=args.context)
    print(json.dumps(res, indent=2))
    return 0


def cmd_create(args: argparse.Namespace) -> int:
    task_id, thread_id = create_kanban_task(
        args.title,
        args.body,
        priority=args.priority,
        assignee=args.assignee,
        tier=args.tier,
        await_approval=args.await_approval,
    )
    print(f"Created task {task_id} with thread ID {thread_id}")
    return 0


def cmd_update(args: argparse.Namespace) -> int:
    update_task_lifecycle(args.task_id, args.status, args.message)
    print(f"Updated task {args.task_id} to status '{args.status}'")
    return 0


def cmd_debrief(args: argparse.Namespace) -> int:
    res = record_task_debrief(
        args.task_id,
        args.summary,
        frictions=args.friction,
        chore_title=args.chore_title,
        pr_url=args.pr_url,
        auto_fix=args.auto_fix,
    )
    print(format_debrief_output(args.task_id, res, args.auto_fix))
    return 0


def cmd_propose(args: argparse.Namespace) -> int:
    prop_id, prop_thread = create_improvement_proposal(
        title=args.title,
        friction=args.friction,
        proposal=args.proposal,
        target_specialist=args.target,
        origin_task_id=args.origin_task or None,
    )
    print(f"Created Tier 3 proposal {prop_id} (in_review) with #info thread ID {prop_thread}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    show_kanban_task(args.task_id, as_json=args.json)
    return 0


def cmd_handoff(args: argparse.Namespace) -> int:
    res = handoff_kanban_task(
        args.task_id,
        args.to_assignee,
        args.summary,
        pr_url=args.pr_url,
        tier=args.tier,
    )
    print(
        f"Handed off {args.task_id} from '{res['from_assignee']}' to '{res['to_assignee']}' ({res['model_override']})"
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    req = resolve_verify_request(
        task_id=args.task_id,
        passed_flag=args.passed,
        failed_flag=args.failed,
        summary=args.summary,
        feedback=args.feedback,
        pr_url=args.pr_url,
        max_retries=args.max_retries,
    )
    if req is None:
        sys.stderr.write("ERROR: Either --pass or --fail must be specified.\n")
        sys.exit(1)
    res = KanbanCliGateway().execute_verify(req)
    print(json.dumps(res, indent=2))
    return 0


def cmd_complexity(args: argparse.Namespace) -> int:
    res = evaluate_task_complexity(args.title, args.body)
    print(json.dumps(res, indent=2))
    return 0


def cmd_acceptance_gate(args: argparse.Namespace) -> int:
    res = evaluate_debrief_acceptance(args.title, args.contract, args.summary)
    print(json.dumps(res, indent=2))
    return 0


def cmd_auto_merge_gate(args: argparse.Namespace) -> int:
    res = evaluate_auto_merge_safety(args.title, args.summary, args.pr_url)
    print(json.dumps(res, indent=2))
    return 0


def cmd_friction_gate(args: argparse.Namespace) -> int:
    res = evaluate_friction_actionability(args.text)
    print(json.dumps(res, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    run_http_server(host=args.host, port=args.port)
    return 0


def cmd_list(_args: argparse.Namespace) -> int:
    list_active_tasks()
    return 0


COMMAND_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "triage": cmd_triage,
    "create": cmd_create,
    "update": cmd_update,
    "debrief": cmd_debrief,
    "propose": cmd_propose,
    "show": cmd_show,
    "handoff": cmd_handoff,
    "verify": cmd_verify,
    "complexity": cmd_complexity,
    "acceptance-gate": cmd_acceptance_gate,
    "auto-merge-gate": cmd_auto_merge_gate,
    "friction-gate": cmd_friction_gate,
    "serve": cmd_serve,
    "list": cmd_list,
    "ls": cmd_list,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Jev Decision Gating & Discord Threaded Kanban Gateway")
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    p_triage = subparsers.add_parser("triage", help="Triage event with Jev; if actionable, create card + thread")
    p_triage.add_argument("text", help="Event or alert text to evaluate")
    p_triage.add_argument("--title", help="Optional explicit title")
    p_triage.add_argument("--context", default="", help="Optional system context")

    p_create = subparsers.add_parser("create", help="Directly create Kanban card and Discord thread")
    p_create.add_argument("--title", required=True, help="Task title")
    p_create.add_argument("--body", required=True, help="Task description")
    p_create.add_argument("--priority", type=int, default=1, help="Task priority (0-3)")
    p_create.add_argument("--assignee", default=DEFAULT_ASSIGNEE, help="Assignee profile")
    p_create.add_argument(
        "--tier",
        choices=["flash_fast", "frontier_deep"],
        default=None,
        help="Optional complexity tier override",
    )
    p_create.add_argument(
        "--await-approval",
        action="store_true",
        default=False,
        help="Park task in 'in_review' awaiting operator approval before execution",
    )

    p_update = subparsers.add_parser("update", help="Update task lifecycle status and notify thread")
    p_update.add_argument("--task-id", required=True, help="Task ID (e.g. kb-a1b2c3d4)")
    p_update.add_argument(
        "--status",
        required=True,
        choices=["todo", "ready", "in_progress", "in_review", "done", "blocked"],
    )
    p_update.add_argument("--message", required=True, help="Status update or progress message")

    p_debrief = subparsers.add_parser(
        "debrief",
        help="Record task debrief/retro and route follow-ups (Tier 2 auto-fix vs Tier 3 #info proposal)",
    )
    p_debrief.add_argument("task_id", help="Kanban Task ID (e.g. kb-a1b2c3d4)")
    p_debrief.add_argument("--summary", required=True, help="Summary of work delivered and testing")
    p_debrief.add_argument("--friction", default="", help="Frictions, tool errors, or failure modes encountered")
    p_debrief.add_argument("--chore-title", default="", help="Optional title for spawned follow-up card")
    p_debrief.add_argument("--pr-url", default="", help="Optional GitHub PR URL requiring review")
    p_debrief.add_argument(
        "--auto-fix",
        action="store_true",
        default=False,
        help="Force Tier 2 direct autonomous script fix ('ready') instead of Tier 3 proposal",
    )

    p_handoff = subparsers.add_parser(
        "handoff",
        help="Hand off task to another Court of Odin specialist in the pipeline",
    )
    p_handoff.add_argument("task_id", help="Kanban Task ID (e.g. kb-a1b2c3d4)")
    p_handoff.add_argument(
        "--to",
        required=True,
        dest="to_assignee",
        help="Target specialist (huginn, sindri, forseti, vor, saga)",
    )
    p_handoff.add_argument("--summary", required=True, help="Compact state digest + file/PR paths for next specialist")
    p_handoff.add_argument("--pr-url", default="", help="Optional PR or branch reference")
    p_handoff.add_argument(
        "--tier",
        choices=["flash_fast", "frontier_deep"],
        default=None,
        help="Optional complexity tier override for target specialist",
    )

    p_verify = subparsers.add_parser("verify", help="Evaluate verification gate and trigger state transition")
    p_verify.add_argument("task_id", help="Kanban Task ID (e.g. kb-a1b2c3d4)")
    p_verify.add_argument("--pass", dest="passed", action="store_true", default=False, help="Verification gate passed")
    p_verify.add_argument("--fail", dest="failed", action="store_true", default=False, help="Verification gate failed")
    p_verify.add_argument("--summary", default="", help="Summary of passed verification and test checks")
    p_verify.add_argument("--feedback", default="", help="Failure feedback, error diff, or visual regression details")
    p_verify.add_argument("--pr-url", default="", help="Optional PR or branch reference")
    p_verify.add_argument("--max-retries", type=int, default=3, help="Max retry cycles before circuit breaker trips (default: 3)")

    p_serve = subparsers.add_parser("serve", help="Run HTTP API server for remote alert ingestion")
    p_serve.add_argument("--host", default="0.0.0.0", help="HTTP server listen host (default: 0.0.0.0)")
    p_serve.add_argument("--port", type=int, default=8643, help="HTTP server listen port (default: 8643)")

    p_complexity = subparsers.add_parser("complexity", help="Evaluate task complexity and model routing tier")
    p_complexity.add_argument("--title", required=True, help="Task title")
    p_complexity.add_argument("--body", default="", help="Task body")

    p_agate = subparsers.add_parser("acceptance-gate", help="Evaluate debrief contract acceptance")
    p_agate.add_argument("--title", required=True, help="Task title")
    p_agate.add_argument("--contract", required=True, help="Task acceptance contract / body")
    p_agate.add_argument("--summary", required=True, help="Worker debrief summary")

    p_mgate = subparsers.add_parser("auto-merge-gate", help="Evaluate PR auto-merge safety")
    p_mgate.add_argument("--title", required=True, help="Task title")
    p_mgate.add_argument("--summary", required=True, help="Debrief summary")
    p_mgate.add_argument("--pr-url", required=True, help="PR URL")

    p_fgate = subparsers.add_parser("friction-gate", help="Classify friction actionability")
    p_fgate.add_argument("--text", required=True, help="Friction text")

    subparsers.add_parser("list", aliases=["ls"], help="List active Kanban cards and mapped threads")

    p_show = subparsers.add_parser("show", help="Inspect a Kanban card and its recent events")
    p_show.add_argument("task_id", help="Kanban Task ID (e.g. kb-a1b2c3d4)")
    p_show.add_argument("--json", action="store_true", help="Output task details as JSON")

    p_propose = subparsers.add_parser(
        "propose",
        help="Create a Tier 3 continuous improvement proposal thread in #info (in_review)",
    )
    p_propose.add_argument("--title", required=True, help="Short proposal title")
    p_propose.add_argument("--friction", required=True, help="Friction or failure mode observed")
    p_propose.add_argument("--proposal", required=True, help="Proposed skill, script, or rule change specification")
    p_propose.add_argument("--target", default="vor", help="Recommended specialist upon approval (vor or sindri)")
    p_propose.add_argument("--origin-task", default="", help="Optional originating Kanban task ID")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = COMMAND_HANDLERS.get(args.command or "")
    if not handler:
        parser.print_help()
        return 0
    return handler(args)


if __name__ == "__main__":
    main()
