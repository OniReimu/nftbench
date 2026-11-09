"""OpenAI Responses API backend with the OpenRouter prompt surface and a price-table spend guard."""

from __future__ import annotations

import json
import os
import re
import time
from decimal import Decimal
from http.client import HTTPException
from urllib import error, request

from .cli import CLIFailure, Invocation, VoidEpisode, count_attempt, event_fields, render_items, reply_to_response
from .openrouter import _EVENTS_LOCK, MAX_TOKENS, RETRY_WAITS, TIMEOUT_SECONDS, SpendGuard, is_context_limit_error

URL = "https://api.openai.com/v1/responses"
KEY_ENV = "OPENAI_API_KEY"
KEY_MARK = "[OPENAI_API_KEY]"
EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh")
# A cut-off or filtered reply is the model's own output failure; any other reason is the service's.
MODEL_FORMAT_INCOMPLETE = ("max_output_tokens", "content_filter")


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def price_cost(usage, prices) -> float | None:
    """USD for one response from (input, cached input, output) prices per million tokens; None if usage is unusable."""
    if not isinstance(usage, dict) or not (_is_count(usage.get("input_tokens")) and _is_count(usage.get("output_tokens"))):
        return None
    details = usage.get("input_tokens_details")
    # A reply without the cached count is charged at the full input price.
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    if (details is not None and not isinstance(details, dict)) or not _is_count(cached) or cached > usage["input_tokens"]:
        return None
    price_in, price_cached, price_out = (Decimal(str(price)) for price in prices)
    total = (
        (usage["input_tokens"] - cached) * price_in + cached * price_cached + usage["output_tokens"] * price_out
    ) / Decimal(1_000_000)
    return float(total)


def _output_text(body) -> str:
    """Concatenated output_text parts of the message items; reasoning and other items are ignored."""
    # A completed response always carries an output list; a missing one is a malformed body, not an empty reply.
    output = body.get("output")
    if not isinstance(output, list):
        raise TypeError("output is not a list")
    parts = []
    for item in output:
        if not isinstance(item, dict):
            raise TypeError("output item is not an object")
        if item["type"] != "message":
            continue
        if not isinstance(item["content"], list):
            raise TypeError("message content is not a list")
        for part in item["content"]:
            if not isinstance(part, dict):
                raise TypeError("content part is not an object")
            if part["type"] == "output_text":
                if not isinstance(part["text"], str):
                    raise TypeError("output_text is not text")
                parts.append(part["text"])
    return "".join(parts)


class OpenAIResponsesBackend:
    def __init__(
        self, model: str, *, guard: SpendGuard, reasoning_effort: str, prices, max_tokens=MAX_TOKENS,
        temperature: float | None = None, top_p: float | None = None, events_path=None, opener=None, sleep=None,
    ):
        key = os.environ.get(KEY_ENV)
        if not key:
            raise CLIFailure("auth_or_quota", "no key in the environment")
        if reasoning_effort not in EFFORTS:
            raise ValueError(f"unknown reasoning effort: {reasoning_effort}")
        self._key = key
        self.cli = "openai"
        self.model = model
        self.guard = guard
        self.reasoning_effort = reasoning_effort
        self.prices = tuple(prices)
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.top_p = top_p
        self._events_path = events_path
        self._opener = opener or request.urlopen
        self._sleep = sleep or time.sleep

    def _scrub(self, text: str) -> str:
        # Scrub both raw text and escaped JSON strings before saving or parsing them.
        return text.replace(self._key, KEY_MARK).replace(json.dumps(self._key)[1:-1], KEY_MARK)

    def _scrub_body(self, raw: str) -> str:
        # A JSON body can spell the key with \\u escapes that a text replace misses.
        raw = self._scrub(raw)
        try:
            decoded = json.dumps(json.loads(raw), ensure_ascii=False)
        except ValueError:
            return raw
        return self._scrub(decoded) if self._key in decoded else raw

    def _served_model_ok(self, served) -> bool:
        # The API answers with a dated snapshot of an alias, for example gpt-x-2026-01-01 for gpt-x; other suffixes are other models.
        return isinstance(served, str) and (served == self.model or re.fullmatch(re.escape(self.model) + r"-\d{4}-\d{2}-\d{2}", served) is not None)

    def invoke(self, system: str, prompt: str) -> Invocation:
        return self.invoke_items(system, [{"role": "user", "content": prompt}])

    def invoke_items(self, head: str, items: list[dict]) -> Invocation:
        payload = {
            "model": self.model,
            "input": [{"role": "developer", "content": head}, *items],
            "reasoning": {"effort": self.reasoning_effort},
            "max_output_tokens": self.max_tokens,
            "store": False,
        }
        if self.temperature is not None:
            payload["temperature"] = self.temperature
        if self.top_p is not None:
            payload["top_p"] = self.top_p
        detail = "no reply"
        for attempt in range(4):
            # The final failure kind follows the last attempt; model_format means the model's own output failed.
            kind = "cli_error"
            self.guard.add(0)
            req = request.Request(
                URL, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
            )
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
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                usage = body.get("usage") if isinstance(body, dict) else None
                cost = price_cost(usage, self.prices)
                if cost is not None:
                    # OpenAI reports no cost; the saved body carries the price-table cost that SpendGuard reads on resume.
                    usage = {**usage, "cost": cost, "cost_source": "price_table"}
                    body["usage"] = usage
                    raw = json.dumps(body)
                if self._events_path:
                    with _EVENTS_LOCK, open(self._events_path, "a") as handle:
                        record = {"returncode": 0 if status == 200 else status, "stdout": raw, "stderr": stderr, **event_fields(t_start)}
                        handle.write(json.dumps(record, sort_keys=True) + "\n")
                try:
                    if cost is not None:
                        self.guard.add(cost)
                except CLIFailure:
                    if status not in (401, 403):
                        raise
                if status in (401, 403):
                    raise CLIFailure("auth_or_quota", f"HTTP {status}: {raw[-300:]}")
                detail = f"HTTP {status}: {raw[-300:]}"
                # A direct 400 or 404 means our request is wrong and is not retried; only a context-window 400 is the model's failure.
                retry = status == 429 or 500 <= status < 600
                if status == 200:
                    if not isinstance(body, dict):
                        raise CLIFailure("cli_error", "unparsable OpenAI reply")
                    if body.get("error") is not None or body.get("status") == "failed":
                        error_detail = self._scrub(json.dumps(body.get("error")))
                        detail, kind, retry = f"the response failed: {error_detail}", "incomplete_output", True
                    else:
                        if cost is None:
                            raise CLIFailure("incomplete_output", "no valid token usage in the reply")
                        served = body.get("model")
                        if not self._served_model_ok(served):
                            raise CLIFailure("model_mismatch", self._scrub(f"asked for {self.model}, got {served}"))
                        reasoning = body.get("reasoning")
                        effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
                        if effort != self.reasoning_effort:
                            raise CLIFailure(
                                "reasoning_mismatch", self._scrub(f"asked for effort {self.reasoning_effort}, got {effort}"),
                            )
                        try:
                            text = _output_text(body)
                        except (AttributeError, KeyError, TypeError):
                            raise CLIFailure("incomplete_output", "malformed OpenAI output") from None
                        incomplete = body.get("incomplete_details")
                        reason = incomplete.get("reason") if isinstance(incomplete, dict) else None
                        if body.get("status") == "incomplete":
                            kind = "model_format" if reason in MODEL_FORMAT_INCOMPLETE else "incomplete_output"
                            detail, retry = f"the response is incomplete: {self._scrub(str(reason))}", True
                        elif body.get("status") != "completed":
                            detail = f"unexpected response status: {self._scrub(str(body.get('status')))}"
                            kind, retry = "incomplete_output", True
                        elif text.strip():
                            return Invocation(text, {"usage": usage, "model": served}, [])
                        else:
                            detail, kind, retry = "the model returned no text", "model_format", True
                if not retry:
                    raise CLIFailure("context_limit" if is_context_limit_error(status, body) else "cli_error", detail)
            if attempt < len(RETRY_WAITS):
                self._sleep(RETRY_WAITS[attempt])
        raise CLIFailure(kind, detail)

    def complete(self, request: str) -> str:
        invocation = self.invoke_items(*render_items(request))
        if invocation.violations:
            raise VoidEpisode(invocation.violations)
        return reply_to_response(invocation.text, invocation.usage)
