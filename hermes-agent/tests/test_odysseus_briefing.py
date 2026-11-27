#!/usr/bin/env python3
"""Unit tests for Odysseus MCP travel briefing client and morning copilot hook."""

from __future__ import annotations

from datetime import date
import io
import json
import os
import sys
import unittest
from unittest.mock import patch
import urllib.error

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
HERMES_AGENT_DIR = os.path.dirname(TESTS_DIR)
SCRIPTS_DIR = os.path.join(HERMES_AGENT_DIR, "scripts")
for path_entry in (HERMES_AGENT_DIR, SCRIPTS_DIR):
    if path_entry not in sys.path:
        sys.path.insert(0, path_entry)

from odin_runtime.odysseus_briefing import (  # noqa: E402
    OdysseusTravelDigest,
    fetch_odysseus_travel_briefings,
    format_odysseus_briefing_section,
    select_briefing_trips,
)
import morning_copilot_aggregator as mca  # noqa: E402


class _FakeHttpResponse:
    def __init__(self, payload: dict) -> None:
        self._body = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeHttpResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None


def _mcp_tool_envelope(tool_result: object, *, is_error: bool = False) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": json.dumps(tool_result)}],
            "isError": is_error,
        },
    }


class TestOdysseusBriefing(unittest.TestCase):
    """Tests for select_briefing_trips, MCP transport, formatting, and copilot routing."""

    def test_select_briefing_trips_window_boundaries(self) -> None:
        today = date(2026, 11, 27)
        trips = [
            {"id": "t-active", "status": "active", "start_date": "2026-11-25", "title": "Active Trip"},
            {"id": "t-day0", "status": "booked", "start_date": "2026-11-27", "title": "Day 0 Trip"},
            {"id": "t-day7", "status": "booked", "start_date": "2026-12-04", "title": "Day 7 Trip"},
            {"id": "t-day8", "status": "booked", "start_date": "2026-12-05", "title": "Day 8 Trip"},
            {"id": "t-past", "status": "booked", "start_date": "2026-11-26", "title": "Past Booked"},
            {"id": "t-plan", "status": "planning", "start_date": "2026-11-28", "title": "Planning"},
            {"id": "t-done", "status": "completed", "start_date": "2026-11-27", "title": "Completed"},
            {"id": "t-arch", "status": "archived", "start_date": "2026-11-27", "title": "Archived"},
        ]

        selected = select_briefing_trips(trips, today, window_days=7)
        self.assertEqual([t["id"] for t in selected], ["t-active", "t-day0", "t-day7"])

    @patch("urllib.request.urlopen")
    def test_fetch_unconfigured_token_skips_network(self, mock_urlopen) -> None:
        digest = fetch_odysseus_travel_briefings(
            mcp_url="http://odysseus-api:8000/mcp",
            token="   ",
            today=date(2026, 11, 27),
        )
        self.assertEqual(digest.status, "unconfigured")
        self.assertEqual(digest.trips, ())
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_fetch_zero_matching_trips_skips_day_brief(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _FakeHttpResponse(
            _mcp_tool_envelope(
                [
                    {"id": "t-plan", "status": "planning", "start_date": "2026-11-28"},
                    {"id": "t-far", "status": "booked", "start_date": "2026-12-20"},
                ]
            )
        )
        digest = fetch_odysseus_travel_briefings(
            mcp_url="http://odysseus-api:8000/mcp",
            token="tok-123",
            today=date(2026, 11, 27),
        )
        self.assertEqual(digest.status, "none")
        self.assertEqual(digest.trips, ())
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("urllib.request.urlopen")
    def test_fetch_active_and_upcoming_booked_trips_and_wire_headers(self, mock_urlopen) -> None:
        recorded: list[tuple[dict, dict]] = []

        def fake_urlopen(req, timeout=4.0):
            body = json.loads(req.data.decode("utf-8"))
            headers = {k.lower(): v for k, v in req.header_items()}
            recorded.append((headers, body))
            tool_name = body["params"]["name"]
            args = body["params"]["arguments"]
            if tool_name == "odysseus_list_trips":
                return _FakeHttpResponse(
                    _mcp_tool_envelope(
                        [
                            {"id": "trip-act", "slug": "wlg", "title": "Wellington", "status": "active", "start_date": "2026-11-26"},
                            {"id": "trip-bkd", "slug": "tyo", "title": "Tokyo", "status": "booked", "start_date": "2026-12-01"},
                        ]
                    )
                )
            return _FakeHttpResponse(
                _mcp_tool_envelope(
                    {
                        "trip_title": "Wellington" if args["trip_id"] == "trip-act" else "Tokyo",
                        "target_date": args["target_date"],
                        "days_until_departure": -1 if args["trip_id"] == "trip-act" else 4,
                        "active_legs": [{"title": "Central Leg"}],
                        "departing_segments": [{"title": "NZ401", "origin": "AKL", "destination": "WLG", "departure_time": "08:00"}],
                        "arriving_segments": [],
                        "itinerary_items": [{"time": "10:00", "title": "Coffee with Alex Tan"}],
                    }
                )
            )

        mock_urlopen.side_effect = fake_urlopen
        digest = fetch_odysseus_travel_briefings(
            mcp_url="http://odysseus-api:8000/mcp",
            token="secret-mcp-token",
            today=date(2026, 11, 27),
        )
        self.assertEqual(digest.status, "ok")
        self.assertEqual(len(digest.trips), 2)
        self.assertEqual(len(recorded), 3)

        first_headers, first_body = recorded[0]
        self.assertEqual(first_headers.get("authorization"), "Bearer secret-mcp-token")
        self.assertEqual(first_headers.get("accept"), "application/json, text/event-stream")
        self.assertEqual(first_headers.get("user-agent"), "Yggdrasil-Odin/1.0")
        self.assertEqual(first_body["params"]["name"], "odysseus_list_trips")

        self.assertEqual(recorded[1][1]["params"]["arguments"], {"trip_id": "trip-act", "target_date": "2026-11-27"})
        self.assertEqual(recorded[2][1]["params"]["arguments"], {"trip_id": "trip-bkd", "target_date": "2026-12-01"})

    @patch("urllib.request.urlopen")
    def test_fetch_returns_explicit_error_on_http_401_timeout_and_tool_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "http://odysseus-api:8000/mcp", 401, "Unauthorized", {}, io.BytesIO(b"invalid token")
        )
        d401 = fetch_odysseus_travel_briefings(
            mcp_url="http://odysseus-api:8000/mcp", token="bad", today=date(2026, 11, 27)
        )
        self.assertEqual(d401.status, "error")
        self.assertIn("401", d401.error or "")

        mock_urlopen.side_effect = TimeoutError("timed out")
        d_to = fetch_odysseus_travel_briefings(
            mcp_url="http://odysseus-api:8000/mcp", token="tok", today=date(2026, 11, 27)
        )
        self.assertEqual(d_to.status, "error")
        self.assertIn("timed out", d_to.error or "")

        mock_urlopen.side_effect = None
        mock_urlopen.return_value = _FakeHttpResponse(_mcp_tool_envelope({"error": "bad args"}, is_error=True))
        d_tool = fetch_odysseus_travel_briefings(
            mcp_url="http://odysseus-api:8000/mcp", token="tok", today=date(2026, 11, 27)
        )
        self.assertEqual(d_tool.status, "error")
        self.assertIn("error", (d_tool.error or "").lower())

    def test_format_odysseus_briefing_section_and_zero_dashes(self) -> None:
        self.assertEqual(format_odysseus_briefing_section(OdysseusTravelDigest(status="none")), "")
        self.assertEqual(format_odysseus_briefing_section(OdysseusTravelDigest(status="unconfigured")), "")

        err_md = format_odysseus_briefing_section(
            OdysseusTravelDigest(status="error", error="HTTP 503 \u2014 upstream down")
        )
        self.assertIn("## Travel Brief (Odysseus)", err_md)
        self.assertIn("Warning", err_md)
        self.assertNotIn("\u2014", err_md)
        self.assertNotIn("\u2013", err_md)

        ok_md = format_odysseus_briefing_section(
            OdysseusTravelDigest(
                status="ok",
                trips=(
                    {
                        "trip_title": "Tokyo \u2014 Autumn",
                        "target_date": "2026-12-01",
                        "days_until_departure": 4,
                        "active_legs": [{"title": "Shinjuku \u2013 Stay"}],
                        "departing_segments": [{"title": "NZ99", "origin": "AKL", "destination": "NRT", "departure_time": "09:50"}],
                        "itinerary_items": [{"time": "19:00", "title": "Hotel Check-in"}],
                    },
                ),
            )
        )
        self.assertIn("## Travel Brief (Odysseus)", ok_md)
        self.assertIn("**Tokyo - Autumn** (In 4d, 2026-12-01)", ok_md)
        self.assertIn("AKL -> NRT", ok_md)
        self.assertIn("Hotel Check-in", ok_md)
        self.assertNotIn("\u2014", ok_md)
        self.assertNotIn("\u2013", ok_md)

    @patch("morning_copilot_aggregator.fetch_odysseus_travel_briefings")
    @patch("morning_copilot_aggregator.fetch_odin_telemetry")
    @patch("morning_copilot_aggregator.fetch_plutus_telemetry")
    @patch("morning_copilot_aggregator.fetch_chronos_summary")
    @patch("morning_copilot_aggregator.fetch_kratos_telemetry")
    @patch("morning_copilot_aggregator.fetch_eir_telemetry")
    def test_morning_copilot_skips_training_and_includes_executive(
        self, mock_eir, mock_kratos, mock_chronos, mock_plutus, mock_odin, mock_odysseus
    ) -> None:
        mock_eir.return_value = mca.EirTelemetry(date="2026-11-27")
        mock_kratos.return_value = mca.KratosTelemetry(date="2026-11-27", iso_week="2026-W48")
        mock_chronos.return_value = mca.ChronosSummary(date="2026-11-27", available=True)
        mock_plutus.return_value = mca.PlutusTelemetry(as_of_date="2026-11-27")
        mock_odin.return_value = mca.OdinTelemetry()
        mock_odysseus.return_value = OdysseusTravelDigest(
            status="ok",
            trips=({"trip_title": "Wellington", "target_date": "2026-11-27", "days_until_departure": 0},),
        )

        train_report = mca.run_morning_copilot(target_date="2026-11-27", section="training")
        mock_odysseus.assert_not_called()
        self.assertNotIn("Travel Brief (Odysseus)", mca.generate_executive_markdown(train_report, section="training"))

        with patch.dict(os.environ, {"ODYSSEUS_MCP_URL": "http://odysseus-api:8000/mcp", "ODYSSEUS_MCP_TOKEN": "tok"}):
            exec_report = mca.run_morning_copilot(target_date="2026-11-27", section="executive")
        mock_odysseus.assert_called_once_with(
            mcp_url="http://odysseus-api:8000/mcp",
            token="tok",
            today=date(2026, 11, 27),
        )
        exec_md = mca.generate_executive_markdown(exec_report, section="executive")
        self.assertIn("## Travel Brief (Odysseus)", exec_md)
        self.assertIn("**Wellington** (Departing today, 2026-11-27)", exec_md)
        self.assertEqual(exec_report.to_dict()["odysseus"]["status"], "ok")


if __name__ == "__main__":
    unittest.main()
