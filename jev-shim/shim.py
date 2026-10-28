#!/usr/bin/env python3
"""shim.py - Lightweight OpenAI-compatible translation shim for TypeSafe Jev.

Translates standard OpenAI /v1/chat/completions requests into TypeSafe System One
typed decision queries (Score, Noul, Choice) against POST https://api.typesafe.ai/v1/systemone,
wrapping the typed answer into a standard chat completion envelope.

Enables Muninn (LiteLLM) and standard OpenAI-compatible callers to route auxiliary decision,
scoring, and gating tasks (auxiliary.monitor, alert triage, fact extraction) to Jev without
native SDK changes.

Usage:
    python3 shim.py [--port 8000] [--host 0.0.0.0]
"""

from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [jev-shim] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("jev-shim")

TYPESAFE_ENDPOINT = os.environ.get("TYPESAFE_ENDPOINT", "https://api.typesafe.ai/v1/systemone")
DEFAULT_MODEL = os.environ.get("TYPESAFE_DEFAULT_MODEL", "jev-latest")


def call_typesafe_systemone(
    state: str,
    questions: Dict[str, Any],
    api_key: str,
    model: str = DEFAULT_MODEL,
    timeout: float = 15.0,
) -> Dict[str, Any]:
    """Sends state and typed questions to TypeSafe Jev System One endpoint."""
    payload = {
        "model": model,
        "state": state,
        "questions": questions,
    }
    req = urllib.request.Request(
        TYPESAFE_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "User-Agent": "Yggdrasil-Jev-Shim/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        logger.error("TypeSafe HTTP %d: %s", e.code, body)
        raise RuntimeError(f"TypeSafe API HTTP {e.code}: {body}") from e
    except Exception as e:
        logger.error("TypeSafe connection error: %s", e)
        raise RuntimeError(f"TypeSafe connection error: {e}") from e


def infer_question_from_messages(messages: List[Dict[str, str]]) -> Tuple[str, Dict[str, Any]]:
    """Inspects chat history to assemble state and derive typed decision primitives."""
    system_text = ""
    user_texts: List[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role == "system":
            system_text += content + "\n"
        elif role in ("user", "assistant"):
            user_texts.append(f"{role.capitalize()}: {content}")

    state_body = "\n".join(user_texts).strip()
    full_prompt = f"{system_text}\n{state_body}".strip()

    # Check for embedded custom schema hints
    custom_hint_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", full_prompt)
    if custom_hint_match:
        try:
            parsed = json.loads(custom_hint_match.group(1))
            questions = None
            if isinstance(parsed, dict):
                if "questions" in parsed and isinstance(parsed["questions"], dict):
                    questions = parsed["questions"]
                elif any("type" in v for v in parsed.values() if isinstance(v, dict)):
                    questions = parsed
            if questions:
                clean_full = full_prompt[:custom_hint_match.start()].strip() + "\n" + full_prompt[custom_hint_match.end():].strip()
                state = state_body or clean_full
                return state.strip(), questions
        except Exception:
            pass

    # Pattern 1: Urgency / score rating (0-10 or 1-10) -> Score
    if re.search(r"(?:score|rate|urgency|rank|severity|priority).*?(?:0[-\s]*10|1[-\s]*10|scale)", full_prompt, re.IGNORECASE):
        questions = {
            "urgency_score": {
                "type": "score",
                "criteria": [
                    "Routine info, transient check, or zero-impact noise",
                    "Low priority or minor observation",
                    "Moderate priority needing standard inspection",
                    "High priority or critical operational issue requiring immediate action",
                ],
            }
        }
        state = f"Context:\n{state_body}\n\nTask:\n{system_text or 'Score operational urgency.'}"
        return state, questions

    # Pattern 1.5: Choice classification (e.g. bracketed list of categories) -> Choice
    choice_match = re.search(
        r"(?:classify|categorize|route|select).*?(?:tier|category|bucket|option|into|one of)[:\s]+(\[.*?\])",
        full_prompt,
        re.IGNORECASE,
    )
    if choice_match:
        try:
            raw_options = choice_match.group(1)
            options_cleaned = [
                opt.strip(" '\"`")
                for opt in raw_options.strip("[]").split(",")
                if opt.strip(" '\"`")
            ]
            if options_cleaned:
                criteria = {opt: f"Matches category '{opt}'" for opt in options_cleaned}
                questions = {
                    "choice": {
                        "type": "choice",
                        "instructions": system_text.strip() or "Classify into exactly one category.",
                        "criteria": criteria,
                    }
                }
                state = f"State to evaluate:\n{state_body or full_prompt}"
                return state, questions
        except Exception:
            pass

    # Pattern 2: Binary classification (true/false, yes/no, should/is) -> Noul
    if re.search(r"(?:true|false|boolean|yes/no|should|is_material|is_actionable|has_fact)", full_prompt, re.IGNORECASE):
        instructions = system_text.strip() or "Evaluate whether the condition described in the prompt is met."
        questions = {
            "decision": {
                "type": "noul",
                "instructions": instructions,
            }
        }
        state = f"State to evaluate:\n{state_body}"
        return state, questions

    # Fallback: General actionability / materiality Noul
    questions = {
        "is_actionable": {
            "type": "noul",
            "instructions": f"True if this content requires operational review or agent action; False otherwise. Context: {system_text[:200]}",
        }
    }
    return full_prompt, questions


def format_openai_completion(
    model: str,
    answers: Dict[str, Any],
    raw_response: Dict[str, Any],
) -> Dict[str, Any]:
    """Converts Jev answers dictionary into an OpenAI completion envelope."""
    content = ""
    for key, val in answers.items():
        if not isinstance(val, dict):
            continue
        if "choice" in val:
            content = str(val.get("choice", ""))
            break
        if "score" in val:
            raw_score = val.get("score", 0.0)
            scaled = int(round(1 + (raw_score / 3.0) * 9))
            content = str(scaled)
            break
        if "noul" in val:
            prob = val.get("noul", 0.0)
            content = "true" if prob >= 0.5 else "false"
            break

    if not content:
        content = json.dumps(answers)

    completion_id = f"chatcmpl-jev-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": content,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(json.dumps(answers)) // 4,
            "completion_tokens": len(content) // 4 + 1,
            "total_tokens": (len(json.dumps(answers)) // 4) + len(content) // 4 + 1,
        },
        "_jev_meta": {
            "answers": answers,
            "raw": raw_response,
        },
    }


class JevOpenAIHandler(http.server.BaseHTTPRequestHandler):
    api_key: str = ""

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "ok", "service": "jev-openai-shim"}).encode("utf-8"))
        elif self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            models_data = {
                "object": "list",
                "data": [
                    {"id": "jev-latest", "object": "model", "owned_by": "typesafe"},
                    {"id": "jev-decision", "object": "model", "owned_by": "yggdrasil"},
                    {"id": "agent-monitor", "object": "model", "owned_by": "yggdrasil"},
                ],
            }
            self.wfile.write(json.dumps(models_data).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Endpoint {self.path} not supported"}).encode("utf-8"))
            return

        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)

        try:
            body = json.loads(post_data.decode("utf-8"))
        except Exception as e:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": f"Invalid JSON payload: {e}"}).encode("utf-8"))
            return

        messages = body.get("messages", [])
        requested_model = body.get("model", DEFAULT_MODEL)
        upstream_model = DEFAULT_MODEL if "jev" in requested_model else DEFAULT_MODEL

        # Resolve API key from header or environment
        auth_header = self.headers.get("Authorization", "")
        api_key = self.api_key
        if auth_header.startswith("Bearer "):
            extracted = auth_header[7:].strip()
            # If client passed an actual TypeSafe key, prioritize it
            if extracted and not extracted.startswith("sk-dummy") and not extracted == "dummy":
                api_key = extracted

        if not api_key:
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": "TYPESAFE_API_KEY is not configured on shim or request header"}).encode("utf-8"))
            return

        state, questions = infer_question_from_messages(messages)
        logger.info("Routing request to Jev. Questions: %s", list(questions.keys()))

        try:
            jev_res = call_typesafe_systemone(state, questions, api_key, model=upstream_model)
            answers = jev_res.get("answers", {})
            completion = format_openai_completion(requested_model, answers, jev_res)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(completion).encode("utf-8"))
        except Exception as e:
            logger.error("Error processing completion: %s", e)
            self.send_response(502)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode("utf-8"))


def main():
    parser = argparse.ArgumentParser(description="TypeSafe Jev OpenAI Translation Shim")
    default_host = os.environ.get("HOST", "0.0.0.0")
    default_port = int(os.environ.get("PORT", "8000"))
    parser.add_argument("--host", default=default_host, help=f"Binding host (default: {default_host})")
    parser.add_argument("--port", type=int, default=default_port, help=f"Port to listen on (default: {default_port})")
    args = parser.parse_args()

    api_key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not api_key:
        logger.warning("TYPESAFE_API_KEY not found in environment. Requests without Bearer key will fail.")
    else:
        logger.info("Loaded TYPESAFE_API_KEY (starts with '%s...')", api_key[:6])

    JevOpenAIHandler.api_key = api_key
    server = http.server.HTTPServer((args.host, args.port), JevOpenAIHandler)
    logger.info("Starting Jev OpenAI Translation Shim on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Jev OpenAI Translation Shim.")
        server.server_close()


if __name__ == "__main__":
    main()
