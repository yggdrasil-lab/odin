#!/usr/bin/env python3
"""mnemosyne_associative.py - HippoRAG-Style Associative Memory Recall via Personalized PageRank (PPR).

Performs single-step multi-hop associative recall over Mnemosyne's knowledge graph
(graph_edges + consolidated_facts) in pure Python standard library (<60ms latency, zero extra packages).

Eliminates the 'tunnel vision' of flat vector similarity search by spreading activation
from query entities across connected infrastructure and task nodes.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Set, Tuple


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


def run_associative_ppr(
    query_terms: List[str],
    top_k: int = 10,
    alpha: float = 0.85,
    max_iter: int = 20,
    db_path: str = "",
) -> Dict[str, Any]:
    """Runs Personalized PageRank over Mnemosyne graph nodes seeded with query terms."""
    resolved_db = db_path or resolve_db_path()
    # Read-only URI mode to prevent any WAL lock contention
    uri_path = f"file:{resolved_db}?mode=ro"
    conn = sqlite3.connect(uri_path, uri=True)
    cursor = conn.cursor()

    t0 = time.perf_counter()

    # 1. Build adjacency list from active graph_edges
    cursor.execute("SELECT source, target, weight FROM graph_edges WHERE weight > 0")
    edges = cursor.fetchall()

    adj: Dict[str, Dict[str, float]] = defaultdict(dict)
    nodes: Set[str] = set()

    for src, tgt, w in edges:
        w_float = float(w or 1.0)
        adj[src][tgt] = max(adj[src].get(tgt, 0.0), w_float)
        adj[tgt][src] = max(adj[tgt].get(src, 0.0), w_float)  # Bidirectional association
        nodes.add(src)
        nodes.add(tgt)

    # 2. Integrate consolidated_facts: subject <-> object and fact_id connections
    cursor.execute("SELECT id, subject, object, confidence FROM consolidated_facts WHERE superseded_by IS NULL")
    for fact_id, subj, obj, conf in cursor.fetchall():
        if subj and obj:
            c = float(conf or 1.0)
            adj[subj][obj] = max(adj[subj].get(obj, 0.0), c)
            adj[obj][subj] = max(adj[obj].get(subj, 0.0), c)
            adj[subj][fact_id] = max(adj[subj].get(fact_id, 0.0), c)
            adj[fact_id][subj] = max(adj[fact_id].get(subj, 0.0), c)
            nodes.add(subj)
            nodes.add(obj)
            nodes.add(fact_id)

    # 3. Transition probability normalization
    trans: Dict[str, Dict[str, float]] = {}
    for u, neighbors in adj.items():
        total_w = sum(neighbors.values())
        if total_w > 0:
            trans[u] = {v: w / total_w for v, w in neighbors.items()}
        else:
            trans[u] = {}

    # 4. Identify seed nodes matching query terms
    seed_nodes: Set[str] = set()
    for term in query_terms:
        term_clean = term.lower().strip()
        if not term_clean:
            continue
        for node in nodes:
            if term_clean in node.lower():
                seed_nodes.add(node)

    # Seed fallback: check FTS table on working_memory
    if not seed_nodes:
        valid_terms = [q for q in query_terms if len(q) >= 3]
        if valid_terms:
            fts_query = " OR ".join(f'"{q}"' for q in valid_terms)
            try:
                cursor.execute("SELECT id FROM fts_working WHERE fts_working MATCH ? LIMIT 15", (fts_query,))
                for r in cursor.fetchall():
                    if r[0] in nodes:
                        seed_nodes.add(r[0])
            except Exception:
                pass

    if not seed_nodes:
        conn.close()
        return {
            "status": "no_seeds_found",
            "query": query_terms,
            "latency_ms": (time.perf_counter() - t0) * 1000,
            "results": [],
        }

    # 5. Initialize teleport vector p
    p = {node: (1.0 / len(seed_nodes) if node in seed_nodes else 0.0) for node in nodes}
    v = dict(p)

    # 6. Power iteration
    for _ in range(max_iter):
        next_v: Dict[str, float] = defaultdict(float)
        for u, rank in v.items():
            if rank <= 0.0:
                continue
            neighbors = trans.get(u, {})
            if neighbors:
                for nxt, prob in neighbors.items():
                    next_v[nxt] += alpha * rank * prob
            else:
                for s, tele_prob in p.items():
                    next_v[s] += alpha * rank * tele_prob

        # Teleport injection
        for node, tele_prob in p.items():
            next_v[node] += (1.0 - alpha) * tele_prob

        # Check L1 convergence
        diff = sum(abs(next_v[n] - v[n]) for n in nodes)
        v = next_v
        if diff < 1e-5:
            break

    elapsed_ms = (time.perf_counter() - t0) * 1000

    # 7. Rank associative nodes excluding seed nodes
    ranked_nodes = sorted(
        [(n, score) for n, score in v.items() if n not in seed_nodes],
        key=lambda x: x[1],
        reverse=True,
    )[:top_k]

    results: List[Dict[str, Any]] = []
    for node_id, score in ranked_nodes:
        # Check if node is working_memory row
        cursor.execute(
            "SELECT content, source, veracity, valid_until FROM working_memory WHERE id = ?",
            (node_id,),
        )
        wm_row = cursor.fetchone()
        if wm_row:
            results.append({
                "type": "memory",
                "id": node_id,
                "score": round(score, 5),
                "content": wm_row[0],
                "source": wm_row[1],
                "veracity": wm_row[2],
                "valid_until": wm_row[3],
            })
            continue

        # Check if node is consolidated_facts row
        cursor.execute(
            "SELECT subject, predicate, object, confidence FROM consolidated_facts WHERE id = ?",
            (node_id,),
        )
        cf_row = cursor.fetchone()
        if cf_row:
            results.append({
                "type": "fact",
                "id": node_id,
                "score": round(score, 5),
                "fact": f"{cf_row[0]} -> {cf_row[1]} -> {cf_row[2]}",
                "confidence": cf_row[3],
            })
            continue

        # Otherwise entity node
        results.append({
            "type": "entity",
            "id": node_id,
            "score": round(score, 5),
            "label": node_id,
        })

    conn.close()

    return {
        "status": "ok",
        "query": query_terms,
        "seed_nodes": list(seed_nodes)[:10],
        "latency_ms": round(elapsed_ms, 2),
        "total_nodes_traversed": len(nodes),
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="HippoRAG-Style Associative Memory Recall via Personalized PageRank")
    parser.add_argument("terms", nargs="+", help="Query keywords or entity names")
    parser.add_argument("--top-k", type=int, default=10, help="Max results to return (default: 10)")
    parser.add_argument("--alpha", type=float, default=0.85, help="Teleport probability parameter (default: 0.85)")
    parser.add_argument("--max-iter", type=int, default=20, help="Power iteration steps (default: 20)")
    parser.add_argument("--db-path", default="", help="Explicit path to mnemosyne.db")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output raw JSON results")

    args = parser.parse_args()

    try:
        output = run_associative_ppr(
            query_terms=args.terms,
            top_k=args.top_k,
            alpha=args.alpha,
            max_iter=args.max_iter,
            db_path=args.db_path,
        )
    except Exception as e:
        sys.stderr.write(f"ERROR: Associative recall failed: {e}\n")
        sys.exit(1)

    if args.as_json:
        print(json.dumps(output, indent=2))
        return

    print(f"\nHippoRAG Associative Recall for: {', '.join(output['query'])}")
    print(f"Latency: {output['latency_ms']}ms | Traversed Nodes: {output.get('total_nodes_traversed', 0)}")
    print(f"Seed Entities ({len(output.get('seed_nodes', []))}): {', '.join(output.get('seed_nodes', []))}\n")
    print(f"{'RANK':4} {'SCORE':8} {'TYPE':8} {'CONTENT / FACT'}")
    print("-" * 80)
    for idx, r in enumerate(output.get("results", []), 1):
        desc = r.get("content") or r.get("fact") or r.get("label", "")
        # Single line truncation
        clean_desc = desc.replace("\n", " ")[:65]
        print(f"{idx:<4} {r['score']:<8.4f} {r['type']:<8} {clean_desc}")
    print()


if __name__ == "__main__":
    main()
