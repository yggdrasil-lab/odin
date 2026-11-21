#!/usr/bin/env python3
"""audit_swarm_health.py - Docker Swarm Replica & Health Audit Engine.

Audits Swarm service convergence and task states across the fleet.
Supports dual-runtime execution:
1. Inside Odin container via read-only DOCKER_HOST (tcp://docker-socket-proxy:2375).
2. On Gaia host via local docker CLI / socket.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("audit-swarm-health")
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] [%(levelname)s] %(message)s")


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


def _collect_service_ips(services: Any) -> set[str]:
    """Extracts VIP and task endpoint IPv4 addresses from Swarm network services."""
    if not isinstance(services, dict):
        return set()
    ips: set[str] = set()
    for svc_data in services.values():
        if not isinstance(svc_data, dict):
            continue
        vip = str(svc_data.get("VIP") or "").strip()
        if vip:
            ips.add(vip)
        for task in svc_data.get("Tasks") or ():
            ip = str(task.get("EndpointIP") or "").strip() if isinstance(task, dict) else ""
            if ip:
                ips.add(ip)
    return ips


def _collect_container_ips(containers: Any) -> set[str]:
    """Extracts container IPv4 addresses from Swarm network inspect payload."""
    if not isinstance(containers, dict):
        return set()
    ips: set[str] = set()
    for cnt_data in containers.values():
        if not isinstance(cnt_data, dict):
            continue
        ip_only = str(cnt_data.get("IPv4Address") or "").split("/", 1)[0].strip()
        if ip_only:
            ips.add(ip_only)
    return ips


def _collect_overlay_active_ips(net_info: Dict[str, Any]) -> set[str]:
    """Extracts active service VIP, task, and container IPv4 addresses."""
    return _collect_service_ips(net_info.get("Services")) | _collect_container_ips(net_info.get("Containers"))


def _load_overlay_net_info(network: str) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Runs docker network inspect -v and validates JSON structure."""
    rc, stdout, stderr = run_cmd(["docker", "network", "inspect", "-v", network], timeout=5)
    if rc != 0 or not stdout:
        detail = stderr or "empty output"
        return None, _ipam_error(network, f"docker network inspect -v {network} failed (rc={rc}): {detail}")
    try:
        parsed = json.loads(stdout)
    except Exception as exc:
        return None, _ipam_error(network, f"Failed parsing docker network inspect JSON for {network}: {exc}")
    if not isinstance(parsed, list) or not parsed or not isinstance(parsed[0], dict):
        return None, _ipam_error(network, f"Unexpected docker network inspect payload for {network}")
    return parsed[0], None


def _parse_subnet_totals(
    subnets: Dict[str, Any], network: str
) -> Tuple[Optional[Tuple[str, int, Optional[int]]], Optional[Dict[str, Any]]]:
    """Parses IPAM subnet counters, returning (totals, error_report)."""
    ipv4_subnets = [(cidr, st) for cidr, st in subnets.items() if isinstance(st, dict) and ":" not in cidr]
    selected = ipv4_subnets or [(cidr, st) for cidr, st in subnets.items() if isinstance(st, dict)]
    if not selected:
        return None, _ipam_error(network, f"Missing subnet IPAM metrics in inspect output for {network}")
    try:
        if any(st.get("IPsInUse") is None for _, st in selected):
            return None, _ipam_error(network, f"Missing IPsInUse in Status.IPAM.Subnets for {network}")
        ips_in_use = sum(int(st["IPsInUse"]) for _, st in selected)
        dynamic_available = (
            sum(int(st["DynamicIPsAvailable"]) for _, st in selected)
            if all(st.get("DynamicIPsAvailable") is not None for _, st in selected)
            else None
        )
    except (TypeError, ValueError) as exc:
        return None, _ipam_error(network, f"Invalid IPAM counter values for {network}: {exc}")
    return (", ".join(cidr for cidr, _ in selected), ips_in_use, dynamic_available), None


def audit_overlay_ipam(
    network: str = "aether-net",
    min_available: int = 40,
    max_phantom_ips: int = 25,
) -> Dict[str, Any]:
    """Audits Swarm overlay network IPAM headroom and phantom IP leakage."""
    net_info, err = _load_overlay_net_info(network)
    if err is not None or net_info is None:
        return err or _ipam_error(network, f"Unexpected inspect failure for {network}")

    ipam_block = (net_info.get("Status") or {}).get("IPAM") if isinstance(net_info.get("Status"), dict) else None
    subnets = ipam_block.get("Subnets") if isinstance(ipam_block, dict) else None
    if not isinstance(subnets, dict) or not subnets:
        return _ipam_error(network, f"Missing Status.IPAM.Subnets in inspect output for {network}")

    totals, err = _parse_subnet_totals(subnets, network)
    if err is not None or totals is None:
        return err or _ipam_error(network, f"Failed reading IPAM subnets for {network}")
    subnet_label, ips_in_use, dynamic_available = totals

    real_active_ips = len(_collect_overlay_active_ips(net_info))
    peers_raw = net_info.get("Peers")
    peers_count = len(peers_raw) if isinstance(peers_raw, list) else 0
    reserved_ips = 3 + max(0, peers_count - 1)
    phantom_leaked_ips = max(0, ips_in_use - real_active_ips - reserved_ips)
    reasons = [
        r for r in (
            f"DynamicIPsAvailable={dynamic_available} < {min_available}" if dynamic_available is not None and dynamic_available < min_available else "",
            f"phantom_leaked_ips={phantom_leaked_ips} > {max_phantom_ips}" if phantom_leaked_ips > max_phantom_ips else "",
        ) if r
    ]
    degraded = bool(reasons)
    return {
        "network": network, "subnet": subnet_label, "status": "FAIL" if degraded else "PASS",
        "degraded": degraded, "ips_in_use": ips_in_use, "dynamic_ips_available": dynamic_available,
        "real_active_ips": real_active_ips, "reserved_ips": reserved_ips,
        "phantom_leaked_ips": phantom_leaked_ips, "peers": peers_count, "reason": "; ".join(reasons),
    }


def audit_swarm_services(
    overlay_network: str = "aether-net",
    min_available_ips: int = 40,
    max_phantom_ips: int = 25,
) -> Dict[str, Any]:
    """Main audit function that evaluates Swarm service replicas and overlay IPAM health."""
    total_count, services = query_swarm_services()
    degraded_list: List[Dict[str, Any]] = []

    for svc in services:
        if svc["degraded"]:
            task_info = query_service_task_state(svc["name"])
            svc["task_state"] = task_info.get("state", "")
            svc["task_error"] = task_info.get("error", "")
            svc["node"] = task_info.get("node", "")
            degraded_list.append(svc)

    ipam_report = audit_overlay_ipam(
        network=overlay_network,
        min_available=min_available_ips,
        max_phantom_ips=max_phantom_ips,
    )

    status = "FAIL" if (degraded_list or ipam_report.get("degraded")) else "PASS"
    return {
        "status": status,
        "total_services": total_count,
        "healthy_services": total_count - len(degraded_list),
        "degraded_count": len(degraded_list),
        "degraded_services": degraded_list,
        "overlay_ipam": ipam_report,
    }


def _print_text_report(report: Dict[str, Any], default_network: str) -> None:
    """Prints a human-readable Swarm service and overlay IPAM health summary."""
    print("=== Docker Swarm Replica & Health Audit ===")
    if report["status"] == "PASS":
        print(f"[PASS] {report['total_services']}/{report['total_services']} services healthy (0 degraded)")
    else:
        print(f"[FAIL] {report['healthy_services']}/{report['total_services']} services healthy ({report['degraded_count']} degraded)")
        for d in report["degraded_services"]:
            print(f"  - DEGRADED: {d['name']} ({d['replicas']}) | State: {d.get('task_state')}")
            if d.get("task_error"):
                print(f"    Error: {d['task_error']}")

    ipam = report.get("overlay_ipam") or {}
    if not ipam:
        return
    net_name = ipam.get("network", default_network)
    ipam_status = ipam.get("status", "UNKNOWN")
    in_use = ipam.get("ips_in_use")
    free = ipam.get("dynamic_ips_available")
    phantom = ipam.get("phantom_leaked_ips")
    if ipam.get("degraded"):
        metrics = f"{in_use} in use, {free} free (~{phantom} phantom)" if in_use is not None else "metrics unavailable"
        print(f"[{ipam_status}] Overlay IPAM ({net_name}): {metrics} | {ipam.get('reason')}")
    else:
        print(f"[{ipam_status}] Overlay IPAM ({net_name}): {in_use} in use, {free} free (~{phantom} phantom)")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit Docker Swarm service replicas and task health.")
    parser.add_argument("--json", action="store_true", help="Output machine-readable JSON")
    parser.add_argument("--quiet", action="store_true", help="Exit 0 even if services are degraded")
    parser.add_argument("--network", default="aether-net", help="Swarm overlay network to audit for IPAM health")
    parser.add_argument("--min-available-ips", type=int, default=40, help="Minimum free dynamic IPs threshold")
    parser.add_argument("--max-phantom-ips", type=int, default=25, help="Maximum phantom leaked IPs threshold")
    args = parser.parse_args()

    report = audit_swarm_services(
        overlay_network=args.network,
        min_available_ips=args.min_available_ips,
        max_phantom_ips=args.max_phantom_ips,
    )

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        _print_text_report(report, args.network)

    if args.quiet:
        return 0
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
