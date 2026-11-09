"""Anthropic Messages API backend with the OpenRouter prompt surface and a price-table spend guard."""

from __future__ import annotations

import json
import os
import time
from decimal import Decimal
from http.client import HTTPException
from urllib import error, request

from .cli import CLIFailure, Invocation, VoidEpisode, count_attempt, event_fields, render_items, reply_to_response
from .openrouter import _EVENTS_LOCK, MAX_TOKENS, RETRY_WAITS, TIMEOUT_SECONDS, SpendGuard, is_context_limit_error

URL = "https://api.anthropic.com/v1/messages"
KEY_ENV = "ANTHROPIC_API_KEY"
KEY_MARK = "[ANTHROPIC_API_KEY]"
ANTHROPIC_VERSION = "2023-06-01"
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# USD per million tokens from the official pricing page (2026-11-08).
# claude-opus-5-5: input 4.00, 5m cache write 5.00, cache read 0.20, output 20.00.
# claude-haiku-5-5:
#   prompt length <= 100,000 tokens: input 0.10, cache write 0.125, cache read 0.01, output 0.50
#   prompt length > 100,000 tokens: input 0.50, cache write 0.625, cache read 0.05, output 2.50
#   prompt length = input + cache_creation + cache_read tokens.
MODEL_PRICES = {
    "claude-opus-5-5": {
        "input": Decimal("4.00"),
        "cache_creation": Decimal("5.00"),
        "cache_read": Decimal("0.20"),
        "output": Decimal("20.00"),
    },
    "claude-haiku-5-5": {
        "low": {
            "input": Decimal("0.10"),
            "cache_creation": Decimal("0.125"),
            "cache_read": Decimal("0.01"),
            "output": Decimal("0.50"),
        },
        "high": {
            "input": Decimal("0.50"),
            "cache_creation": Decimal("0.625"),
            "cache_read": Decimal("0.05"),
            "output": Decimal("2.50"),
        },
    },
}


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def price_cost(usage, model: str) -> float | None:
    """USD for one Anthropic messages response from the model's price table; None if usage or model is unusable."""
    if not isinstance(usage, dict):
        return None
    in_tokens = usage.get("input_tokens")
    out_tokens = usage.get("output_tokens")
    if not (_is_count(in_tokens) and _is_count(out_tokens)):
        return None
    # Missing cache fields count as 0; a non-count value returns None.
    cache_creation = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    if not (_is_count(cache_creation) and _is_count(cache_read)):
        return None

    if model == "claude-opus-5-5":
        prices = MODEL_PRICES["claude-opus-5-5"]
    elif model == "claude-haiku-5-5":
        prompt_length = in_tokens + cache_creation + cache_read
        tier = "low" if prompt_length <= 100_000 else "high"
        prices = MODEL_PRICES["claude-haiku-5-5"][tier]
    else:
        return None

    total = (
        in_tokens * prices["input"]
        + cache_creation * prices["cache_creation"]
        + cache_read * prices["cache_read"]
        + out_tokens * prices["output"]
    ) / Decimal(1_000_000)
    return float(total)


def _output_text(body) -> str:
    """Concatenated text of content blocks with type == 'text'; thinking and redacted_thinking are ignored."""
    content = body.get("content")
    if not isinstance(content, list):
        raise TypeError("content is not a list")
    parts = []
    for block in content:
        if not isinstance(block, dict):
            raise TypeError("content block is not an object")
        block_type = block.get("type")
        if not isinstance(block_type, str):
            raise TypeError("content block type is not a string")
        if block_type in ("thinking", "redacted_thinking"):
            continue
        if block_type == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise TypeError("content block text is not text")
            parts.append(text)
        else:
            raise TypeError(f"unexpected content block type: {block_type}")
    return "".join(parts)


class AnthropicMessagesBackend:
    def __init__(
        self,
        model: str,
        *,
        guard: SpendGuard,
        reasoning_effort: str,
        max_tokens=MAX_TOKENS,
        events_path=None,
        opener=None,
        sleep=None,
        clock=None,
    ):
        key = os.environ.get(KEY_ENV)
        if not key:
            raise CLIFailure("auth_or_quota", "no key in the environment")
        if reasoning_effort not in EFFORTS:
            raise ValueError(f"unknown reasoning effort: {reasoning_effort}")
        if model not in MODEL_PRICES:
            raise ValueError(f"unknown model: {model}")
        self._key = key
        self.cli = "anthropic"
        self.model = model
        self.guard = guard
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self._events_path = events_path
        self._opener = opener or request.urlopen
        self._sleep = sleep or time.sleep
        self._clock = clock or time.time
        self._first_call = True
        self._last_cache_touch: float | None = None

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

    def invoke(self, system: str, prompt: str) -> Invocation:
        return self.invoke_items(system, [{"role": "user", "content": prompt}])

    def invoke_items(self, head: str, items: list[dict]) -> Invocation:
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": head,
            "messages": items,
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": self.reasoning_effort},
            "cache_control": {"type": "ephemeral"},
        }
        detail = "no reply"
        for attempt in range(4):
            # The final failure kind follows the last attempt; model_format means the model's own output failed.
            kind = "cli_error"
            self.guard.add(0)
            req = request.Request(
                URL,
                data=json.dumps(payload).encode(),
                headers={
                    "x-api-key": self._key,
                    "anthropic-version": ANTHROPIC_VERSION,
                    "content-type": "application/json",
                },
            )
            status, raw, stderr = None, None, ""
            count_attempt()
            t_start = self._clock()
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
                cost = price_cost(usage, self.model)
                if cost is not None:
                    # Anthropic reports no cost; the saved body carries the price-table cost that SpendGuard reads on resume.
                    usage = {
                        "cache_creation_input_tokens": 0,
                        "cache_read_input_tokens": 0,
                        **usage,
                        "cost": cost,
                        "cost_source": "price_table",
                    }
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
                    if status not in (401, 402, 403):
                        raise
                if status in (401, 402, 403):
                    raise CLIFailure("auth_or_quota", f"HTTP {status}: {raw[-300:]}")
                detail = f"HTTP {status}: {raw[-300:]}"
                # A direct 400 or 404 means our request is wrong and is not retried; only a context-window 400 is the model's failure.
                retry = status in (429, 529) or (500 <= status < 600)
                if status == 200:
                    if not isinstance(body, dict):
                        raise CLIFailure("cli_error", "unparsable Anthropic reply")
                    if body.get("type") == "error" or body.get("error") is not None:
                        error_detail = self._scrub(json.dumps(body.get("error")))
                        detail, kind, retry = f"the response failed: {error_detail}", "incomplete_output", True
                    else:
                        if cost is None:
                            raise CLIFailure("incomplete_output", "no valid token usage in the reply")
                        served = body.get("model")
                        if served != self.model:
                            raise CLIFailure("model_mismatch", self._scrub(f"asked for {self.model}, got {served}"))
                        input_tokens = usage.get("input_tokens", 0)
                        cache_creation_input_tokens = usage.get("cache_creation_input_tokens", 0)
                        cache_read_input_tokens = usage.get("cache_read_input_tokens", 0)
                        prompt = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
                        is_first = self._first_call
                        self._first_call = False
                        expired = self._last_cache_touch is not None and (t_start - self._last_cache_touch) > 270
                        self._last_cache_touch = t_start
                        if not expired:
                            if cache_read_input_tokens > 0:
                                self.guard.reset_cache_misses()
                            elif not is_first and prompt >= 4096 and cache_read_input_tokens == 0:
                                if self.guard.record_cache_miss() >= 3:
                                    reason = f"3 consecutive cache misses: {usage}"
                                    self.guard.trip(reason)
                                    raise CLIFailure("budget", f"tripped: {reason}")
                        stop_reason = body.get("stop_reason")
                        if stop_reason == "refusal":
                            stop_details = body.get("stop_details")
                            raise CLIFailure("model_refusal", self._scrub(f"model refused: {stop_details}"))
                        if stop_reason == "model_context_window_exceeded":
                            stop_details = body.get("stop_details")
                            detail = f"model context window exceeded: {stop_details}" if stop_details is not None else "model context window exceeded"
                            raise CLIFailure("context_limit", self._scrub(detail))
                        try:
                            text = _output_text(body)
                        except (AttributeError, KeyError, TypeError):
                            raise CLIFailure("incomplete_output", "malformed Anthropic output") from None
                        if stop_reason == "max_tokens":
                            detail, kind, retry = "stop_reason is max_tokens", "model_format", True
                        elif stop_reason in ("end_turn", "stop_sequence"):
                            if text.strip():
                                return Invocation(text, {"usage": usage, "model": served}, [])
                            detail, kind, retry = "the model returned no text", "model_format", True
                        else:
                            detail = f"unexpected stop_reason: {self._scrub(str(stop_reason))}"
                            kind, retry = "incomplete_output", True
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
