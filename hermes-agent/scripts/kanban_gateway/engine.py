"""engine.py - Core Kanban database operations, task lifecycle state machine, and retrospective logic."""

from __future__ import annotations

import json
import sqlite3
import time
from typing import Any, Dict, Optional, Tuple
import uuid

from .config import (
    DEFAULT_ASSIGNEE,
    DISCORD_CHANNEL_ID,
    DISCORD_USER_ID,
    DISCORD_WEBHOOK_URL,
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
    if notify_discord and DISCORD_WEBHOOK_URL:
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


def trigger_session_wrapup_chore(task_id: str, orig_title: str, thread_id: Optional[str] = None) -> Optional[str]:
    """Auto-spawns an autonomous session wrap-up chore when a dev or engineering task transitions to done."""
    clean_title = orig_title.lower()
    # Recursion guard: do not trigger wrap-up for wrap-up chores, friction chores, or triage tasks
    if (
        clean_title.startswith("wrap-up")
        or clean_title.startswith("auto-sync")
        or "address friction" in clean_title
        or "session wrap-up" in clean_title
    ):
        logger.info("Skipping wrap-up trigger for maintenance/wrap-up task %s", task_id)
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
        f"Execute the full canonical wrap-up protocol (`.agents/skills/session-wrapup/SKILL.md`):\n\n"
        f"### Protocol Execution Checklist:\n"
        f"- [ ] **Gate 1 & Gate 2A (Auto Self-Improvement)**: Audit the reported frictions and execution trace. If any friction occurred, formulate a concrete hardening action and patch the corresponding skill under `/opt/odin/hermes/skills/` or rule under `.agents/rules/` to prevent recurrence.\n"
        f"{stack_instruction}"
        f"- [ ] **Gate 2C (Active Effort Audit)**: Check and update matching note in `second-brain/Efforts/` if linked to an active project.\n"
        f"- [ ] **Gate 2D (Mnemosyne Memory)**: Query and record durable architectural facts via Mnemosyne tools or host CLI.\n"
        f"- [ ] **Step 3 (Obsidian Daily Journal)**: Append a complete engineering session block into `second-brain/Journal/YYYY/MM/YYYY-MM-DD.md` (respecting the 06:00 Auckland sleep boundary). Include Dashboard, Narrative, Retrospective table (Friction / Root Cause / Hardening Action), and Next Session Handoff.\n"
        f"- [ ] **Step 4 (Readiness Probe & Cleanup)**: Run `python3 /opt/odin/hermes/skills/productivity/session-wrapup/scripts/check_wrapup_readiness.py` to verify formatting, structure, and zero em/en dashes.\n"
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

    # 4. If friction is actionable (spawn_chore) or chore_title explicitly provided, auto-spawn chore card
    chore_id = None
    chore_thread_id = None
    should_spawn = bool(chore_title) or (friction_gate and friction_gate.get("action") == "spawn_chore")
    if should_spawn:
        ctitle = chore_title or f"Address friction in {task_id}"
        cbody = (
            f"Auto-spawned from task debrief for {task_id}:\n\n"
            f"**Friction Report:**\n{frictions}\n\n"
            f"**Origin Task:** {task_row[1]} ({task_id})\n\n"
            f"**Triage Verdict:** {friction_gate.get('raw_verdict', '') if friction_gate else 'Explicit chore requested'}"
        )
        conn.commit()
        conn.close()
        friction_assignee = batch_gates.get("friction_assignee") or "vor"
        chore_tier = "frontier_deep" if friction_assignee == "sindri" else "flash_fast"
        chore_id, chore_thread_id = create_kanban_task(
            title=ctitle,
            body=cbody,
            priority=2,
            assignee=friction_assignee,
            precomputed_complexity={
                "tier": chore_tier,
                "recommended_assignee": friction_assignee,
                "recommended_model": (
                    "gemini-4-argon-high"
                    if friction_assignee == "sindri"
                    else SPECIALIST_MODELS.get(friction_assignee, "gemini-3.8-flash-high")
                ),
                "raw_verdict": friction_gate.get("raw_verdict", "debrief_batch") if friction_gate else "debrief_batch",
                "latency_seconds": friction_gate.get("latency_seconds", 0.0) if friction_gate else 0.0,
            },
        )
        conn = sqlite3.connect(KANBAN_DB_PATH)
        cursor = conn.cursor()
    elif friction_gate and friction_gate.get("action") == "ignore_transient":
        logger.info("Friction in task %s classified as transient noise; suppressed follow-up chore card.", task_id)

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
