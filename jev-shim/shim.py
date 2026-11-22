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

VALID_QUESTION_TYPES = frozenset({"choice", "noul", "score", "box"})
MAX_CHOICE_OPTIONS = 255
MIN_SCORE_LEVELS = 2
MAX_SCORE_LEVELS = 10
DEFAULT_SCORE_LEVELS = 4


class SchemaValidationError(ValueError):
    """Raised when a Jev System One questions schema fails preflight validation."""


class TypeSafeAPIError(RuntimeError):
    """Raised when the upstream TypeSafe System One API returns a non-2xx HTTP status."""

    def __init__(self, status_code: int, body: str) -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(f"TypeSafe API HTTP {status_code}: {body}")


def validate_questions_schema(questions: Dict[str, Any]) -> None:
    """Validates Jev System One question specs before sending upstream."""
    if not isinstance(questions, dict) or not questions:
        raise SchemaValidationError("Questions schema must be a non-empty dictionary.")

    for key, spec in questions.items():
        if not isinstance(spec, dict):
            raise SchemaValidationError(f"Question '{key}' specification must be a dictionary.")

        q_type = spec.get("type")
        if not isinstance(q_type, str) or q_type not in VALID_QUESTION_TYPES:
            raise SchemaValidationError(
                f"Unsupported Jev question type '{q_type}' for '{key}'. "
                f"Must be one of: {', '.join(sorted(VALID_QUESTION_TYPES))}."
            )

        if q_type == "choice":
            criteria = spec.get("criteria")
            if not isinstance(criteria, (dict, list)) or not (1 <= len(criteria) <= MAX_CHOICE_OPTIONS):
                count = len(criteria) if isinstance(criteria, (dict, list)) else 0
                raise SchemaValidationError(
                    f"Choice question '{key}' must define 'criteria' as a non-empty dict or list "
                    f"with 1 to {MAX_CHOICE_OPTIONS} options (got {type(criteria).__name__} of length {count})."
                )
        elif q_type == "score":
            _validate_score_spec(key, spec)


def _validate_score_spec(key: str, spec: Dict[str, Any]) -> None:
    """Validate optional score criteria or levels bounds (2..10)."""
    if "criteria" in spec and spec["criteria"] is not None:
        criteria = spec["criteria"]
        if not isinstance(criteria, (list, tuple, dict)) or not (MIN_SCORE_LEVELS <= len(criteria) <= MAX_SCORE_LEVELS):
            count = len(criteria) if isinstance(criteria, (list, tuple, dict)) else 0
            raise SchemaValidationError(
                f"Score question '{key}' criteria must contain {MIN_SCORE_LEVELS} to {MAX_SCORE_LEVELS} "
                f"rubric levels (got {type(criteria).__name__} of length {count})."
            )
    if "levels" in spec and spec["levels"] is not None:
        levels = spec["levels"]
        if isinstance(levels, bool) or not isinstance(levels, int) or not (MIN_SCORE_LEVELS <= levels <= MAX_SCORE_LEVELS):
            raise SchemaValidationError(
                f"Score question '{key}' levels must be an integer between {MIN_SCORE_LEVELS} and {MAX_SCORE_LEVELS} (got {levels!r})."
            )


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
        raise TypeSafeAPIError(e.code, body) from e
    except Exception as e:
        logger.error("TypeSafe connection error: %s", e)
        raise RuntimeError(f"TypeSafe connection error: {e}") from e


def _extract_questions_dict(parsed: Any) -> Optional[Dict[str, Any]]:
    """Extracts a valid Jev System One questions mapping from a parsed JSON object."""
    if not isinstance(parsed, dict) or not parsed:
        return None
    if "questions" in parsed and isinstance(parsed["questions"], dict) and parsed["questions"]:
        return parsed["questions"]
    if any(isinstance(v, dict) and "type" in v for v in parsed.values()):
        return parsed
    return None


def _extract_include_meta(messages: List[Dict[str, str]], body: Dict[str, Any]) -> bool:
    """Checks top-level request body or embedded JSON prompt schemas for opt-in include_meta."""
    if isinstance(body, dict) and body.get("include_meta") is True:
        return True
    if not isinstance(messages, list):
        return False

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if not isinstance(content, str):
            continue

        for match in re.finditer(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", content):
            try:
                parsed = json.loads(match.group(1))
                if isinstance(parsed, dict) and parsed.get("include_meta") is True:
                    return True
            except Exception:
                pass

        if msg.get("role", "") == "system":
            raw_sys = content.strip()
            if raw_sys.startswith("{") and raw_sys.endswith("}"):
                try:
                    parsed = json.loads(raw_sys)
                    if isinstance(parsed, dict) and parsed.get("include_meta") is True:
                        return True
                except Exception:
                    pass

    return False


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

    # Check for embedded custom schema hints in fenced code blocks
    custom_hint_match = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", full_prompt)
    if custom_hint_match:
        try:
            parsed = json.loads(custom_hint_match.group(1))
            questions = _extract_questions_dict(parsed)
            if questions:
                clean_state = re.sub(r"```(?:json)?\s*\{[\s\S]*?\}\s*```", "", state_body).strip()
                clean_full = (
                    full_prompt[:custom_hint_match.start()].strip()
                    + "\n"
                    + full_prompt[custom_hint_match.end():].strip()
                )
                state = clean_state or clean_full
                return state.strip(), questions
        except Exception:
            pass

    # Check for raw JSON schema in system prompt
    raw_sys = system_text.strip()
    if raw_sys.startswith("{") and raw_sys.endswith("}"):
        try:
            parsed = json.loads(raw_sys)
            questions = _extract_questions_dict(parsed)
            if questions:
                state = state_body or full_prompt
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


def _resolve_score_levels(
    val: Any,
    question_spec: Optional[Dict[str, Any]] = None,
    score_levels: Optional[int] = None,
) -> int:
    """Resolve declared score rubric level count (2..10), defaulting to 4."""
    if score_levels is not None:
        if isinstance(score_levels, bool) or not isinstance(score_levels, int) or not (MIN_SCORE_LEVELS <= score_levels <= MAX_SCORE_LEVELS):
            raise ValueError(f"score_levels must be an int between {MIN_SCORE_LEVELS} and {MAX_SCORE_LEVELS} (got {score_levels!r})")
        return score_levels

    if isinstance(question_spec, dict):
        criteria = question_spec.get("criteria")
        if isinstance(criteria, (list, tuple, dict)) and MIN_SCORE_LEVELS <= len(criteria) <= MAX_SCORE_LEVELS:
            return len(criteria)
        spec_levels = question_spec.get("levels")
        if not isinstance(spec_levels, bool) and isinstance(spec_levels, int) and MIN_SCORE_LEVELS <= spec_levels <= MAX_SCORE_LEVELS:
            return spec_levels

    if isinstance(val, dict):
        val_levels = val.get("levels")
        if not isinstance(val_levels, bool) and isinstance(val_levels, int) and MIN_SCORE_LEVELS <= val_levels <= MAX_SCORE_LEVELS:
            return val_levels

    return DEFAULT_SCORE_LEVELS


def extract_verdict(
    val: Any,
    question_spec: Optional[Dict[str, Any]] = None,
    *,
    score_levels: Optional[int] = None,
) -> Optional[str]:
    """Extracts a normalized string verdict from a single Jev answer entry."""
    if not isinstance(val, dict):
        return None
    if "choice" in val:
        return str(val.get("choice", ""))
    if "score" in val:
        raw_score = float(val.get("score", 0.0))
        levels = _resolve_score_levels(val, question_spec, score_levels)
        scaled = int(round(1 + (raw_score / float(levels - 1)) * 9))
        return str(max(1, min(10, scaled)))
    if "noul" in val:
        prob = float(val.get("noul", 0.0))
        return "true" if prob >= 0.5 else "false"
    if "box" in val:
        box_val = val.get("box")
        if box_val is None:
            return None
        if isinstance(box_val, str):
            return box_val
        return json.dumps(box_val)
    return None


def _extract_question_confidences(answers: Dict[str, Any]) -> Dict[str, float]:
    """Extract per-question confidence scores from Jev answer entries."""
    question_confidences: Dict[str, float] = {}
    for key, val in answers.items():
        if not isinstance(val, dict):
            continue
        if val.get("confidence") is not None:
            question_confidences[key] = float(val["confidence"])
        elif "noul" in val and val["noul"] is not None:
            question_confidences[key] = round(abs(float(val["noul"]) - 0.5) * 2.0, 4)
    return question_confidences


def _build_completion_content(
    verdicts: Dict[str, str],
    answers: Dict[str, Any],
    raw_response: Dict[str, Any],
    response_format: Optional[Dict[str, Any]],
    include_meta: bool,
) -> str:
    """Serialize normalized verdicts and optional calibrated confidence metadata."""
    force_json = isinstance(response_format, dict) and response_format.get("type") == "json_object"
    if include_meta:
        question_confidences = _extract_question_confidences(answers)
        if isinstance(raw_response, dict) and raw_response.get("confidence") is not None:
            overall_conf: Optional[float] = float(raw_response["confidence"])
        elif question_confidences:
            overall_conf = min(question_confidences.values())
        else:
            overall_conf = None
        output: Dict[str, Any] = dict(verdicts)
        output["_confidence"] = overall_conf
        output["_question_confidence"] = question_confidences
        return json.dumps(output)
    if not verdicts:
        return json.dumps(answers)
    if len(answers) > 1 or len(verdicts) > 1 or force_json:
        return json.dumps(verdicts)
    return next(iter(verdicts.values()))


def format_openai_completion(
    model: str,
    answers: Dict[str, Any],
    raw_response: Dict[str, Any],
    response_format: Optional[Dict[str, Any]] = None,
    include_meta: bool = False,
    questions: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Converts Jev answers dictionary into an OpenAI completion envelope."""
    verdicts: Dict[str, str] = {}
    for key, val in answers.items():
        q_spec = questions.get(key) if isinstance(questions, dict) else None
        verdict = extract_verdict(val, question_spec=q_spec)
        if verdict is not None:
            verdicts[key] = verdict

    content = _build_completion_content(verdicts, answers, raw_response, response_format, include_meta)
    prompt_tokens = len(json.dumps(answers)) // 4
    completion_tokens = len(content) // 4 + 1
    return {
        "id": f"chatcmpl-jev-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
        "_jev_meta": {"answers": answers, "raw": raw_response},
    }


class JevOpenAIHandler(http.server.BaseHTTPRequestHandler):
    api_key: str = ""

    def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def _resolve_request_api_key(self) -> str:
        """Resolve TypeSafe API key from Bearer header or configured shim default."""
        auth_header = self.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            extracted = auth_header[7:].strip()
            if extracted and not extracted.startswith("sk-dummy") and extracted != "dummy":
                return extracted
        return self.api_key

    def _parse_json_body(self) -> Optional[Dict[str, Any]]:
        """Read and decode JSON body, writing HTTP 400 on malformed JSON."""
        content_length = int(self.headers.get("Content-Length", 0))
        post_data = self.rfile.read(content_length)
        try:
            return json.loads(post_data.decode("utf-8"))
        except Exception as e:
            self._send_json(400, {"error": f"Invalid JSON payload: {e}"})
            return None

    def do_GET(self):
        if self.path == "/health" or self.path == "/":
            self._send_json(200, {"status": "ok", "service": "jev-openai-shim"})
        elif self.path == "/v1/models":
            models_data = {
                "object": "list",
                "data": [
                    {"id": "jev-latest", "object": "model", "owned_by": "typesafe"},
                    {"id": "jev-decision", "object": "model", "owned_by": "yggdrasil"},
                    {"id": "agent-monitor", "object": "model", "owned_by": "yggdrasil"},
                ],
            }
            self._send_json(200, models_data)
        else:
            self.send_response(404)
            self.end_headers()

    def _dispatch_completion(
        self,
        body: Dict[str, Any],
        state: str,
        questions: Dict[str, Any],
        api_key: str,
    ) -> None:
        """Forward validated questions to TypeSafe System One and write OpenAI JSON."""
        logger.info("Routing request to Jev. Questions: %s", list(questions.keys()))
        try:
            jev_res = call_typesafe_systemone(state, questions, api_key, model=DEFAULT_MODEL)
            completion = format_openai_completion(
                body.get("model", DEFAULT_MODEL),
                jev_res.get("answers", {}),
                jev_res,
                response_format=body.get("response_format"),
                include_meta=_extract_include_meta(body.get("messages", []), body),
                questions=questions,
            )
            self._send_json(200, completion)
        except TypeSafeAPIError as e:
            status = e.status_code if 400 <= e.status_code < 500 else 502
            logger.error("TypeSafe API error (returning HTTP %d): %s", status, e)
            self._send_json(status, {"error": str(e)})
        except Exception as e:
            logger.error("Error processing completion: %s", e)
            self._send_json(502, {"error": str(e)})

    def do_POST(self):
        if self.path not in ("/v1/chat/completions", "/chat/completions"):
            self._send_json(404, {"error": f"Endpoint {self.path} not supported"})
            return
        body = self._parse_json_body()
        if body is None:
            return
        api_key = self._resolve_request_api_key()
        if not api_key:
            self._send_json(401, {"error": "TYPESAFE_API_KEY is not configured on shim or request header"})
            return

        state, questions = infer_question_from_messages(body.get("messages", []))
        questions = _extract_questions_dict(body.get("questions")) or questions
        try:
            validate_questions_schema(questions)
        except SchemaValidationError as e:
            logger.warning("Invalid Jev questions schema: %s", e)
            self._send_json(400, {"error": str(e)})
            return
        self._dispatch_completion(body, state, questions, api_key)


def create_server(host: str, port: int) -> http.server.ThreadingHTTPServer:
    """Create a multi-threaded HTTP server bound to (host, port)."""
    server = http.server.ThreadingHTTPServer((host, port), JevOpenAIHandler)
    server.daemon_threads = True
    return server


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
    server = create_server(args.host, args.port)
    logger.info("Starting Jev OpenAI Translation Shim on http://%s:%d", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down Jev OpenAI Translation Shim.")
        server.server_close()


if __name__ == "__main__":
    main()
