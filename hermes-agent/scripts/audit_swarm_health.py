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


def _run_kanban_triage(event_text: str, title: str, label: str) -> Optional[str]:
    """Dispatches an event to kanban-gateway triage and parses the created task ID."""
    cmd = [
        "kanban-gateway", "triage", event_text,
        "--title", title,
        "--context", "Daily Morning Swarm Audit on Gaia"
    ]
    rc, stdout, stderr = run_cmd(cmd, timeout=12)
    if rc == 0 and stdout:
        logger.info("kanban-gateway triage succeeded for %s: %s", label, stdout)
        # Parse task ID from output if present (e.g. kb-xxxxxxxx)
        match = re.search(r"\b(kb-[0-9a-fA-F]{8})\b", stdout)
        return match.group(1) if match else "created"
    logger.warning("kanban-gateway triage returned code %d: %s", rc, stderr or stdout)
    return None


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
    return _run_kanban_triage(event_text, title, service_name)


def dispatch_ipam_to_kanban_triage(ipam_report: Dict[str, Any]) -> Optional[str]:
    """Dispatches a degraded overlay IPAM event to kanban-gateway triage."""
    network = str(ipam_report.get("network") or "aether-net")
    title = f"Investigate Swarm overlay IPAM: {network}"
    event_text = (
        f"Docker Swarm overlay IPAM: {network} audit failed ({ipam_report.get('status')}). "
        f"Reason: {ipam_report.get('reason') or 'unknown error'}. "
        f"IPsInUse={ipam_report.get('ips_in_use')}, "
        f"DynamicIPsAvailable={ipam_report.get('dynamic_ips_available')}, "
        f"real_active_ips={ipam_report.get('real_active_ips')}, "
        f"phantom_leaked_ips={ipam_report.get('phantom_leaked_ips')}, "
        f"peers={ipam_report.get('peers')}."
    )
    return _run_kanban_triage(event_text, title, f"overlay IPAM {network}")


def _ipam_error(network: str, reason: str) -> Dict[str, Any]:
    """Builds a fail-loud ERROR report for overlay IPAM inspection failures."""
    return {
        "network": network,
        "subnet": None,
        "status": "ERROR",
        "degraded": True,
        "ips_in_use": None,
        "dynamic_ips_available": None,
        "real_active_ips": 0,
        "reserved_ips": 0,
        "phantom_leaked_ips": None,
        "peers": 0,
        "reason": reason,
    }


def audit_overlay_ipam(
    network: str = "aether-net",
    min_available: int = 40,
    max_phantom_ips: int = 25,
) -> Dict[str, Any]:
    """Audits Swarm overlay network IPAM headroom and phantom IP leakage."""
    rc, stdout, stderr = run_cmd(["docker", "network", "inspect", "-v", network], timeout=5)
    if rc != 0 or not stdout:
        detail = stderr or "empty output"
        return _ipam_error(network, f"docker network inspect -v {network} failed (rc={rc}): {detail}")

    try:
        parsed = json.loads(stdout)
    except Exception as exc:
        return _ipam_error(network, f"Failed parsing docker network inspect JSON for {network}: {exc}")

    if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
        return _ipam_error(network, f"Unexpected docker network inspect payload for {network}")

    net_info = parsed[0]
    status_block = net_info.get("Status")
    ipam_block = status_block.get("IPAM") if isinstance(status_block, dict) else None
    subnets = ipam_block.get("Subnets") if isinstance(ipam_block, dict) else None
    if not isinstance(subnets, dict) or not subnets:
        return _ipam_error(network, f"Missing Status.IPAM.Subnets in inspect output for {network}")

    ipv4_subnets = [(cidr, st) for cidr, st in subnets.items() if isinstance(st, dict) and ":" not in cidr]
    selected = ipv4_subnets if ipv4_subnets else [(cidr, st) for cidr, st in subnets.items() if isinstance(st, dict)]
    if not selected:
        return _ipam_error(network, f"Missing subnet IPAM metrics in inspect output for {network}")

    try:
        if any(st.get("IPsInUse") is None for _, st in selected):
            return _ipam_error(network, f"Missing IPsInUse in Status.IPAM.Subnets for {network}")
        ips_in_use = sum(int(st["IPsInUse"]) for _, st in selected)
        if all(st.get("DynamicIPsAvailable") is not None for _, st in selected):
            dynamic_available: Optional[int] = sum(int(st["DynamicIPsAvailable"]) for _, st in selected)
        else:
            dynamic_available = None
    except (TypeError, ValueError) as exc:
        return _ipam_error(network, f"Invalid IPAM counter values for {network}: {exc}")

    subnet_label = ", ".join(cidr for cidr, _ in selected)

    active_ips = set()
    services = net_info.get("Services")
    if isinstance(services, dict):
        for svc_data in services.values():
            if not isinstance(svc_data, dict):
                continue
            vip = str(svc_data.get("VIP") or "").strip()
            if vip:
                active_ips.add(vip)
            tasks = svc_data.get("Tasks")
            if isinstance(tasks, list):
                for task in tasks:
                    if not isinstance(task, dict):
                        continue
                    endpoint_ip = str(task.get("EndpointIP") or "").strip()
                    if endpoint_ip:
                        active_ips.add(endpoint_ip)

    containers = net_info.get("Containers")
    if isinstance(containers, dict):
        for cnt_data in containers.values():
            if not isinstance(cnt_data, dict):
                continue
            ipv4_addr = str(cnt_data.get("IPv4Address") or "").strip()
            if ipv4_addr:
                ip_only = ipv4_addr.split("/", 1)[0].strip()
                if ip_only:
                    active_ips.add(ip_only)

    real_active_ips = len(active_ips)
    peers_raw = net_info.get("Peers")
    peers_count = len(peers_raw) if isinstance(peers_raw, list) else 0
    reserved_ips = 3 + max(0, peers_count - 1)
    phantom_leaked_ips = max(0, ips_in_use - real_active_ips - reserved_ips)

    reasons: List[str] = []
    if dynamic_available is not None and dynamic_available < min_available:
        reasons.append(f"DynamicIPsAvailable={dynamic_available} < {min_available}")
    if phantom_leaked_ips > max_phantom_ips:
        reasons.append(f"phantom_leaked_ips={phantom_leaked_ips} > {max_phantom_ips}")

    degraded = bool(reasons)
    status = "FAIL" if degraded else "PASS"
    reason = "; ".join(reasons)

    return {
        "network": network,
        "subnet": subnet_label,
        "status": status,
        "degraded": degraded,
        "ips_in_use": ips_in_use,
        "dynamic_ips_available": dynamic_available,
        "real_active_ips": real_active_ips,
        "reserved_ips": reserved_ips,
        "phantom_leaked_ips": phantom_leaked_ips,
        "peers": peers_count,
        "reason": reason,
    }


def audit_swarm_services(
    triage: bool = False,
    kanban_db_path: str = DEFAULT_KANBAN_DB,
    overlay_network: str = "aether-net",
    min_available_ips: int = 40,
    max_phantom_ips: int = 25,
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

    ipam_report = audit_overlay_ipam(
        network=overlay_network,
        min_available=min_available_ips,
        max_phantom_ips=max_phantom_ips,
    )
    if ipam_report.get("degraded"):
        already_tracked = is_already_tracked_in_kanban(f"overlay IPAM: {overlay_network}", kanban_db_path)
        ipam_report["already_tracked"] = already_tracked
        ipam_report["triage_id"] = None
        if triage and not already_tracked:
            ipam_report["triage_id"] = dispatch_ipam_to_kanban_triage(ipam_report)

    status = "FAIL" if (degraded_list or ipam_report.get("degraded")) else "PASS"
    healthy_count = total_count - len(degraded_list)
    return {
        "status": status,
        "total_services": total_count,
        "healthy_services": healthy_count,
        "degraded_count": len(degraded_list),
        "degraded_services": degraded_list,
        "overlay_ipam": ipam_report,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Docker Swarm service replicas and task health.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--triage", action="store_true", help="Auto-dispatch new degraded services to kanban-gateway triage")
    parser.add_argument("--kanban-db", default=DEFAULT_KANBAN_DB, help="Path to kanban.db")
    parser.add_argument("--quiet", action="store_true", help="Exit 0 even if services are degraded")
    parser.add_argument("--network", default="aether-net", help="Swarm overlay network to audit for IPAM health")
    parser.add_argument("--min-available-ips", type=int, default=40, help="Minimum free dynamic IPs threshold")
    parser.add_argument("--max-phantom-ips", type=int, default=25, help="Maximum phantom leaked IPs threshold")
    args = parser.parse_args()

    report = audit_swarm_services(
        triage=args.triage,
        kanban_db_path=args.kanban_db,
        overlay_network=args.network,
        min_available_ips=args.min_available_ips,
        max_phantom_ips=args.max_phantom_ips,
    )

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

        ipam = report.get("overlay_ipam") or {}
        if ipam:
            net_name = ipam.get("network", args.network)
            ipam_status = ipam.get("status", "UNKNOWN")
            in_use = ipam.get("ips_in_use")
            free = ipam.get("dynamic_ips_available")
            phantom = ipam.get("phantom_leaked_ips")
            if ipam.get("degraded"):
                tracked_note = " (Already in Kanban)" if ipam.get("already_tracked") else ""
                triage_note = f" -> Dispatched to Kanban: {ipam['triage_id']}" if ipam.get("triage_id") else ""
                metrics = (
                    f"{in_use} in use, {free} free (~{phantom} phantom)"
                    if in_use is not None
                    else "metrics unavailable"
                )
                print(f"[{ipam_status}] Overlay IPAM ({net_name}): {metrics} | {ipam.get('reason')}{tracked_note}{triage_note}")
            else:
                print(
                    f"[{ipam_status}] Overlay IPAM ({net_name}): "
                    f"{in_use} in use, {free} free (~{phantom} phantom)"
                )

    if args.quiet:
        return 0
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
