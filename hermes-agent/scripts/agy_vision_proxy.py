#!/usr/bin/env python3
"""Stateless OpenAI-compatible vision/completion proxy backed by Antigravity CLI (agy).

Exposes POST /v1/chat/completions on the internal Swarm network so Muninn can route
multimodal requests (such as Plutus receipt parsing) to Gemini 4 Hydrogen without
entering Huginn Gateway's stateful tool/approval loop.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, status
import uvicorn

DATA_URI_RE = re.compile(r"^data:(image/[a-zA-Z0-9.+-]+);base64,(.+)$", re.DOTALL)
MODEL_NAME_RE = re.compile(r"^[a-zA-Z0-9._-]+$")

MODEL_ALIASES = {
    "gemini-4-hydrogen": "gemini-4-hydrogen-high",
    "hydrogen": "gemini-4-hydrogen-high",
    "gemini-4-carbon": "gemini-4-carbon-high",
    "carbon": "gemini-4-carbon-high",
}

MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

AGY_TIMEOUT_SECONDS = int(os.environ.get("AGY_VISION_TIMEOUT_SECONDS", "90"))
AGY_BIN = os.environ.get("AGY_BIN", "agy")

app = FastAPI(title="Odin Agy Vision Proxy", version="1.0.0")


def require_bearer_auth(authorization: str = Header(default="")) -> None:
    """Enforce fail-closed bearer token auth using API_SERVER_KEY."""
    expected = os.environ.get("API_SERVER_KEY", "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="API_SERVER_KEY is not configured",
        )
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.strip(), expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid gateway API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def resolve_agy_model(requested: str | None) -> str:
    """Resolve OpenAI/Muninn model names to a valid agy CLI model identifier."""
    raw = (requested or "gemini-4-hydrogen-high").strip()
    if "/" in raw:
        raw = raw.rsplit("/", 1)[-1]
    resolved = MODEL_ALIASES.get(raw, raw)
    if not MODEL_NAME_RE.match(resolved):
        raise HTTPException(status_code=400, detail=f"Invalid model identifier: {raw}")
    return resolved


def extract_prompt_and_images(
    messages: list[dict[str, Any]],
    work_dir: Path,
) -> tuple[str, list[Path]]:
    """Flatten OpenAI chat messages into a single prompt and materialize base64 images."""
    sections: list[str] = []
    image_paths: list[Path] = []

    for msg in messages:
        role = str(msg.get("role", "user")).strip().upper()
        content = msg.get("content", "")
        text_parts: list[str] = []

        if isinstance(content, str):
            if content.strip():
                text_parts.append(content.strip())
        elif isinstance(content, list):
            for part in content:
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "text" and part.get("text"):
                    text_parts.append(str(part["text"]).strip())
                elif ptype == "image_url":
                    raw_url = part.get("image_url")
                    url = raw_url.get("url", "") if isinstance(raw_url, dict) else str(raw_url or "")
                    match = DATA_URI_RE.match(url.strip())
                    if not match:
                        raise HTTPException(
                            status_code=400,
                            detail="Only base64 data:image/* URLs are supported",
                        )
                    mime, b64_payload = match.groups()
                    ext = MIME_EXTENSIONS.get(mime.lower(), "bin")
                    img_path = work_dir / f"image_{len(image_paths) + 1}.{ext}"
                    try:
                        img_path.write_bytes(base64.b64decode(b64_payload, validate=True))
                    except Exception as exc:
                        raise HTTPException(status_code=400, detail=f"Invalid base64 image: {exc}") from exc
                    image_paths.append(img_path)
                    text_parts.append(f"[Attached image file: {img_path}]")

        if text_parts:
            combined = "\n".join(text_parts)
            sections.append(combined if role == "USER" else f"{role}:\n{combined}")

    if not sections and not image_paths:
        raise HTTPException(status_code=400, detail="messages must contain text or image content")

    if image_paths:
        file_list = ", ".join(str(p) for p in image_paths)
        sections.insert(0, f"Read and inspect the attached image file(s): {file_list}")

    return "\n\n".join(sections), image_paths


async def run_agy_prompt(model: str, prompt: str, work_dir: Path | None) -> str:
    """Invoke agy CLI in stateless print mode and return stdout."""
    cmd = [
        AGY_BIN,
        "--model",
        model,
        "--dangerously-skip-permissions",
        "--disable-slash-commands",
    ]
    if work_dir is not None:
        cmd.extend(["--add-dir", str(work_dir)])
    cmd.extend(["-p", prompt])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=AGY_TIMEOUT_SECONDS)
    except asyncio.TimeoutError as exc:
        proc.kill()
        await proc.communicate()
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=f"agy vision request timed out after {AGY_TIMEOUT_SECONDS}s",
        ) from exc

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip() or f"exit code {proc.returncode}"
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"agy invocation failed: {err_msg}",
        )

    return stdout.decode("utf-8", errors="replace").strip()


@app.get("/health")
async def health() -> dict[str, str]:
    """Healthcheck endpoint for Swarm/monitoring."""
    return {"status": "ok"}


@app.post("/v1/chat/completions", dependencies=[Depends(require_bearer_auth)])
async def chat_completions(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle OpenAI-compatible chat completion requests with optional base64 images."""
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages must be a non-empty list")

    model = resolve_agy_model(payload.get("model"))

    with tempfile.TemporaryDirectory(prefix="agy-vision-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        prompt, image_paths = extract_prompt_and_images(messages, tmp_path)
        answer = await run_agy_prompt(
            model=model,
            prompt=prompt,
            work_dir=tmp_path if image_paths else None,
        )

    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }
        ],
    }


if __name__ == "__main__":
    port = int(os.environ.get("AGY_VISION_PORT", "8643"))
    uvicorn.run(app, host="0.0.0.0", port=port)
