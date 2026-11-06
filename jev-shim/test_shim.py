#!/usr/bin/env python3
"""Unit tests for TypeSafe Jev OpenAI translation shim (shim.py)."""

from __future__ import annotations

import http.client
import http.server
import json
import os
import sys
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import shim  # noqa: E402


class TestJevShim(unittest.TestCase):
    """Tests single-question backwards compatibility and multi-question batching."""

    def test_single_question_choice_returns_plain_string(self) -> None:
        answers = {
            "recovery_tier": {
                "choice": "MAV_OVERLOAD",
                "confidence": 0.94,
            }
        }
        res = shim.format_openai_completion("jev-decision", answers, {"answers": answers})
        content = res["choices"][0]["message"]["content"]
        self.assertEqual(content, "MAV_OVERLOAD")

    def test_single_question_score_returns_plain_string(self) -> None:
        answers = {
            "urgency_score": {
                "score": 2.0,
                "confidence": 0.88,
            }
        }
        res = shim.format_openai_completion("agent-monitor", answers, {"answers": answers})
        content = res["choices"][0]["message"]["content"]
        self.assertEqual(content, "7")

    def test_single_question_noul_returns_plain_string(self) -> None:
        answers = {
            "is_actionable": {
                "noul": 0.82,
            }
        }
        res = shim.format_openai_completion("jev-decision", answers, {"answers": answers})
        content = res["choices"][0]["message"]["content"]
        self.assertEqual(content, "true")

    def test_single_question_with_json_response_format_returns_json_object(self) -> None:
        answers = {
            "recovery_tier": {
                "choice": "MAINTENANCE",
                "confidence": 0.91,
            }
        }
        res = shim.format_openai_completion(
            "jev-decision",
            answers,
            {"answers": answers},
            response_format={"type": "json_object"},
        )
        content = res["choices"][0]["message"]["content"]
        self.assertEqual(json.loads(content), {"recovery_tier": "MAINTENANCE"})

    def test_multi_question_choice_batch_returns_json_dict(self) -> None:
        answers = {
            "recovery_tier": {"choice": "MAV_OVERLOAD", "confidence": 0.95},
            "joint_focus": {"choice": "SHOULDER_SPARING", "confidence": 0.89},
            "time_budget": {"choice": "TIME_CRUNCH_30M", "confidence": 0.92},
        }
        res = shim.format_openai_completion("jev-decision", answers, {"answers": answers})
        content = res["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        self.assertEqual(
            parsed,
            {
                "recovery_tier": "MAV_OVERLOAD",
                "joint_focus": "SHOULDER_SPARING",
                "time_budget": "TIME_CRUNCH_30M",
            },
        )

    def test_multi_question_mixed_primitives_batch_returns_json_dict(self) -> None:
        answers = {
            "category": {"choice": "GROCERIES", "confidence": 0.97},
            "urgency_score": {"score": 3.0, "confidence": 0.90},
            "is_recurring": {"noul": 0.15},
        }
        res = shim.format_openai_completion("jev-decision", answers, {"answers": answers})
        content = res["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        self.assertEqual(
            parsed,
            {
                "category": "GROCERIES",
                "urgency_score": "10",
                "is_recurring": "false",
            },
        )

    def test_infer_multi_question_from_fenced_json(self) -> None:
        schema = {
            "questions": {
                "recovery_tier": {
                    "type": "choice",
                    "criteria": {
                        "MAV_OVERLOAD": "High readiness",
                        "MAINTENANCE": "Normal baseline",
                    },
                },
                "joint_focus": {
                    "type": "choice",
                    "criteria": {
                        "SHOULDER_SPARING": "Shoulder pain",
                        "NOMINAL": "No pain",
                    },
                },
                "time_budget": {
                    "type": "choice",
                    "criteria": {
                        "TIME_CRUNCH_30M": "Quick 30m session",
                        "TIME_STANDARD_45M": "Standard 45m session",
                    },
                },
            }
        }
        messages = [
            {"role": "system", "content": f"Evaluate readiness and constraints:\n```json\n{json.dumps(schema)}\n```"},
            {"role": "user", "content": "Sleep: 8.1h, HRV: 52ms. Slight right shoulder pinch, only have 30 mins."},
        ]
        state, questions = shim.infer_question_from_messages(messages)
        self.assertEqual(set(questions.keys()), {"recovery_tier", "joint_focus", "time_budget"})
        self.assertIn("Sleep: 8.1h", state)
        self.assertNotIn("```json", state)

    def test_infer_multi_question_from_raw_json_system_prompt(self) -> None:
        schema = {
            "recovery_tier": {"type": "choice", "criteria": {"MAV_OVERLOAD": "High", "MAINTENANCE": "Normal"}},
            "joint_focus": {"type": "choice", "criteria": {"NOMINAL": "Fine"}},
        }
        messages = [
            {"role": "system", "content": json.dumps(schema)},
            {"role": "user", "content": "Sleep: 7.8h, feeling great."},
        ]
        state, questions = shim.infer_question_from_messages(messages)
        self.assertEqual(set(questions.keys()), {"recovery_tier", "joint_focus"})
        self.assertIn("Sleep: 7.8h", state)

    def test_http_handler_multi_question_single_round_trip(self) -> None:
        shim.JevOpenAIHandler.api_key = "ts-test-key"
        server = http.server.HTTPServer(("127.0.0.1", 0), shim.JevOpenAIHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        mock_typesafe_response = {
            "answers": {
                "recovery_tier": {"choice": "MAV_OVERLOAD", "confidence": 0.93},
                "joint_focus": {"choice": "NOMINAL", "confidence": 0.96},
                "time_budget": {"choice": "TIME_STANDARD_45M", "confidence": 0.91},
            }
        }

        try:
            with patch.object(shim, "call_typesafe_systemone", return_value=mock_typesafe_response) as mock_call:
                schema = {
                    "questions": {
                        "recovery_tier": {"type": "choice", "criteria": {"MAV_OVERLOAD": "Optimal"}},
                        "joint_focus": {"type": "choice", "criteria": {"NOMINAL": "No pain"}},
                        "time_budget": {"type": "choice", "criteria": {"TIME_STANDARD_45M": "45 mins"}},
                    }
                }
                payload = {
                    "model": "jev-decision",
                    "messages": [
                        {"role": "system", "content": f"```json\n{json.dumps(schema)}\n```"},
                        {"role": "user", "content": "Sleep 8h, HRV 50ms, 45m session"},
                    ],
                    "response_format": {"type": "json_object"},
                }
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request(
                    "POST",
                    "/v1/chat/completions",
                    body=json.dumps(payload).encode("utf-8"),
                    headers={
                        "Content-Type": "application/json",
                        "Authorization": "Bearer dummy",
                    },
                )
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                conn.close()

                self.assertEqual(mock_call.call_count, 1)
                verdicts = json.loads(data["choices"][0]["message"]["content"])
                self.assertEqual(
                    verdicts,
                    {
                        "recovery_tier": "MAV_OVERLOAD",
                        "joint_focus": "NOMINAL",
                        "time_budget": "TIME_STANDARD_45M",
                    },
                )
        finally:
            server.shutdown()
            server.server_close()

    def test_muninn_config_template_includes_app_synthesize_and_gemini_flash_lite(self) -> None:
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config",
            "muninn",
            "config.yaml.template",
        )
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("- model_name: app-synthesize", content)
        self.assertIn("gemini/gemini-2.5-flash-lite", content)
        self.assertIn("os.environ/GEMINI_API_KEY", content)
        self.assertIn("- model_name: gemini-2.5-flash-lite", content)
        self.assertIn("fallbacks:", content)
        self.assertNotIn("\u2014", content)
        self.assertNotIn("\u2013", content)


if __name__ == "__main__":
    unittest.main()
