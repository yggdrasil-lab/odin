#!/usr/bin/env python3
"""audit_swarm_health.py - Docker Swarm Replica & Health Audit Engine.

Audits Swarm service convergence and task states across the fleet.
Supports dual-runtime execution:
1. Inside Odin container via read-only DOCKER_HOST (tcp://docker-socket-proxy:2375).
2. On Gaia host via local docker CLI / socket.

When degraded services are detected and --triage is enabled:
- Checks kanban.db for existing active tasks to prevent duplicate alerts.
- Dispatches actionable incidents to `kanban-gateway triage` for Jev evaluation.
"""

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("audit-swarm-health")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")

DEFAULT_KANBAN_DB = (
    "/opt/data/kanban.db" if os.path.exists("/opt/data/kanban.db") else "/opt/odin/hermes/kanban.db"
)


def run_cmd(cmd: List[str], timeout: int = 5) -> Tuple[int, str, str]:
    """Executes a bounded command without persistent subshells."""
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            ["timeout", str(timeout)] + cmd,
            capture_output=True,
            text=True,
            env=env,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except Exception as exc:
        return 1, "", str(exc)


def query_swarm_services() -> Tuple[int, List[Dict[str, Any]]]:
    """Queries all Swarm services and parses replica status."""
    rc, stdout, stderr = run_cmd([
        "docker", "service", "ls", "--format", "{{.Name}}\t{{.Replicas}}\t{{.Image}}"
    ])
    if rc != 0:
        logger.warning("Failed querying docker service ls: %s", stderr)
        return 0, []

    services: List[Dict[str, Any]] = []
    for line in stdout.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        parts = line.split("\t")
        name = parts[0]
        replicas = parts[1] if len(parts) > 1 else ""
        image = parts[2] if len(parts) > 2 else ""

        is_degraded = False
        cur_str, des_str = "0", "0"
        rep_clean = replicas.split(" ")[0]
        if "/" in rep_clean:
            cur_str, des_str = rep_clean.split("/", 1)
            try:
                cur = int(cur_str)
                des = int(des_str)
                is_degraded = cur < des
            except ValueError:
                is_degraded = False

        services.append({
            "name": name,
            "replicas": replicas,
            "image": image,
            "current_replicas": cur_str,
            "desired_replicas": des_str,
            "degraded": is_degraded,
        })

    return len(services), services


def query_service_task_state(service_name: str) -> Dict[str, str]:
    """Queries the most recent task state and error message for a given service."""
    rc, stdout, _ = run_cmd([
        "docker", "service", "ps", service_name, "--no-trunc",
        "--format", "{{.Name}}\t{{.Node}}\t{{.CurrentState}}\t{{.Error}}"
    ])
    if rc != 0 or not stdout:
        return {"task_name": "", "node": "", "state": "unknown", "error": ""}

    first_line = stdout.splitlines()[0].strip()
    parts = first_line.split("\t")
    return {
        "task_name": parts[0] if len(parts) > 0 else "",
        "node": parts[1] if len(parts) > 1 else "",
        "state": parts[2] if len(parts) > 2 else "",
        "error": parts[3] if len(parts) > 3 else "",
    }


def is_already_tracked_in_kanban(service_name: str, db_path: str = DEFAULT_KANBAN_DB) -> bool:
    """Checks whether an active Kanban task already exists for this service."""
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        cursor = conn.cursor()
        query = (
            "SELECT COUNT(*) FROM tasks "
            "WHERE status != 'done' AND ("
            "  title LIKE ? OR body LIKE ?"
            ")"
        )
        term = f"%{service_name}%"
        cursor.execute(query, (term, term))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0
    except Exception as exc:
        logger.debug("Failed querying kanban.db for duplicate check: %s", exc)
        return False


def dispatch_to_kanban_triage(
    service_name: str, replicas: str, task_state: str, task_error: str, node: str
) -> Optional[str]:
    """Dispatches a degraded service event to kanban-gateway triage."""
    err_context = f" Task error: {task_error}" if task_error else ""
    event_text = (
        f"Docker Swarm service '{service_name}' is degraded ({replicas}) on node '{node}'. "
        f"Current task state: {task_state}.{err_context}"
    )
    title = f"Fix degraded Swarm service: {service_name}"
    cmd = [
        "kanban-gateway", "triage", event_text,
        "--title", title,
        "--context", "Daily Morning Swarm Audit on Gaia"
    ]
    rc, stdout, stderr = run_cmd(cmd, timeout=12)
    if rc == 0 and stdout:
        logger.info("kanban-gateway triage succeeded for %s: %s", service_name, stdout)
        # Parse task ID from output if present (e.g. kb-xxxxxxxx)
        match = re.search(r"\b(kb-[0-9a-fA-F]{8})\b", stdout)
        return match.group(1) if match else "created"
    logger.warning("kanban-gateway triage returned code %d: %s", rc, stderr or stdout)
    return None


def audit_swarm_services(
    triage: bool = False,
    kanban_db_path: str = DEFAULT_KANBAN_DB
) -> Dict[str, Any]:
    """Main audit function that evaluates Swarm service health and optionally triggers triage."""
    total_count, services = query_swarm_services()
    degraded_list: List[Dict[str, Any]] = []

    for svc in services:
        if svc["degraded"]:
            task_info = query_service_task_state(svc["name"])
            svc["task_state"] = task_info.get("state", "")
            svc["task_error"] = task_info.get("error", "")
            svc["node"] = task_info.get("node", "")
            svc["already_tracked"] = is_already_tracked_in_kanban(svc["name"], kanban_db_path)
            svc["triage_id"] = None

            if triage and not svc["already_tracked"]:
                svc["triage_id"] = dispatch_to_kanban_triage(
                    svc["name"],
                    svc["replicas"],
                    svc["task_state"],
                    svc["task_error"],
                    svc["node"],
                )
            degraded_list.append(svc)

    status = "PASS" if not degraded_list else "FAIL"
    healthy_count = total_count - len(degraded_list)
    return {
        "status": status,
        "total_services": total_count,
        "healthy_services": healthy_count,
        "degraded_count": len(degraded_list),
        "degraded_services": degraded_list,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Docker Swarm service replicas and task health.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--triage", action="store_true", help="Auto-dispatch new degraded services to kanban-gateway triage")
    parser.add_argument("--kanban-db", default=DEFAULT_KANBAN_DB, help="Path to kanban.db")
    parser.add_argument("--quiet", action="store_true", help="Exit 0 even if services are degraded")
    args = parser.parse_args()

    report = audit_swarm_services(triage=args.triage, kanban_db_path=args.kanban_db)

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print("=== Docker Swarm Replica & Health Audit ===")
        if report["status"] == "PASS":
            print(f"[PASS] {report['total_services']}/{report['total_services']} services healthy (0 degraded)")
        else:
            print(f"[FAIL] {report['healthy_services']}/{report['total_services']} services healthy ({report['degraded_count']} degraded)")
            for d in report["degraded_services"]:
                tracked_note = " (Already in Kanban)" if d.get("already_tracked") else ""
                triage_note = f" -> Dispatched to Kanban: {d['triage_id']}" if d.get("triage_id") else ""
                print(f"  - DEGRADED: {d['name']} ({d['replicas']}) | State: {d.get('task_state')}{tracked_note}{triage_note}")
                if d.get("task_error"):
                    print(f"    Error: {d['task_error']}")

    if args.quiet:
        return 0
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
