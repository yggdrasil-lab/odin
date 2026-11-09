#!/usr/bin/env python3
"""test_morning_copilot_aggregator.py - Comprehensive Unit Tests for Morning Copilot Aggregator."""

import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

# Add scripts directory to path
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.join(os.path.dirname(TESTS_DIR), "scripts")
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

from morning_copilot_aggregator import (
    CreditCardDeadline,
    EirTelemetry,
    KratosTelemetry,
    MorningCopilotReport,
    OdinTelemetry,
    PlutusTelemetry,
    append_to_obsidian_journal,
    evaluate_biological_recovery,
    fetch_eir_telemetry,
    fetch_kratos_telemetry,
    fetch_odin_telemetry,
    fetch_plutus_telemetry,
    generate_executive_markdown,
    parse_settlement_date_from_text,
    resolve_iso_week,
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

    def test_fetch_odin_telemetry_with_sqlite(self):
        """Verify Odin Kanban database inspection for blocked cards and Iris alerts."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE tasks ("
                "id TEXT PRIMARY KEY, title TEXT, assignee TEXT, status TEXT, "
                "created_by TEXT, created_at INTEGER)"
            )
            now_ts = int(datetime.now(timezone.utc).timestamp())
            conn.execute("INSERT INTO tasks VALUES ('kb-01', 'Blocked task', 'sindri', 'blocked', 'user', ?)", (now_ts - 3600,))
            conn.execute("INSERT INTO tasks VALUES ('kb-02', 'Iris alert task', 'huginn', 'done', 'jev-gateway', ?)", (now_ts - 1800,))
            conn.execute("INSERT INTO tasks VALUES ('kb-03', 'Active task', 'vor', 'in_progress', 'user', ?)", (now_ts - 600,))
            conn.commit()
            conn.close()

            telemetry = fetch_odin_telemetry(db_path, lookback_hours=24)
            self.assertEqual(len(telemetry.blocked_cards), 1)
            self.assertEqual(telemetry.blocked_cards[0]["id"], "kb-01")
            self.assertEqual(len(telemetry.iris_alerts_24h), 1)
            self.assertEqual(telemetry.iris_alerts_24h[0]["id"], "kb-02")
            self.assertEqual(telemetry.total_active_cards, 2)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

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
                blocked_cards=[{"id": "kb-7837e28f", "title": "Decommission legacy backup", "assignee": "swarm-worker"}],
                iris_alerts_24h=[],
                active_cards_by_status={"blocked": 1},
                total_active_cards=1,
            ),
        )

        md = generate_executive_markdown(report)
        self.assertIn("# 🌅 Morning Executive Copilot - 2026-11-09", md)
        self.assertIn("## 🏋️ Biological Recovery & Training Directive", md)
        self.assertIn("## 💰 Wealth Runway & Financial Deadlines", md)
        self.assertIn("## 🛡️ Homelab Swarm & Kanban Queue", md)
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


if __name__ == "__main__":
    unittest.main()
