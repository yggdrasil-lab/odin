"""engine.py - Core Kanban database operations, task lifecycle state machine, and retrospective logic."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional, Tuple
import uuid

from .config import (
    DEFAULT_ASSIGNEE,
    DISCORD_BOT_TOKEN,
    DISCORD_CHANNEL_ID,
    DISCORD_USER_ID,
    KANBAN_DB_PATH,
    SPECIALIST_MODELS,
    STATUS_CANONICAL,
    logger,
    normalize_assignee,
)
from .discord import (
    post_discord_thread,
    post_thread_debrief,
    post_thread_lifecycle_update,
)
from .jev import (
    evaluate_auto_merge_safety,
    evaluate_debrief_acceptance,
    evaluate_debrief_gates_batch,
    evaluate_friction_actionability,
    evaluate_jev_actionability,
    evaluate_task_complexity,
    evaluate_triage_and_complexity,
)


def create_kanban_task(
    title: str,
    body: str,
    priority: int = 1,
    assignee: Optional[str] = None,
    workspace_kind: str = "scratch",
    notify_discord: bool = True,
    precomputed_complexity: Optional[Dict[str, Any]] = None,
) -> Tuple[str, Optional[str]]:
    """Inserts a new task into kanban.db and provisions its Discord thread subscription.

    Returns:
        (task_id, discord_thread_id)
    """
    complexity = precomputed_complexity or evaluate_task_complexity(title, body)
    if assignee and assignee != DEFAULT_ASSIGNEE:
        resolved_assignee = normalize_assignee(assignee)
    else:
        resolved_assignee = normalize_assignee(complexity.get("recommended_assignee") or DEFAULT_ASSIGNEE)

    if complexity["tier"] == "human_pairing" and (not assignee or assignee == DEFAULT_ASSIGNEE):
        resolved_assignee = "raymund"

    model_override = None
    provider_override = None
    if resolved_assignee == "raymund":
        model_override = None
        provider_override = None
    elif resolved_assignee == "sindri" or complexity["tier"] == "frontier_deep":
        model_override = "gemini-4-argon-high"
        provider_override = "antigravity-subscription-directsdk"
    else:
        model_override = complexity.get("recommended_model") or SPECIALIST_MODELS.get(
            resolved_assignee, "gemini-3.8-flash-high"
        )
        provider_override = "antigravity-subscription-directsdk"

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
            created_at, workspace_kind, consecutive_failures, goal_mode, block_recurrences,
            model_override, provider_override
        ) VALUES (?, ?, ?, ?, 'ready', ?, 'jev-gateway', ?, ?, 0, 0, 0, ?, ?)
        """,
        (
            task_id,
            full_title,
            body,
            resolved_assignee,
            priority,
            now,
            workspace_kind,
            model_override,
            provider_override,
        ),
    )

    # 2. Record created event
    created_payload = {
        "source": "jev-gateway",
        "title": full_title,
        "complexity": complexity,
    }
    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'created', ?, ?)
        """,
        (task_id, json.dumps(created_payload), now),
    )

    thread_id = None
    if notify_discord and DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID:
        try:
            fields = [
                {"name": "Status", "value": "`READY`", "inline": True},
                {"name": "Priority", "value": f"`P{priority}`", "inline": True},
                {"name": "Assignee", "value": f"`{resolved_assignee}`", "inline": True},
                {"name": "Model Tier", "value": f"`{complexity['tier']}` ({complexity['recommended_model']})", "inline": True},
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


def create_improvement_proposal(
    title: str,
    friction: str,
    proposal: str,
    target_specialist: str = "vor",
    origin_task_id: Optional[str] = None,
    notify_discord: bool = True,
) -> Tuple[str, Optional[str]]:
    """Creates a Tier 3 continuous improvement proposal card in 'in_review' status assigned to raymund
    and provisions a dedicated Discord thread in #info for focused human analysis and decision.
    """
    resolved_target = normalize_assignee(target_specialist or "vor")
    if resolved_target == "raymund":
        resolved_target = "vor"

    recommended_model = (
        "gemini-4-argon-high"
        if resolved_target == "sindri"
        else SPECIALIST_MODELS.get(resolved_target, "gemini-3.8-flash-high")
    )

    short_id = uuid.uuid4().hex[:8]
    task_id = f"kb-{short_id}"
    clean_raw_title = title.strip()
    if clean_raw_title.lower().startswith("proposal:"):
        clean_raw_title = clean_raw_title[len("proposal:"):].strip()
    elif clean_raw_title.startswith("💡 Proposal:"):
        clean_raw_title = clean_raw_title[len("💡 Proposal:"):].strip()
    full_title = f"[KB-{short_id}] 💡 Proposal: {clean_raw_title}"
    now = int(time.time())

    body = (
        f"### Tier 3 Continuous Improvement Proposal\n"
        f"- **Origin Task:** `{origin_task_id or 'Session / Wrap-up Audit'}`\n"
        f"- **Recommended Specialist:** `{resolved_target}` (`{recommended_model}`)\n"
        f"- **Status:** `in_review` (Parked for human analysis & decision)\n\n"
        f"#### 1. Friction & Root Cause\n"
        f"{friction.strip() or 'Identified during session retrospective.'}\n\n"
        f"#### 2. Proposed Skill, Script, or Rule Change\n"
        f"{proposal.strip() or clean_raw_title}\n\n"
        f"#### 3. Decision & Execution Commands\n"
        f"- **Approve & Dispatch to `{resolved_target}`**:\n"
        f"  `kanban-gateway handoff {task_id} --to {resolved_target} --summary \"Approved for implementation\"`\n"
        f"- **Close / Defer Proposal**:\n"
        f"  `kanban-gateway update --task-id {task_id} --status done --message \"Deferred / Rejected\"`"
    )

    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO tasks (
            id, title, body, assignee, status, priority, created_by,
            created_at, workspace_kind, consecutive_failures, goal_mode, block_recurrences,
            model_override, provider_override
        ) VALUES (?, ?, ?, 'raymund', 'in_review', 2, 'jev-gateway', ?, 'scratch', 0, 0, 0, NULL, NULL)
        """,
        (task_id, full_title, body, now),
    )

    proposal_payload = {
        "source": "jev-gateway-proposal",
        "title": full_title,
        "origin_task_id": origin_task_id,
        "target_specialist": resolved_target,
        "recommended_model": recommended_model,
    }
    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'proposal_created', ?, ?)
        """,
        (task_id, json.dumps(proposal_payload), now),
    )

    thread_id = None
    if notify_discord and DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID:
        try:
            fields = [
                {"name": "Status", "value": "`IN_REVIEW` (Awaiting Decision)", "inline": True},
                {"name": "Proposal ID", "value": f"`{task_id}`", "inline": True},
                {"name": "Target Specialist", "value": f"`{resolved_target}` (`{recommended_model}`)", "inline": True},
                {"name": "Origin", "value": f"`{origin_task_id or 'Wrap-up / Session'}`", "inline": True},
            ]
            thread_id = post_discord_thread(
                title=full_title,
                thread_name=full_title,
                description=body,
                fields=fields,
            )
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
            logger.error("Failed to provision Discord proposal thread for %s: %s", task_id, e)

    conn.commit()
    conn.close()

    logger.info("Created Tier 3 Proposal %s in 'in_review' (Discord Thread: %s)", task_id, thread_id)
    return task_id, thread_id


def trigger_session_wrapup_chore(task_id: str, orig_title: str, thread_id: Optional[str] = None) -> Optional[str]:
    """Auto-spawns an autonomous session wrap-up chore when a dev or engineering task transitions to done."""
    clean_title = orig_title.lower()
    # Recursion guard: do not trigger wrap-up for wrap-up chores, friction chores, proposals, or triage tasks
    if (
        clean_title.startswith("wrap-up")
        or clean_title.startswith("auto-sync")
        or "address friction" in clean_title
        or "session wrap-up" in clean_title
        or "proposal:" in clean_title
    ):
        logger.info("Skipping wrap-up trigger for maintenance/wrap-up/proposal task %s", task_id)
        return None

    # Retrieve debrief event payload (summary, frictions, pr_url) if present
    pr_url = None
    summary = ""
    frictions = "None"
    try:
        conn = sqlite3.connect(KANBAN_DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'debrief' ORDER BY id DESC LIMIT 1",
            (task_id,),
        )
        row = cursor.fetchone()
        if row and row[0]:
            payload = json.loads(row[0])
            pr_url = payload.get("pr_url")
            summary = payload.get("summary", "")
            frictions = payload.get("frictions", "None")
        conn.close()
    except Exception as e:
        logger.warning("Failed to fetch debrief details for wrap-up trigger on %s: %s", task_id, e)

    # Detect target stack if mentioned in title or PR URL
    stack_name = ""
    for known in ("poseidon", "odin", "kratos", "charon", "argus", "apollo", "mimir", "olympus", "hephaestus", "comics", "cerberus", "palworld"):
        if known in clean_title or (pr_url and known in pr_url.lower()):
            stack_name = known.capitalize()
            break

    stack_instruction = ""
    if stack_name:
        stack_instruction = (
            f"- [ ] **Gate 2B (Canonical Domain Notes)**: Open and update `second-brain/Areas/90-Infrastructure/{stack_name}/{stack_name} Stack.md`:\n"
            f"  - Add newly deployed services, ports, and volumes to the Services and Volumes tables.\n"
            f"  - Update the Mermaid architecture diagram edges if communication flows changed.\n"
            f"  - Add an entry under Historical Timeline (PR {pr_url or 'merged'}).\n"
        )
    else:
        stack_instruction = (
            "- [ ] **Gate 2B (Canonical Domain Notes)**: Inspect affected stack/repo and update its canonical domain note under `second-brain/Areas/90-Infrastructure/` if applicable.\n"
        )

    chore_title = f"Wrap-up: Session wrap-up and docs sync for {orig_title}"
    chore_body = (
        f"Automated post-completion session wrap-up for task `{task_id}`:\n\n"
        f"**Origin PR:** {pr_url or 'N/A'}\n"
        f"**Origin Summary:** {summary or orig_title}\n"
        f"**Reported Frictions:** {frictions}\n\n"
        f"Execute the full canonical wrap-up protocol (`${{HERMES_SKILLS_PATH:-/opt/odin/hermes/skills}}/productivity/session-wrapup/SKILL.md`):\n\n"
        f"### Protocol Execution Checklist:\n"
        f"- [ ] **Gate 1 & Gate 2A (3-Tier Bounded Continuous Improvement)**: Audit the reported frictions and execution trace. Apply Tier 1/2 doc/reference syncs inline (keeping all skills `<= 4,000 B` body and `<= 5,000 B` view). For any Tier 3 change (new script, new skill, or rule modification), NEVER mutate rules/skills unreviewed: submit a proposal via `kanban-gateway propose --title \"...\" --friction \"...\" --proposal \"...\" --origin-task {task_id}` (skip if a proposal thread was already spawned during debrief).\n"
        f"{stack_instruction}"
        f"- [ ] **Gate 2C (Active Effort Audit)**: Check and update matching note in `second-brain/Efforts/` if linked to an active project.\n"
        f"- [ ] **Gate 2D (Mnemosyne Memory)**: Query and record durable architectural facts via Mnemosyne tools or host CLI.\n"
        f"- [ ] **Step 3 (Obsidian Daily Journal)**: Append a complete engineering session block into `second-brain/Journal/YYYY/MM/YYYY-MM-DD.md` (respecting the 06:00 Auckland sleep boundary). Include Dashboard, Narrative, Retrospective table (Friction / Root Cause / Hardening Action), and Next Session Handoff.\n"
        f"- [ ] **Step 4 (Readiness Probe & Cleanup)**: Run `python3 ${{HERMES_SKILLS_PATH:-/opt/odin/hermes/skills}}/productivity/session-wrapup/scripts/check_wrapup_readiness.py` to verify formatting, structure, and zero em/en dashes.\n"
        f"- [ ] **Completion**: Run `kanban-gateway debrief <THIS_TASK_ID> --summary \"Session wrap-up complete for {task_id}\"` and call `kanban_complete`.\n"
    )

    try:
        chore_id, chore_thread_id = create_kanban_task(
            title=chore_title,
            body=chore_body,
            priority=1,
            assignee="vor",
        )
        logger.info("Auto-spawned session wrap-up chore %s (vor) for completed task %s", chore_id, task_id)

        if thread_id:
            post_thread_lifecycle_update(
                thread_id=thread_id,
                status="wrapup_triggered",
                message=f"📌 Triggered automated session wrap-up chore `{chore_id}` (`vor`) to sync infrastructure notes, journal, and self-improvement hardening.",
            )
        return chore_id
    except Exception as e:
        logger.error("Failed to auto-spawn session wrap-up chore for %s: %s", task_id, e)
        return None


def handoff_kanban_task(
    task_id: str,
    to_assignee: str,
    summary: str,
    pr_url: str = "",
) -> Dict[str, Any]:
    """Transitions a Kanban card to the next specialist in an async pipeline without conversational chat."""
    resolved_to = normalize_assignee(to_assignee)
    now = int(time.time())

    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT title, body, assignee FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Task {task_id} not found in kanban database")

    orig_title, orig_body, from_assignee = row[0] or "", row[1] or "", row[2] or "unknown"

    handoff_block = (
        f"\n\n---\n"
        f"### Pipeline Handoff ({from_assignee} -> {resolved_to})\n"
        f"{summary.strip()}"
    )
    if pr_url:
        handoff_block += f"\n**PR / Branch:** {pr_url}"

    new_body = f"{orig_body}{handoff_block}"
    if resolved_to == "raymund":
        model_override = None
        provider_override = None
    elif resolved_to == "sindri":
        model_override = "gemini-4-argon-high"
        provider_override = "antigravity-subscription-directsdk"
    else:
        model_override = SPECIALIST_MODELS.get(resolved_to, "gemini-3.8-flash-high")
        provider_override = "antigravity-subscription-directsdk"

    cursor.execute(
        """
        UPDATE tasks
        SET assignee = ?, status = 'ready', body = ?, model_override = ?, provider_override = ?, consecutive_failures = 0, last_failure_error = NULL
        WHERE id = ?
        """,
        (resolved_to, new_body, model_override, provider_override, task_id),
    )

    event_payload = {
        "from_assignee": from_assignee,
        "to_assignee": resolved_to,
        "summary": summary,
        "pr_url": pr_url,
        "model_override": model_override,
    }
    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'handoff', ?, ?)
        """,
        (task_id, json.dumps(event_payload), now),
    )

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

    if thread_id:
        fields = [
            {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
            {"name": "Pipeline Handoff", "value": f"`{from_assignee}` -> `{resolved_to}`", "inline": True},
            {"name": "Model", "value": f"`{model_override or 'human/raymund'}`", "inline": True},
        ]
        if pr_url:
            fields.append({"name": "PR / Branch", "value": pr_url, "inline": False})
        post_thread_lifecycle_update(
            thread_id=thread_id,
            status="ready",
            message=f"**Handoff Summary (`{from_assignee}` -> `{resolved_to}`):**\n{summary}",
            fields=fields,
        )

    logger.info("Handed off task %s (%s): %s -> %s (%s)", task_id, orig_title, from_assignee, resolved_to, model_override)
    return {
        "task_id": task_id,
        "from_assignee": from_assignee,
        "to_assignee": resolved_to,
        "model_override": model_override,
        "summary": summary,
        "pr_url": pr_url,
        "thread_id": thread_id,
    }


def verify_pipeline_stage(
    task_id: str,
    passed: bool,
    summary: str = "",
    feedback: str = "",
    pr_url: str = "",
    max_retries: int = 3,
) -> Dict[str, Any]:
    """Evaluates a verification gate in the Court of Odin pipeline and executes deterministic state transitions."""
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT title, body, assignee, consecutive_failures FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Task {task_id} not found in kanban database")

    title, body, current_assignee, current_failures = row[0] or "", row[1] or "", row[2] or "unknown", row[3] or 0
    now = int(time.time())

    cursor.execute(
        """
        SELECT thread_id FROM kanban_notify_subs
        WHERE task_id = ? AND platform = 'discord' LIMIT 1
        """,
        (task_id,),
    )
    sub_row = cursor.fetchone()
    thread_id = sub_row[0] if sub_row else None

    if passed:
        # Determine next stage based on current assignee
        if current_assignee == "forseti":
            next_assignee = "vor"
            stage_msg = "Verification passed by forseti. Advancing to vor for canonical documentation & memory curation."
        elif current_assignee == "vor":
            next_assignee = "odin"
            stage_msg = "Documentation & memory curation passed by vor. Advancing to odin for final sign-off."
        else:
            next_assignee = "vor"
            stage_msg = f"Verification passed by {current_assignee}. Advancing to {next_assignee}."

        verify_block = (
            f"\n\n---\n"
            f"### Verification Gate Passed ({current_assignee})\n"
            f"{summary.strip() or 'All automated tests and visual inspections passed.'}"
        )
        if pr_url:
            verify_block += f"\n**PR / Branch:** {pr_url}"

        new_body = f"{body}{verify_block}"
        model_override = SPECIALIST_MODELS.get(next_assignee, "gemini-3.8-flash-high")
        provider_override = "antigravity-subscription-directsdk"

        cursor.execute(
            """
            UPDATE tasks
            SET assignee = ?, status = 'ready', body = ?, model_override = ?, provider_override = ?,
                consecutive_failures = 0, last_failure_error = NULL
            WHERE id = ?
            """,
            (next_assignee, new_body, model_override, provider_override, task_id),
        )

        cursor.execute(
            """
            INSERT INTO task_events (task_id, kind, payload, created_at)
            VALUES (?, 'verification_passed', ?, ?)
            """,
            (task_id, json.dumps({"gate": current_assignee, "summary": summary, "pr_url": pr_url, "next": next_assignee}), now),
        )
        conn.commit()
        conn.close()

        if thread_id:
            fields = [
                {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
                {"name": "Verification Gate", "value": f"`{current_assignee}`: **PASSED**", "inline": True},
                {"name": "Next Assignee", "value": f"`{next_assignee}`", "inline": True},
            ]
            if pr_url:
                fields.append({"name": "PR / Branch", "value": pr_url, "inline": False})
            post_thread_lifecycle_update(
                thread_id=thread_id,
                status="ready",
                message=f"**Verification Gate Passed:**\n{stage_msg}\n\n**Summary:** {summary or 'Green.'}",
                fields=fields,
            )

        logger.info("Task %s verification PASSED at gate %s -> advancing to %s", task_id, current_assignee, next_assignee)
        return {
            "task_id": task_id,
            "status": "ready",
            "passed": True,
            "current_assignee": current_assignee,
            "next_assignee": next_assignee,
            "consecutive_failures": 0,
            "message": stage_msg,
        }

    else:
        # Failure path: evaluate retry circuit breaker
        failures = current_failures + 1
        err_msg = feedback.strip() or "Verification gate reported unspecified failure."

        if failures <= max_retries:
            next_assignee = "sindri"
            retry_block = (
                f"\n\n---\n"
                f"### Verification Gate Failed (Attempt {failures}/{max_retries})\n"
                f"**Reported By:** {current_assignee}\n"
                f"**Feedback / Errors:**\n{err_msg}\n"
                f"**Next Action:** Automatically routed back to sindri to address regressions."
            )
            new_body = f"{body}{retry_block}"
            model_override = "gemini-4-argon-high"
            provider_override = "antigravity-subscription-directsdk"

            cursor.execute(
                """
                UPDATE tasks
                SET assignee = ?, status = 'ready', body = ?, model_override = ?, provider_override = ?,
                    consecutive_failures = ?, last_failure_error = ?
                WHERE id = ?
                """,
                (next_assignee, new_body, model_override, provider_override, failures, err_msg, task_id),
            )

            cursor.execute(
                """
                INSERT INTO task_events (task_id, kind, payload, created_at)
                VALUES (?, 'verification_failed_retry', ?, ?)
                """,
                (task_id, json.dumps({"gate": current_assignee, "feedback": err_msg, "failures": failures, "max_retries": max_retries}), now),
            )
            conn.commit()
            conn.close()

            if thread_id:
                fields = [
                    {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
                    {"name": "Verification Gate", "value": f"`{current_assignee}`: **FAILED**", "inline": True},
                    {"name": "Retry Attempt", "value": f"`{failures}/{max_retries}` -> `sindri`", "inline": True},
                ]
                post_thread_lifecycle_update(
                    thread_id=thread_id,
                    status="ready",
                    message=f"⚠️ **Verification Gate Failed (Attempt {failures}/{max_retries}):**\nAutomatically bouncing task back to `{next_assignee}` for remediation.\n\n**Error Details:**\n```\n{err_msg[:500]}\n```",
                    fields=fields,
                )

            logger.warning("Task %s verification FAILED at gate %s (attempt %d/%d) -> bouncing to sindri", task_id, current_assignee, failures, max_retries)
            return {
                "task_id": task_id,
                "status": "ready",
                "passed": False,
                "current_assignee": current_assignee,
                "next_assignee": next_assignee,
                "consecutive_failures": failures,
                "circuit_tripped": False,
                "message": f"Bounced back to {next_assignee} (attempt {failures}/{max_retries})",
            }
        else:
            # Circuit breaker tripped: block and escalate to Raymund
            next_assignee = "raymund"
            blocked_block = (
                f"\n\n---\n"
                f"### Verification Circuit Breaker Tripped ({failures} Failures)\n"
                f"Exceeded max retry limit ({max_retries}). Task blocked for human review.\n"
                f"**Last Error:**\n{err_msg}"
            )
            new_body = f"{body}{blocked_block}"

            cursor.execute(
                """
                UPDATE tasks
                SET assignee = ?, status = 'blocked', body = ?, model_override = NULL, provider_override = NULL,
                    consecutive_failures = ?, last_failure_error = ?
                WHERE id = ?
                """,
                (next_assignee, new_body, failures, err_msg, task_id),
            )

            cursor.execute(
                """
                INSERT INTO task_events (task_id, kind, payload, created_at)
                VALUES (?, 'verification_circuit_tripped', ?, ?)
                """,
                (task_id, json.dumps({"gate": current_assignee, "feedback": err_msg, "failures": failures}), now),
            )
            conn.commit()
            conn.close()

            if thread_id:
                fields = [
                    {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
                    {"name": "Verification Gate", "value": f"`{current_assignee}`: **CIRCUIT TRIPPED**", "inline": True},
                    {"name": "Status", "value": "`BLOCKED` -> `raymund`", "inline": True},
                ]
                post_thread_lifecycle_update(
                    thread_id=thread_id,
                    status="blocked",
                    message=f"🛑 **Verification Circuit Breaker Tripped ({failures} consecutive failures):**\nTask is blocked and escalated to Raymund for human inspection.\n\n**Last Error:**\n```\n{err_msg[:500]}\n```",
                    fields=fields,
                )

            logger.error("Task %s verification circuit breaker tripped after %d failures -> blocked for raymund", task_id, failures)
            return {
                "task_id": task_id,
                "status": "blocked",
                "passed": False,
                "current_assignee": current_assignee,
                "next_assignee": next_assignee,
                "consecutive_failures": failures,
                "circuit_tripped": True,
                "message": f"Circuit breaker tripped after {failures} failures -> blocked for human review",
            }


def update_task_lifecycle(task_id: str, new_status: str, message: str) -> bool:
    """Updates task status in kanban.db and notifies its assigned Discord thread."""
    canonical_status = STATUS_CANONICAL.get(new_status.lower(), new_status.lower())
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    cursor.execute("SELECT status, title FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        logger.warning("Task %s not found in kanban database", task_id)
        conn.close()
        return False
    current_status = row[0]
    task_title = row[1] if len(row) > 1 and row[1] else ""

    if current_status == canonical_status:
        logger.info("Task %s is already '%s'; skipping no-op lifecycle update.", task_id, canonical_status)
        conn.close()
        return True

    now = int(time.time())
    extra_clause = ""
    if canonical_status in ("running", "in_progress"):
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

    # Auto-trigger full autonomous session wrap-up when a task transitions to done
    if canonical_status == "done":
        trigger_session_wrapup_chore(task_id, task_title, thread_id)

    return True


def record_task_debrief(
    task_id: str,
    summary: str,
    frictions: Optional[str] = None,
    chore_title: Optional[str] = None,
    pr_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Records a task debrief / retrospective receipt and optionally spawns a follow-up chore card."""
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()

    now = int(time.time())

    # 1. Verify task exists
    cursor.execute("SELECT id, title, body FROM tasks WHERE id = ?", (task_id,))
    task_row = cursor.fetchone()
    if not task_row:
        conn.close()
        raise ValueError(f"Task {task_id} not found in kanban database")

    task_body = task_row[2] or ""

    # Auto-extract PR URL from summary if not explicitly passed
    extracted_pr = pr_url
    if not extracted_pr and ("github.com/" in summary and "/pull/" in summary):
        import re
        m = re.search(r"https://github\.com/[^\s)]+/pull/\d+", summary)
        if m:
            extracted_pr = m.group(0)

    # Jev System One Decision Gates (batched in a single round-trip)
    batch_gates = evaluate_debrief_gates_batch(
        task_title=task_row[1],
        task_body=task_body,
        debrief_summary=summary,
        pr_url=extracted_pr,
        friction_text=frictions,
    )
    acceptance_gate = batch_gates["acceptance_gate"]
    auto_merge_gate = batch_gates["auto_merge_gate"]
    friction_gate = batch_gates["friction_gate"]

    # 2. Record debrief event
    payload = {
        "summary": summary,
        "frictions": frictions or "None",
        "pr_url": extracted_pr,
        "timestamp": now,
        "acceptance_gate": acceptance_gate,
        "auto_merge_gate": auto_merge_gate,
        "friction_gate": friction_gate,
    }
    cursor.execute(
        """
        INSERT INTO task_events (task_id, kind, payload, created_at)
        VALUES (?, 'debrief', ?, ?)
        """,
        (task_id, json.dumps(payload), now),
    )

    # 3. If a PR URL was produced, automatically transition task status to in_review
    if extracted_pr:
        cursor.execute("UPDATE tasks SET status = 'in_review' WHERE id = ?", (task_id,))
        logger.info("Transitioned task %s to 'in_review' due to PR: %s", task_id, extracted_pr)

    # 4. If friction is actionable (spawn_chore) or chore_title explicitly provided, spawn an in_review Proposal thread in #info
    chore_id = None
    chore_thread_id = None
    should_spawn = bool(chore_title) or (friction_gate and friction_gate.get("action") == "spawn_chore")
    if should_spawn:
        ctitle = chore_title or f"Address friction in {task_id}"
        triage_verdict = friction_gate.get("raw_verdict", "Explicit chore requested") if friction_gate else "Explicit chore requested"
        cproposal = (
            f"Formulate and implement a targeted fix for the friction encountered during `{task_id}` ({task_row[1]}).\n"
            f"- **Jev Triage Verdict:** {triage_verdict}"
        )
        conn.commit()
        conn.close()
        friction_assignee = batch_gates.get("friction_assignee") or "vor"
        chore_id, chore_thread_id = create_improvement_proposal(
            title=ctitle,
            friction=frictions or "See task debrief summary.",
            proposal=cproposal,
            target_specialist=friction_assignee,
            origin_task_id=task_id,
        )
        conn = sqlite3.connect(KANBAN_DB_PATH)
        cursor = conn.cursor()
    elif friction_gate and friction_gate.get("action") == "ignore_transient":
        logger.info("Friction in task %s classified as transient noise; suppressed follow-up proposal.", task_id)

    # 5. Find Discord thread subscription for parent task
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

    # 6. Post Discord embed
    if thread_id:
        post_thread_debrief(
            thread_id=thread_id,
            task_id=task_id,
            summary=summary,
            frictions=frictions,
            chore_task_id=chore_id,
            pr_url=extracted_pr,
            acceptance_gate=acceptance_gate,
            auto_merge_gate=auto_merge_gate,
            friction_gate=friction_gate,
        )

    return {
        "task_id": task_id,
        "summary": summary,
        "frictions": frictions,
        "chore_id": chore_id,
        "chore_thread_id": chore_thread_id,
        "thread_id": thread_id,
        "pr_url": extracted_pr,
        "acceptance_gate": acceptance_gate,
        "auto_merge_gate": auto_merge_gate,
        "friction_gate": friction_gate,
    }


def triage_and_intake(event_text: str, title: Optional[str] = None, context: str = "") -> Optional[Dict[str, Any]]:
    """Complete entrypoint: evaluates actionability + complexity in 1 Jev round-trip and provisions task if actionable."""
    logger.info("Running Jev triage on event: %s", event_text[:80])

    # Extract short title if not provided
    if not title:
        first_line = event_text.strip().split("\n")[0]
        title = first_line[:60].strip()
        if len(first_line) > 60:
            title += "..."

    triage_res = evaluate_triage_and_complexity(event_text, title=title, context=context)
    is_actionable = bool(triage_res.get("actionable"))
    verdict = str(triage_res.get("raw_verdict", ""))
    elapsed = float(triage_res.get("latency_seconds", 0.0))

    if not is_actionable:
        logger.info("Event classified as NON-ACTIONABLE by Jev (suppressed from Kanban).")
        return {
            "actionable": False,
            "verdict": verdict,
            "elapsed_seconds": elapsed,
            "task_id": None,
            "thread_id": None,
        }

    task_id, thread_id = create_kanban_task(
        title=title,
        body=event_text,
        priority=1,
        assignee=DEFAULT_ASSIGNEE,
        precomputed_complexity=triage_res,
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


def show_kanban_task(task_id: str, as_json: bool = False) -> Dict[str, Any]:
    """Retrieves and displays full details and recent event history for a Kanban task."""
    conn = sqlite3.connect(KANBAN_DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT t.id, t.title, t.body, t.assignee, t.status, t.priority,
               t.created_at, t.started_at, t.completed_at, t.model_override,
               t.consecutive_failures, t.last_failure_error, s.thread_id
        FROM tasks t
        LEFT JOIN kanban_notify_subs s ON t.id = s.task_id
        WHERE t.id = ?
        """,
        (task_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Task {task_id} not found in kanban database")

    cursor.execute(
        """
        SELECT kind, payload, created_at
        FROM task_events
        WHERE task_id = ?
        ORDER BY id DESC LIMIT 10
        """,
        (task_id,),
    )
    event_rows = cursor.fetchall()
    conn.close()

    events = []
    for kind, payload_str, created_at in event_rows:
        try:
            parsed_payload = json.loads(payload_str) if payload_str else {}
        except Exception:
            parsed_payload = {"raw": payload_str}
        events.append({"kind": kind, "payload": parsed_payload, "created_at": created_at})

    info = {
        "id": row[0],
        "title": row[1],
        "body": row[2] or "",
        "assignee": row[3],
        "status": row[4],
        "priority": row[5],
        "created_at": row[6],
        "started_at": row[7],
        "completed_at": row[8],
        "model_override": row[9],
        "consecutive_failures": row[10] or 0,
        "last_failure_error": row[11],
        "thread_id": row[12],
        "events": events,
    }

    if as_json:
        print(json.dumps(info, indent=2))
    else:
        print(f"\n{'=' * 75}")
        print(f"TASK: {info['id']} | STATUS: {info['status'].upper()} | ASSIGNEE: {info['assignee']}")
        print(f"{'=' * 75}")
        print(f"Title:          {info['title']}")
        print(f"Priority:       P{info['priority']}")
        print(f"Model Override: {info['model_override'] or 'default'}")
        print(f"Discord Thread: {info['thread_id'] or 'none'}")
        if info["consecutive_failures"]:
            print(f"Failures:       {info['consecutive_failures']} ({info['last_failure_error']})")
        print(f"\n--- Description ---\n{info['body']}\n")
        if events:
            print("--- Recent Events ---")
            for ev in events[:5]:
                print(f"  [{ev['created_at']}] {ev['kind']}")
        print()

    return info

