"""Pre-compression Auckland journal checkpointing, replay suppression, and Discord handover."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Protocol
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

_AUCKLAND_TZ = ZoneInfo("Pacific/Auckland")
_SLEEP_ROLLOVER_HOUR = 6
_DISCORD_API_BASE = "https://discord.com/api/v10"
_DISCORD_USER_AGENT = "Yggdrasil-Odin/1.0 (DiscordBot)"
_SKIP_TITLE_PREFIXES: tuple[str, ...] = ("⚡", "🍽️", "🚨")
_WRAPUP_KEYWORDS: tuple[str, ...] = (
    "wrap up",
    "wrap-up",
    "wrapup",
    "close out",
    "close-out",
    "closeout",
    "session-wrapup",
    "session wrapup",
)
_DEFAULT_VAULT_CANDIDATES: tuple[Path, ...] = (
    Path("/app/vault"),
    Path("/opt/atlas/vault"),
)
_DEFAULT_STATE_DB = Path("/opt/data/state.db")


class DiscordThreadPort(Protocol):
    """Single external boundary for Discord thread migration and archival."""

    def create_thread(
        self, parent_channel_id: str, title: str, starter_content: str
    ) -> str | None: ...

    def post_notice(self, thread_id: str, content: str) -> None: ...

    def schedule_archive(self, thread_id: str, delay_seconds: float = 120.0) -> None: ...


@dataclass(frozen=True)
class HandoverConfig:
    """Explicit runtime configuration for pre-compression checkpoints and handover."""

    vault_root: Path | None
    state_db: Path
    info_channel_id: str
    user_id: str

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        vault_candidates: Sequence[Path] = _DEFAULT_VAULT_CANDIDATES,
        state_db: Path = _DEFAULT_STATE_DB,
    ) -> HandoverConfig:
        """Resolve vault path, state.db, DISCORD_CHANNEL_INFO_ID, and user ID from env."""
        source = os.environ if env is None else env
        vault_root = next((p for p in vault_candidates if p.is_dir()), None)
        info_channel_id = (source.get("DISCORD_CHANNEL_INFO_ID") or "").strip()
        allowed_first = (source.get("DISCORD_ALLOWED_USERS") or "").split(",")[0].strip()
        user_id = (source.get("DISCORD_USER_ID") or allowed_first).strip()
        return cls(
            vault_root=vault_root,
            state_db=state_db,
            info_channel_id=info_channel_id,
            user_id=user_id,
        )


@dataclass(frozen=True)
class HandoverResult:
    """Outcome summary returned by `run_pre_compress_handover`."""

    journal_file: Path | None = None
    new_thread_id: str | None = None
    skipped_reason: str | None = None


@dataclass(frozen=True)
class _SessionRow:
    source: str
    chat_id: str
    thread_id: str
    title: str
    origin: dict[str, Any]


class UrllibDiscordAdapter:
    """Production Discord REST v10 adapter using stdlib urllib and daemon Timer."""

    def __init__(
        self,
        bot_token: str,
        *,
        api_base: str = _DISCORD_API_BASE,
        user_agent: str = _DISCORD_USER_AGENT,
        timeout: float = 15.0,
    ) -> None:
        if not bot_token or not bot_token.strip():
            raise ValueError("bot_token must be non-empty")
        self._bot_token = bot_token.strip()
        self._api_base = api_base.rstrip("/")
        self._user_agent = user_agent
        self._timeout = timeout

    def create_thread(
        self, parent_channel_id: str, title: str, starter_content: str
    ) -> str | None:
        """Create a continuation thread, retrying without type=11 for forum channels."""
        url = f"{self._api_base}/channels/{parent_channel_id}/threads"
        payload: dict[str, Any] = {
            "name": title,
            "type": 11,
            "message": {"content": starter_content},
        }
        try:
            data = self._request_json("POST", url, payload)
            return str(data["id"]) if data.get("id") else None
        except urllib.error.HTTPError as err:
            return self._handle_create_http_error(err, url, title, starter_content)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("[odin-handover] Error creating continuation thread: %s", exc)
            return None

    def post_notice(self, thread_id: str, content: str) -> None:
        """Post a migration notice message into the retiring thread."""
        url = f"{self._api_base}/channels/{thread_id}/messages"
        try:
            self._request_json("POST", url, {"content": content})
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("[odin-handover] Failed to post migration notice: %s", exc)

    def schedule_archive(self, thread_id: str, delay_seconds: float = 120.0) -> None:
        """Archive the retiring thread asynchronously after `delay_seconds`."""
        timer = threading.Timer(delay_seconds, self._archive_thread, args=(thread_id,))
        timer.daemon = True
        timer.start()

    def _archive_thread(self, thread_id: str) -> None:
        url = f"{self._api_base}/channels/{thread_id}"
        try:
            self._request_json("PATCH", url, {"archived": True})
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
            logger.warning("[odin-handover] Failed to archive thread %s: %s", thread_id, exc)

    def _handle_create_http_error(
        self,
        err: urllib.error.HTTPError,
        url: str,
        title: str,
        starter_content: str,
    ) -> str | None:
        body = err.read().decode("utf-8", errors="replace")
        if err.code == 400 and ("type" in body.lower() or "forum" in body.lower()):
            retry_payload = {"name": title, "message": {"content": starter_content}}
            try:
                data = self._request_json("POST", url, retry_payload)
                return str(data["id"]) if data.get("id") else None
            except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError) as exc:
                logger.warning("[odin-handover] Forum thread retry failed: %s", exc)
                return None
        logger.warning("[odin-handover] Discord thread creation failed (HTTP %s): %s", err.code, body)
        return None

    def _request_json(
        self, method: str, url: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bot {self._bot_token}",
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method=method,
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}


class FakeDiscordAdapter:
    """In-memory DiscordThreadPort test double capturing calls deterministically."""

    def __init__(self, next_thread_id: str | None = "thread-part-2") -> None:
        self.next_thread_id = next_thread_id
        self.created_threads: list[tuple[str, str, str]] = []
        self.notices: list[tuple[str, str]] = []
        self.archived: list[tuple[str, float]] = []

    def create_thread(
        self, parent_channel_id: str, title: str, starter_content: str
    ) -> str | None:
        self.created_threads.append((parent_channel_id, title, starter_content))
        return self.next_thread_id

    def post_notice(self, thread_id: str, content: str) -> None:
        self.notices.append((thread_id, content))

    def schedule_archive(self, thread_id: str, delay_seconds: float = 120.0) -> None:
        self.archived.append((thread_id, delay_seconds))


def strip_dashes(text: str) -> str:
    """Replace forbidden em/en dashes with ASCII hyphens."""
    return text.replace("\u2014", "-").replace("\u2013", "-")


def auckland_journal_date(now_utc: datetime | None = None) -> tuple[date, str]:
    """Return `(journal_date, timestamp_label)` honoring the 06:00 NZDT sleep rollover."""
    current = datetime.now(timezone.utc) if now_utc is None else now_utc
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    now_nz = current.astimezone(_AUCKLAND_TZ)
    journal_date = (
        (now_nz - timedelta(days=1)).date()
        if now_nz.hour < _SLEEP_ROLLOVER_HOUR
        else now_nz.date()
    )
    tz_abbr = now_nz.strftime("%Z") or "NZDT"
    return journal_date, f"{now_nz.strftime('%H:%M')} {tz_abbr}"


def clean_compaction_summary(summary: str) -> str:
    """Strip internal compaction preambles, trailing indexes, and em/en dashes."""
    cleaned = strip_dashes(summary or "").strip()
    if cleaned.startswith("[CONTEXT COMPACTION"):
        first_heading = cleaned.find("\n## ")
        if first_heading != -1:
            cleaned = cleaned[first_heading + 1 :].strip()
    for section in ("\n## Anchor Index", "\n## Context Recovery"):
        idx = cleaned.find(section)
        if idx != -1:
            cleaned = cleaned[:idx].strip()
    return cleaned


def extract_user_queries(
    turns_to_summarize: Sequence[Mapping[str, Any]], *, limit: int = 5
) -> list[str]:
    """Collect deduplicated, dash-free user inquiry headlines from compacted turns."""
    queries: list[str] = []
    for msg in turns_to_summarize:
        if not isinstance(msg, Mapping) or msg.get("role") != "user":
            continue
        text = _extract_content_text(msg.get("content")).strip()
        if not text:
            continue
        first_line = strip_dashes(text.splitlines()[0])
        truncated = f"{first_line[:157]}..." if len(first_line) > 160 else first_line
        if truncated and truncated not in queries:
            queries.append(truncated)
            if len(queries) >= limit:
                break
    return queries


def write_journal_checkpoint(
    vault_root: Path,
    *,
    summary: str,
    turns_count: int,
    user_queries: Sequence[str],
    display_tokens: int | None = None,
    now_utc: datetime | None = None,
) -> Path:
    """Append a dash-free pre-compression checkpoint to the Auckland Daily Journal."""
    journal_date, timestamp_str = auckland_journal_date(now_utc)
    date_str = journal_date.isoformat()
    journal_file = (
        vault_root
        / "second-brain"
        / "Journal"
        / f"{journal_date.year:04d}"
        / f"{journal_date.month:02d}"
        / f"{date_str}.md"
    )
    checkpoint = _format_checkpoint(
        timestamp_str=timestamp_str,
        display_tokens=display_tokens,
        turns_count=turns_count,
        user_queries=user_queries,
        clean_summary=summary,
    )
    journal_file.parent.mkdir(parents=True, exist_ok=True)
    if journal_file.exists():
        existing = journal_file.read_text(encoding="utf-8")
        updated = existing.rstrip() + "\n\n---\n\n" + checkpoint + "\n"
    else:
        updated = (
            f"---\ndate: {date_str}\ntags: [journal, auto-checkpoint]\n"
            f'location: "Auckland"\n---\n\n## Journal Entry\n\n{checkpoint}\n'
        )
    journal_file.write_text(strip_dashes(updated), encoding="utf-8")
    return journal_file


def should_skip_handover_thread(
    chat_name: str, parent_chat_name: str, title: str
) -> bool:
    """Return True for health/workout/fasting/alert threads that must stay singletons."""
    lowered_chat = chat_name.lower()
    lowered_title = title.lower()
    return (
        "/ health /" in lowered_chat
        or parent_chat_name.lower() == "health"
        or title.startswith(_SKIP_TITLE_PREFIXES)
        or "workout" in lowered_title
        or "fasting" in lowered_title
    )


def next_part_title(title: str, max_len: int = 95) -> str:
    """Increment `Topic` -> `Topic - Part 2` -> `Topic - Part 3`, capped at `max_len`."""
    match = re.search(r"-\s*Part\s*(\d+)$", title, re.IGNORECASE)
    if match:
        next_part = int(match.group(1)) + 1
        candidate = re.sub(
            r"-\s*Part\s*\d+$", f"- Part {next_part}", title, flags=re.IGNORECASE
        )
    else:
        candidate = f"{title} - Part 2"
    return candidate[:max_len]


def resolve_parent_channel_id(
    origin: Mapping[str, Any],
    chat_id: str | None,
    thread_id: str,
    info_channel_id: str,
) -> str | None:
    """Resolve continuation parent channel, falling back strictly to `DISCORD_CHANNEL_INFO_ID`."""
    parent = str(origin.get("parent_chat_id") or chat_id or "").strip()
    if parent and parent != thread_id:
        return parent
    fallback = info_channel_id.strip()
    return fallback or None


def should_suppress_inflight_replay(
    messages: Sequence[Mapping[str, Any]], last_user_idx: int
) -> bool:
    """Suppress compaction task replay for wrapups, stale headers, or answered turns."""
    if last_user_idx < 0 or last_user_idx >= len(messages):
        return False
    user_content = _extract_content_text(messages[last_user_idx].get("content")).lower()
    if should_suppress_reappend_text(user_content):
        return True
    subsequent = messages[last_user_idx + 1 :]
    for msg in reversed(subsequent):
        if not isinstance(msg, Mapping) or msg.get("role") != "assistant":
            return _has_delivered_assistant_answer(subsequent)
        if msg.get("tool_calls"):
            return False
        if _extract_content_text(msg.get("content")).strip():
            return True
    return False


def should_suppress_reappend_text(task_text: str) -> bool:
    """Return True if restated task text is a wrapup request or stale `[STILL IN PROGRESS`."""
    lowered = (task_text or "").lower()
    return "[still in progress" in lowered or any(kw in lowered for kw in _WRAPUP_KEYWORDS)


def run_pre_compress_handover(
    *,
    turns_to_summarize: Sequence[Mapping[str, Any]],
    summary: str,
    display_tokens: int | None = None,
    session_id: str | None = None,
    config: HandoverConfig | None = None,
    discord_port: DiscordThreadPort | None = None,
    env: Mapping[str, str] | None = None,
    now_utc: datetime | None = None,
) -> HandoverResult:
    """Execute Auckland journal checkpointing and optional Discord thread handover."""
    cfg = HandoverConfig.from_env(env) if config is None else config
    clean_summary = clean_compaction_summary(summary)
    user_queries = extract_user_queries(turns_to_summarize)
    journal_file: Path | None = None
    if cfg.vault_root is not None:
        journal_file = write_journal_checkpoint(
            cfg.vault_root, summary=clean_summary, turns_count=len(turns_to_summarize),
            user_queries=user_queries, display_tokens=display_tokens, now_utc=now_utc,
        )
    new_thread_id, skipped = _run_discord_handover(
        cfg=cfg, session_id=session_id, clean_summary=clean_summary,
        display_tokens=display_tokens, user_queries=user_queries,
        discord_port=discord_port, env=env,
    )
    return HandoverResult(journal_file=journal_file, new_thread_id=new_thread_id, skipped_reason=skipped)


def _run_discord_handover(
    *,
    cfg: HandoverConfig,
    session_id: str | None,
    clean_summary: str,
    display_tokens: int | None,
    user_queries: Sequence[str],
    discord_port: DiscordThreadPort | None,
    env: Mapping[str, str] | None,
) -> tuple[str | None, str | None]:
    if not session_id or not cfg.state_db.is_file():
        return None, "no_session_state"
    row = _load_session_row(cfg.state_db, session_id)
    if row is None or row.source != "discord" or not row.thread_id:
        return None, "not_discord_thread"
    chat_name = str(row.origin.get("chat_name") or "").strip()
    title = _derive_thread_title(chat_name, row.title)
    if should_skip_handover_thread(chat_name, str(row.origin.get("parent_chat_name") or ""), title):
        return None, "entity_thread_skipped"
    parent_id = resolve_parent_channel_id(row.origin, row.chat_id, row.thread_id, cfg.info_channel_id)
    if parent_id is None:
        logger.warning("[odin-handover] Missing parent_chat_id and DISCORD_CHANNEL_INFO_ID; skipping handover")
        return None, "missing_parent_channel"
    port = _resolve_discord_port(discord_port, env)
    if port is None:
        return None, "missing_bot_token"
    return _execute_thread_migration(
        port=port, parent_id=parent_id, old_thread_id=row.thread_id,
        new_title=next_part_title(title), summary=clean_summary,
        display_tokens=display_tokens, user_queries=user_queries, user_id=cfg.user_id,
    )


def _resolve_discord_port(
    discord_port: DiscordThreadPort | None,
    env: Mapping[str, str] | None,
) -> DiscordThreadPort | None:
    if discord_port is not None:
        return discord_port
    source = os.environ if env is None else env
    token = (source.get("DISCORD_BOT_TOKEN") or "").strip()
    if not token:
        logger.debug("[odin-handover] DISCORD_BOT_TOKEN unset; skipping Discord handover")
        return None
    return UrllibDiscordAdapter(token)


def _execute_thread_migration(
    *,
    port: DiscordThreadPort,
    parent_id: str,
    old_thread_id: str,
    new_title: str,
    summary: str,
    display_tokens: int | None,
    user_queries: Sequence[str],
    user_id: str,
) -> tuple[str | None, str | None]:
    tok_str = f"{display_tokens:,}" if display_tokens else "140,000+"
    starter = _build_starter_content(
        old_thread_id=old_thread_id,
        tok_str=tok_str,
        summary=summary,
        user_queries=user_queries,
        user_id=user_id,
    )
    new_thread_id = port.create_thread(parent_id, new_title, starter)
    if not new_thread_id:
        return None, "thread_create_failed"
    notice = strip_dashes(
        f"Context threshold reached (~{tok_str} tokens). "
        f"To prevent compacted-context degradation, conversation has been migrated to <#{new_thread_id}>.\n\n"
        "Archiving this thread."
    )
    port.post_notice(old_thread_id, notice)
    port.schedule_archive(old_thread_id, 120.0)
    return new_thread_id, None


def _load_session_row(db_path: Path, session_id: str) -> _SessionRow | None:
    try:
        with closing(sqlite3.connect(str(db_path))) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT source, chat_id, thread_id, title, origin_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
    except sqlite3.Error as exc:
        logger.warning("[odin-handover] Failed to query session state: %s", exc)
        return None
    if row is None:
        return None
    try:
        origin = json.loads(row["origin_json"] or "{}")
    except ValueError as exc:
        logger.warning("[odin-handover] Invalid origin_json for %s: %s", session_id, exc)
        origin = {}
    return _SessionRow(
        source=str(row["source"] or "").lower(),
        chat_id=str(row["chat_id"] or ""),
        thread_id=str(row["thread_id"] or "").strip(),
        title=str(row["title"] or ""),
        origin=origin if isinstance(origin, dict) else {},
    )


def _derive_thread_title(chat_name: str, fallback_title: str) -> str:
    if chat_name and " / " in chat_name:
        discord_title = chat_name.split(" / ")[-1].strip()
    else:
        discord_title = chat_name
    raw = (discord_title or fallback_title or "Discussion").strip()
    return strip_dashes(raw)


def _format_checkpoint(
    *,
    timestamp_str: str,
    display_tokens: int | None,
    turns_count: int,
    user_queries: Sequence[str],
    clean_summary: str,
) -> str:
    lines = [
        f"### Auto-Checkpoint (Pre-Compression {timestamp_str})",
        "- **Trigger**: Automatic pre-compression checkpoint before context compaction",
    ]
    if display_tokens:
        lines.append(
            f"- **Context Usage**: ~{display_tokens:,} tokens ({turns_count} turns compacted)"
        )
    else:
        lines.append(f"- **Compacted Turns**: {turns_count} turns")
    if user_queries:
        lines.append("- **User Inquiries Compacted**:")
        lines.extend(f"  * {query}" for query in user_queries[:5])
    if clean_summary:
        lines.append("- **Compacted Summary**:")
        lines.extend(
            f"  > {line}" if line.strip() else "  >"
            for line in clean_summary.splitlines()
        )
    return strip_dashes("\n".join(lines))


def _build_starter_content(
    *,
    old_thread_id: str,
    tok_str: str,
    summary: str,
    user_queries: Sequence[str],
    user_id: str,
) -> str:
    mention = f"<@{user_id}> " if user_id else ""
    lines = [
        f"{mention}Continuing from <#{old_thread_id}>. Context reached ~{tok_str} tokens; "
        "transitioned to this fresh thread to prevent compacted context degradation.",
        "",
        "### Handover Context",
    ]
    if summary:
        lines.extend(f"> {line}" if line.strip() else ">" for line in summary.splitlines())
    else:
        lines.append("> Previous session context preserved.")
    if user_queries:
        lines.extend(["", "### Recent Focus Topics"])
        lines.extend(f"- {query}" for query in user_queries[:3])
    lines.extend(
        [
            "",
            "### Status",
            "- Fresh session active with 0 context tokens consumed.",
            "- Full reasoning power restored.",
            f"- Previous thread (<#{old_thread_id}>) archived.",
            "",
            "Ready for next instruction.",
        ]
    )
    content = strip_dashes("\n".join(lines))
    if len(content) > 1950:
        return content[:1940] + "\n\n[Context truncated for Discord 2k limit]"
    return content


def _extract_content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(item.get("text") or "")
            for item in content
            if isinstance(item, Mapping) and item.get("type") == "text"
        ]
        return "\n".join(p for p in parts if p)
    return ""


def _has_delivered_assistant_answer(subsequent: Sequence[Mapping[str, Any]]) -> bool:
    return any(
        isinstance(msg, Mapping)
        and msg.get("role") == "assistant"
        and not msg.get("tool_calls")
        and bool(_extract_content_text(msg.get("content")).strip())
        for msg in subsequent
    )
