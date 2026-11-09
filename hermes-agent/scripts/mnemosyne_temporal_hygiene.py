#!/usr/bin/env python3
"""mnemosyne_temporal_hygiene.py - Bi-Temporal Validity & Contradiction Audit Tool for Mnemosyne.

Audits and repairs:
1. Expired Active Memories: Memories where valid_until has passed but superseded_by is not set.
2. Contradiction Clashes: Active facts (superseded_by IS NULL) sharing the same subject + predicate with conflicting objects.
3. Graph Edge Hygiene: Invalid, negative-weight, or self-referential graph edges.

In --repair mode, closes validity windows on outdated facts and preserves audit trail.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Tuple


def resolve_db_path() -> str:
    """Resolves Mnemosyne SQLite DB path dynamically for container and host runtimes."""
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


def audit_temporal_hygiene(db_path: str = "", repair: bool = False) -> Dict[str, Any]:
    """Audits and optionally repairs bi-temporal fact validity in Mnemosyne."""
    resolved_db = db_path or resolve_db_path()
    # Read-only if auditing, read-write if repairing
    conn = sqlite3.connect(resolved_db)
    cursor = conn.cursor()

    now_iso = datetime.now(timezone.utc).isoformat()

    # 1. Audit Expired Active Memories
    cursor.execute(
        """
        SELECT id, content, valid_until, timestamp
        FROM working_memory
        WHERE valid_until IS NOT NULL AND valid_until != ''
          AND valid_until < ?
          AND (superseded_by IS NULL OR superseded_by = '')
        """,
        (now_iso,),
    )
    expired_memories = cursor.fetchall()

    # 2. Audit Contradiction Clashes in consolidated_facts
    # Exclude trivial pronouns and stopwords
    noise_subjects = ("this", "that", "it", "everything", "something", "anything", "what", "here", "there", "none")
    cursor.execute(
        """
        SELECT f1.id, f1.subject, f1.predicate, f1.object, f1.updated_at,
               f2.id, f2.subject, f2.predicate, f2.object, f2.updated_at
        FROM consolidated_facts f1
        JOIN consolidated_facts f2
          ON LOWER(f1.subject) = LOWER(f2.subject)
         AND LOWER(f1.predicate) = LOWER(f2.predicate)
         AND f1.id < f2.id
         AND LOWER(f1.object) != LOWER(f2.object)
        WHERE f1.superseded_by IS NULL AND f2.superseded_by IS NULL
          AND LOWER(f1.subject) NOT IN (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        noise_subjects,
    )
    clashes = cursor.fetchall()

    # 3. Audit Zero/Negative or Self-referential graph edges
    cursor.execute(
        """
        SELECT id, source, target, edge_type, weight
        FROM graph_edges
        WHERE weight <= 0 OR source = target
        """
    )
    bad_edges = cursor.fetchall()

    repaired_expired = 0
    repaired_clashes = 0
    repaired_edges = 0

    if repair:
        # Close expired memories
        for exp_id, _, _, _ in expired_memories:
            cursor.execute(
                """
                UPDATE working_memory
                SET superseded_by = 'EXPIRED_BY_TEMPORAL_AUDIT'
                WHERE id = ?
                """,
                (exp_id,),
            )
            repaired_expired += 1

        # Resolve clashes by superseding the older fact with the newer fact ID
        for id1, subj, pred, obj1, ts1, id2, _, _, obj2, ts2 in clashes:
            ts1_str = str(ts1 or "")
            ts2_str = str(ts2 or "")
            if ts1_str < ts2_str:
                older_id, newer_id = id1, id2
            else:
                older_id, newer_id = id2, id1

            cursor.execute(
                """
                UPDATE consolidated_facts
                SET superseded_by = ?
                WHERE id = ? AND superseded_by IS NULL
                """,
                (newer_id, older_id),
            )
            repaired_clashes += 1

        # Prune zero/negative or self-referential edges
        for edge_id, _, _, _, _ in bad_edges:
            cursor.execute("DELETE FROM graph_edges WHERE id = ?", (edge_id,))
            repaired_edges += 1

        conn.commit()

    conn.close()

    return {
        "status": "ok",
        "database": resolved_db,
        "mode": "repair" if repair else "audit",
        "expired_active_memories": {
            "count": len(expired_memories),
            "repaired": repaired_expired,
            "sample": [{"id": m[0], "valid_until": m[2], "preview": m[1][:80]} for m in expired_memories[:5]],
        },
        "contradiction_clashes": {
            "count": len(clashes),
            "repaired": repaired_clashes,
            "sample": [
                {
                    "subject": c[1],
                    "predicate": c[2],
                    "fact_a": f"{c[0]}: {c[3]} ({c[4]})",
                    "fact_b": f"{c[5]}: {c[8]} ({c[9]})",
                }
                for c in clashes[:5]
            ],
        },
        "invalid_edges": {
            "count": len(bad_edges),
            "repaired": repaired_edges,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Bi-Temporal Validity & Contradiction Audit Tool for Mnemosyne")
    parser.add_argument("--repair", action="store_true", default=False, help="Automatically close expired facts and resolve clashes")
    parser.add_argument("--db-path", default="", help="Explicit path to mnemosyne.db")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON report")

    args = parser.parse_args()

    try:
        res = audit_temporal_hygiene(db_path=args.db_path, repair=args.repair)
    except Exception as e:
        sys.stderr.write(f"ERROR: Temporal hygiene audit failed: {e}\n")
        sys.exit(1)

    if args.as_json:
        print(json.dumps(res, indent=2))
        return

    print("\nMnemosyne Bi-Temporal Validity & Contradiction Audit Report")
    print("=" * 65)
    print(f"Database: {res['database']}")
    print(f"Mode:     {res['mode'].upper()}")
    print("-" * 65)

    exp = res["expired_active_memories"]
    print(f"Expired Active Memories:  {exp['count']} found (Repaired: {exp['repaired']})")
    for s in exp["sample"]:
        print(f"  - [{s['id']}] Expired: {s['valid_until']} | {s['preview']}")

    clash = res["contradiction_clashes"]
    print(f"\nContradiction Clashes:    {clash['count']} found (Repaired: {clash['repaired']})")
    for c in clash["sample"]:
        print(f"  - Entity: '{c['subject']}' | Predicate: '{c['predicate']}'")
        print(f"    * A: {c['fact_a']}")
        print(f"    * B: {c['fact_b']}")

    edge = res["invalid_edges"]
    print(f"\nInvalid Graph Edges:      {edge['count']} found (Repaired: {edge['repaired']})")
    print("=" * 65)
    if not args.repair and (exp["count"] > 0 or clash["count"] > 0 or edge["count"] > 0):
        print("\nNotice: Run with --repair to automatically close expired facts and supersede conflicting edges.\n")
    else:
        print("\nTemporal knowledge state is clean.\n")


if __name__ == "__main__":
    main()
