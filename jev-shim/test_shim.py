#!/usr/bin/env python3
"""Unit tests for TypeSafe Jev OpenAI translation shim (shim.py)."""

from __future__ import annotations

import http.client
import http.server
import io
import json
import os
import sys
import threading
import unittest
import urllib.error
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
        self.assertIn("gemini/gemini-3.5-flash-lite", content)
        self.assertIn("gemini/gemini-3.8-flash", content)
        self.assertIn("os.environ/GEMINI_API_KEY", content)
        self.assertIn("- model_name: gemini-2.5-flash-lite", content)
        self.assertIn("- model_name: agent-loop-deepseek", content)
        self.assertIn("provider_budget_config:", content)
        self.assertIn("budget_limit: 35.0", content)
        self.assertIn("fallbacks:", content)
        self.assertNotIn("\u2014", content)
        self.assertNotIn("\u2013", content)

    def test_kanban_gateway_batched_triage_and_debrief_gates(self) -> None:
        scripts_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "hermes-agent",
            "scripts",
        )
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        from kanban_gateway import jev as kg_jev

        with patch.object(
            kg_jev,
            "call_jev_raw",
            return_value=(json.dumps({"actionable": "true", "tier": "frontier_deep", "role": "sindri"}), 0.12),
        ) as mock_triage:
            res = kg_jev.evaluate_triage_and_complexity(
                "Update Forge/odin docker-compose.yml and open PR",
                title="Update Odin Compose",
            )
            self.assertEqual(mock_triage.call_count, 1)
            self.assertTrue(res["actionable"])
            self.assertEqual(res["tier"], "frontier_deep")
            self.assertEqual(res["recommended_assignee"], "sindri")
            self.assertEqual(res["recommended_model"], "gemini-4-argon-high")

        with patch.object(
            kg_jev,
            "call_jev_raw",
            return_value=(
                json.dumps(
                    {
                        "accepted": "true",
                        "auto_merge_eligible": "false",
                        "friction_action": "propose_review",
                        "friction_assignee": "vor",
                    }
                ),
                0.18,
            ),
        ) as mock_debrief:
            gates = kg_jev.evaluate_debrief_gates_batch(
                task_title="Update Odin Compose",
                task_body="Update compose and verify",
                debrief_summary="Completed in PR #128",
                pr_url="https://github.com/yggdrasil-lab/odin/pull/128",
                friction_text="Skill script had outdated CLI flag",
            )
            self.assertEqual(mock_debrief.call_count, 1)
            self.assertTrue(gates["acceptance_gate"]["accepted"])
            self.assertFalse(gates["auto_merge_gate"]["auto_merge_eligible"])
            self.assertEqual(gates["friction_gate"]["action"], "propose_review")
            self.assertEqual(gates["friction_assignee"], "vor")


class TestJevSchemaValidation(unittest.TestCase):
    """Tests preflight schema validation and upstream TypeSafe error classification."""

    def test_validate_questions_schema_accepts_all_valid_primitives(self) -> None:
        questions = {
            "category": {
                "type": "choice",
                "criteria": {"FOOD": "Groceries and dining", "RENT": "Housing"},
            },
            "category_list": {
                "type": "choice",
                "criteria": ["A", "B"],
            },
            "is_subscription": {
                "type": "noul",
                "instructions": "True if recurring subscription charge.",
            },
            "urgency_score": {
                "type": "score",
                "criteria": ["Low", "Medium", "High"],
            },
            "receipt_total_box": {
                "type": "box",
                "instructions": "Locate the total amount on the receipt.",
            },
        }
        shim.validate_questions_schema(questions)

    def test_validate_questions_schema_accepts_max_choice_options(self) -> None:
        questions = {
            "many_choices": {
                "type": "choice",
                "criteria": {f"opt_{i}": f"Option {i}" for i in range(shim.MAX_CHOICE_OPTIONS)},
            }
        }
        shim.validate_questions_schema(questions)

    def test_validate_questions_schema_rejects_unsupported_type_naming_key_and_type(self) -> None:
        questions = {
            "merchant_tags": {
                "type": "list",
                "instructions": "Return a list of tags.",
            }
        }
        with self.assertRaises(shim.SchemaValidationError) as ctx:
            shim.validate_questions_schema(questions)
        msg = str(ctx.exception)
        self.assertIn("merchant_tags", msg)
        self.assertIn("list", msg)

    def test_validate_questions_schema_rejects_empty_or_missing_choice_criteria(self) -> None:
        for bad_criteria in ({}, [], None):
            spec = {"type": "choice"}
            if bad_criteria is not None:
                spec["criteria"] = bad_criteria
            with self.assertRaises(shim.SchemaValidationError) as ctx:
                shim.validate_questions_schema({"bucket": spec})
            self.assertIn("bucket", str(ctx.exception))

    def test_validate_questions_schema_rejects_choice_criteria_over_255_options(self) -> None:
        questions = {
            "too_many": {
                "type": "choice",
                "criteria": {f"opt_{i}": f"Option {i}" for i in range(shim.MAX_CHOICE_OPTIONS + 1)},
            }
        }
        with self.assertRaises(shim.SchemaValidationError) as ctx:
            shim.validate_questions_schema(questions)
        msg = str(ctx.exception)
        self.assertIn("too_many", msg)
        self.assertIn("255", msg)

    def test_validate_questions_schema_rejects_empty_or_non_dict_inputs(self) -> None:
        for bad in ({}, None, [], {"q1": "not-a-dict"}):
            with self.assertRaises(shim.SchemaValidationError):
                shim.validate_questions_schema(bad)  # type: ignore[arg-type]

    def test_call_typesafe_systemone_raises_typesafe_api_error_on_http_error(self) -> None:
        http_err = urllib.error.HTTPError(
            url="https://api.typesafe.ai/v1/systemone",
            code=400,
            msg="Bad Request",
            hdrs=None,  # type: ignore[arg-type]
            fp=io.BytesIO(b'{"detail":{"message":"Invalid request."}}'),
        )
        with patch.object(shim.urllib.request, "urlopen", side_effect=http_err):
            with self.assertRaises(shim.TypeSafeAPIError) as ctx:
                shim.call_typesafe_systemone(
                    "state",
                    {"is_ok": {"type": "noul"}},
                    "ts-key",
                )
        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("Invalid request.", ctx.exception.body)

    def test_call_typesafe_systemone_raises_runtime_error_on_network_failure(self) -> None:
        with patch.object(shim.urllib.request, "urlopen", side_effect=urllib.error.URLError("dns failure")):
            with self.assertRaises(RuntimeError) as ctx:
                shim.call_typesafe_systemone(
                    "state",
                    {"is_ok": {"type": "noul"}},
                    "ts-key",
                )
        self.assertNotIsInstance(ctx.exception, shim.TypeSafeAPIError)


class TestJevOpenAIHandlerValidationAnd4xx(unittest.TestCase):
    """Tests HTTP 400 schema fail-fast and 4xx/502 forwarding on JevOpenAIHandler."""

    def setUp(self) -> None:
        shim.JevOpenAIHandler.api_key = "ts-test-key"
        self.server = http.server.HTTPServer(("127.0.0.1", 0), shim.JevOpenAIHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _post_chat_completion(self, payload: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
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
            data = json.loads(resp.read().decode("utf-8"))
            return resp.status, data
        finally:
            conn.close()

    def test_invalid_schema_returns_400_without_calling_upstream(self) -> None:
        schema = {
            "questions": {
                "merchant_tags": {
                    "type": "list",
                    "instructions": "Extract tags",
                }
            }
        }
        payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "system", "content": f"```json\n{json.dumps(schema)}\n```"},
                {"role": "user", "content": "Coffee shop $5.00"},
            ],
        }
        with patch.object(shim, "call_typesafe_systemone") as mock_call:
            status, data = self._post_chat_completion(payload)
        self.assertEqual(status, 400)
        self.assertIn("merchant_tags", data["error"])
        self.assertIn("list", data["error"])
        mock_call.assert_not_called()

    def test_upstream_400_and_422_forward_4xx_status(self) -> None:
        valid_payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "user", "content": "Is this actionable?"},
            ],
        }
        for upstream_code in (400, 422):
            with self.subTest(upstream_code=upstream_code):
                err = shim.TypeSafeAPIError(upstream_code, '{"detail":"bad client payload"}')
                with patch.object(shim, "call_typesafe_systemone", side_effect=err):
                    status, data = self._post_chat_completion(valid_payload)
                self.assertEqual(status, upstream_code)
                self.assertIn(f"HTTP {upstream_code}", data["error"])

    def test_upstream_500_and_runtime_error_return_502(self) -> None:
        valid_payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "user", "content": "Is this actionable?"},
            ],
        }
        for exc in (
            shim.TypeSafeAPIError(500, '{"detail":"upstream internal error"}'),
            RuntimeError("TypeSafe connection error: timed out"),
        ):
            with self.subTest(exc=type(exc).__name__):
                with patch.object(shim, "call_typesafe_systemone", side_effect=exc):
                    status, data = self._post_chat_completion(valid_payload)
                self.assertEqual(status, 502)
                self.assertIn("error", data)

    def test_valid_request_returns_200_openai_completion(self) -> None:
        schema = {
            "questions": {
                "category": {
                    "type": "choice",
                    "criteria": {"DINING": "Restaurants and cafes", "GROCERIES": "Supermarkets"},
                }
            }
        }
        payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "system", "content": f"```json\n{json.dumps(schema)}\n```"},
                {"role": "user", "content": "Flat white at coffee roaster"},
            ],
        }
        mock_response = {"answers": {"category": {"choice": "DINING", "confidence": 0.96}}}
        with patch.object(shim, "call_typesafe_systemone", return_value=mock_response) as mock_call:
            status, data = self._post_chat_completion(payload)
        self.assertEqual(status, 200)
        self.assertEqual(mock_call.call_count, 1)
        self.assertEqual(data["object"], "chat.completion")
        self.assertEqual(data["choices"][0]["message"]["content"], "DINING")


if __name__ == "__main__":
    unittest.main()
