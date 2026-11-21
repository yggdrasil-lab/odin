"""engine.py - Core Kanban database operations, task lifecycle state machine, and retrospective logic."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
import sqlite3
import time
from typing import Any, Dict, Optional, Sequence, Tuple
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
    resolve_flash_fast_model,
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


# ============================================================================
# PURE DOMAIN MODELS & ROUTING EVALUATORS (Zero I/O)
# ============================================================================
@dataclass(frozen=True)
class TaskRoutingResolution:
    resolved_assignee: str
    model_override: Optional[str]
    provider_override: Optional[str]
    complexity: Dict[str, Any]
    initial_status: str


@dataclass(frozen=True)
class RecoveryEscalationDecision:
    next_assignee: str
    model_override: str
    provider_override: str
    escalated_from: Optional[str]
    escalation_note: str
    retry_block: str


def resolve_handoff_model(
    resolved_assignee: str,
    tier: Optional[str] = None,
    recommended_model: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Pure evaluator for specialist model and provider overrides across complexity tiers."""
    clean_tier = (tier or "").strip().lower()
    if resolved_assignee == "raymund":
        return None, None
    if clean_tier == "frontier_deep":
        return "gemini-4-argon-high", "antigravity-subscription-directsdk"
    if clean_tier == "flash_fast":
        return (recommended_model or resolve_flash_fast_model(resolved_assignee)), "antigravity-subscription-directsdk"
    if resolved_assignee == "sindri":
        return "gemini-4-argon-high", "antigravity-subscription-directsdk"
    return (
        recommended_model or SPECIALIST_MODELS.get(resolved_assignee, "gemini-4-hydrogen-high"),
        "antigravity-subscription-directsdk",
    )


def resolve_task_routing(
    assignee: Optional[str],
    complexity: Dict[str, Any],
    tier: Optional[str] = None,
    await_approval: bool = False,
) -> TaskRoutingResolution:
    """Pure evaluator resolving task assignee, tier, model override, and initial status."""
    comp = dict(complexity)
    if assignee and assignee != DEFAULT_ASSIGNEE:
        resolved_assignee = normalize_assignee(assignee)
    else:
        resolved_assignee = normalize_assignee(comp.get("recommended_assignee") or DEFAULT_ASSIGNEE)

    if tier:
        resolved_tier = tier.strip().lower()
        comp["tier"] = resolved_tier
        comp["recommended_model"] = (
            "gemini-4-argon-high" if resolved_tier == "frontier_deep" else resolve_flash_fast_model(resolved_assignee)
        )

    if comp["tier"] == "human_pairing" and (not assignee or assignee == DEFAULT_ASSIGNEE):
        resolved_assignee = "raymund"

    model_override, provider_override = resolve_handoff_model(
        resolved_assignee=resolved_assignee,
        tier=comp.get("tier"),
        recommended_model=comp.get("recommended_model"),
    )
    return TaskRoutingResolution(
        resolved_assignee=resolved_assignee,
        model_override=model_override,
        provider_override=provider_override,
        complexity=comp,
        initial_status="in_review" if await_approval else "ready",
    )


def format_hitl_approval_body(
    body: str,
    task_id: str,
    routing: TaskRoutingResolution,
    await_approval: bool,
) -> str:
    """Pure formatter appending HITL approval instructions when await_approval is enabled."""
    if not await_approval:
        return body
    approval_cmd = (
        f'kanban-gateway handoff {task_id} --to {routing.resolved_assignee} '
        f'--tier {routing.complexity["tier"]} --summary "Approved for execution"'
    )
    return (
        f"{body.rstrip()}\n\n---\n"
        f"### Human-in-the-Loop (HITL) Approval Gate\n"
        f"- **Status:** `in_review` (Parked awaiting operator approval before execution)\n"
        f"- **Target Specialist:** `{routing.resolved_assignee}` (`{routing.model_override or 'default'}`)\n"
        f"- **Approve & Dispatch:** `{approval_cmd}`"
    )


def evaluate_two_way_recovery(
    current_assignee: str,
    prev_model: str,
    failures: int,
    max_retries: int,
    err_msg: str,
) -> RecoveryEscalationDecision:
    """Pure evaluator for Two-Way Recovery escalation (Hydrogen -> Argon) when a verification gate fails."""
    next_assignee = "sindri"
    model_override = "gemini-4-argon-high"
    provider_override = "antigravity-subscription-directsdk"
    escalated_from = prev_model if prev_model and prev_model != "gemini-4-argon-high" else None
    escalation_note = (
        f"\n**Two-Way Recovery Escalation:** Escalated model from `{escalated_from}` to `{model_override}`."
        if escalated_from
        else ""
    )
    retry_block = (
        f"\n\n---\n"
        f"### Verification Gate Failed (Attempt {failures}/{max_retries})\n"
        f"**Reported By:** {current_assignee}\n"
        f"**Feedback / Errors:**\n{err_msg}\n"
        f"**Next Action:** Automatically routed back to sindri (`{model_override}`) to address regressions.{escalation_note}"
    )
    return RecoveryEscalationDecision(
        next_assignee=next_assignee,
        model_override=model_override,
        provider_override=provider_override,
        escalated_from=escalated_from,
        escalation_note=escalation_note,
        retry_block=retry_block,
    )


ALLOWED_TASK_OWNERS: Tuple[str, ...] = ("hermes", "ide")
ALLOWED_CREATE_STATUSES: Tuple[str, ...] = ("todo", "ready", "in_progress", "in_review")
TERMINAL_DEPENDENCY_STATUSES: Tuple[str, ...] = ("done", "archived")
ACTIVE_RUNNING_STATUSES: Tuple[str, ...] = ("running", "in_progress")
_KB_PREFIX_RE = re.compile(r"^(?:\[kb-[0-9a-f]+\]\s*)+", re.IGNORECASE)
_WRAPUP_SKIP_PREFIXES: Tuple[str, ...] = ("wrap-up", "auto-sync")
_WRAPUP_SKIP_SUBSTRINGS: Tuple[str, ...] = (
    "address friction",
    "session wrap-up",
    "proposal:",
    "[auto-fix]",
)


def validate_task_owner(owner: str) -> str:
    """Normalizes and validates task creation owner against ALLOWED_TASK_OWNERS."""
    if not isinstance(owner, str):
        raise ValueError(f"Invalid task owner {owner!r}. Allowed: {', '.join(ALLOWED_TASK_OWNERS)}")
    cleaned = owner.strip().lower()
    if cleaned not in ALLOWED_TASK_OWNERS:
        raise ValueError(f"Invalid task owner {owner!r}. Allowed: {', '.join(ALLOWED_TASK_OWNERS)}")
    return cleaned


def validate_create_status(status: Optional[str]) -> Optional[str]:
    """Normalizes and validates optional creation status against ALLOWED_CREATE_STATUSES."""
    if status is None:
        return None
    if not isinstance(status, str):
        raise ValueError(f"Invalid initial status {status!r}; expected one of {ALLOWED_CREATE_STATUSES}")
    cleaned = status.strip().lower()
    if cleaned not in ALLOWED_CREATE_STATUSES:
        raise ValueError(f"Invalid initial status '{status}'; expected one of {ALLOWED_CREATE_STATUSES}")
    return cleaned


def _strip_kb_prefix(title: str) -> str:
    return _KB_PREFIX_RE.sub("", (title or "").strip()).strip()


def _should_skip_wrapup_title(title: str) -> bool:
    clean = _strip_kb_prefix(title).lower()
    return clean.startswith(_WRAPUP_SKIP_PREFIXES) or any(s in clean for s in _WRAPUP_SKIP_SUBSTRINGS)


def _normalize_parent_ids(depends_on: Optional[Sequence[str]]) -> Tuple[str, ...]:
    if not depends_on:
        return ()
    return tuple(dict.fromkeys(str(p).strip() for p in depends_on if str(p).strip()))


def _compute_initial_task_status(
    await_approval: bool,
    open_parents: Sequence[str],
    explicit_status: Optional[str],
    owner: str = "hermes",
) -> str:
    """Resolves initial task status from approval gate, dependencies, explicit status, and owner.

    Never default parentless IDE cards to 'todo' because Hermes recompute_ready
    auto-promotes parentless 'todo' tasks into 'ready' on the next tick.
    """
    normalized_owner = validate_task_owner(owner)
    clean_status = validate_create_status(explicit_status)
    if await_approval:
        return "in_review"
    if open_parents:
        return "todo"
    if clean_status is not None:
        return clean_status
    if normalized_owner == "ide":
        return "in_progress"
    return "ready"


def _ensure_task_links_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS task_links (
            parent_id TEXT NOT NULL,
            child_id  TEXT NOT NULL,
            PRIMARY KEY (parent_id, child_id)
        )
        """
    )


def _task_links_table_exists(cursor: sqlite3.Cursor) -> bool:
    row = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_links' LIMIT 1"
    ).fetchone()
    return row is not None


def _fetch_task_statuses(cursor: sqlite3.Cursor, task_ids: Sequence[str]) -> Dict[str, str]:
    if not task_ids:
        return {}
    placeholders = ",".join("?" * len(task_ids))
    rows = cursor.execute(
        f"SELECT id, status FROM tasks WHERE id IN ({placeholders})",
        tuple(task_ids),
    ).fetchall()
    return {row[0]: (row[1] or "") for row in rows}


def _fetch_open_parents_for_child(cursor: sqlite3.Cursor, child_id: str) -> list[str]:
    if not _task_links_table_exists(cursor):
        return []
    rows = cursor.execute(
        """
        SELECT t.id, t.status
        FROM task_links l
        JOIN tasks t ON t.id = l.parent_id
        WHERE l.child_id = ?
        ORDER BY t.id
        """,
        (child_id,),
    ).fetchall()
    return [row[0] for row in rows if (row[1] or "") not in TERMINAL_DEPENDENCY_STATUSES]


def _would_create_cycle(cursor: sqlite3.Cursor, parent_id: str, child_id: str) -> bool:
    seen: set[str] = set()
    stack = [child_id]
    while stack:
        node = stack.pop()
        if node == parent_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        rows = cursor.execute("SELECT child_id FROM task_links WHERE parent_id = ?", (node,)).fetchall()
        stack.extend(row[0] for row in rows)
    return False


def _record_dependency_events(
    cursor: sqlite3.Cursor,
    task_id: str,
    parent_ids: Sequence[str],
    open_parents: Sequence[str],
    now: int,
) -> None:
    for pid in parent_ids:
        cursor.execute("INSERT OR IGNORE INTO task_links (parent_id, child_id) VALUES (?, ?)", (pid, task_id))
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'linked', ?, ?)",
            (task_id, json.dumps({"parent": pid, "child": task_id}), now),
        )
    if open_parents:
        payload = {"reason": "parent_not_done", "parent": open_parents[0], "parents": list(open_parents)}
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', ?, ?)",
            (task_id, json.dumps(payload), now),
        )


def _resolve_create_routing(
    title: str,
    body: str,
    assignee: Optional[str],
    precomputed_complexity: Optional[Dict[str, Any]],
    tier: Optional[str],
    await_approval: bool,
) -> TaskRoutingResolution:
    explicit_specialist = bool(assignee and assignee != DEFAULT_ASSIGNEE)
    if (tier or explicit_specialist) and not precomputed_complexity:
        target_role = normalize_assignee(assignee) if explicit_specialist else "sindri"
        resolved_tier = (tier or ("frontier_deep" if target_role == "sindri" else "flash_fast")).strip().lower()
        base_complexity: Dict[str, Any] = {
            "tier": resolved_tier,
            "recommended_assignee": target_role,
            "recommended_model": (
                "gemini-4-argon-high"
                if resolved_tier == "frontier_deep"
                else resolve_flash_fast_model(target_role)
            ),
            "raw_verdict": f"explicit_routing:{target_role}:{resolved_tier}",
            "latency_seconds": 0.0,
        }
    else:
        base_complexity = dict(precomputed_complexity or evaluate_task_complexity(title, body))
    return resolve_task_routing(
        assignee=assignee,
        complexity=base_complexity,
        tier=tier,
        await_approval=await_approval,
    )


def _format_create_status_badge(
    initial_status: str, owner: str = "hermes", await_approval: bool = False,
) -> str:
    if initial_status == "in_review":
        return "`IN_REVIEW (AWAITING APPROVAL)`" if await_approval else "`IN_REVIEW`"
    if initial_status == "in_progress":
        return "`IN_PROGRESS` (IDE-Owned)" if owner == "ide" else "`IN_PROGRESS`"
    if initial_status == "todo":
        return "`TODO (WAITING ON DEPENDENCY)`"
    if initial_status == "ready":
        return "`READY`"
    return f"`{initial_status.upper()}`"


def _build_create_embed_fields(
    initial_status: str, priority: int, resolved_assignee: str,
    complexity: Dict[str, Any], model_override: Optional[str],
    owner: str = "hermes", await_approval: bool = False,
) -> list[Dict[str, Any]]:
    status_badge = _format_create_status_badge(initial_status, owner=owner, await_approval=await_approval)
    model_label = model_override or complexity.get("recommended_model")
    return [
        {"name": "Status", "value": status_badge, "inline": True},
        {"name": "Priority", "value": f"`P{priority}`", "inline": True},
        {"name": "Assignee", "value": f"`{resolved_assignee}`", "inline": True},
        {"name": "Model Tier", "value": f"`{complexity['tier']}` ({model_label})", "inline": True},
    ]


def _record_notify_subscription(task_id: str, thread_id: str, now: int) -> None:
    conn = sqlite3.connect(KANBAN_DB_PATH, timeout=10.0)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO kanban_notify_subs "
            "(task_id, platform, chat_id, thread_id, user_id, notifier_profile, created_at, last_event_id, delivery_mode) "
            "VALUES (?, 'discord', ?, ?, ?, 'kanban-gateway', ?, 0, 'notify')",
            (task_id, DISCORD_CHANNEL_ID, thread_id, DISCORD_USER_ID, now),
        )
        conn.commit()
    finally:
        conn.close()


def _fetch_discord_thread_id(cursor: sqlite3.Cursor, task_id: str) -> Optional[str]:
    cursor.execute(
        "SELECT thread_id FROM kanban_notify_subs WHERE task_id = ? AND platform = 'discord' LIMIT 1",
        (task_id,),
    )
    row = cursor.fetchone()
    return row[0] if row else None


def _provision_create_thread(
    task_id: str, full_title: str, final_body: str, initial_status: str,
    priority: int, resolved_assignee: str, complexity: Dict[str, Any],
    model_override: Optional[str], now: int,
    owner: str = "hermes", await_approval: bool = False,
) -> Optional[str]:
    if not (DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID):
        return None
    fields = _build_create_embed_fields(
        initial_status, priority, resolved_assignee, complexity, model_override,
        owner=owner, await_approval=await_approval,
    )
    try:
        thread_id = post_discord_thread(
            title=full_title, thread_name=full_title, description=final_body, fields=fields,
        )
        _record_notify_subscription(task_id, thread_id, now)
        return thread_id
    except Exception as e:
        logger.error("Failed to provision Discord thread for %s: %s", task_id, e)
        return None


def _insert_task_on_conn(
    conn: sqlite3.Connection, *, task_id: str, full_title: str, final_body: str,
    routing: TaskRoutingResolution, initial_status: str, priority: int,
    workspace_kind: str, await_approval: bool, parent_ids: Tuple[str, ...],
    open_parents: list[str], now: int, owner: str = "hermes",
) -> None:
    created_by = "ide" if owner == "ide" else "jev-gateway"
    started_at = now if initial_status == "in_progress" else None
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO tasks (id, title, body, assignee, status, priority, created_by, "
        "created_at, started_at, workspace_kind, consecutive_failures, goal_mode, block_recurrences, "
        "model_override, provider_override) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0, ?, ?)",
        (
            task_id, full_title, final_body, routing.resolved_assignee, initial_status,
            priority, created_by, now, started_at, workspace_kind, routing.model_override, routing.provider_override,
        ),
    )
    created_payload: Dict[str, Any] = {
        "source": "jev-gateway", "owner": owner, "title": full_title,
        "complexity": routing.complexity, "await_approval": await_approval, "initial_status": initial_status,
    }
    if parent_ids:
        created_payload["depends_on"] = list(parent_ids)
    cursor.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'created', ?, ?)",
        (task_id, json.dumps(created_payload), now),
    )
    if parent_ids:
        _record_dependency_events(cursor, task_id, parent_ids, open_parents, now)


def _persist_new_task(
    *, task_id: str, full_title: str, final_body: str, routing: TaskRoutingResolution,
    priority: int, workspace_kind: str, await_approval: bool, owner: str,
    parent_ids: Tuple[str, ...], explicit_status: Optional[str], now: int,
) -> str:
    conn = sqlite3.connect(KANBAN_DB_PATH, timeout=10.0)
    try:
        if parent_ids:
            _ensure_task_links_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        parent_statuses = _fetch_task_statuses(conn.cursor(), parent_ids)
        missing = [pid for pid in parent_ids if pid not in parent_statuses]
        if missing:
            raise ValueError(f"Unknown dependency parent task(s): {', '.join(missing)}")
        open_parents = [pid for pid in parent_ids if parent_statuses[pid] not in TERMINAL_DEPENDENCY_STATUSES]
        initial_status = _compute_initial_task_status(await_approval, open_parents, explicit_status, owner=owner)
        _insert_task_on_conn(
            conn, task_id=task_id, full_title=full_title, final_body=final_body, routing=routing,
            initial_status=initial_status, priority=priority, workspace_kind=workspace_kind,
            await_approval=await_approval, owner=owner, parent_ids=parent_ids, open_parents=open_parents, now=now,
        )
        conn.commit()
        return initial_status
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def create_kanban_task(
    title: str, body: str, priority: int = 1, assignee: Optional[str] = None,
    workspace_kind: str = "scratch", notify_discord: bool = True,
    precomputed_complexity: Optional[Dict[str, Any]] = None, tier: Optional[str] = None,
    await_approval: bool = False, depends_on: Optional[Sequence[str]] = None,
    owner: str = "hermes", status: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Inserts a new task into kanban.db atomically and provisions its Discord thread subscription.

    IDE-owned tasks default to 'in_progress' (excluded from Hermes worker daemon
    polling, reapers, and concurrency caps; never default parentless IDE cards to
    'todo' because Hermes recompute_ready auto-promotes parentless 'todo' -> 'ready').
    """
    normalized_owner = validate_task_owner(owner)
    explicit_status = validate_create_status(status)
    parent_ids = _normalize_parent_ids(depends_on)
    routing = _resolve_create_routing(title, body, assignee, precomputed_complexity, tier, await_approval)
    short_id = uuid.uuid4().hex[:8]
    task_id = f"kb-{short_id}"
    if task_id in parent_ids:
        raise ValueError(f"Task {task_id} cannot depend on itself")
    full_title = f"[KB-{short_id}] {title}"
    now = int(time.time())
    final_body = format_hitl_approval_body(body, task_id, routing, await_approval)
    initial_status = _persist_new_task(
        task_id=task_id, full_title=full_title, final_body=final_body, routing=routing,
        priority=priority, workspace_kind=workspace_kind, await_approval=await_approval,
        owner=normalized_owner, parent_ids=parent_ids, explicit_status=explicit_status, now=now,
    )
    thread_id = (
        _provision_create_thread(
            task_id, full_title, final_body, initial_status, priority,
            routing.resolved_assignee, routing.complexity, routing.model_override, now,
            owner=normalized_owner, await_approval=await_approval,
        )
        if notify_discord else None
    )
    logger.info("Created Kanban task %s (Status: %s, Discord Thread: %s)", task_id, initial_status, thread_id)
    return task_id, thread_id


def _fetch_and_validate_link_pair(
    cursor: sqlite3.Cursor, clean_parent: str, clean_child: str,
) -> Tuple[str, str, bool]:
    statuses = _fetch_task_statuses(cursor, (clean_parent, clean_child))
    missing = [tid for tid in (clean_parent, clean_child) if tid not in statuses]
    if missing:
        raise ValueError(f"Unknown task(s): {', '.join(missing)}")
    parent_status, child_status = statuses[clean_parent], statuses[clean_child]
    if child_status in ACTIVE_RUNNING_STATUSES:
        raise ValueError(f"Cannot link {clean_parent} -> {clean_child}: child task is {child_status}")
    existing = cursor.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ? LIMIT 1",
        (clean_parent, clean_child),
    ).fetchone()
    if existing is None and _would_create_cycle(cursor, clean_parent, clean_child):
        raise ValueError(f"Linking {clean_parent} -> {clean_child} would create a dependency cycle")
    return parent_status, child_status, existing is None


def _apply_link_and_demote(
    cursor: sqlite3.Cursor, clean_parent: str, clean_child: str,
    parent_status: str, child_status: str, needs_insert: bool, now: int,
) -> Dict[str, Any]:
    if needs_insert:
        cursor.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (clean_parent, clean_child))
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'linked', ?, ?)",
            (clean_child, json.dumps({"parent": clean_parent, "child": clean_child}), now),
        )
    demoted, final_status = False, child_status
    if child_status == "ready" and parent_status not in TERMINAL_DEPENDENCY_STATUSES:
        cursor.execute("UPDATE tasks SET status = 'todo' WHERE id = ? AND status = 'ready'", (clean_child,))
        demoted = cursor.rowcount == 1
        if demoted:
            final_status = "todo"
            cursor.execute(
                "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', ?, ?)",
                (clean_child, json.dumps({"reason": "parent_not_done", "demoted": True, "parent": clean_parent}), now),
            )
    return {
        "parent_id": clean_parent, "child_id": clean_child, "linked": needs_insert,
        "already_linked": not needs_insert, "demoted": demoted, "child_status": final_status,
    }


def link_kanban_tasks(parent_id: str, child_id: str) -> Dict[str, Any]:
    """Attaches a parent -> child dependency link, validating cycles and demoting ready children."""
    clean_parent, clean_child = (parent_id or "").strip(), (child_id or "").strip()
    if not clean_parent or not clean_child:
        raise ValueError("Both parent_id and child_id are required")
    if clean_parent == clean_child:
        raise ValueError(f"Task {clean_parent} cannot depend on itself")
    conn = sqlite3.connect(KANBAN_DB_PATH, timeout=10.0)
    try:
        _ensure_task_links_table(conn)
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        parent_status, child_status, needs_insert = _fetch_and_validate_link_pair(cursor, clean_parent, clean_child)
        result = _apply_link_and_demote(cursor, clean_parent, clean_child, parent_status, child_status, needs_insert, int(time.time()))
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


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
        else SPECIALIST_MODELS.get(resolved_target, "gemini-4-hydrogen-high")
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


def _resolve_wrapup_stack_instruction(orig_title: str, pr_url: Optional[str]) -> str:
    clean_lower = _strip_kb_prefix(orig_title).lower()
    known_stacks = (
        "poseidon", "odin", "kratos", "charon", "argus", "apollo",
        "mimir", "olympus", "hephaestus", "comics", "cerberus", "palworld",
    )
    stack_name = next(
        (known.capitalize() for known in known_stacks if known in clean_lower or (pr_url and known in pr_url.lower())),
        "",
    )
    if stack_name:
        return (
            f"- [ ] **Gate 2B (Canonical Domain Notes)**: Open and update `second-brain/Areas/90-Infrastructure/{stack_name}/{stack_name} Stack.md`:\n"
            f"  - Add newly deployed services, ports, and volumes to the Services and Volumes tables.\n"
            f"  - Update the Mermaid architecture diagram edges if communication flows changed.\n"
            f"  - Add an entry under Historical Timeline (PR {pr_url or 'merged'}).\n"
        )
    return (
        "- [ ] **Gate 2B (Canonical Domain Notes)**: Inspect affected stack/repo and update its canonical domain note under `second-brain/Areas/90-Infrastructure/` if applicable.\n"
    )


def _build_wrapup_chore_spec(
    task_id: str, orig_title: str, pr_url: Optional[str], summary: str, frictions: str,
) -> Tuple[str, str]:
    stack_instruction = _resolve_wrapup_stack_instruction(orig_title, pr_url)
    chore_title = f"Wrap-up: Session wrap-up and docs sync for {orig_title}"
    chore_body = (
        f"Automated post-completion session wrap-up for task `{task_id}`:\n\n"
        f"**Origin PR:** {pr_url or 'N/A'}\n"
        f"**Origin Summary:** {summary or orig_title}\n"
        f"**Reported Frictions:** {frictions}\n\n"
        f"Execute the full canonical wrap-up protocol (`${{HERMES_SKILLS_PATH:-/opt/odin/hermes/skills}}/productivity/session-wrapup/SKILL.md`):\n\n"
        f"### Protocol Execution Checklist:\n"
        f"- [ ] **Gate 1 & Gate 2A (3-Tier Bounded Continuous Improvement)**: Audit the reported frictions and execution trace. If an existing script failed or had a broken path/flag (Tier 1/2), fix it directly and autonomously inline (keeping all skills `<= 4,000 B` body and `<= 5,000 B` view). For any Tier 3 change (brand-new script, brand-new skill, or canonical rule change), NEVER mutate rules/skills unreviewed: submit a proposal via `kanban-gateway propose --title \"...\" --friction \"...\" --proposal \"...\" --origin-task {task_id}` (skip if a card was already spawned during debrief).\n"
        f"{stack_instruction}"
        f"- [ ] **Gate 2C (Active Effort Audit)**: Check and update matching note in `second-brain/Efforts/` if linked to an active project.\n"
        f"- [ ] **Gate 2D (Mnemosyne Memory)**: Query and record durable architectural facts via Mnemosyne tools or host CLI.\n"
        f"- [ ] **Step 3 (Obsidian Daily Journal)**: Append a complete engineering session block into `second-brain/Journal/YYYY/MM/YYYY-MM-DD.md` (respecting the 06:00 Auckland sleep boundary). Include Dashboard, Narrative, Retrospective table (Friction / Root Cause / Hardening Action), and Next Session Handoff.\n"
        f"- [ ] **Step 4 (Readiness Probe & Cleanup)**: Run `python3 ${{HERMES_SKILLS_PATH:-/opt/odin/hermes/skills}}/productivity/session-wrapup/scripts/check_wrapup_readiness.py` to verify formatting, structure, and zero em/en dashes.\n"
        f"- [ ] **Completion**: Run `kanban-gateway debrief <THIS_TASK_ID> --summary \"Session wrap-up complete for {task_id}\"` and call `kanban_complete`.\n"
    )
    return chore_title, chore_body


def _persist_wrapup_chore(
    task_id: str, orig_title: str, chore_id: str, short_id: str,
    routing: TaskRoutingResolution, now: int,
) -> Optional[Tuple[str, str]]:
    conn = sqlite3.connect(KANBAN_DB_PATH, timeout=10.0)
    try:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.cursor()
        if cursor.execute(
            "SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'wrapup_triggered' LIMIT 1", (task_id,),
        ).fetchone():
            conn.rollback()
            logger.info("Wrap-up already triggered for task %s; skipping duplicate spawn", task_id)
            return None
        row = cursor.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'debrief' ORDER BY id DESC LIMIT 1", (task_id,),
        ).fetchone()
        payload = json.loads(row[0]) if row and row[0] else {}
        chore_title, chore_body = _build_wrapup_chore_spec(
            task_id, orig_title, payload.get("pr_url"), payload.get("summary", ""), payload.get("frictions", "None"),
        )
        full_chore_title = f"[KB-{short_id}] {chore_title}"
        _insert_task_on_conn(
            conn, task_id=chore_id, full_title=full_chore_title, final_body=chore_body,
            routing=routing, initial_status="ready", priority=1, workspace_kind="scratch",
            await_approval=False, parent_ids=(), open_parents=[], now=now,
        )
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'wrapup_triggered', ?, ?)",
            (task_id, json.dumps({"chore_id": chore_id, "title": full_chore_title}), now),
        )
        conn.commit()
        return full_chore_title, chore_body
    except Exception as e:
        conn.rollback()
        logger.error("Failed to auto-spawn session wrap-up chore for %s: %s", task_id, e)
        return None
    finally:
        conn.close()


def _notify_wrapup_spawned(
    chore_id: str, full_chore_title: str, chore_body: str,
    routing: TaskRoutingResolution, thread_id: Optional[str], now: int,
) -> None:
    _provision_create_thread(
        chore_id, full_chore_title, chore_body, "ready", 1, "vor", routing.complexity, routing.model_override, now,
    )
    if not thread_id:
        return
    try:
        post_thread_lifecycle_update(
            thread_id=thread_id, status="wrapup_triggered",
            message=f"📌 Triggered automated session wrap-up chore `{chore_id}` (`vor`) to sync infrastructure notes, journal, and self-improvement hardening.",
        )
    except Exception as e:
        logger.warning("Failed to post wrapup_triggered update to thread %s: %s", thread_id, e)


def trigger_session_wrapup_chore(task_id: str, orig_title: str, thread_id: Optional[str] = None) -> Optional[str]:
    """Auto-spawns an autonomous session wrap-up chore once when a dev/engineering task transitions to done."""
    if _should_skip_wrapup_title(orig_title):
        logger.info("Skipping wrap-up trigger for maintenance/wrap-up/proposal task %s", task_id)
        return None
    now = int(time.time())
    short_id = uuid.uuid4().hex[:8]
    chore_id = f"kb-{short_id}"
    vor_model = resolve_flash_fast_model("vor")
    routing = TaskRoutingResolution(
        resolved_assignee="vor", model_override=vor_model, provider_override="antigravity-subscription-directsdk",
        complexity={
            "tier": "flash_fast", "recommended_assignee": "vor", "recommended_model": vor_model,
            "raw_verdict": "auto_spawn_session_wrapup", "latency_seconds": 0.0,
        },
        initial_status="ready",
    )
    persisted = _persist_wrapup_chore(task_id, orig_title, chore_id, short_id, routing, now)
    if not persisted:
        return None
    full_chore_title, chore_body = persisted
    logger.info("Auto-spawned session wrap-up chore %s (vor) for completed task %s", chore_id, task_id)
    _notify_wrapup_spawned(chore_id, full_chore_title, chore_body, routing, thread_id, now)
    return chore_id


def _record_handoff_events(
    cursor: sqlite3.Cursor, task_id: str, from_assignee: str, resolved_to: str,
    summary: str, pr_url: str, model_override: Optional[str],
    next_status: str, open_parents: list[str], now: int,
) -> None:
    event_payload = {
        "from_assignee": from_assignee, "to_assignee": resolved_to,
        "summary": summary, "pr_url": pr_url,
        "model_override": model_override, "status": next_status,
    }
    cursor.execute(
        "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'handoff', ?, ?)",
        (task_id, json.dumps(event_payload), now),
    )
    if open_parents:
        wait_payload = {"reason": "parent_not_done", "parent": open_parents[0], "parents": open_parents}
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'dependency_wait', ?, ?)",
            (task_id, json.dumps(wait_payload), now),
        )


def _persist_handoff_tx(
    task_id: str, resolved_to: str, summary: str, pr_url: str, tier: Optional[str], now: int,
) -> Tuple[Dict[str, Any], str]:
    conn = sqlite3.connect(KANBAN_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT title, body, assignee FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Task {task_id} not found in kanban database")
        orig_title, orig_body, from_assignee = row[0] or "", row[1] or "", row[2] or "unknown"
        handoff_block = f"\n\n---\n### Pipeline Handoff ({from_assignee} -> {resolved_to})\n{summary.strip()}"
        if pr_url:
            handoff_block += f"\n**PR / Branch:** {pr_url}"
        model_override, provider_override = resolve_handoff_model(resolved_to, tier=tier)
        open_parents = _fetch_open_parents_for_child(cursor, task_id)
        next_status = "todo" if open_parents else "ready"
        cursor.execute(
            "UPDATE tasks SET assignee = ?, status = ?, body = ?, model_override = ?, provider_override = ?, "
            "consecutive_failures = 0, last_failure_error = NULL WHERE id = ?",
            (resolved_to, next_status, f"{orig_body}{handoff_block}", model_override, provider_override, task_id),
        )
        _record_handoff_events(
            cursor, task_id, from_assignee, resolved_to, summary, pr_url, model_override, next_status, open_parents, now,
        )
        thread_id = _fetch_discord_thread_id(cursor, task_id)
        conn.commit()
    finally:
        conn.close()
    result = {
        "task_id": task_id, "status": next_status, "open_parents": open_parents,
        "from_assignee": from_assignee, "to_assignee": resolved_to, "model_override": model_override,
        "summary": summary, "pr_url": pr_url, "thread_id": thread_id,
    }
    return result, orig_title


def _notify_handoff_thread(result: Dict[str, Any]) -> None:
    thread_id = result["thread_id"]
    if not thread_id:
        return
    fields = [
        {"name": "Task ID", "value": f"`{result['task_id']}`", "inline": True},
        {"name": "Pipeline Handoff", "value": f"`{result['from_assignee']}` -> `{result['to_assignee']}`", "inline": True},
        {"name": "Model", "value": f"`{result['model_override'] or 'human/raymund'}`", "inline": True},
    ]
    if result["pr_url"]:
        fields.append({"name": "PR / Branch", "value": result["pr_url"], "inline": False})
    post_thread_lifecycle_update(
        thread_id=thread_id, status=result["status"],
        message=f"**Handoff Summary (`{result['from_assignee']}` -> `{result['to_assignee']}`):**\n{result['summary']}",
        fields=fields,
    )


def handoff_kanban_task(
    task_id: str, to_assignee: str, summary: str, pr_url: str = "", tier: Optional[str] = None,
) -> Dict[str, Any]:
    """Transitions a Kanban card to the next specialist in an async pipeline without conversational chat."""
    resolved_to = normalize_assignee(to_assignee)
    result, orig_title = _persist_handoff_tx(task_id, resolved_to, summary, pr_url, tier, int(time.time()))
    _notify_handoff_thread(result)
    logger.info(
        "Handed off task %s (%s): %s -> %s (%s, %s)",
        task_id, orig_title, result["from_assignee"], resolved_to, result["model_override"], result["status"],
    )
    return result


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
    cursor.execute("SELECT title, body, assignee, consecutive_failures, model_override FROM tasks WHERE id = ?", (task_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        raise ValueError(f"Task {task_id} not found in kanban database")

    title, body, current_assignee, current_failures, prev_model = (
        row[0] or "",
        row[1] or "",
        row[2] or "unknown",
        row[3] or 0,
        row[4] or "",
    )
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
        model_override = SPECIALIST_MODELS.get(next_assignee, "gemini-4-hydrogen-high")
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
            recovery = evaluate_two_way_recovery(
                current_assignee=current_assignee,
                prev_model=prev_model,
                failures=failures,
                max_retries=max_retries,
                err_msg=err_msg,
            )
            next_assignee = recovery.next_assignee
            model_override = recovery.model_override
            provider_override = recovery.provider_override
            escalated_from = recovery.escalated_from
            new_body = f"{body}{recovery.retry_block}"

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
                (
                    task_id,
                    json.dumps(
                        {
                            "gate": current_assignee,
                            "feedback": err_msg,
                            "failures": failures,
                            "max_retries": max_retries,
                            "model_override": model_override,
                            "escalated_from": escalated_from,
                        }
                    ),
                    now,
                ),
            )
            conn.commit()
            conn.close()

            if thread_id:
                fields = [
                    {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
                    {"name": "Verification Gate", "value": f"`{current_assignee}`: **FAILED**", "inline": True},
                    {"name": "Retry Attempt", "value": f"`{failures}/{max_retries}` -> `sindri` (`{model_override}`)", "inline": True},
                ]
                post_thread_lifecycle_update(
                    thread_id=thread_id,
                    status="ready",
                    message=f"⚠️ **Verification Gate Failed (Attempt {failures}/{max_retries}):**\nAutomatically bouncing task back to `{next_assignee}` (`{model_override}`) for remediation.{recovery.escalation_note}\n\n**Error Details:**\n```\n{err_msg[:500]}\n```",
                    fields=fields,
                )

            logger.warning("Task %s verification FAILED at gate %s (attempt %d/%d) -> bouncing to sindri (%s)", task_id, current_assignee, failures, max_retries, model_override)
            return {
                "task_id": task_id,
                "status": "ready",
                "passed": False,
                "current_assignee": current_assignee,
                "next_assignee": next_assignee,
                "model_override": model_override,
                "escalated_from": escalated_from,
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


@dataclass(frozen=True)
class _LifecycleUpdateResult:
    ok: bool
    changed: bool
    thread_id: Optional[str]
    task_title: str
    created_by: str


def _commit_lifecycle_update(
    task_id: str, canonical_status: str, new_status: str, message: str, force: bool,
) -> _LifecycleUpdateResult:
    conn = sqlite3.connect(KANBAN_DB_PATH)
    try:
        cursor = conn.cursor()
        row = cursor.execute("SELECT status, title, created_by FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            logger.warning("Task %s not found in kanban database", task_id)
            return _LifecycleUpdateResult(False, False, None, "", "")
        current_status, task_title, created_by = row[0], (row[1] or ""), (row[2] or "")
        if current_status == canonical_status:
            logger.info("Task %s is already '%s'; skipping no-op lifecycle update.", task_id, canonical_status)
            return _LifecycleUpdateResult(True, False, None, task_title, created_by)
        if current_status == "done" and canonical_status == "in_review" and not force:
            logger.warning("Refusing status regression 'done' -> 'in_review' for task %s (pass force=True to override).", task_id)
            return _LifecycleUpdateResult(False, False, None, task_title, created_by)
        now = int(time.time())
        extra = ", started_at = ?" if canonical_status in ("running", "in_progress") else (", completed_at = ?" if canonical_status == "done" else "")
        params = [canonical_status, now, task_id] if extra else [canonical_status, task_id]
        cursor.execute(f"UPDATE tasks SET status = ?{extra} WHERE id = ?", params)
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'status_changed', ?, ?)",
            (task_id, json.dumps({"status": new_status, "message": message}), now),
        )
        thread_id = _fetch_discord_thread_id(cursor, task_id)
        conn.commit()
        return _LifecycleUpdateResult(True, True, thread_id, task_title, created_by)
    finally:
        conn.close()


def update_task_lifecycle(task_id: str, new_status: str, message: str, force: bool = False) -> bool:
    """Updates task status in kanban.db and notifies its assigned Discord thread."""
    canonical_status = STATUS_CANONICAL.get(new_status.lower(), new_status.lower())
    res = _commit_lifecycle_update(task_id, canonical_status, new_status, message, force)
    if not res.ok:
        return False
    if not res.changed:
        return True
    if res.thread_id:
        fields = [
            {"name": "Task ID", "value": f"`{task_id}`", "inline": True},
            {"name": "New Status", "value": f"`{new_status.upper()}`", "inline": True},
        ]
        post_thread_lifecycle_update(res.thread_id, new_status, message, fields=fields)
    if canonical_status == "done":
        if res.created_by.strip().lower() == "ide":
            logger.info(
                "Skipping background Hermes wrap-up chore for IDE-owned task %s; wrap-up runs interactively in IDE.",
                task_id,
            )
        else:
            trigger_session_wrapup_chore(task_id, res.task_title, res.thread_id)
    return True


def _extract_pr_url(summary: str, pr_url: Optional[str]) -> Optional[str]:
    if pr_url or ("github.com/" not in summary or "/pull/" not in summary):
        return pr_url
    match = re.search(r"https://github\.com/[^\s)]+/pull/\d+", summary)
    return match.group(0) if match else None


def _fetch_task_for_debrief(task_id: str) -> Tuple[str, str]:
    conn = sqlite3.connect(KANBAN_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("SELECT title, body FROM tasks WHERE id = ?", (task_id,))
        row = cursor.fetchone()
        if not row:
            raise ValueError(f"Task {task_id} not found in kanban database")
        return row[0] or "", row[1] or ""
    finally:
        conn.close()


def _write_debrief_tx(
    task_id: str, payload: Dict[str, Any], extracted_pr: Optional[str], now: int,
) -> Optional[str]:
    conn = sqlite3.connect(KANBAN_DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'debrief', ?, ?)",
            (task_id, json.dumps(payload), now),
        )
        if extracted_pr:
            status_row = cursor.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if status_row and status_row[0] == "done":
                logger.info("Skipping debrief auto-transition to 'in_review' for already-done task %s", task_id)
            else:
                cursor.execute("UPDATE tasks SET status = 'in_review' WHERE id = ?", (task_id,))
                logger.info("Transitioned task %s to 'in_review' due to PR: %s", task_id, extracted_pr)
        thread_id = _fetch_discord_thread_id(cursor, task_id)
        conn.commit()
        return thread_id
    finally:
        conn.close()


def _resolve_debrief_action(
    auto_fix: bool, friction_gate: Optional[Dict[str, Any]], chore_title: Optional[str],
) -> str:
    if auto_fix:
        if friction_gate is not None:
            friction_gate["action"] = "auto_fix_script"
        return "auto_fix_script"
    if friction_gate:
        return friction_gate.get("action") or "ignore_transient"
    return "propose_review" if chore_title else "ignore_transient"


def _spawn_debrief_autofix(
    task_id: str, task_title: str, frictions: Optional[str],
    chore_title: Optional[str], friction_gate: Optional[Dict[str, Any]], friction_assignee: str,
) -> Tuple[Optional[str], Optional[str]]:
    raw_title = chore_title or f"Fix failing script in {task_id}"
    ctitle = raw_title if raw_title.lower().startswith("[auto-fix]") else f"[Auto-Fix] {raw_title}"
    cbody = (
        f"Autonomous Tier 2 script/path repair spawned from task debrief for `{task_id}`:\n\n"
        f"**Failing Script / Friction Report:**\n{frictions}\n\n"
        f"**Origin Task:** {task_title} (`{task_id}`)\n\n"
        f"**Execution Mandate:** Fix the failing existing script, broken path, or CLI flag directly and verify with tests (`validate_skill.py --all` if touching skills). Do not create brand-new skills or alter canonical rules in `AGENTS.md`."
    )
    chore_tier = "frontier_deep" if friction_assignee == "sindri" else "flash_fast"
    precomputed = {
        "tier": chore_tier,
        "recommended_assignee": friction_assignee,
        "recommended_model": SPECIALIST_MODELS.get(friction_assignee, "gemini-4-hydrogen-high"),
        "raw_verdict": friction_gate.get("raw_verdict", "auto_fix_script") if friction_gate else "auto_fix_script",
        "latency_seconds": friction_gate.get("latency_seconds", 0.0) if friction_gate else 0.0,
    }
    return create_kanban_task(
        title=ctitle, body=cbody, priority=1, assignee=friction_assignee, precomputed_complexity=precomputed,
    )


def _spawn_debrief_followup(
    action: str, task_id: str, task_title: str, frictions: Optional[str],
    chore_title: Optional[str], batch_gates: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str]]:
    friction_gate = batch_gates["friction_gate"]
    friction_assignee = batch_gates.get("friction_assignee") or "vor"
    if action == "auto_fix_script":
        return _spawn_debrief_autofix(task_id, task_title, frictions, chore_title, friction_gate, friction_assignee)
    if action in ("propose_review", "spawn_chore") or bool(chore_title):
        ctitle = chore_title or f"Address friction in {task_id}"
        verdict = friction_gate.get("raw_verdict", "Explicit proposal requested") if friction_gate else "Explicit proposal requested"
        cproposal = (
            f"Formulate and implement a targeted fix for the friction encountered during `{task_id}` ({task_title}).\n"
            f"- **Jev Triage Verdict:** {verdict}"
        )
        return create_improvement_proposal(
            title=ctitle, friction=frictions or "See task debrief summary.",
            proposal=cproposal, target_specialist=friction_assignee, origin_task_id=task_id,
        )
    if friction_gate and friction_gate.get("action") == "ignore_transient":
        logger.info("Friction in task %s classified as transient noise; suppressed follow-up proposal.", task_id)
    return None, None


def record_task_debrief(
    task_id: str, summary: str, frictions: Optional[str] = None,
    chore_title: Optional[str] = None, pr_url: Optional[str] = None, auto_fix: bool = False,
) -> Dict[str, Any]:
    """Records a task debrief / retrospective receipt and routes Tier 2 auto-fix vs Tier 3 proposal follow-ups."""
    now = int(time.time())
    task_title, task_body = _fetch_task_for_debrief(task_id)
    extracted_pr = _extract_pr_url(summary, pr_url)
    batch_gates = evaluate_debrief_gates_batch(
        task_title=task_title, task_body=task_body, debrief_summary=summary, pr_url=extracted_pr, friction_text=frictions,
    )
    payload = {
        "summary": summary, "frictions": frictions or "None", "pr_url": extracted_pr, "timestamp": now,
        "acceptance_gate": batch_gates["acceptance_gate"],
        "auto_merge_gate": batch_gates["auto_merge_gate"],
        "friction_gate": batch_gates["friction_gate"],
    }
    thread_id = _write_debrief_tx(task_id, payload, extracted_pr, now)
    action = _resolve_debrief_action(auto_fix, batch_gates["friction_gate"], chore_title)
    chore_id, chore_thread_id = _spawn_debrief_followup(action, task_id, task_title, frictions, chore_title, batch_gates)
    if thread_id:
        post_thread_debrief(
            thread_id=thread_id, task_id=task_id, summary=summary, frictions=frictions,
            chore_task_id=chore_id, pr_url=extracted_pr,
            acceptance_gate=batch_gates["acceptance_gate"],
            auto_merge_gate=batch_gates["auto_merge_gate"],
            friction_gate=batch_gates["friction_gate"],
        )
    return {
        "task_id": task_id, "summary": summary, "frictions": frictions, "chore_id": chore_id,
        "chore_thread_id": chore_thread_id, "thread_id": thread_id, "pr_url": extracted_pr,
        "acceptance_gate": batch_gates["acceptance_gate"],
        "auto_merge_gate": batch_gates["auto_merge_gate"],
        "friction_gate": batch_gates["friction_gate"],
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

