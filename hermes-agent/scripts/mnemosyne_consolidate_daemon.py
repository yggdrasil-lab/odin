#!/usr/bin/env python3
"""mnemosyne_consolidate_daemon.py - Autonomous Memory Consolidation & Dreaming Daemon for Mnemosyne.

Pillar 1 of the Yggdrasil Autonomous Expansion Roadmap:
1. Batches un-consolidated working memories (consolidated_at IS NULL).
2. Calls the host LLM backend on task 'memory' (gemini-3.8-flash-high via Antigravity subscription)
   to extract structured entity triples (subject, predicate, object).
3. Upserts extracted facts into consolidated_facts and builds bidirectional graph_edges.
4. Executes temporal hygiene repairs via mnemosyne_temporal_hygiene to close expired validity
   windows and resolve contradiction clashes.
5. Marks processed memories with consolidated_at = CURRENT_TIMESTAMP.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Set, Tuple

# Ensure current script directory is in sys.path for importing sibling modules
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from mnemosyne_temporal_hygiene import audit_temporal_hygiene
except ImportError:
    audit_temporal_hygiene = None

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [dreaming-daemon] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dreaming-daemon")

NOISE_SUBJECTS = frozenset([
    "this", "that", "it", "everything", "something", "anything",
    "what", "here", "there", "none", "i", "we", "you", "they",
    "he", "she", "someone", "anyone", "nothing", "all",
])


def resolve_db_path(explicit_path: Optional[str] = None) -> str:
    """Resolves Mnemosyne SQLite DB path dynamically for container and host runtimes."""
    if explicit_path and os.path.isfile(explicit_path):
        return explicit_path
    env_path = os.environ.get("MNEMOSYNE_DB_PATH")
    if env_path and os.path.isfile(env_path):
        return env_path
    candidates = [
        "/opt/data/.hermes/mnemosyne/data/mnemosyne.db",  # Hermes container
        "/opt/odin/mnemosyne/data/mnemosyne.db",          # Gaia host
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    raise FileNotFoundError("Could not find mnemosyne.db in standard container or host paths.")


def generate_fact_id(subject: str, predicate: str, obj: str) -> str:
    """Generates a deterministic fact ID based on normalized triple values."""
    norm = f"{subject.strip().lower()}:{predicate.strip().lower()}:{obj.strip().lower()}"
    digest = hashlib.sha256(norm.encode("utf-8")).hexdigest()[:24]
    return f"cf_{digest}"


class LLMCaller:
    """Dual runtime LLM caller: uses HermesAuxLLMBackend in container or fallback on host."""

    def __init__(self, timeout: int = 120) -> None:
        self.timeout = timeout
        self._backend: Any = None
        self._mode = "unknown"
        self._init_backend()

    def _init_backend(self) -> None:
        try:
            from mnemosyne_hermes.hermes_llm_adapter import HermesAuxLLMBackend
            self._backend = HermesAuxLLMBackend()
            self._mode = "hermes_aux"
            logger.info("Initialized HermesAuxLLMBackend (task: memory, gemini-3.8-flash-high)")
            return
        except Exception:
            pass

        # Check if Muninn is reachable directly via overlay or localhost
        muninn_key = os.environ.get("MUNINN_MASTER_KEY")
        if muninn_key:
            self._mode = "muninn_api"
            logger.info("Initialized Muninn HTTP client fallback")
            return

        self._mode = "subprocess_docker"
        logger.info("HermesAuxLLMBackend not available locally; container bridge fallback enabled")

    def complete(self, prompt: str, max_tokens: int = 1024, temperature: float = 0.1) -> Optional[str]:
        if self._mode == "hermes_aux" and self._backend:
            try:
                res = self._backend.complete(
                    prompt,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    timeout=self.timeout,
                )
                if res and isinstance(res, str) and res.strip():
                    return res.strip()
            except Exception as exc:
                logger.warning("HermesAuxLLMBackend call failed: %s", exc)

        # Container bridge fallback if running on Gaia host
        if self._mode == "subprocess_docker":
            try:
                cid_proc = subprocess.run(
                    ["docker", "ps", "-q", "-f", "name=odin_huginn-gateway"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                cid = cid_proc.stdout.strip().splitlines()[0] if cid_proc.returncode == 0 and cid_proc.stdout.strip() else ""
                if cid:
                    py_code = (
                        "import json, sys; "
                        "from mnemosyne_hermes.hermes_llm_adapter import HermesAuxLLMBackend; "
                        "b = HermesAuxLLMBackend(); "
                        f"res = b.complete({json.dumps(prompt)}, max_tokens={max_tokens}, temperature={temperature}, timeout={self.timeout}); "
                        "print(res if res else '')"
                    )
                    res = subprocess.run(
                        ["docker", "exec", "-i", cid, "/opt/hermes/.venv/bin/python", "-c", py_code],
                        capture_output=True,
                        text=True,
                        timeout=self.timeout + 15,
                    )
                    if res.returncode == 0 and res.stdout.strip():
                        return res.stdout.strip()
            except Exception as exc:
                logger.warning("Container bridge LLM fallback failed: %s", exc)

        # Fallback via Muninn API if available
        muninn_key = os.environ.get("MUNINN_MASTER_KEY")
        if muninn_key:
            try:
                import urllib.request
                import json as py_json
                url = os.environ.get("MUNINN_API_URL") or "http://muninn:4000/v1/chat/completions"
                payload = {
                    "model": "gemini-3.8-flash-high",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are a memory consolidation engine. Extract structured knowledge triples. "
                                "Output strict JSON format only."
                            ),
                        },
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
                req = urllib.request.Request(
                    url,
                    data=py_json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": f"Bearer {muninn_key}",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = py_json.loads(resp.read().decode("utf-8"))
                    content = data["choices"][0]["message"]["content"]
                    if content and isinstance(content, str):
                        return content.strip()
            except Exception as exc:
                logger.warning("Muninn fallback call failed: %s", exc)

        return None


def clean_llm_json_response(raw_text: str) -> List[Dict[str, Any]]:
    """Cleans code fences and parses JSON array of extracted triples."""
    if not raw_text or not raw_text.strip():
        return []

    text = raw_text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
        text = text.strip()

    # Find JSON array bracket
    start = text.find("[")
    end = text.rfind("]")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    try:
        data = json.loads(text)
        if isinstance(data, list):
            return data
    except Exception as exc:
        logger.debug("Failed parsing JSON directly: %s. Attempting regex extraction.", exc)

    # Fallback regex object extraction
    results = []
    pattern = re.compile(r'\{[^{}]*"subject"\s*:\s*"([^"]+)"[^{}]*"predicate"\s*:\s*"([^"]+)"[^{}]*"object"\s*:\s*"([^"]+)"[^{}]*\}')
    for match in pattern.finditer(raw_text):
        subj, pred, obj = match.groups()
        results.append({
            "subject": subj,
            "predicate": pred,
            "object": obj,
            "confidence": 0.8,
        })
    return results


def filter_valid_triple(triple: Dict[str, Any]) -> Optional[Tuple[str, str, str, float, str]]:
    """Validates triple structure and applies anti-noise filters."""
    subj = str(triple.get("subject") or "").strip()
    pred = str(triple.get("predicate") or "").strip()
    obj = str(triple.get("object") or "").strip()
    mem_id = str(triple.get("memory_id") or "").strip()

    if not subj or not pred or not obj:
        return None

    # Noise and pronoun filters
    if subj.lower() in NOISE_SUBJECTS:
        return None
    if obj.lower() in NOISE_SUBJECTS:
        return None
    if subj.lower() == obj.lower():
        return None

    # Length constraints: avoid giant prose blobs captured as subject/object
    if len(subj) > 120 or len(pred) > 60 or len(obj) > 200:
        return None
    if len(subj) < 2 or len(pred) < 2 or len(obj) < 2:
        return None

    raw_conf = triple.get("confidence", 0.75)
    try:
        conf = float(raw_conf)
        conf = max(0.1, min(1.0, conf))
    except (TypeError, ValueError):
        conf = 0.75

    return (subj, pred, obj, conf, mem_id)


def build_extraction_prompt(chunk: List[Tuple[str, str]]) -> str:
    """Builds a structured prompt instructing the LLM to extract durable knowledge triples."""
    memories_text = []
    for mem_id, content in chunk:
        clean_content = content.replace("\n", " ").strip()
        memories_text.append(f"- ID: {mem_id}\n  Content: {clean_content}")

    prompt = (
        "You are an autonomous memory consolidation engine (Dreaming Daemon).\n"
        "Your task is to extract atomic, durable knowledge triples (subject, predicate, object) from the working memories below.\n\n"
        "EXTRACTION RULES:\n"
        "1. Focus strictly on durable facts, architectural decisions, preferences, versions, configurations, and relationships.\n"
        "2. Do NOT extract ephemeral conversational noise, greeting pleasantries, or temporary status (e.g. 'working', 'done').\n"
        "3. Subjects MUST be specific entities (names, services, libraries, repositories, files, protocols). NEVER use pronouns or generic words ('it', 'this', 'that', 'everything', 'something', 'none').\n"
        "4. Predicates MUST be concise relationship verbs (e.g. 'uses', 'depends_on', 'deployed_to', 'prefers', 'configured_as', 'version', 'manages', 'author_of').\n"
        "5. Assign a confidence score from 0.5 to 1.0 based on how explicitly the fact is stated.\n"
        "6. Output MUST be a valid JSON array of objects with keys: 'memory_id', 'subject', 'predicate', 'object', 'confidence'.\n\n"
        "INPUT MEMORIES:\n"
        f"{chr(10).join(memories_text)}\n\n"
        "RESPONSE (strict JSON array only):"
    )
    return prompt


def consolidate_memory_batch(
    db_path: Optional[str] = None,
    batch_size: int = 50,
    chunk_size: int = 5,
    dry_run: bool = False,
    max_items: Optional[int] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    """Runs the autonomous memory consolidation pipeline."""
    t0 = time.perf_counter()
    resolved_db = resolve_db_path(db_path)
    logger.info("Opening Mnemosyne database: %s", resolved_db)

    conn = sqlite3.connect(resolved_db, timeout=15.0)
    cursor = conn.cursor()

    effective_limit = min(batch_size, max_items) if max_items else batch_size

    # Step A: Fetch un-consolidated working memories
    cursor.execute(
        """
        SELECT id, content, timestamp
        FROM working_memory
        WHERE consolidated_at IS NULL
        ORDER BY timestamp ASC
        LIMIT ?
        """,
        (effective_limit,),
    )
    unconsolidated_rows = cursor.fetchall()

    if not unconsolidated_rows:
        conn.close()
        logger.info("No un-consolidated working memories found. Database is fully consolidated.")
        return {
            "status": "ok",
            "database": resolved_db,
            "items_processed": 0,
            "triples_extracted": 0,
            "facts_created": 0,
            "facts_updated": 0,
            "edges_created": 0,
            "edges_updated": 0,
            "hygiene_report": None,
            "latency_ms": round((time.perf_counter() - t0) * 1000, 2),
        }

    logger.info("Found %d un-consolidated memories to process (batch limit: %d)", len(unconsolidated_rows), effective_limit)

    # Initialize LLM backend
    llm = LLMCaller(timeout=timeout)

    now_iso = datetime.now(timezone.utc).isoformat()
    processed_memory_ids: List[str] = []
    total_triples_extracted = 0
    facts_created = 0
    facts_updated = 0
    edges_created = 0
    edges_updated = 0

    # Process in chunks of chunk_size
    for i in range(0, len(unconsolidated_rows), chunk_size):
        chunk = unconsolidated_rows[i : i + chunk_size]
        chunk_items = [(row[0], row[1]) for row in chunk]
        chunk_ids = [row[0] for row in chunk]

        prompt = build_extraction_prompt(chunk_items)
        llm_response = llm.complete(prompt)

        raw_triples = clean_llm_json_response(llm_response or "")
        valid_triples = []

        for rt in raw_triples:
            # If LLM omitted memory_id, assign the first ID in chunk
            if not rt.get("memory_id") and len(chunk_ids) == 1:
                rt["memory_id"] = chunk_ids[0]
            val = filter_valid_triple(rt)
            if val:
                valid_triples.append(val)

        logger.info("Chunk %d-%d: extracted %d valid triples from %d memories", i + 1, min(i + chunk_size, len(unconsolidated_rows)), len(valid_triples), len(chunk))
        total_triples_extracted += len(valid_triples)

        # Step C: Upsert into consolidated_facts and graph_edges
        for subj, pred, obj, conf, mem_id in valid_triples:
            fact_id = generate_fact_id(subj, pred, obj)
            source_id = mem_id if mem_id in chunk_ids else chunk_ids[0]

            if not dry_run:
                # 1. Upsert consolidated_facts
                cursor.execute(
                    """
                    SELECT id, confidence, mention_count, sources_json
                    FROM consolidated_facts
                    WHERE id = ? OR (LOWER(subject) = LOWER(?) AND LOWER(predicate) = LOWER(?) AND LOWER(object) = LOWER(?))
                    """,
                    (fact_id, subj, pred, obj),
                )
                existing_fact = cursor.fetchone()

                if existing_fact:
                    ext_id, ext_conf, ext_count, ext_sources = existing_fact
                    try:
                        sources_list = json.loads(ext_sources) if ext_sources else []
                    except Exception:
                        sources_list = []
                    if source_id not in sources_list:
                        sources_list.append(source_id)

                    new_count = (ext_count or 1) + 1
                    new_conf = max(float(ext_conf or 0.5), conf)

                    cursor.execute(
                        """
                        UPDATE consolidated_facts
                        SET confidence = ?,
                            mention_count = ?,
                            last_seen = ?,
                            sources_json = ?,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (new_conf, new_count, now_iso, json.dumps(sources_list), ext_id),
                    )
                    facts_updated += 1
                else:
                    cursor.execute(
                        """
                        INSERT INTO consolidated_facts (
                            id, subject, predicate, object, confidence,
                            mention_count, first_seen, last_seen, sources_json,
                            veracity, superseded_by, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?, 'stated', NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                        """,
                        (fact_id, subj, pred, obj, conf, now_iso, now_iso, json.dumps([source_id])),
                    )
                    facts_created += 1

                # 2. Upsert bidirectional graph_edges
                # Edge A: subject -> object (relationship)
                cursor.execute(
                    "SELECT id, weight FROM graph_edges WHERE source = ? AND target = ? AND edge_type = ?",
                    (subj, obj, pred),
                )
                edge_a = cursor.fetchone()
                if edge_a:
                    cursor.execute(
                        "UPDATE graph_edges SET weight = MAX(weight, ?), timestamp = ? WHERE id = ?",
                        (conf, now_iso, edge_a[0]),
                    )
                    edges_updated += 1
                else:
                    cursor.execute(
                        """
                        INSERT INTO graph_edges (source, target, edge_type, weight, timestamp, created_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (subj, obj, pred, conf, now_iso),
                    )
                    edges_created += 1

                # Edge B: object -> subject (inverse relationship)
                inv_pred = f"inv_{pred}" if not pred.startswith("inv_") else pred[4:]
                cursor.execute(
                    "SELECT id, weight FROM graph_edges WHERE source = ? AND target = ? AND edge_type = ?",
                    (obj, subj, inv_pred),
                )
                edge_b = cursor.fetchone()
                if edge_b:
                    cursor.execute(
                        "UPDATE graph_edges SET weight = MAX(weight, ?), timestamp = ? WHERE id = ?",
                        (conf, now_iso, edge_b[0]),
                    )
                    edges_updated += 1
                else:
                    cursor.execute(
                        """
                        INSERT INTO graph_edges (source, target, edge_type, weight, timestamp, created_at)
                        VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (obj, subj, inv_pred, conf, now_iso),
                    )
                    edges_created += 1

                # Edge C: provenance edge memory -> subject
                mem_source = f"mem_{source_id}"
                cursor.execute(
                    "SELECT id, weight FROM graph_edges WHERE source = ? AND target = ? AND edge_type = 'mentions'",
                    (mem_source, subj),
                )
                edge_c = cursor.fetchone()
                if not edge_c:
                    cursor.execute(
                        """
                        INSERT INTO graph_edges (source, target, edge_type, weight, timestamp, created_at)
                        VALUES (?, ?, 'mentions', ?, ?, CURRENT_TIMESTAMP)
                        """,
                        (mem_source, subj, conf, now_iso),
                    )
                    edges_created += 1

        processed_memory_ids.extend(chunk_ids)

    # Step E: Mark processed working memories with consolidated_at = CURRENT_TIMESTAMP
    if not dry_run and processed_memory_ids:
        placeholders = ",".join("?" for _ in processed_memory_ids)
        cursor.execute(
            f"""
            UPDATE working_memory
            SET consolidated_at = CURRENT_TIMESTAMP
            WHERE id IN ({placeholders})
            """,
            processed_memory_ids,
        )
        conn.commit()
        logger.info("Successfully stamped %d working memories with consolidated_at timestamp", len(processed_memory_ids))

    conn.close()

    # Step D: Run temporal hygiene audit and repair
    hygiene_report = None
    if not dry_run:
        try:
            if audit_temporal_hygiene:
                hygiene_report = audit_temporal_hygiene(db_path=resolved_db, repair=True)
                logger.info(
                    "Temporal hygiene completed: %d expired closed, %d clashes resolved, %d bad edges removed",
                    hygiene_report["expired_active_memories"]["repaired"],
                    hygiene_report["contradiction_clashes"]["repaired"],
                    hygiene_report["invalid_edges"]["repaired"],
                )
            else:
                # Fallback to host/container CLI invocation
                cmd = ["mnemosyne-hygiene-audit", "--repair", "--db-path", resolved_db, "--json"]
                res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                if res.returncode == 0:
                    hygiene_report = json.loads(res.stdout)
        except Exception as exc:
            logger.warning("Temporal hygiene audit step failed non-fatally: %s", exc)

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
    return {
        "status": "ok",
        "database": resolved_db,
        "mode": "dry_run" if dry_run else "commit",
        "items_processed": len(processed_memory_ids),
        "triples_extracted": total_triples_extracted,
        "facts_created": facts_created,
        "facts_updated": facts_updated,
        "edges_created": edges_created,
        "edges_updated": edges_updated,
        "hygiene_report": hygiene_report,
        "latency_ms": elapsed_ms,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Autonomous Memory Consolidation & Dreaming Daemon for Mnemosyne")
    parser.add_argument("--batch-size", type=int, default=50, help="Number of memories to process per run (default: 50)")
    parser.add_argument("--chunk-size", type=int, default=5, help="Number of memories per LLM extraction call (default: 5)")
    parser.add_argument("--dry-run", action="store_true", help="Preview consolidation without persisting changes")
    parser.add_argument("--db-path", default="", help="Explicit path to mnemosyne.db")
    parser.add_argument("--max-items", type=int, default=None, help="Upper bound cap on memories to process")
    parser.add_argument("--timeout", type=int, default=120, help="LLM timeout in seconds (default: 120)")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON results")

    args = parser.parse_args()

    try:
        report = consolidate_memory_batch(
            db_path=args.db_path or None,
            batch_size=args.batch_size,
            chunk_size=args.chunk_size,
            dry_run=args.dry_run,
            max_items=args.max_items,
            timeout=args.timeout,
        )
    except Exception as exc:
        sys.stderr.write(f"ERROR: Dreaming daemon consolidation failed: {exc}\n")
        sys.exit(1)

    if args.as_json:
        print(json.dumps(report, indent=2))
        return

    print("\n" + "=" * 65)
    print("Mnemosyne Autonomous Memory Consolidation ('Dreaming' Daemon)")
    print("=" * 65)
    print(f"Database:         {report['database']}")
    print(f"Mode:             {report['mode'].upper()}")
    print(f"Memories Batched: {report['items_processed']}")
    print(f"Triples Extracted:{report['triples_extracted']}")
    print(f"Facts Created:    {report['facts_created']} (Updated: {report['facts_updated']})")
    print(f"Edges Created:    {report['edges_created']} (Updated: {report['edges_updated']})")
    print(f"Latency:          {report['latency_ms']}ms")

    hygiene = report.get("hygiene_report")
    if hygiene:
        exp = hygiene.get("expired_active_memories", {})
        clash = hygiene.get("contradiction_clashes", {})
        edges = hygiene.get("invalid_edges", {})
        print("-" * 65)
        print("Temporal Hygiene & Contradiction Resolution:")
        print(f"  - Expired active memories closed:  {exp.get('repaired', 0)}")
        print(f"  - Contradiction clashes resolved:  {clash.get('repaired', 0)}")
        print(f"  - Invalid graph edges removed:     {edges.get('repaired', 0)}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()
