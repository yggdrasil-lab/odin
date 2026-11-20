#!/usr/bin/env python3
"""test_audit_swarm_health.py - Unit tests for Swarm Replica & Health Audit Engine."""

import json
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from audit_swarm_health import (
    audit_overlay_ipam,
    audit_swarm_services,
    is_already_tracked_in_kanban,
    query_swarm_services,
)
from morning_copilot_aggregator import (
    OdinTelemetry,
    generate_executive_markdown,
    MorningCopilotReport,
    EirTelemetry,
    KratosTelemetry,
    PlutusTelemetry,
)


class TestAuditSwarmHealth(unittest.TestCase):
    def test_query_swarm_services_parsing(self):
        mock_output = (
            "apollo-core_jellyfin\t1/1\tregistry/jellyfin\n"
            "apollo-core_recyclarr\t0/1\tghcr.io/recyclarr/recyclarr:latest\n"
            "olympus_traefik\t1/1\ttraefik:latest\n"
        )
        with patch("audit_swarm_health.run_cmd", return_value=(0, mock_output, "")):
            total, services = query_swarm_services()
            self.assertEqual(total, 3)
            self.assertEqual(len(services), 3)

            recyclarr = next(s for s in services if s["name"] == "apollo-core_recyclarr")
            self.assertTrue(recyclarr["degraded"])
            self.assertEqual(recyclarr["current_replicas"], "0")
            self.assertEqual(recyclarr["desired_replicas"], "1")

            jellyfin = next(s for s in services if s["name"] == "apollo-core_jellyfin")
            self.assertFalse(jellyfin["degraded"])

    def test_kanban_deduplication(self):
        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            conn = sqlite3.connect(tmp.name)
            conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT, body TEXT, status TEXT)")
            conn.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?)",
                ("kb-test01", "Fix apollo-core_recyclarr degraded", "Task restarting", "ready")
            )
            conn.commit()
            conn.close()

            # Should be detected as already tracked
            self.assertTrue(is_already_tracked_in_kanban("apollo-core_recyclarr", db_path=tmp.name))
            # Unknown service should not be tracked
            self.assertFalse(is_already_tracked_in_kanban("unknown_service", db_path=tmp.name))

    @patch("audit_swarm_health.audit_overlay_ipam")
    @patch("audit_swarm_health.dispatch_to_kanban_triage", return_value="kb-autofix1")
    @patch("audit_swarm_health.is_already_tracked_in_kanban", return_value=False)
    @patch("audit_swarm_health.query_service_task_state")
    @patch("audit_swarm_health.query_swarm_services")
    def test_audit_swarm_services_with_triage(
        self, mock_query, mock_task_state, mock_tracked, mock_dispatch, mock_ipam
    ):
        mock_query.return_value = (
            2,
            [
                {"name": "svc1", "replicas": "1/1", "degraded": False},
                {"name": "svc2", "replicas": "0/1", "degraded": True},
            ],
        )
        mock_task_state.return_value = {
            "task_name": "svc2.1",
            "node": "gaia",
            "state": "Starting 10 seconds ago",
            "error": "task failed",
        }
        mock_ipam.return_value = {
            "network": "aether-net",
            "subnet": "10.0.1.0/24",
            "status": "PASS",
            "degraded": False,
            "ips_in_use": 133,
            "dynamic_ips_available": 123,
            "real_active_ips": 129,
            "reserved_ips": 4,
            "phantom_leaked_ips": 0,
            "peers": 2,
            "reason": "",
        }

        report = audit_swarm_services(triage=True, kanban_db_path="/tmp/fake.db")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["total_services"], 2)
        self.assertEqual(report["healthy_services"], 1)
        self.assertEqual(report["degraded_count"], 1)
        self.assertEqual(report["overlay_ipam"]["status"], "PASS")

        degraded = report["degraded_services"][0]
        self.assertEqual(degraded["name"], "svc2")
        self.assertEqual(degraded["triage_id"], "kb-autofix1")
        mock_dispatch.assert_called_once()

    @staticmethod
    def _build_inspect_payload(
        ips_in_use: int,
        dynamic_available: int,
        real_ips_count: int,
        peers_count: int = 2,
    ) -> str:
        """Builds a mock docker network inspect -v JSON payload with real_ips_count unique IPs."""
        # Put half in Services VIP/Tasks and half in Containers, plus empty/duplicate entries
        services = {}
        containers = {}
        ips = [f"10.0.{(i // 250) + 1}.{(i % 250) + 2}" for i in range(real_ips_count)]
        split_idx = len(ips) // 2

        tasks = [{"EndpointIP": ip} for ip in ips[:split_idx]]
        if ips:
            # Duplicate first IP as VIP and include an empty string to test union & empty filtering
            services["svc_a"] = {"VIP": ips[0], "Tasks": tasks}
        services["svc_empty"] = {"VIP": "", "Tasks": [{"EndpointIP": ""}]}

        for idx, ip in enumerate(ips[split_idx:]):
            containers[f"cnt_{idx}"] = {"IPv4Address": f"{ip}/24"}
        containers["cnt_empty"] = {"IPv4Address": ""}

        peers = [{"Name": f"peer_{i}", "IP": f"192.168.86.{160 + i}"} for i in range(peers_count)]
        payload = [
            {
                "Name": "aether-net",
                "Peers": peers,
                "Services": services,
                "Containers": containers,
                "Status": {
                    "IPAM": {
                        "Subnets": {
                            "10.0.1.0/24": {
                                "IPsInUse": ips_in_use,
                                "DynamicIPsAvailable": dynamic_available,
                            }
                        }
                    }
                },
            }
        ]
        return json.dumps(payload)

    def test_audit_overlay_ipam_healthy_baseline(self):
        payload = self._build_inspect_payload(
            ips_in_use=133, dynamic_available=123, real_ips_count=129, peers_count=2
        )
        with patch("audit_swarm_health.run_cmd", return_value=(0, payload, "")):
            res = audit_overlay_ipam("aether-net", min_available=40, max_phantom_ips=25)
        self.assertEqual(res["status"], "PASS")
        self.assertFalse(res["degraded"])
        self.assertEqual(res["ips_in_use"], 133)
        self.assertEqual(res["dynamic_ips_available"], 123)
        self.assertEqual(res["real_active_ips"], 129)
        self.assertEqual(res["reserved_ips"], 4)
        self.assertEqual(res["phantom_leaked_ips"], 0)

    def test_audit_overlay_ipam_low_headroom(self):
        payload = self._build_inspect_payload(
            ips_in_use=231, dynamic_available=25, real_ips_count=227, peers_count=2
        )
        with patch("audit_swarm_health.run_cmd", return_value=(0, payload, "")):
            res = audit_overlay_ipam("aether-net", min_available=40, max_phantom_ips=25)
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(res["degraded"])
        self.assertIn("DynamicIPsAvailable=25 < 40", res["reason"])

    def test_audit_overlay_ipam_high_phantom_leak(self):
        payload = self._build_inspect_payload(
            ips_in_use=210, dynamic_available=46, real_ips_count=130, peers_count=2
        )
        with patch("audit_swarm_health.run_cmd", return_value=(0, payload, "")):
            res = audit_overlay_ipam("aether-net", min_available=40, max_phantom_ips=25)
        self.assertEqual(res["status"], "FAIL")
        self.assertTrue(res["degraded"])
        self.assertEqual(res["phantom_leaked_ips"], 76)
        self.assertIn("phantom_leaked_ips=76 > 25", res["reason"])

    def test_audit_overlay_ipam_inspect_error_fails_loud(self):
        with patch("audit_swarm_health.run_cmd", return_value=(1, "", "network not found")):
            res = audit_overlay_ipam("aether-net")
        self.assertEqual(res["status"], "ERROR")
        self.assertTrue(res["degraded"])
        self.assertIn("failed", res["reason"])

    @patch("audit_swarm_health.query_swarm_services", return_value=(1, [{"name": "svc1", "replicas": "1/1", "degraded": False}]))
    def test_audit_swarm_services_ipam_triage_dispatch_and_dedup(self, _mock_query):
        degraded_ipam = {
            "network": "aether-net",
            "subnet": "10.0.1.0/24",
            "status": "FAIL",
            "degraded": True,
            "ips_in_use": 210,
            "dynamic_ips_available": 46,
            "real_active_ips": 130,
            "reserved_ips": 4,
            "phantom_leaked_ips": 76,
            "peers": 2,
            "reason": "phantom_leaked_ips=76 > 25",
        }
        # First call: not yet tracked -> should dispatch kanban-gateway triage
        with patch("audit_swarm_health.audit_overlay_ipam", return_value=dict(degraded_ipam)), \
             patch("audit_swarm_health.is_already_tracked_in_kanban", return_value=False) as mock_tracked, \
             patch("audit_swarm_health.run_cmd", return_value=(0, "Created kb-12ab34cd", "")) as mock_cmd:
            report = audit_swarm_services(triage=True, kanban_db_path="/tmp/fake.db")
        self.assertEqual(report["status"], "FAIL")
        self.assertFalse(report["overlay_ipam"]["already_tracked"])
        self.assertEqual(report["overlay_ipam"]["triage_id"], "kb-12ab34cd")
        mock_tracked.assert_called_once_with("overlay IPAM: aether-net", "/tmp/fake.db")
        mock_cmd.assert_called_once()
        cmd_args = mock_cmd.call_args[0][0]
        self.assertIn("Investigate Swarm overlay IPAM: aether-net", cmd_args)

        # Second call: already tracked -> should skip dispatch
        with patch("audit_swarm_health.audit_overlay_ipam", return_value=dict(degraded_ipam)), \
             patch("audit_swarm_health.is_already_tracked_in_kanban", return_value=True), \
             patch("audit_swarm_health.run_cmd") as mock_cmd_skip:
            report_dup = audit_swarm_services(triage=True, kanban_db_path="/tmp/fake.db")
        self.assertEqual(report_dup["status"], "FAIL")
        self.assertTrue(report_dup["overlay_ipam"]["already_tracked"])
        self.assertIsNone(report_dup["overlay_ipam"]["triage_id"])
        mock_cmd_skip.assert_not_called()

    def test_executive_markdown_rendering(self):
        # Case 1: All healthy (with healthy overlay IPAM)
        odin_ok = OdinTelemetry(
            swarm_total_services=75,
            swarm_healthy_services=75,
            swarm_degraded_services=[],
            overlay_ipam={
                "network": "aether-net",
                "status": "PASS",
                "degraded": False,
                "ips_in_use": 133,
                "dynamic_ips_available": 123,
                "phantom_leaked_ips": 0,
                "reason": "",
            },
            total_active_cards=1,
            active_cards_by_status={"ready": 1},
        )
        report_ok = MorningCopilotReport(
            date="2026-11-14",
            eir=EirTelemetry(date="2026-11-14"),
            kratos=KratosTelemetry(date="2026-11-14", iso_week="2026-W46"),
            plutus=PlutusTelemetry(as_of_date="2026-11-14"),
            odin=odin_ok,
        )
        md_ok = generate_executive_markdown(report_ok, section="executive")
        self.assertIn("75/75 services healthy (0 degraded)", md_ok)
        self.assertIn("Overlay IPAM (`aether-net`)**: 🟢 133 in use, 123 free (~0 phantom).", md_ok)

        # Case 2: Degraded service + degraded overlay IPAM
        odin_degraded = OdinTelemetry(
            swarm_total_services=75,
            swarm_healthy_services=74,
            swarm_degraded_services=[{
                "name": "apollo-core_recyclarr",
                "replicas": "0/1",
                "task_state": "Restarting",
                "task_error": "",
                "triage_id": "kb-12345678",
                "already_tracked": False,
            }],
            overlay_ipam={
                "network": "aether-net",
                "status": "FAIL",
                "degraded": True,
                "ips_in_use": 210,
                "dynamic_ips_available": 46,
                "phantom_leaked_ips": 76,
                "reason": "phantom_leaked_ips=76 > 25",
            },
            total_active_cards=1,
            active_cards_by_status={"ready": 1},
        )
        report_deg = MorningCopilotReport(
            date="2026-11-14",
            eir=EirTelemetry(date="2026-11-14"),
            kratos=KratosTelemetry(date="2026-11-14", iso_week="2026-W46"),
            plutus=PlutusTelemetry(as_of_date="2026-11-14"),
            odin=odin_degraded,
        )
        md_deg = generate_executive_markdown(report_deg, section="executive")
        self.assertIn("Degraded Swarm Services (1)", md_deg)
        self.assertIn("apollo-core_recyclarr", md_deg)
        self.assertIn("Triaged to Kanban: `kb-12345678`", md_deg)
        self.assertIn(
            "Overlay IPAM Alert (`aether-net`)**: 210 IPs in use, 46 free (~76 phantom) | phantom_leaked_ips=76 > 25",
            md_deg,
        )


if __name__ == "__main__":
    unittest.main()
