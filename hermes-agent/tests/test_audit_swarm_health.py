#!/usr/bin/env python3
"""test_audit_swarm_health.py - Unit tests for Swarm Replica & Health Audit Engine."""

import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from audit_swarm_health import (
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

    @patch("audit_swarm_health.dispatch_to_kanban_triage", return_value="kb-autofix1")
    @patch("audit_swarm_health.is_already_tracked_in_kanban", return_value=False)
    @patch("audit_swarm_health.query_service_task_state")
    @patch("audit_swarm_health.query_swarm_services")
    def test_audit_swarm_services_with_triage(
        self, mock_query, mock_task_state, mock_tracked, mock_dispatch
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

        report = audit_swarm_services(triage=True, kanban_db_path="/tmp/fake.db")
        self.assertEqual(report["status"], "FAIL")
        self.assertEqual(report["total_services"], 2)
        self.assertEqual(report["healthy_services"], 1)
        self.assertEqual(report["degraded_count"], 1)

        degraded = report["degraded_services"][0]
        self.assertEqual(degraded["name"], "svc2")
        self.assertEqual(degraded["triage_id"], "kb-autofix1")
        mock_dispatch.assert_called_once()

    def test_executive_markdown_rendering(self):
        # Case 1: All healthy
        odin_ok = OdinTelemetry(
            swarm_total_services=75,
            swarm_healthy_services=75,
            swarm_degraded_services=[],
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

        # Case 2: Degraded service
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


if __name__ == "__main__":
    unittest.main()
