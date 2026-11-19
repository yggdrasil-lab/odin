"""Unit tests for agy_vision_proxy OpenAI multimodal adapter."""

from __future__ import annotations

import base64
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

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
                json={"model": "gemini-4-hydrogen", "messages": [{"role": "user", "content": "hi"}]},
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

        with (
            patch.dict("os.environ", {"API_SERVER_KEY": "secret-123"}),
            patch.object(agy_vision_proxy, "run_agy_prompt", side_effect=fake_run),
        ):
            res = self.client.post(
                "/v1/chat/completions",
                headers={"Authorization": "Bearer secret-123"},
                json={
                    "model": "gemini-4-hydrogen",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Parse this receipt."},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                            ],
                        }
                    ],
                },
            )

        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertEqual(captured["model"], "gemini-4-hydrogen-high")
        self.assertTrue(captured["has_dir"])
        self.assertIn("Parse this receipt.", str(captured["prompt"]))
        self.assertEqual(
            body["choices"][0]["message"]["content"],
            '{"merchant": "Costco", "total_amount": 154.93}',
        )


if __name__ == "__main__":
    unittest.main()
