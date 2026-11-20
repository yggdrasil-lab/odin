#!/usr/bin/env python3
"""Unit tests for opt-in include_meta calibrated confidence output in shim.py."""

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


class TestIncludeMetaExtraction(unittest.TestCase):
    """Tests _extract_include_meta and infer_question_from_messages parity."""

    def test_top_level_body_flag_enables_include_meta(self) -> None:
        messages = [{"role": "user", "content": "Is this actionable?"}]
        self.assertTrue(shim._extract_include_meta(messages, {"include_meta": True}))

    def test_fenced_json_block_enables_include_meta(self) -> None:
        schema = {
            "include_meta": True,
            "questions": {
                "category": {"type": "choice", "criteria": {"DINING": "Restaurants", "GROCERIES": "Markets"}},
            },
        }
        messages = [
            {"role": "system", "content": f"Classify transaction:\n```json\n{json.dumps(schema)}\n```"},
            {"role": "user", "content": "Countdown Supermarket $42.50"},
        ]
        self.assertTrue(shim._extract_include_meta(messages, {}))
        res = shim.infer_question_from_messages(messages)
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 2)
        state, questions = res
        self.assertIn("Countdown Supermarket", state)
        self.assertEqual(set(questions.keys()), {"category"})

    def test_untagged_fenced_json_block_enables_include_meta(self) -> None:
        schema = {"include_meta": True, "is_ok": {"type": "noul"}}
        messages = [
            {"role": "user", "content": f"Evaluate:\n```\n{json.dumps(schema)}\n```\nCheck disk alert"},
        ]
        self.assertTrue(shim._extract_include_meta(messages, {}))

    def test_raw_system_json_enables_include_meta(self) -> None:
        schema = {
            "include_meta": True,
            "questions": {
                "is_subscription": {"type": "noul", "instructions": "True if recurring subscription."},
            },
        }
        messages = [
            {"role": "system", "content": json.dumps(schema)},
            {"role": "user", "content": "Spotify NZ $16.99"},
        ]
        self.assertTrue(shim._extract_include_meta(messages, {}))
        res = shim.infer_question_from_messages(messages)
        self.assertIsInstance(res, tuple)
        self.assertEqual(len(res), 2)
        self.assertEqual(set(res[1].keys()), {"is_subscription"})

    def test_false_non_bool_and_malformed_json_return_false(self) -> None:
        for body in (
            {},
            {"include_meta": False},
            {"include_meta": "true"},
            {"include_meta": 1},
        ):
            self.assertFalse(shim._extract_include_meta([{"role": "user", "content": "Hello"}], body))

        malformed_messages = [
            {"role": "system", "content": "{not valid json"},
            {"role": "user", "content": "```json\n{bad: json}\n```"},
            {"role": "system", "content": json.dumps({"include_meta": "true"})},
        ]
        self.assertFalse(shim._extract_include_meta(malformed_messages, {}))


class TestFormatOpenAICompletionIncludeMeta(unittest.TestCase):
    """Tests backward compatibility and include_meta confidence formatting."""

    def test_default_include_meta_false_preserves_legacy_outputs(self) -> None:
        single_choice = {"category": {"choice": "DINING", "confidence": 0.96}}
        res_single = shim.format_openai_completion("jev-decision", single_choice, {"answers": single_choice})
        self.assertEqual(res_single["choices"][0]["message"]["content"], "DINING")

        res_json_fmt = shim.format_openai_completion(
            "jev-decision",
            single_choice,
            {"answers": single_choice},
            response_format={"type": "json_object"},
        )
        self.assertEqual(json.loads(res_json_fmt["choices"][0]["message"]["content"]), {"category": "DINING"})

        single_score = {"urgency_score": {"score": 2.0, "confidence": 0.88}}
        res_score = shim.format_openai_completion("agent-monitor", single_score, {"answers": single_score})
        self.assertEqual(res_score["choices"][0]["message"]["content"], "7")

        single_noul = {"is_actionable": {"noul": 0.82}}
        res_noul = shim.format_openai_completion("jev-decision", single_noul, {"answers": single_noul})
        self.assertEqual(res_noul["choices"][0]["message"]["content"], "true")

        multi_answers = {
            "category": {"choice": "GROCERIES", "confidence": 0.95},
            "is_recurring": {"noul": 0.15},
        }
        res_multi = shim.format_openai_completion("jev-decision", multi_answers, {"answers": multi_answers})
        self.assertEqual(
            json.loads(res_multi["choices"][0]["message"]["content"]),
            {"category": "GROCERIES", "is_recurring": "false"},
        )

    def test_include_meta_derives_noul_confidence_and_uses_min_overall(self) -> None:
        answers = {
            "category": {"choice": "GROCERIES", "confidence": 0.95},
            "is_recurring": {"noul": 0.82},
            "is_transfer": {"noul": 0.15},
        }
        res = shim.format_openai_completion(
            "jev-decision",
            answers,
            {"answers": answers},
            include_meta=True,
        )
        parsed = json.loads(res["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed,
            {
                "category": "GROCERIES",
                "is_recurring": "true",
                "is_transfer": "false",
                "_confidence": 0.64,
                "_question_confidence": {
                    "category": 0.95,
                    "is_recurring": 0.64,
                    "is_transfer": 0.7,
                },
            },
        )

    def test_include_meta_prefers_explicit_top_level_and_zero_confidence(self) -> None:
        answers = {
            "is_ambiguous": {"noul": 0.5, "confidence": 0.0},
            "category": {"choice": "OTHER", "confidence": 0.85},
        }
        raw_response = {"answers": answers, "confidence": 0.42}
        res = shim.format_openai_completion(
            "jev-decision",
            answers,
            raw_response,
            include_meta=True,
        )
        parsed = json.loads(res["choices"][0]["message"]["content"])
        self.assertEqual(parsed["_confidence"], 0.42)
        self.assertEqual(parsed["_question_confidence"], {"is_ambiguous": 0.0, "category": 0.85})

        res_min = shim.format_openai_completion(
            "jev-decision",
            answers,
            {"answers": answers},
            include_meta=True,
        )
        parsed_min = json.loads(res_min["choices"][0]["message"]["content"])
        self.assertEqual(parsed_min["_confidence"], 0.0)

    def test_include_meta_explicit_noul_confidence_overrides_derived(self) -> None:
        answers = {"decision": {"noul": 0.82, "confidence": 0.91}}
        res = shim.format_openai_completion(
            "jev-decision",
            answers,
            {"answers": answers},
            include_meta=True,
        )
        parsed = json.loads(res["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed,
            {
                "decision": "true",
                "_confidence": 0.91,
                "_question_confidence": {"decision": 0.91},
            },
        )

    def test_include_meta_empty_and_missing_confidence_handling(self) -> None:
        res_empty = shim.format_openai_completion("jev-decision", {}, {}, include_meta=True)
        parsed_empty = json.loads(res_empty["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed_empty,
            {
                "_confidence": None,
                "_question_confidence": {},
            },
        )

        answers_no_conf = {"urgency_score": {"score": 2.0}}
        res_no_conf = shim.format_openai_completion(
            "agent-monitor",
            answers_no_conf,
            {"answers": answers_no_conf},
            include_meta=True,
        )
        parsed_no_conf = json.loads(res_no_conf["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed_no_conf,
            {
                "urgency_score": "7",
                "_confidence": None,
                "_question_confidence": {},
            },
        )


class TestJevOpenAIHandlerIncludeMeta(unittest.TestCase):
    """End-to-end HTTP POST /v1/chat/completions tests for include_meta opt-in."""

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

    def test_http_fenced_json_include_meta_single_question(self) -> None:
        schema = {
            "include_meta": True,
            "questions": {
                "category": {"type": "choice", "criteria": {"DINING": "Cafes", "GROCERIES": "Markets"}},
            },
        }
        payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "system", "content": f"```json\n{json.dumps(schema)}\n```"},
                {"role": "user", "content": "Flat white $6.00"},
            ],
        }
        mock_res = {"answers": {"category": {"choice": "DINING", "confidence": 0.94}}}
        with patch.object(shim, "call_typesafe_systemone", return_value=mock_res):
            status, data = self._post_chat_completion(payload)
        self.assertEqual(status, 200)
        parsed = json.loads(data["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed,
            {
                "category": "DINING",
                "_confidence": 0.94,
                "_question_confidence": {"category": 0.94},
            },
        )

    def test_http_raw_system_json_include_meta(self) -> None:
        schema = {
            "include_meta": True,
            "questions": {
                "category": {"type": "choice", "criteria": {"GROCERIES": "Markets", "DINING": "Cafes"}},
                "is_subscription": {"type": "noul", "instructions": "True if recurring subscription."},
            },
        }
        payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "system", "content": json.dumps(schema)},
                {"role": "user", "content": "New World Metro $55.40"},
            ],
        }
        mock_res = {
            "answers": {
                "category": {"choice": "GROCERIES", "confidence": 0.97},
                "is_subscription": {"noul": 0.12},
            }
        }
        with patch.object(shim, "call_typesafe_systemone", return_value=mock_res):
            status, data = self._post_chat_completion(payload)
        self.assertEqual(status, 200)
        parsed = json.loads(data["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed,
            {
                "category": "GROCERIES",
                "is_subscription": "false",
                "_confidence": 0.76,
                "_question_confidence": {
                    "category": 0.97,
                    "is_subscription": 0.76,
                },
            },
        )

    def test_http_body_flag_include_meta(self) -> None:
        payload = {
            "model": "jev-decision",
            "include_meta": True,
            "messages": [
                {"role": "user", "content": "Is this actionable? true/false"},
            ],
        }
        mock_res = {"answers": {"decision": {"noul": 0.85}}}
        with patch.object(shim, "call_typesafe_systemone", return_value=mock_res):
            status, data = self._post_chat_completion(payload)
        self.assertEqual(status, 200)
        parsed = json.loads(data["choices"][0]["message"]["content"])
        self.assertEqual(
            parsed,
            {
                "decision": "true",
                "_confidence": 0.7,
                "_question_confidence": {"decision": 0.7},
            },
        )

    def test_http_default_omits_meta(self) -> None:
        payload = {
            "model": "jev-decision",
            "messages": [
                {"role": "user", "content": "Is this actionable? true/false"},
            ],
        }
        mock_res = {"answers": {"decision": {"noul": 0.85}}}
        with patch.object(shim, "call_typesafe_systemone", return_value=mock_res):
            status, data = self._post_chat_completion(payload)
        self.assertEqual(status, 200)
        self.assertEqual(data["choices"][0]["message"]["content"], "true")


if __name__ == "__main__":
    unittest.main()
