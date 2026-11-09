"""OpenRouter text backend using the same prompt surface as the CLI backends."""

from __future__ import annotations

import json
import math
import os
import re
import threading
import time
from decimal import Decimal
from http.client import HTTPException
from pathlib import Path
from urllib import error, request

from .cli import CLIFailure, Invocation, VoidEpisode, count_attempt, event_fields, render_items, reply_to_response

URL = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_SECONDS = 600
RETRY_WAITS = (5, 20, 60)
# Reasoning counts towards the limit; 8192 cut off 3 of 9 trial episodes before they replied.
MAX_TOKENS = 32000
# Runs launched in one process share an events file.
_EVENTS_LOCK = threading.Lock()
# Provider wordings for a request past the model's context window (OpenRouter's own, and upstream ones in metadata.raw).
CONTEXT_LIMIT_TEXT = re.compile(
    r"maximum context length|combined input and output tokens|prompt is too long|input is too long"
    r"|exceeds? the context|context (?:window|length) (?:is |was |has been )?exceeded",
    re.IGNORECASE,
)
# Explicit Gemini caching (openrouter.ai/docs/features/prompt-caching): a cache_control breakpoint on a content part,
# of which Gemini uses only the last; a write needs at least 4096 tokens and lives 5 minutes without refresh.
CACHE_MIN_TOKENS = 4096
CACHE_EXPIRE_SECONDS = 290


def _chars(items: list[dict]) -> int:
    return sum(len(item["content"]) for item in items)


def is_model_format_error(choice_error) -> bool:
    """A choice-level 4xx (not 429) rejects what the model wrote; 429, 5xx or no code is the provider's failure."""
    code = choice_error.get("code") if isinstance(choice_error, dict) else None
    return isinstance(code, int) and not isinstance(code, bool) and 400 <= code < 500 and code != 429


def is_context_limit_error(status, body) -> bool:
    """A 400 saying the conversation outgrew the model's context window; the model's own tool calls grew it.
    OpenRouter can also send this error with code 400 inside an HTTP 200 body."""
    error_body = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error_body, dict) or not (status == 400 or (status == 200 and error_body.get("code") == 400)):
        return False
    metadata = error_body.get("metadata")
    if "context_length_exceeded" in (error_body.get("code"), metadata.get("error_type") if isinstance(metadata, dict) else None):
        return True
    texts = (error_body.get("message"), metadata.get("raw") if isinstance(metadata, dict) else None)
    return any(isinstance(text, str) and CONTEXT_LIMIT_TEXT.search(text) for text in texts)


class SpendGuard:
    """One run's spend, including saved attempts from earlier launches."""

    def __init__(self, cap_usd: float, output_dir=None):
        if not math.isfinite(cap_usd) or cap_usd <= 0:
            raise ValueError("spend cap must be a positive finite number")
        self.cap_usd = cap_usd
        self._cap = Decimal(str(cap_usd))
        self._total = Decimal(0)
        self._lock = threading.Lock()
        self._tripped_reason: str | None = None
        self._consecutive_misses = 0
        if output_dir is not None:
            for path in Path(output_dir).rglob("events.jsonl"):
                for line in path.read_text().splitlines():
                    record = json.loads(line)
                    try:
                        body = json.loads(record["stdout"])
                    except ValueError:
                        continue
                    if isinstance(body, dict) and isinstance(body.get("usage"), dict):
                        cost = body["usage"].get("cost")
                        if cost is not None:
                            self._total += self._cost(cost)

    @staticmethod
    def _cost(cost) -> Decimal:
        if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost < 0:
            raise CLIFailure("incomplete_output", "no valid usage.cost in the reply")
        return Decimal(str(cost))

    @property
    def total(self) -> float:
        with self._lock:
            return float(self._total)

    @property
    def consecutive_misses(self) -> int:
        with self._lock:
            return self._consecutive_misses

    @property
    def tripped_reason(self) -> str | None:
        with self._lock:
            return self._tripped_reason

    @property
    def tripped(self) -> bool:
        with self._lock:
            return self._tripped_reason is not None

    def trip(self, reason: str) -> None:
        with self._lock:
            if self._tripped_reason is None:
                self._tripped_reason = str(reason)

    def reset_cache_misses(self) -> None:
        with self._lock:
            self._consecutive_misses = 0

    def record_cache_miss(self) -> int:
        with self._lock:
            self._consecutive_misses += 1
            return self._consecutive_misses

    def add(self, cost) -> None:
        with self._lock:
            if self._tripped_reason is not None:
                raise CLIFailure("budget", f"tripped: {self._tripped_reason}")
            cost = self._cost(cost)
            self._total += cost
            if self._total >= self._cap:
                raise CLIFailure("budget", f"spend {self._total:.6f} USD reached cap {self.cap_usd:.6f} USD")


class OpenRouterBackend:
    def __init__(self, model: str, *, guard: SpendGuard, events_path=None, opener=None, sleep=None, max_tokens=MAX_TOKENS, extra=None, provider=None,
                 cache=False, clock=None):
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key:
            raise CLIFailure("auth_or_quota", "no key in the environment")
        self._key = key
        self.cli = "openrouter"
        self.model = model
        self.guard = guard
        self.max_tokens = max_tokens
        self.extra = extra or {}
        self.provider = provider
        self._events_path = events_path
        self._opener = opener or request.urlopen
        self._sleep = sleep or time.sleep
        self.cache = cache
        self._clock = clock or time.monotonic
        # evaluate_task builds one backend per episode and its turns run in sequence, so this needs no lock:
        # (index, head, items up to and including the marked one, clock reading when it was placed).
        self._breakpoint = None

    def _scrub(self, text: str) -> str:
        # Scrub both raw text and escaped JSON strings before saving or parsing them.
        return text.replace(self._key, "[OPENROUTER_API_KEY]").replace(
            json.dumps(self._key)[1:-1], "[OPENROUTER_API_KEY]",
        )

    def _scrub_body(self, raw: str) -> str:
        # A JSON body can spell the key with \\u escapes that a text replace misses.
        raw = self._scrub(raw)
        try:
            decoded = json.dumps(json.loads(raw), ensure_ascii=False)
        except ValueError:
            return raw
        return self._scrub(decoded) if self._key in decoded else raw

    def _mark(self, head: str, items: list[dict]) -> tuple[list[dict], int | None]:
        """The items to send, with the cache breakpoint on a copy of one item, and that item's index or None.

        Tokens are estimated as characters / 4, over the whole prompt for the write bound and over the items after the
        breakpoint for the suffix bound, as gemini_generate does.
        """
        from .gemini_generate import RECACHE_TOKENS

        mark = self._breakpoint
        if mark is not None:
            index, mark_head, prefix, placed = mark
            if (
                mark_head != head
                or items[: index + 1] != prefix
                or _chars(items[index + 1:]) / 4 > RECACHE_TOKENS
                or self._clock() - placed > CACHE_EXPIRE_SECONDS
            ):
                mark = None
        if mark is None and (len(head) + _chars(items)) // 4 >= CACHE_MIN_TOKENS:
            mark = (len(items) - 1, head, list(items), self._clock())
        self._breakpoint = mark
        if mark is None:
            return items, None
        index = mark[0]
        item = items[index]
        marked = {**item, "content": [{"type": "text", "text": item["content"], "cache_control": {"type": "ephemeral"}}]}
        return [*items[:index], marked, *items[index + 1:]], index

    def invoke(self, system: str, prompt: str) -> Invocation:
        return self.invoke_items(system, [{"role": "user", "content": prompt}])

    def invoke_items(self, head: str, items: list[dict]) -> Invocation:
        cache_index = None
        if self.cache:
            items, cache_index = self._mark(head, items)
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": head}, *items],
            "max_tokens": self.max_tokens,
            "usage": {"include": True},
            **self.extra,
        }
        if self.provider is not None:
            payload["provider"] = {"order": [self.provider], "allow_fallbacks": False, "require_parameters": True}
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
                if self._events_path:
                    with _EVENTS_LOCK, open(self._events_path, "a") as handle:
                        record = {"returncode": 0 if status == 200 else status, "stdout": raw, "stderr": stderr, **event_fields(t_start)}
                        if self.cache:
                            record["cache_breakpoint"] = cache_index
                        handle.write(json.dumps(record, sort_keys=True) + "\n")
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                usage = body.get("usage") if isinstance(body, dict) else None
                try:
                    if isinstance(usage, dict) and usage.get("cost") is not None:
                        self.guard.add(usage["cost"])
                except CLIFailure:
                    if status not in (401, 402, 403):
                        raise
                if status in (401, 402, 403):
                    raise CLIFailure("auth_or_quota", f"HTTP {status}: {raw[-300:]}")
                detail = f"HTTP {status}: {raw[-300:]}"
                retry = status == 429 or 500 <= status < 600
                if status == 200:
                    if is_context_limit_error(status, body):
                        raise CLIFailure("context_limit", detail)
                    retry = isinstance(body, dict) and ("error" in body or not body.get("choices"))
                    if not retry:
                        if not isinstance(body, dict):
                            raise CLIFailure("cli_error", "unparsable OpenRouter reply")
                        if not isinstance(usage, dict) or usage.get("cost") is None:
                            raise CLIFailure("incomplete_output", "no usage.cost in the reply")
                        if body.get("model") != self.model:
                            raise CLIFailure("model_mismatch", self._scrub(f"asked for {self.model}, got {body.get('model')}"))
                        if self.provider is not None and body.get("provider") != self.provider:
                            raise CLIFailure("provider_mismatch", self._scrub(f"asked for {self.provider}, got {body.get('provider')}"))
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
                                # Some providers parse the model's text into this field; no tool exists on our side.
                                function = calls[0]["function"]
                                text = json.dumps({"name": function["name"], "arguments": json.loads(function["arguments"])})
                            elif calls:
                                text = ""
                        except (AttributeError, KeyError, IndexError, TypeError, ValueError):
                            raise CLIFailure("incomplete_output", "malformed OpenRouter choice") from None
                        if not isinstance(text, str):
                            raise CLIFailure("incomplete_output", "reply content is not text")
                        content = message.get("content")
                        # Like gemini_generate.TEXT_FINISH: Gemini ends most JSON-action replies as MALFORMED_FUNCTION_CALL
                        # with the full text, which OpenRouter reports as finish_reason "error"; that text is the reply.
                        malformed_call_text = (
                            finish_reason == "error" and choice.get("native_finish_reason") == "MALFORMED_FUNCTION_CALL"
                            and not choice.get("error") and isinstance(content, str) and content.strip() != "" and text.strip() != ""
                        )
                        if malformed_call_text:
                            return Invocation(text, {"usage": usage}, violations)
                        if finish_reason == "error" or choice.get("error"):
                            # A mid-generation provider failure; its partial text is not a reply.
                            error_detail = self._scrub(json.dumps(choice.get("error")))
                            kind = "model_format" if is_model_format_error(choice.get("error")) else "incomplete_output"
                            detail, retry = f"the reply ended in an error: {error_detail}", True
                        elif finish_reason == "length":
                            detail, kind, retry = "finish_reason is length", "model_format", True
                        elif text.strip():
                            return Invocation(text, {"usage": usage}, violations)
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
