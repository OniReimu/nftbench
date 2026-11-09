"""OpenAI-compatible chat-completions backend (for example vLLM) with the OpenRouter prompt surface."""

from __future__ import annotations

import json
import os
import time
from http.client import HTTPException
from urllib import error, request

from .cli import CLIFailure, Invocation, VoidEpisode, count_attempt, event_fields, render_items, reply_to_response
from .openrouter import _EVENTS_LOCK, MAX_TOKENS, RETRY_WAITS, TIMEOUT_SECONDS, is_context_limit_error, is_model_format_error

TOKEN_ENV = "NFTB_ENDPOINT_TOKEN"
TOKEN_MARK = "[NFTB_ENDPOINT_TOKEN]"


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_context_limit(status, body) -> bool:
    """vLLM 0.20.0 answers a prompt past max-model-len with HTTP 400 {"error": {"message": "This model's maximum
    context length is ...", "type": "BadRequestError", "code": 400}}; older vLLM sent the error object unwrapped."""
    if isinstance(body, dict) and body.get("object") == "error":
        body = {"error": body}
    return is_context_limit_error(status, body)


class OpenAICompatBackend:
    def __init__(
        self, base_url: str, model: str, *, temperature: float | None = None, top_p: float | None = None,
        seed: int | None = None, max_tokens: int = MAX_TOKENS, extra=None, events_path=None, opener=None, sleep=None,
    ):
        self._token = os.environ.get(TOKEN_ENV) or None
        self.cli = "endpoint"
        self.url = base_url.rstrip("/") + "/v1/chat/completions"
        self.model = model
        self.temperature = temperature
        self.top_p = top_p
        self.seed = seed
        self.max_tokens = max_tokens
        self.extra = extra or {}
        self._events_path = events_path
        self._opener = opener or request.urlopen
        self._sleep = sleep or time.sleep

    def _scrub(self, text: str) -> str:
        if not self._token:
            return text
        return text.replace(self._token, TOKEN_MARK).replace(json.dumps(self._token)[1:-1], TOKEN_MARK)

    def _scrub_body(self, raw: str) -> str:
        # A JSON body can spell the token with \\u escapes that a text replace misses.
        raw = self._scrub(raw)
        if not self._token:
            return raw
        try:
            decoded = json.dumps(json.loads(raw), ensure_ascii=False)
        except ValueError:
            return raw
        return self._scrub(decoded) if self._token in decoded else raw

    def invoke(self, system: str, prompt: str) -> Invocation:
        return self.invoke_items(system, [{"role": "user", "content": prompt}])

    def invoke_items(self, head: str, items: list[dict]) -> Invocation:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": head}, *items],
            "max_tokens": self.max_tokens,
            **self.extra,
        }
        # Unset sampling values are left to the server, which applies the model's generation_config defaults.
        payload |= {
            key: value for key, value in (("temperature", self.temperature), ("top_p", self.top_p), ("seed", self.seed))
            if value is not None
        }
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        detail = "no reply"
        retried = {"attempts": 0, "prompt_tokens": 0, "completion_tokens": 0}
        for attempt in range(4):
            # The final failure kind follows the last attempt; model_format means the model's own output failed.
            kind = "cli_error"
            req = request.Request(self.url, data=json.dumps(payload).encode(), headers=headers)
            status, raw, stderr = None, None, ""
            count_attempt()
            t_start = time.time()
            try:
                with self._opener(req, timeout=TIMEOUT_SECONDS) as response:
                    status = response.status
                    raw = response.read().decode("utf-8", errors="replace")
            except error.HTTPError as exc:
                status = exc.code
                raw = exc.read().decode("utf-8", errors="replace")
                stderr = self._scrub(str(exc))
                exc.close()
            except (error.URLError, OSError, HTTPException) as exc:
                detail = self._scrub(str(exc))
            except ValueError as exc:
                raise CLIFailure("cli_error", self._scrub(str(exc))) from None

            if raw is not None:
                raw = self._scrub_body(raw)
                if self._events_path:
                    # Shared with the OpenRouter backend: the tasks of one run append to one events file.
                    with _EVENTS_LOCK, open(self._events_path, "a") as handle:
                        record = {"returncode": 0 if status == 200 else status, "stdout": raw, "stderr": stderr, **event_fields(t_start)}
                        handle.write(json.dumps(record, sort_keys=True) + "\n")
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                usage = body.get("usage") if isinstance(body, dict) else None
                if status in (401, 402, 403):
                    raise CLIFailure("auth_or_quota", f"HTTP {status}: {raw[-300:]}")
                detail = f"HTTP {status}: {raw[-300:]}"
                retry = status == 429 or 500 <= status < 600
                if status == 200:
                    if _is_context_limit(status, body):
                        raise CLIFailure("context_limit", detail)
                    retry = isinstance(body, dict) and ("error" in body or not body.get("choices"))
                    if not retry:
                        if not isinstance(body, dict):
                            raise CLIFailure("cli_error", "unparsable endpoint reply")
                        if not (
                            isinstance(usage, dict)
                            and _is_count(usage.get("prompt_tokens"))
                            and _is_count(usage.get("completion_tokens"))
                        ):
                            raise CLIFailure("incomplete_output", "no integer usage.prompt_tokens and usage.completion_tokens in the reply")
                        if body.get("model") != self.model:
                            raise CLIFailure("model_mismatch", self._scrub(f"asked for {self.model}, got {body.get('model')}"))
                        try:
                            choice = body["choices"][0]
                            message = choice["message"]
                            # Only a missing content is empty text; any other non-string is a malformed body.
                            text = message.get("content")
                            if text is None:
                                text = ""
                            violations = []
                            calls = message.get("tool_calls") or []
                            finish_reason = choice.get("finish_reason")
                            # A reply cut at the length limit is retried below, so its tool arguments are not decoded.
                            if len(calls) == 1 and finish_reason != "length":
                                # Some servers parse the model's text into this field; no tool exists on our side.
                                function = calls[0]["function"]
                                text = json.dumps({"name": function["name"], "arguments": json.loads(function["arguments"])})
                            elif calls:
                                text = ""
                        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                            raise CLIFailure("incomplete_output", "malformed endpoint choice") from None
                        if not isinstance(text, str):
                            raise CLIFailure("incomplete_output", "reply content is not text")
                        if finish_reason == "error" or choice.get("error"):
                            # A mid-generation server failure; its partial text is not a reply.
                            error_detail = self._scrub(json.dumps(choice.get("error")))
                            kind = "model_format" if is_model_format_error(choice.get("error")) else "incomplete_output"
                            detail, retry = f"the reply ended in an error: {error_detail}", True
                        elif finish_reason == "length":
                            detail, kind, retry = "finish_reason is length", "model_format", True
                        elif text.strip():
                            if retried["attempts"]:
                                usage = {**usage, "retried": dict(retried)}
                            return Invocation(text, {"usage": usage}, violations)
                        else:
                            detail, kind, retry = "the model returned no text", "model_format", True
                # Tokens spent on a discarded attempt still count towards the episode's cost.
                if retry and isinstance(usage, dict) and _is_count(usage.get("prompt_tokens")) and _is_count(usage.get("completion_tokens")):
                    retried["attempts"] += 1
                    retried["prompt_tokens"] += usage["prompt_tokens"]
                    retried["completion_tokens"] += usage["completion_tokens"]
                if not retry:
                    raise CLIFailure("context_limit" if _is_context_limit(status, body) else "cli_error", detail)
            if attempt < len(RETRY_WAITS):
                self._sleep(RETRY_WAITS[attempt])
        raise CLIFailure(kind, detail)

    def complete(self, request: str) -> str:
        invocation = self.invoke_items(*render_items(request))
        if invocation.violations:
            raise VoidEpisode(invocation.violations)
        return reply_to_response(invocation.text, invocation.usage)
