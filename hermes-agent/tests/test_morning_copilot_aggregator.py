#!/usr/bin/env python3
"""test_morning_copilot_aggregator.py - Comprehensive Unit Tests for Morning Copilot Aggregator."""

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from unittest.mock import patch

# Add scripts directory to path
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from morning_copilot_aggregator import (
    ChronosCommitment,
    ChronosFrictionNote,
    ChronosGridException,
    ChronosPassiveSignal,
    ChronosSummary,
    ChronosWorkoutHydration,
    CreditCardDeadline,
    EirTelemetry,
    KratosTelemetry,
    MorningCopilotReport,
    OdinTelemetry,
    PlutusTelemetry,
    append_to_obsidian_journal,
    evaluate_biological_recovery,
    fetch_chronos_summary,
    fetch_eir_telemetry,
    fetch_kratos_telemetry,
    fetch_odin_telemetry,
    fetch_plutus_telemetry,
    generate_executive_markdown,
    main as copilot_main,
    parse_settlement_date_from_text,
    resolve_iso_week,
    run_morning_copilot,
)


class TestMorningCopilotAggregator(unittest.TestCase):
    """Test suite for Morning Executive Copilot Aggregator."""

    def test_evaluate_biological_recovery_green(self):
        """Verify Green tier when biometrics are all optimal."""
        tier, label, rationale = evaluate_biological_recovery(
            sleep_hours=7.8,
            deep_hours=1.6,
            rhr=58,
            hrv=42.0,
        )
        self.assertEqual(tier, "green")
        self.assertIn("Green", label)
        self.assertIn("nominal", rationale)

    def test_evaluate_biological_recovery_amber(self):
        """Verify Amber tier when mild fatigue constraint is detected."""
        tier, label, rationale = evaluate_biological_recovery(
            sleep_hours=6.4,
            deep_hours=1.2,
            rhr=64,
            hrv=32.0,
        )
        self.assertEqual(tier, "amber")
        self.assertIn("Amber", label)
        self.assertIn("5.5h-7.0h", rationale)

    def test_evaluate_biological_recovery_red(self):
        """Verify Red tier on acute deficit signals."""
        tier, label, rationale = evaluate_biological_recovery(
            sleep_hours=4.8,
            deep_hours=0.6,
            rhr=78,
            hrv=14.5,
        )
        self.assertEqual(tier, "red")
        self.assertIn("Red", label)
        self.assertIn("acute", rationale.lower())

    def test_resolve_iso_week(self):
        """Verify ISO week string formatting."""
        iso_w = resolve_iso_week("2026-11-09")
        self.assertEqual(iso_w, "2026-W46")

    def test_parse_settlement_date_from_text(self):
        """Verify regex extraction of payment deadlines from bank transaction text."""
        ref_date = date(2026, 11, 9)
        # Test DD/MM/YY
        d1 = parse_settlement_date_from_text("will be debited from your account on 12/11/26", ref_date)
        self.assertEqual(d1, date(2026, 11, 12))

        # Test DD/MM/YYYY
        d2 = parse_settlement_date_from_text("Payment due 15/11/2026", ref_date)
        self.assertEqual(d2, date(2026, 11, 15))

        # Test DD Mon YYYY
        d3 = parse_settlement_date_from_text("Statement date 18 Oct 2026", ref_date)
        self.assertEqual(d3, date(2026, 10, 18))

        # Test no match
        d4 = parse_settlement_date_from_text("General transaction without date", ref_date)
        self.assertIsNone(d4)

    @patch("morning_copilot_aggregator.fetch_overlay_json")
    def test_fetch_eir_telemetry_mock(self, mock_fetch):
        """Verify parsing of Eir response and fallback handling."""
        mock_fetch.return_value = {
            "date": "2026-11-09",
            "sleep_duration_hours": 6.2,
            "sleep_deep_hours": 1.09,
            "resting_hr_bpm": 70,
            "hrv_ms": 15.94,
            "latest_weight_kg": 86.51,
            "latest_body_fat_pct": 25.51,
            "active_energy_kcal": 41,
            "readiness_notes": ["Daily health rollup for 2026-11-09"],
        }
        telemetry = fetch_eir_telemetry("http://mock-eir", "2026-11-09")
        self.assertEqual(telemetry.date, "2026-11-09")
        self.assertEqual(telemetry.sleep_duration_hours, 6.2)
        self.assertEqual(telemetry.recovery_tier, "red")
        self.assertIn("Red", telemetry.recovery_label)

    @patch("morning_copilot_aggregator.fetch_overlay_json")
    def test_fetch_kratos_telemetry_mock(self, mock_fetch):
        """Verify Kratos plan fetch, athletic events, and volume autoregulation."""
        def side_effect(url):
            if "plans/week" in url:
                return {
                    "iso_week": "2026-W46",
                    "days": [
                        {"day": "Mon", "date": "2026-11-09", "type": "lift", "activity": "Upper Body A"},
                        {"day": "Tue", "date": "2026-11-10", "type": "sport", "activity": "Badminton"},
                        {"day": "Wed", "date": "2026-11-11", "type": "lift", "activity": "Lower Body A"},
                    ],
                }
            if "workout-recommendation" in url:
                return {
                    "routine_name": "Upper Body A",
                    "readiness_adaptation": "Volume scaled to Minimum Effective Volume (MEV).",
                    "volume_landmark": "MEV",
                    "target_rir": "RIR 3-4",
                    "exercises": [{"exercise_name": "Incline Bench Press (Dumbbell)"}],
                    "extra": {"discord_thread_id": "123456789"},
                }
            return None

        mock_fetch.side_effect = side_effect
        telemetry = fetch_kratos_telemetry("http://mock-kratos", "2026-11-09", recovery_tier="red")
        self.assertEqual(telemetry.today_activity, "Upper Body A")
        self.assertTrue(telemetry.badminton_buffer_active)
        self.assertIn("Tuesday", telemetry.badminton_buffer_note)
        self.assertEqual(telemetry.discord_thread_id, "123456789")

    @patch("morning_copilot_aggregator.fetch_overlay_json")
    def test_fetch_plutus_telemetry_mock(self, mock_fetch):
        """Verify Plutus runway metrics, unallocated counts, and credit card settlement deadlines."""
        def side_effect(url):
            if "/runway" in url:
                return {
                    "primary_runway_weeks": 16.71,
                    "primary_runway_years": 0.32,
                    "time_banked_this_week": 1.0,
                    "breakdown": {
                        "locked_weeks": 0.0,
                        "emergency_weeks": 16.57,
                        "weekly_burn_rate": 691.18,
                    },
                }
            if "/transactions?limit=100" in url:
                return [
                    {"id": 1, "merchant": "Store A", "category": "Food", "amount": -20.0},
                    {"id": 2, "merchant": "Unknown", "category": "Unallocated", "amount": -50.0},
                    {"id": 3, "merchant": "Store B", "category": "", "amount": -10.0},
                ]
            if "/accounts?is_active=true" in url:
                return [
                    {"id": 1, "name": "ANZ Freedom", "account_type": "checking"},
                    {"id": 2, "name": "ANZ Low Rate Visa", "account_type": "credit_card"},
                ]
            if "/accounts/2/snapshots" in url:
                return [{"balance_nzd": 2177.64, "snapshot_date": "2026-10-15"}]
            if "/transactions?account_id=2" in url:
                return [{
                    "raw_description": "Closing Balance: $2,177.64 will be debited from your account on 12/11/26",
                }]
            return None

        mock_fetch.side_effect = side_effect
        telemetry = fetch_plutus_telemetry("http://mock-plutus", "2026-11-09")
        self.assertAlmostEqual(telemetry.runway_weeks, 16.71)
        self.assertEqual(telemetry.unallocated_tx_count, 2)
        self.assertEqual(len(telemetry.credit_card_deadlines), 1)
        cd = telemetry.credit_card_deadlines[0]
        self.assertEqual(cd.account_name, "ANZ Low Rate Visa")
        self.assertEqual(cd.due_date, "2026-11-12")
        self.assertEqual(cd.days_remaining, 3)

    @patch(
        "morning_copilot_aggregator.audit_swarm_services",
        return_value={
            "status": "FAIL",
            "total_services": 10,
            "healthy_services": 9,
            "degraded_count": 1,
            "degraded_services": [
                {"name": "apollo-core_recyclarr", "replicas": "0/1", "task_state": "Restarting", "task_error": "boom"}
            ],
            "overlay_ipam": {"network": "aether-net", "status": "PASS", "degraded": False},
        },
    )
    def test_fetch_odin_telemetry_from_swarm_audit(self, mock_audit):
        """Verify Odin Swarm telemetry aggregation without Kanban database."""
        telemetry = fetch_odin_telemetry()
        mock_audit.assert_called_once_with()
        self.assertEqual(telemetry.swarm_total_services, 10)
        self.assertEqual(telemetry.swarm_healthy_services, 9)
        self.assertEqual(len(telemetry.swarm_degraded_services), 1)
        self.assertEqual(telemetry.swarm_degraded_services[0]["name"], "apollo-core_recyclarr")
        self.assertEqual(telemetry.overlay_ipam["status"], "PASS")

    def test_generate_executive_markdown_and_zero_em_dashes(self):
        """Verify markdown report generation and strict zero em/en dash invariant."""
        report = MorningCopilotReport(
            date="2026-11-09",
            eir=EirTelemetry(
                date="2026-11-09",
                sleep_duration_hours=6.2,
                sleep_deep_hours=1.1,
                resting_hr_bpm=70,
                hrv_ms=16.0,
                weight_kg=86.5,
                body_fat_pct=25.5,
                recovery_tier="red",
                recovery_label="🔴 Red (Acute Under-recovery)",
                recovery_rationale="Low recovery telemetry detected.",
            ),
            kratos=KratosTelemetry(
                date="2026-11-09",
                iso_week="2026-W46",
                today_activity="Upper Body A",
                volume_adaptation="Volume scaled to MEV (1 set)",
                badminton_buffer_note="Badminton buffer: Tuesday 7:00pm upcoming.",
                recommended_exercises=[{"exercise_name": "Incline Bench Press (Dumbbell)"}],
            ),
            plutus=PlutusTelemetry(
                as_of_date="2026-11-09",
                runway_weeks=16.7,
                runway_years=0.32,
                emergency_weeks=16.6,
                weekly_burn_rate=691.18,
                time_banked_this_week=1.0,
                unallocated_tx_count=0,
                credit_card_deadlines=[
                    CreditCardDeadline(
                        account_name="ANZ Low Rate Visa",
                        balance_nzd=2177.64,
                        due_date="2026-11-12",
                        days_remaining=3,
                        notice="Direct debit scheduled for 12/11/2026 ($2,177.64 NZD)",
                    )
                ],
            ),
            odin=OdinTelemetry(
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
            ),
        )

        md = generate_executive_markdown(report)
        self.assertIn("# 🌅 Morning Executive Copilot - 2026-11-09", md)
        self.assertIn("## 🏋️ Biological Recovery & Training Directive", md)
        self.assertIn("## 💰 Wealth Runway & Financial Deadlines", md)
        self.assertIn("## 🛡️ Homelab Swarm Health", md)
        self.assertIn("75/75 services healthy (0 degraded)", md)
        self.assertNotIn("Kanban", md)
        self.assertIn("## 🔍 Operational Review & Run Frictions", md)
        self.assertIn("0 frictions or inefficiencies encountered; run was clean and optimal.", md)

        # STRICT INVARIANT: Zero em dash (U+2014) and zero en dash (U+2013)
        self.assertNotIn("\u2014", md, "Em dash U+2014 found in generated markdown")
        self.assertNotIn("\u2013", md, "En dash U+2013 found in generated markdown")

    def test_append_to_obsidian_journal(self):
        """Verify writing and appending to Obsidian journal structure."""
        with tempfile.TemporaryDirectory() as temp_vault:
            test_date = "2026-11-09"
            test_content = "## 🌅 Test Copilot Content"
            success = append_to_obsidian_journal(temp_vault, test_date, test_content)
            self.assertTrue(success)

            expected_path = os.path.join(temp_vault, "second-brain", "Journal", "2026", "11", "2026-11-09.md")
            self.assertTrue(os.path.exists(expected_path))
            with open(expected_path, "r", encoding="utf-8") as f:
                saved = f.read()
            self.assertIn(test_content, saved)

    @patch("morning_copilot_aggregator.fetch_overlay_json")
    def test_fetch_chronos_summary_populated(self, mock_fetch):
        """Verify Chronos daily summary parsing and executive markdown rendering."""
        mock_fetch.return_value = {
            "date": "2026-11-26",
            "yesterday": "2026-11-25",
            "commitments": [
                {
                    "id": 10,
                    "title": "Dentist Checkup",
                    "target_date": "2026-11-26",
                    "start_minute": 540,
                    "end_minute": 600,
                    "all_day": False,
                    "source": "google",
                    "tentative": True,
                    "has_conflict": False,
                    "notes": None,
                },
                {
                    "id": 11,
                    "title": "Flight NZ502 to Wellington",
                    "target_date": "2026-11-26",
                    "start_minute": None,
                    "end_minute": None,
                    "all_day": True,
                    "source": "manual",
                    "tentative": False,
                    "has_conflict": True,
                    "notes": "Terminal check-in",
                },
            ],
            "tentative_count": 1,
            "conflict_count": 1,
            "grid_exceptions": [
                {
                    "id": 5,
                    "grid_block_id": 2,
                    "target_date": "2026-11-26",
                    "action": "move",
                    "new_start_minute": 840,
                    "new_end_minute": 900,
                    "extension_count": 0,
                    "notes": "Adjusted around appointment",
                }
            ],
            "workout_hydration": {
                "status": "hydrated",
                "planned_routine_name": "Upper Push A",
                "planned_day_of_week": "Thu",
                "completed_sessions": 0,
                "attached_grid_block_id": 3,
                "error": None,
            },
            "yesterday_friction_notes": [
                {
                    "id": 7,
                    "target_date": "2026-11-25",
                    "tag": "energy",
                    "note": "Late afternoon focus dip",
                    "habit_cue_id": 1,
                    "grid_block_id": None,
                }
            ],
            "yesterday_passive_signals": [
                {
                    "cue_id": 1,
                    "cue_title": "Morning Weigh-In",
                    "rule_key": "eir_morning_weigh_in",
                    "observed": True,
                    "fact_label": "82.4 kg logged",
                    "peer_status": "ok",
                },
                {
                    "cue_id": 2,
                    "cue_title": "Fasted Walk",
                    "rule_key": "eir_fasted_walk_40m",
                    "observed": False,
                    "fact_label": "Not detected",
                    "peer_status": "ok",
                },
            ],
            "degraded_peers": [],
        }

        summary = fetch_chronos_summary("http://mock-chronos/", "2026-11-26")
        mock_fetch.assert_called_once_with(
            "http://mock-chronos/integrations/daily-summary?target_date=2026-11-26"
        )
        self.assertTrue(summary.available)
        self.assertEqual(summary.tentative_count, 1)
        self.assertEqual(summary.conflict_count, 1)
        self.assertEqual(len(summary.commitments), 2)
        self.assertEqual(summary.workout_hydration.planned_routine_name, "Upper Push A")

        report = MorningCopilotReport(
            date="2026-11-26",
            eir=EirTelemetry(date="2026-11-26"),
            kratos=KratosTelemetry(date="2026-11-26", iso_week="2026-W48"),
            plutus=PlutusTelemetry(as_of_date="2026-11-26"),
            odin=OdinTelemetry(),
            chronos=summary,
        )
        md = generate_executive_markdown(report, section="all")
        self.assertIn("## 📅 Schedule & Habits (Chronos)", md)
        self.assertIn("09:00-10:00: Dentist Checkup [Tentative]", md)
        self.assertIn("all-day: Flight NZ502 to Wellington [Conflict]", md)
        self.assertIn("1 active (move (14:00-15:00))", md)
        self.assertIn("hydrated | Routine: Upper Push A | Completed: 0", md)
        self.assertIn("Morning Weigh-In (82.4 kg logged)", md)
        self.assertIn("[energy] Late afternoon focus dip", md)
        self.assertNotIn("\u2014", md)
        self.assertNotIn("\u2013", md)

    @patch("morning_copilot_aggregator.fetch_overlay_json")
    def test_fetch_chronos_summary_degrades_and_validates_date(self, mock_fetch):
        """Verify unreachable/malformed Chronos degrades cleanly and invalid date fails before fallback."""
        with self.assertRaises(ValueError):
            fetch_chronos_summary("http://mock-chronos", "2026-11-26'; rm -rf /")
        mock_fetch.assert_not_called()

        for payload in (
            None,
            {},
            {"date": "2026-11-26", "commitments": "not-a-list"},
            {"date": "2026-11-26", "commitments": [123]},
            {"date": "2026-11-26", "commitments": [], "workout_hydration": "bad"},
        ):
            mock_fetch.return_value = payload
            summary = fetch_chronos_summary("http://mock-chronos", "2026-11-26")
            self.assertFalse(summary.available)
            self.assertEqual(summary.date, "2026-11-26")

        report = MorningCopilotReport(
            date="2026-11-26",
            eir=EirTelemetry(date="2026-11-26"),
            kratos=KratosTelemetry(date="2026-11-26", iso_week="2026-W48"),
            plutus=PlutusTelemetry(as_of_date="2026-11-26"),
            odin=OdinTelemetry(),
            chronos=ChronosSummary(date="2026-11-26", available=False),
        )
        md = generate_executive_markdown(report, section="executive")
        self.assertIn("## 📅 Schedule & Habits (Chronos)", md)
        self.assertIn("Summary unavailable", md)

    @patch("morning_copilot_aggregator.fetch_odin_telemetry")
    @patch("morning_copilot_aggregator.fetch_plutus_telemetry")
    @patch("morning_copilot_aggregator.fetch_chronos_summary")
    @patch("morning_copilot_aggregator.fetch_kratos_telemetry")
    @patch("morning_copilot_aggregator.fetch_eir_telemetry")
    def test_chronos_section_routing_and_json_cli(
        self, mock_eir, mock_kratos, mock_chronos, mock_plutus, mock_odin
    ):
        """Verify executive includes Chronos, training skips it, and --json/--chronos-url work."""
        mock_eir.return_value = EirTelemetry(date="2026-11-26")
        mock_kratos.return_value = KratosTelemetry(date="2026-11-26", iso_week="2026-W48")
        mock_plutus.return_value = PlutusTelemetry(as_of_date="2026-11-26")
        mock_odin.return_value = OdinTelemetry()
        mock_chronos.return_value = ChronosSummary(
            date="2026-11-26",
            yesterday="2026-11-25",
            available=True,
            commitments=(ChronosCommitment(title="Dentist Checkup", start_minute=540, end_minute=600, tentative=True),),
            tentative_count=1,
            grid_exceptions=(ChronosGridException(action="skip"),),
            workout_hydration=ChronosWorkoutHydration(status="hydrated", planned_routine_name="Upper Push A"),
            yesterday_friction_notes=(ChronosFrictionNote(tag="focus", note="Midday interruption"),),
            yesterday_passive_signals=(
                ChronosPassiveSignal(cue_title="Morning Weigh-In", rule_key="eir_morning_weigh_in", observed=True),
            ),
        )

        exec_report = run_morning_copilot(
            target_date="2026-11-26",
            chronos_url="http://custom-chronos:8000",
            section="executive",
        )
        mock_chronos.assert_called_once_with("http://custom-chronos:8000", "2026-11-26")
        exec_md = generate_executive_markdown(exec_report, section="executive")
        self.assertIn("Schedule & Habits (Chronos)", exec_md)
        self.assertIn("Dentist Checkup [Tentative]", exec_md)

        mock_chronos.reset_mock()
        train_report = run_morning_copilot(target_date="2026-11-26", section="training")
        mock_chronos.assert_not_called()
        train_md = generate_executive_markdown(train_report, section="training")
        self.assertNotIn("Schedule & Habits (Chronos)", train_md)

        buf = io.StringIO()
        argv = [
            "morning_copilot_aggregator.py",
            "--date",
            "2026-11-26",
            "--chronos-url",
            "http://cli-chronos:8000",
            "--json",
        ]
        with patch.object(sys, "argv", argv), redirect_stdout(buf):
            rc = copilot_main()
        self.assertEqual(rc, 0)
        mock_chronos.assert_called_with("http://cli-chronos:8000", "2026-11-26")
        parsed = json.loads(buf.getvalue())
        self.assertIn("chronos", parsed)
        self.assertTrue(parsed["chronos"]["available"])
        self.assertEqual(parsed["chronos"]["commitments"][0]["title"], "Dentist Checkup")
        self.assertEqual(parsed["chronos"]["workout_hydration"]["planned_routine_name"], "Upper Push A")
        self.assertNotIn("\u2014", exec_md)
        self.assertNotIn("\u2013", exec_md)


if __name__ == "__main__":
    unittest.main()
