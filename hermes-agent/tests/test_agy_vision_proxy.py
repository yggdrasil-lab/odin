"""Unit tests for agy_vision_proxy OpenAI multimodal adapter."""

from __future__ import annotations

import base64
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agy_vision_proxy  # noqa: E402


class TestAgyVisionProxy(unittest.TestCase):
    def setUp(self) -> None:
        self.client = TestClient(agy_vision_proxy.app)

    def test_requires_auth_and_rejects_bad_key(self) -> None:
        with patch.dict("os.environ", {"API_SERVER_KEY": "secret-123"}):
            res = self.client.post(
                "/v1/chat/completions",
                json={"model": "claude-sonnet-5-5", "messages": [{"role": "user", "content": "hi"}]},
            )
            self.assertEqual(res.status_code, 401)

    def test_multimodal_image_url_routes_to_agy(self) -> None:
        sample_bytes = b"fake-jpeg-bytes"
        b64 = base64.b64encode(sample_bytes).decode("ascii")
        captured: dict[str, object] = {}

        async def fake_run(model: str, prompt: str, work_dir: Path | None) -> str:
            captured["model"] = model
            captured["prompt"] = prompt
            captured["has_dir"] = work_dir is not None and (work_dir / "image_1.jpg").is_file()
            return '{"merchant": "Costco", "total_amount": 154.93}'

        env = {
            "API_SERVER_KEY": "secret-123",
            "HERMES_AGY_ALLOWED_MODELS": "claude-sonnet-5-5",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high",
        }
        payload = {
            "model": "claude-sonnet-5-5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Parse this receipt."},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }
            ],
        }
        with patch.dict("os.environ", env), patch.object(agy_vision_proxy, "run_agy_prompt", side_effect=fake_run):
            res = self.client.post("/v1/chat/completions", headers={"Authorization": "Bearer secret-123"}, json=payload)

        self.assertEqual(res.status_code, 200)
        self.assertEqual(captured["model"], "claude-sonnet-5-5-high")
        self.assertTrue(captured["has_dir"])
        self.assertIn("Parse this receipt.", str(captured["prompt"]))
        self.assertEqual(res.json()["choices"][0]["message"]["content"], '{"merchant": "Costco", "total_amount": 154.93}')

    def test_accepts_claude_sonnet_55_and_explicit_allowlist_defaults(self) -> None:
        env = {
            "HERMES_AGY_ALLOWED_MODELS": "claude-sonnet-5-5",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high",
        }
        with patch.dict("os.environ", env):
            self.assertEqual(agy_vision_proxy.resolve_agy_model(None), "claude-sonnet-5-5-high")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("claude-sonnet-5-5"), "claude-sonnet-5-5-high")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("sonnet"), "claude-sonnet-5-5-high")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("claude-sonnet"), "claude-sonnet-5-5-high")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("claude-sonnet-5-5-low"), "claude-sonnet-5-5-low")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("claude-sonnet-5-5-medium"), "claude-sonnet-5-5-medium")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("openai/claude-sonnet-5-5-high"), "claude-sonnet-5-5-high")

        with patch.dict("os.environ", {"HERMES_AGY_ALLOWED_MODELS": "gemini-4-hydrogen", "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high"}):
            self.assertEqual(agy_vision_proxy.resolve_agy_model(None), "gemini-4-hydrogen-high")

        clean_env = {k: v for k, v in os.environ.items() if k not in ("HERMES_AGY_ALLOWED_MODELS", "HERMES_AGY_ALLOWED_EFFORTS")}
        with patch.dict("os.environ", clean_env, clear=True):
            self.assertEqual(agy_vision_proxy.resolve_agy_model(None), "claude-sonnet-5-5-high")
            with self.assertRaises(HTTPException) as ctx:
                agy_vision_proxy.resolve_agy_model("gemini-4-hydrogen-high")
            self.assertEqual(ctx.exception.status_code, 400)

    def _assert_rejected_400(self, model_name: str) -> None:
        with self.assertRaises(HTTPException) as ctx:
            agy_vision_proxy.resolve_agy_model(model_name)
        self.assertEqual(ctx.exception.status_code, 400)
        res = self.client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer secret-123"},
            json={"model": model_name, "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(res.status_code, 400)

    def test_rejects_pool1_gemini_and_disallowed_models_with_400(self) -> None:
        disallowed = (
            "gemini-4-hydrogen",
            "hydrogen",
            "gemini-4-hydrogen-high",
            "gemini-4-carbon",
            "gemini-4-carbon-high",
            "gemini-4-neon-high",
            "neon",
            "gemini-4-argon-high",
            "argon",
            "claude-opus-5-5-high",
            "gpt-oss-120b",
            "gemini-3.8-flash",
        )
        env = {
            "API_SERVER_KEY": "secret-123",
            "HERMES_AGY_ALLOWED_MODELS": "claude-sonnet-5-5",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium,high",
        }
        with patch.dict("os.environ", env), patch.object(agy_vision_proxy, "run_agy_prompt") as mock_run:
            for model_name in disallowed:
                self._assert_rejected_400(model_name)
            mock_run.assert_not_called()

    def test_rejects_disallowed_efforts_and_empty_allowlist(self) -> None:
        env = {
            "HERMES_AGY_ALLOWED_MODELS": "claude-sonnet-5-5",
            "HERMES_AGY_ALLOWED_EFFORTS": "low,medium",
        }
        with patch.dict("os.environ", env):
            self.assertEqual(agy_vision_proxy.resolve_agy_model(None), "claude-sonnet-5-5-medium")
            self.assertEqual(agy_vision_proxy.resolve_agy_model("claude-sonnet-5-5-low"), "claude-sonnet-5-5-low")
            with self.assertRaises(HTTPException) as ctx:
                agy_vision_proxy.resolve_agy_model("claude-sonnet-5-5-high")
            self.assertEqual(ctx.exception.status_code, 400)

        for bad_env in ({"HERMES_AGY_ALLOWED_MODELS": ""}, {"HERMES_AGY_ALLOWED_EFFORTS": ""}):
            with patch.dict("os.environ", bad_env), self.assertRaises(HTTPException) as ctx:
                agy_vision_proxy.resolve_agy_model("claude-sonnet-5-5-high")
            self.assertEqual(ctx.exception.status_code, 400)


if __name__ == "__main__":
    unittest.main()
