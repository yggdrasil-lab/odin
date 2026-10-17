#!/usr/bin/env python3
"""Build-time patch: inject automated pre-compression wrapup checkpoint hook into ContextCompressor.

Problem
-------
When interacting with Hermes Agent via Discord or Open-WebUI, users cannot monitor
real-time context token metrics. As sessions lengthen and approach the context limit,
Hermes Agent executes automatic context compression, pruning tool results and middle turns.
Without an automated pre-compression hook, dialogue snapshots, key decisions, and session
learnings can be compacted away before being recorded in durable memory or the daily Obsidian
journal (/app/vault/second-brain/Journal/YYYY/MM/YYYY-MM-DD.md).

Fix
---
Inject ``_trigger_odin_pre_compress_wrapup()`` into ``ContextCompressor`` in
``/opt/hermes/agent/context_compressor.py`` and invoke it right after summary generation,
before the compressed message list is assembled:
1. Resolves Auckland local time (Pacific/Auckland) with the < 06:00 NZDT sleep rollover.
2. Identifies the active Obsidian journal at /app/vault/second-brain/Journal/YYYY/MM/YYYY-MM-DD.md
   (with fallback to /opt/atlas/vault).
3. Strictly strips all em dashes (—) and en dashes (–), enforcing the zero em/en dash invariant.
4. Formats and appends an auto-checkpoint containing context usage, user topics, and the compacted summary.
5. Fans out an ``agent:pre_compress`` event to any external hooks located in /opt/data/hooks/.
6. Runs in a failsafe try/except block so compression never aborts if vault writes encounter errors.
"""
from __future__ import annotations

import py_compile
import sys
import tempfile
from pathlib import Path

TARGET = Path("/opt/hermes/agent/context_compressor.py")
MARKER = "# odin-patch: pre-compression wrapup hook"

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
        and context metrics into the Auckland daily Obsidian journal before middle turns are compacted.
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
        clean_summary = (summary or "").replace("\\u2014", "-").replace("\\u2013", "-").replace("—", "-").replace("–", "-").strip()

        # Extract recent user queries/topics from turns_to_summarize or messages
        user_queries = []
        for msg in (turns_to_summarize or []):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str) and content.strip():
                    txt = content.strip().split("\\n")[0]
                    clean_txt = txt.replace("—", "-").replace("–", "-")
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
        checkpoint_text = checkpoint_text.replace("—", "-").replace("–", "-")

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

        updated = updated.replace("—", "-").replace("–", "-")
        journal_file.write_text(updated, encoding="utf-8")
        logger.info("[odin-wrapup] Successfully recorded pre-compression wrapup checkpoint to %s", journal_file)

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
'''


def main() -> int:
    if not TARGET.is_file():
        print(f"[odin-patch] ERROR: {TARGET} not found", file=sys.stderr)
        return 1

    src = TARGET.read_text(encoding="utf-8")
    if MARKER in src:
        print("[odin-patch] pre-compression wrapup hook already applied; skipping")
        return 0

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

    TARGET.write_text(src, encoding="utf-8")
    print(f"[odin-patch] Patched {TARGET} (injected _trigger_odin_pre_compress_wrapup)")

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
