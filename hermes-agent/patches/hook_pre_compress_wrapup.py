#!/usr/bin/env python3
"""Build-time patch: pre-compression wrapup checkpoint hook, automated Discord thread handover,
and compaction task replay suppression in ContextCompressor.

Problem
-------
1. When interacting with Hermes Agent via Discord or Open-WebUI, users cannot monitor
   real-time context token metrics. As sessions lengthen and approach the context limit,
   Hermes Agent executes automatic context compression, pruning tool results and middle turns.
   Without an automated pre-compression hook, dialogue snapshots, key decisions, and session
   learnings can be compacted away before being recorded in durable memory or the daily Obsidian
   journal (/app/vault/second-brain/Journal/YYYY/MM/YYYY-MM-DD.md).
2. Continuing conversations inside a compacted context degrades reasoning fidelity, causes
   repeated tool calls, and risks subtle hallucinations. In Discord thread sessions, continuing
   inline after context saturation forces the agent to operate on a lossy summary.
3. Hermes Agent's in-flight task replay mechanism (_find_inflight_user_task and _reappend_inflight_user_task)
   re-appends unfinished user asks after compaction headers with '[STILL IN PROGRESS]'. When closeout/wrapup
   tasks are executed, or when trailing background notifications arrive after a final text answer was already delivered,
   _find_inflight_user_task treats the task as still in-flight, repeatedly re-injecting the wrap-up ask across
   compaction boundaries and causing infinite duplicate response loops.

Fix
---
1. Inject ``_trigger_odin_pre_compress_wrapup()`` into ``ContextCompressor`` in
   ``/opt/hermes/agent/context_compressor.py`` and invoke it right after summary generation,
   before the compressed message list is assembled:
   - Resolves Auckland local time (Pacific/Auckland) with the < 06:00 NZDT sleep rollover.
   - Identifies the active Obsidian journal at /app/vault/second-brain/Journal/YYYY/MM/YYYY-MM-DD.md
     (with fallback to /opt/atlas/vault).
   - Strictly strips all em dashes and en dashes, enforcing the zero em/en dash invariant.
   - Formats and appends an auto-checkpoint containing context usage, user topics, and the compacted summary.
   - Automated Discord Thread Handover: if the active session is a Discord thread, automatically spawns
     a fresh continuation thread (e.g. '[Topic] - Part 2'), auto-joins the user (<@DISCORD_USER_ID>),
     seeds message 1 with the structured Handover Prompt, posts a migration link in the old thread,
     and schedules a delayed 20-second background archive of the old thread.
   - Fans out an ``agent:pre_compress`` event to any external hooks located in /opt/data/hooks/.
   - Runs in a failsafe try/except block so compression never aborts if vault writes encounter errors.
2. Patch ``_find_inflight_user_task`` and ``_reappend_inflight_user_task`` to suppress duplicate task replays:
   - Suppress re-injecting wrapup/closeout tasks as in-flight since session wrapup is already handled or captured.
   - Check if an assistant message already delivered a final answer text after the user request without pending tool calls,
     preventing trailing background/cron tool results from falsely marking finished requests as in-flight.
"""
from __future__ import annotations

import py_compile
import sys
import tempfile
from pathlib import Path

TARGET = Path("/opt/hermes/agent/context_compressor.py")
MARKER = "# odin-patch: pre-compression wrapup hook"
INFLIGHT_MARKER = "# odin-patch: prevent compaction duplicate response and wrapup replay"

INFLIGHT_ANCHOR = (
    "        if last_user_idx < 0:\n"
    "            return None\n"
    "\n"
    "        for msg in reversed(messages[last_user_idx + 1:]):\n"
    "            if not isinstance(msg, dict) or msg.get(\"role\") != \"assistant\":\n"
    "                # Trailing tool result (or anything else): still mid-task.\n"
    "                break\n"
    "            if msg.get(\"tool_calls\"):\n"
    "                break\n"
    "            if _content_text_for_contains(msg.get(\"content\")).strip():\n"
    "                # Final answer already delivered \u2014 replaying the ask would\n"
    "                # hand the model finished work as a fresh instruction.\n"
    "                return None\n"
    "            # Empty assistant row (a bare reasoning/stub turn): keep looking.\n"
    "        return messages[last_user_idx]\n"
)

INFLIGHT_INJECTION = (
    "        if last_user_idx < 0:\n"
    "            return None\n"
    "\n"
    f"        {INFLIGHT_MARKER}\n"
    "        # Check if the user request was a wrapup/closeout task that should not be replayed\n"
    "        user_content = _content_text_for_contains(messages[last_user_idx].get(\"content\")).lower()\n"
    "        _wrapup_keywords = (\"wrap up\", \"wrap-up\", \"wrapup\", \"close out\", \"close-out\", \"closeout\", \"session-wrapup\", \"session wrapup\")\n"
    "        if any(kw in user_content for kw in _wrapup_keywords):\n"
    "            return None\n"
    "\n"
    "        # If any assistant message after the request already delivered non-empty text without pending tool calls,\n"
    "        # the ask was answered - do not let trailing background/tool results re-trigger in-flight replay.\n"
    "        subsequent = messages[last_user_idx + 1:]\n"
    "        for msg in reversed(subsequent):\n"
    "            if not isinstance(msg, dict) or msg.get(\"role\") != \"assistant\":\n"
    "                has_delivered_answer = any(\n"
    "                    isinstance(m, dict) and m.get(\"role\") == \"assistant\" and not m.get(\"tool_calls\") and bool(_content_text_for_contains(m.get(\"content\")).strip())\n"
    "                    for m in subsequent\n"
    "                )\n"
    "                if has_delivered_answer:\n"
    "                    return None\n"
    "                break\n"
    "            if msg.get(\"tool_calls\"):\n"
    "                break\n"
    "            if _content_text_for_contains(msg.get(\"content\")).strip():\n"
    "                # Final answer already delivered \u2014 replaying the ask would\n"
    "                # hand the model finished work as a fresh instruction.\n"
    "                return None\n"
    "            # Empty assistant row (a bare reasoning/stub turn): keep looking.\n"
    "        return messages[last_user_idx]\n"
)

REAPPEND_ANCHOR = (
    "        # Keep any reply pointer: the restated task still needs its disambiguation.\n"
    "        task_text = _restated_request_text(inflight.get(\"content\"))\n"
    "        if not task_text:\n"
    "            return compressed\n"
)

REAPPEND_INJECTION = (
    "        # Keep any reply pointer: the restated task still needs its disambiguation.\n"
    "        task_text = _restated_request_text(inflight.get(\"content\"))\n"
    "        if not task_text:\n"
    "            return compressed\n"
    "\n"
    "        _wrapup_keywords = (\"wrap up\", \"wrap-up\", \"wrapup\", \"close out\", \"close-out\", \"closeout\", \"session-wrapup\", \"session wrapup\")\n"
    "        if any(kw in task_text.lower() for kw in _wrapup_keywords):\n"
    "            return compressed\n"
)

METHOD_ANCHOR = "    def _assemble_compressed(\n"

CALL_ANCHOR = (
    "        if not summary:\n"
    "            summary = self._fallback_summary_for_window(\n"
    "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
    "            )\n"
    "        # Phase 4: Assemble compressed message list\n"
    "        compressed = self._assemble_compressed(messages, compress_start, compress_end, scan, summary)\n"
)

CALL_INJECTION = (
    "        if not summary:\n"
    "            summary = self._fallback_summary_for_window(\n"
    "                telemetry, turns_to_summarize, compress_end - compress_start, feasibility_skip,\n"
    "            )\n"
    f"        {MARKER}\n"
    "        try:\n"
    "            self._trigger_odin_pre_compress_wrapup(\n"
    "                messages=canonical_messages,\n"
    "                turns_to_summarize=turns_to_summarize,\n"
    "                summary=summary,\n"
    "                display_tokens=display_tokens,\n"
    "            )\n"
    "        except Exception as _wrapup_err:\n"
    "            logger.warning(\"odin-patch: pre-compression wrapup failed: %s\", _wrapup_err, exc_info=True)\n"
    "        # Phase 4: Assemble compressed message list\n"
    "        compressed = self._assemble_compressed(messages, compress_start, compress_end, scan, summary)\n"
)

METHOD_INJECTION = '''    def _trigger_odin_pre_compress_wrapup(
        self,
        messages: list[dict[str, Any]],
        turns_to_summarize: list[dict[str, Any]],
        summary: str,
        display_tokens: int | None = None,
    ) -> None:
        """Odin pre-compression wrapup hook: auto-captures session dialogue, decisions,
        and context metrics into the Auckland daily Obsidian journal before middle turns are compacted,
        and automatically migrates Discord threads to a fresh continuation thread.
        """
        import os
        from datetime import datetime, timedelta
        from zoneinfo import ZoneInfo
        from pathlib import Path

        now_nz = datetime.now(ZoneInfo("Pacific/Auckland"))
        # 06:00 rollover: turns before 06:00 NZDT/NZST belong to previous date
        if now_nz.hour < 6:
            journal_date = (now_nz - timedelta(days=1)).date()
        else:
            journal_date = now_nz.date()

        date_str = journal_date.isoformat()
        tz_abbr = now_nz.strftime("%Z") or "NZDT"
        timestamp_str = f"{now_nz.strftime('%H:%M')} {tz_abbr}"

        # Resolve Obsidian vault path (inside container: /app/vault, fallback: /opt/atlas/vault)
        vault_root = None
        for candidate in (Path("/app/vault"), Path("/opt/atlas/vault")):
            if candidate.is_dir():
                vault_root = candidate
                break

        if not vault_root:
            logger.debug("[odin-wrapup] No vault root found; skipping pre-compress journal checkpoint")
            return

        journal_file = vault_root / "second-brain" / "Journal" / f"{journal_date.year:04d}" / f"{journal_date.month:02d}" / f"{date_str}.md"

        # Sanitize summary and strictly strip all em and en dashes (invariant)
        clean_summary = (summary or "").replace("\\u2014", "-").replace("\\u2013", "-").strip()

        # Extract recent user queries/topics from turns_to_summarize or messages
        user_queries = []
        for msg in (turns_to_summarize or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    txt = content.strip().split("\\n")[0]
                    clean_txt = txt.replace("\\u2014", "-").replace("\\u2013", "-")
                    if len(clean_txt) > 160:
                        clean_txt = clean_txt[:157] + "..."
                    if clean_txt and clean_txt not in user_queries:
                        user_queries.append(clean_txt)

        # Assemble checkpoint block
        lines = [
            f"### Auto-Checkpoint (Pre-Compression {timestamp_str})",
            "- **Trigger**: Automatic pre-compression checkpoint before context compaction",
        ]
        if display_tokens:
            lines.append(f"- **Context Usage**: ~{display_tokens:,} tokens ({len(turns_to_summarize)} turns compacted)")
        else:
            lines.append(f"- **Compacted Turns**: {len(turns_to_summarize)} turns")

        if user_queries:
            lines.append("- **User Inquiries Compacted**:")
            for q in user_queries[:5]:
                lines.append(f"  * {q}")

        if clean_summary:
            lines.append("- **Compacted Summary**:")
            for s_line in clean_summary.splitlines():
                if s_line.strip():
                    lines.append(f"  > {s_line}")
                else:
                    lines.append("  >")

        checkpoint_text = "\\n".join(lines)
        checkpoint_text = checkpoint_text.replace("\\u2014", "-").replace("\\u2013", "-")

        # Write to journal note
        journal_file.parent.mkdir(parents=True, exist_ok=True)
        if journal_file.exists():
            existing = journal_file.read_text(encoding="utf-8")
            if "## Next Session Handoff Block" in existing:
                parts = existing.split("## Next Session Handoff Block", 1)
                updated = parts[0].rstrip() + "\\n\\n" + checkpoint_text + "\\n\\n## Next Session Handoff Block" + parts[1]
            else:
                updated = existing.rstrip() + "\\n\\n" + checkpoint_text + "\\n"
        else:
            updated = f"---\\ndate: {date_str}\\ntags: [journal, auto-checkpoint]\\nlocation: \\"Auckland\\"\\n---\\n\\n## Journal Entry\\n\\n{checkpoint_text}\\n"

        updated = updated.replace("\\u2014", "-").replace("\\u2013", "-")
        journal_file.write_text(updated, encoding="utf-8")
        logger.info("[odin-wrapup] Successfully recorded pre-compression wrapup checkpoint to %s", journal_file)

        # Automated Discord Thread Handover
        try:
            self._trigger_odin_discord_thread_handover(
                summary=clean_summary,
                display_tokens=display_tokens,
                user_queries=user_queries,
            )
        except Exception as d_err:
            logger.warning("[odin-wrapup] Discord thread handover notice: %s", d_err, exc_info=True)

        # Notify any external hook scripts under /opt/data/hooks
        hooks_dir = Path("/opt/data/hooks")
        if hooks_dir.is_dir():
            for hook_subdir in hooks_dir.iterdir():
                handler_script = hook_subdir / "handler.py"
                if handler_script.is_file():
                    try:
                        import importlib.util
                        spec = importlib.util.spec_from_file_location(f"odin_hook_{hook_subdir.name}", handler_script)
                        if spec and spec.loader:
                            mod = importlib.util.module_from_spec(spec)
                            spec.loader.exec_module(mod)
                            if hasattr(mod, "handle"):
                                mod.handle("agent:pre_compress", {
                                    "display_tokens": display_tokens,
                                    "summary": clean_summary,
                                    "journal_file": str(journal_file),
                                    "timestamp": timestamp_str,
                                })
                    except Exception as h_err:
                        logger.debug("[odin-wrapup] Hook %s execution notice: %s", hook_subdir.name, h_err)

    def _trigger_odin_discord_thread_handover(
        self,
        summary: str,
        display_tokens: int | None = None,
        user_queries: list[str] | None = None,
    ) -> None:
        """Automated Discord Thread Handover: When a Discord thread session approaches the
        context limit, spawns a fresh continuation thread (e.g. '[Topic] - Part 2'),
        seeds it with the structured Handover Prompt, notifies the old thread, and archives it.
        """
        import os
        import re
        import json
        import sqlite3
        import urllib.request
        import urllib.error
        from pathlib import Path

        session_id = getattr(self, "_session_id", None)
        if not session_id:
            return

        db_path = Path("/opt/data/state.db")
        if not db_path.is_file():
            return

        try:
            conn = sqlite3.connect(str(db_path))
            conn.row_factory = sqlite3.Row
            row = conn.cursor().execute(
                "SELECT source, chat_id, thread_id, title, origin_json FROM sessions WHERE id = ?",
                (session_id,),
            ).fetchone()
            conn.close()
        except Exception as db_err:
            logger.debug("[odin-handover] Failed to query session state: %s", db_err)
            return

        if not row:
            return

        source = str(row["source"] or "").lower()
        thread_id = row["thread_id"]
        if source != "discord" or not thread_id:
            # Only Discord threads get automated thread continuation
            return

        thread_id = str(thread_id).strip()

        origin_json_str = row["origin_json"] or "{}"
        try:
            origin = json.loads(origin_json_str)
        except Exception:
            origin = {}

        # Prefer the true Discord thread title from origin_json['chat_name']
        # over row['title'], because Hermes's LLM auto-titler frequently mutates
        # session['title'] based on subsequent user prompts.
        chat_name = str(origin.get("chat_name") or "").strip()
        if chat_name and " / " in chat_name:
            discord_thread_title = chat_name.split(" / ")[-1].strip()
        elif chat_name:
            discord_thread_title = chat_name
        else:
            discord_thread_title = ""

        raw_title = (discord_thread_title or row["title"] or "Discussion").strip()
        clean_title = raw_title.replace("\\u2014", "-").replace("\\u2013", "-")

        parent_chat_id = origin.get("parent_chat_id") or row["chat_id"]
        # Fallback to #Home (1507958299428130907) if parent_chat_id is identical to the thread itself
        if not parent_chat_id or str(parent_chat_id) == str(thread_id):
            parent_chat_id = "1507958299428130907"

        # Increment Part number: 'Topic' -> 'Topic - Part 2', 'Topic - Part 2' -> 'Topic - Part 3'
        part_match = re.search(r"-\\s*Part\\s*(\\d+)$", clean_title, re.IGNORECASE)
        if part_match:
            next_part = int(part_match.group(1)) + 1
            new_title = re.sub(r"-\\s*Part\\s*\\d+$", f"- Part {next_part}", clean_title, flags=re.IGNORECASE)
        else:
            new_title = f"{clean_title} - Part 2"

        if len(new_title) > 95:
            new_title = new_title[:95]

        # Extract Bot token from environment or /opt/data/.env
        bot_token = os.environ.get("DISCORD_BOT_TOKEN")
        if not bot_token:
            env_file = Path("/opt/data/.env")
            if env_file.is_file():
                for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.strip().startswith("DISCORD_BOT_TOKEN="):
                        bot_token = line.strip().split("=", 1)[1].strip()
                        break

        if not bot_token:
            logger.debug("[odin-handover] No DISCORD_BOT_TOKEN available; skipping thread handover")
            return

        tok_str = f"{display_tokens:,}" if display_tokens else "140,000+"
        discord_user_id = os.environ.get("DISCORD_USER_ID") or os.environ.get("DISCORD_ALLOWED_USERS", "").split(",")[0].strip()
        mention = f"<@{discord_user_id}> " if discord_user_id else ""
        starter_lines = [
            f"{mention}Continuing from <#{thread_id}>. Context reached ~{tok_str} tokens; transitioned to this fresh thread to prevent compacted context degradation.",
            "",
            "### Handover Context",
        ]
        if summary:
            for s_line in summary.splitlines():
                if s_line.strip():
                    starter_lines.append(f"> {s_line}")
                else:
                    starter_lines.append(">")
        else:
            starter_lines.append("> Previous session context preserved.")

        if user_queries:
            starter_lines.append("")
            starter_lines.append("### Recent Focus Topics")
            for q in user_queries[:3]:
                starter_lines.append(f"- {q}")

        starter_lines.append("")
        starter_lines.append("### Status")
        starter_lines.append("- Fresh session active with 0 context tokens consumed.")
        starter_lines.append("- Full reasoning power restored.")
        starter_lines.append(f"- Previous thread (<#{thread_id}>) archived.")
        starter_lines.append("")
        starter_lines.append("Ready for next instruction.")

        starter_content = "\\n".join(starter_lines).replace("\\u2014", "-").replace("\\u2013", "-")
        if len(starter_content) > 1950:
            starter_content = starter_content[:1940] + "\\n\\n[Context truncated for Discord 2k limit]"

        headers = {
            "Authorization": f"Bot {bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "HermesAgentDiscordLifecycle/1.0",
        }

        # 1. Spawn continuation thread in parent channel
        create_url = f"https://discord.com/api/v10/channels/{parent_chat_id}/threads"
        payload_with_type = {
            "name": new_title,
            "type": 11,
            "message": {
                "content": starter_content,
            },
        }

        new_thread_id = None
        try:
            req = urllib.request.Request(
                create_url,
                data=json.dumps(payload_with_type).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(req) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                new_thread_id = data.get("id")
        except urllib.error.HTTPError as h_err:
            err_body = h_err.read().decode("utf-8", errors="replace")
            # If type 11 failed because parent channel is a forum (type 15), retry without 'type'
            if h_err.code == 400 and ("type" in err_body.lower() or "forum" in err_body.lower()):
                payload_no_type = {
                    "name": new_title,
                    "message": {
                        "content": starter_content,
                    },
                }
                req2 = urllib.request.Request(
                    create_url,
                    data=json.dumps(payload_no_type).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                with urllib.request.urlopen(req2) as resp2:
                    data = json.loads(resp2.read().decode("utf-8"))
                    new_thread_id = data.get("id")
            else:
                logger.warning("[odin-handover] Discord thread creation failed (HTTP %s): %s", h_err.code, err_body)
                return
        except Exception as c_err:
            logger.warning("[odin-handover] Error creating continuation thread: %s", c_err)
            return

        if not new_thread_id:
            return

        logger.info("[odin-handover] Successfully created continuation thread %s (%s)", new_thread_id, new_title)

        # 2. Post migration notice into old thread
        msg_url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
        notice = (
            f"Context threshold reached (~{tok_str} tokens). "
            f"To prevent compacted-context degradation, conversation has been migrated to <#{new_thread_id}>.\\n\\n"
            "Archiving this thread."
        ).replace("\\u2014", "-").replace("\\u2013", "-")

        try:
            msg_req = urllib.request.Request(
                msg_url,
                data=json.dumps({"content": notice}).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            with urllib.request.urlopen(msg_req) as _:
                pass
        except Exception as m_err:
            logger.warning("[odin-handover] Failed to post migration notice to old thread: %s", m_err)

        # 3. Trigger delayed archiving of the old thread (20s delay for message delivery)
        import subprocess
        archive_script = Path("/opt/data/skills/productivity/discord-thread-lifecycle/scripts/archive_thread.py")
        if archive_script.is_file():
            try:
                subprocess.Popen(["python3", str(archive_script), str(thread_id), "--delay", "20"])
            except Exception as a_err:
                logger.warning("[odin-handover] Failed to launch archive_thread.py: %s", a_err)
        else:
            import threading
            def _delayed_archive():
                import time
                time.sleep(20)
                try:
                    p_url = f"https://discord.com/api/v10/channels/{thread_id}"
                    p_req = urllib.request.Request(
                        p_url,
                        data=json.dumps({"archived": True}).encode("utf-8"),
                        headers=headers,
                        method="PATCH",
                    )
                    with urllib.request.urlopen(p_req) as _:
                        pass
                except Exception:
                    pass
            threading.Thread(target=_delayed_archive, daemon=True).start()
'''


def main() -> int:
    if not TARGET.is_file():
        print(f"[odin-patch] ERROR: {TARGET} not found", file=sys.stderr)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    modified = False

    # 1. Apply pre-compression wrapup hook if not already applied
    if MARKER in src:
        print("[odin-patch] pre-compression wrapup hook already applied; skipping hook injection")
    else:
        if CALL_ANCHOR not in src:
            print("[odin-patch] ERROR: CALL_ANCHOR not found in context_compressor.py; upstream changed", file=sys.stderr)
            return 1

        if METHOD_ANCHOR not in src:
            print("[odin-patch] ERROR: METHOD_ANCHOR not found in context_compressor.py; upstream changed", file=sys.stderr)
            return 1

        # Inject call site
        src = src.replace(CALL_ANCHOR, CALL_INJECTION, 1)

        # Inject method
        method_idx = src.find(METHOD_ANCHOR)
        src = src[:method_idx] + METHOD_INJECTION + "\n" + src[method_idx:]
        modified = True
        print(f"[odin-patch] Injected pre-compression wrapup hook into {TARGET}")

    # 2. Apply in-flight task replay suppression if not already applied
    if INFLIGHT_MARKER in src:
        print("[odin-patch] in-flight task replay suppression already applied; skipping")
    else:
        if INFLIGHT_ANCHOR not in src:
            print("[odin-patch] ERROR: INFLIGHT_ANCHOR not found in context_compressor.py; upstream changed", file=sys.stderr)
            return 1

        if REAPPEND_ANCHOR not in src:
            print("[odin-patch] ERROR: REAPPEND_ANCHOR not found in context_compressor.py; upstream changed", file=sys.stderr)
            return 1

        src = src.replace(INFLIGHT_ANCHOR, INFLIGHT_INJECTION, 1)
        src = src.replace(REAPPEND_ANCHOR, REAPPEND_INJECTION, 1)
        modified = True
        print(f"[odin-patch] Injected in-flight task replay suppression into {TARGET}")

    if modified:
        TARGET.write_text(src, encoding="utf-8")
        print(f"[odin-patch] Patched {TARGET}")

    # Verify python syntax
    try:
        py_compile.compile(str(TARGET), doraise=True)
        print("[odin-patch] Syntax check passed successfully")
    except py_compile.PyCompileError as e:
        print(f"[odin-patch] ERROR: Syntax check failed on patched file: {e}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
