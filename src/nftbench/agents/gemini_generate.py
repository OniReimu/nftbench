"""Gemini generateContent API backend with the OpenRouter prompt surface and a price-table spend guard."""

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

URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Explicit context caching (aistudio.google.com/docs/caching?generate-content=true and aistudio.google.com/api-reference/caching):
# create is POST cachedContents, refresh is PATCH {name} with a new ttl (only the expiration is updatable), delete is DELETE {name}.
CACHE_URL = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
CACHE_ITEM_URL = "https://generativelanguage.googleapis.com/v1beta/{name}"
CACHE_TTL_SECONDS = 300
CACHE_REFRESH_SECONDS = 120
# A new cache is made when the uncached suffix is estimated above this many tokens.
RECACHE_TOKENS = 16000
# No cache below the request size at which gemini-3.8-flash caches implicitly (docs/caching).
CACHE_MIN_TOKENS = 4096
# A 400 or 404 about the cache, sent with cachedContent, means the cache is gone; the turn is resent in full once.
CACHE_GONE_TEXT = re.compile(r"cache", re.IGNORECASE)
KEY_ENV = "GOOGLE_API_KEY"
KEY_MARK = "[GOOGLE_API_KEY]"
# Thinking levels the model page lists for gemini-3.8-flash; "minimal" returns an error there.
EFFORTS = ("low", "medium", "high")
ROLES = {"user": "user", "assistant": "model"}
# Finish reasons and prompt blocks that end the reply on a content policy; they are the model's refusal.
REFUSAL_FINISH = ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII")
# Finish reasons whose text is the model's reply. With no tools declared, gemini-3.8-flash still ends most replies
# that are a JSON tool call as MALFORMED_FUNCTION_CALL while returning the full text (probe of 2026-11-08: 5 of 6);
# the loop parses that text like any other model's.
TEXT_FINISH = ("STOP", "MALFORMED_FUNCTION_CALL")
# Per-item margin for role and turn markers in the byte bound charged when a cache create reply is lost.
CACHE_BOUND_ITEM_TOKENS = 16
# Google's wording for a request past the context window, which the OpenRouter patterns do not cover.
CONTEXT_LIMIT_TEXT = re.compile(r"input token count.*exceeds the maximum number of tokens", re.IGNORECASE)
# A 400 about the key is an authentication failure, not a malformed request.
KEY_ERROR_TEXT = re.compile(r"API[ _]key", re.IGNORECASE)

# USD per million tokens from the official pricing page, Standard paid tier, valid through 2026-12-31 (2026-11-08).
# gemini-3.8-flash: input 0.75, context caching 0.075, output (including thinking tokens) 3.75, and context caching
# storage 0.50 per million tokens per hour (aistudio.google.com/docs/pricing). Creating an explicit cache is billed as
# input for its totalTokenCount.
MODEL_PRICES = {
    "gemini-3.8-flash": {
        "input": Decimal("0.75"), "cached": Decimal("0.075"), "output": Decimal("3.75"), "storage_hour": Decimal("0.50"),
    },
}


def _is_count(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def price_cost(usage, model: str) -> float | None:
    """USD for one generateContent response from the model's price table; None if usage or model is unusable."""
    if not isinstance(usage, dict) or model not in MODEL_PRICES:
        return None
    prompt = usage.get("promptTokenCount")
    if not _is_count(prompt):
        return None
    # The API leaves out zero counts; a non-count value returns None.
    cached = usage.get("cachedContentTokenCount", 0)
    candidates = usage.get("candidatesTokenCount", 0)
    thoughts = usage.get("thoughtsTokenCount", 0)
    if not (_is_count(cached) and _is_count(candidates) and _is_count(thoughts)) or cached > prompt:
        return None
    prices = MODEL_PRICES[model]
    # promptTokenCount includes the cached tokens; thinking tokens are billed as output.
    total = (
        (prompt - cached) * prices["input"] + cached * prices["cached"] + (candidates + thoughts) * prices["output"]
    ) / Decimal(1_000_000)
    return float(total)


def _chars(contents) -> int:
    return sum(len(part["text"]) for item in contents for part in item["parts"])


def is_gemini_context_limit_error(status, body) -> bool:
    if is_context_limit_error(status, body):
        return True
    error_body = body.get("error") if isinstance(body, dict) else None
    message = error_body.get("message") if isinstance(error_body, dict) else None
    return status == 400 and isinstance(message, str) and CONTEXT_LIMIT_TEXT.search(message) is not None


def _output_text(candidate) -> str:
    """Concatenated text of the candidate's parts; thought parts are ignored."""
    content = candidate.get("content")
    if not isinstance(content, dict):
        raise TypeError("candidate content is not an object")
    parts = content.get("parts", [])
    if not isinstance(parts, list):
        raise TypeError("content parts is not a list")
    texts, calls = [], []
    for part in parts:
        if not isinstance(part, dict):
            raise TypeError("content part is not an object")
        if part.get("thought") is True:
            continue
        if "functionCall" in part:
            calls.append(part["functionCall"])
            continue
        text = part.get("text")
        if not isinstance(text, str):
            raise TypeError("content part text is not text")
        texts.append(text)
    # Like openrouter.py: with no tools declared the API still parses some replies into a functionCall part; one call
    # becomes the equivalent {"name", "arguments"} reply, and several leave no reply.
    if len(calls) == 1:
        call = calls[0]
        if not isinstance(call, dict) or not isinstance(call.get("name"), str):
            raise TypeError("functionCall is not a named call")
        return json.dumps({"name": call["name"], "arguments": call.get("args", {})})
    if calls:
        return ""
    return "".join(texts)


class GeminiGenerateBackend:
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
        self.cli = "gemini"
        self.model = model
        self.guard = guard
        self.reasoning_effort = reasoning_effort
        self.max_tokens = max_tokens
        self._events_path = events_path
        self._opener = opener or request.urlopen
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self._first_call = True
        # One backend per episode; at most one live cache: {"name", "head", "items", "tokens", "anchor"}. anchor is a
        # clock reading taken before the create or last refresh was sent, so the cache lives at least TTL past it.
        self._cache = None
        # The last replied request's head, contents and promptTokenCount, for estimating the next request's size.
        self._last = None

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
        # modelVersion names the model that answered; a numbered version of the same model is the same family.
        return isinstance(served, str) and (served == self.model or re.fullmatch(re.escape(self.model) + r"-\d+", served) is not None)

    def _cache_request(self, method: str, url: str, data=None):
        """One cachedContents call: (status, scrubbed body or None, stderr, error detail, t_start)."""
        req = request.Request(
            url,
            data=None if data is None else json.dumps(data).encode(),
            headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
            method=method,
        )
        status, raw, stderr, detail = None, None, "", ""
        t_start = time.time()
        try:
            with self._opener(req, timeout=TIMEOUT_SECONDS) as response:
                status = response.status
                raw = response.read().decode("utf-8", errors="replace")
        except error.HTTPError as exc:
            status = exc.code
            stderr = self._scrub(str(exc))
            try:
                raw = exc.read().decode("utf-8", errors="replace")
            except (OSError, HTTPException, ValueError):
                raw = ""
            exc.close()
        except (error.URLError, OSError, HTTPException, ValueError) as exc:
            detail = self._scrub(str(exc))
        return status, None if raw is None else self._scrub_body(raw), stderr, detail, t_start

    def _cache_event(self, kind, status, raw, stderr, t_start, cost=None, extra=None) -> None:
        """An events.jsonl line for a cache call; a charged call's body carries usage.cost for SpendGuard's resume."""
        stdout = raw or ""
        if cost is not None:
            try:
                body = json.loads(raw)
            except (TypeError, ValueError):
                body = None
            if not isinstance(body, dict):
                body = {"raw": raw}
            body["usage"] = {"cost": cost, "cost_source": "price_table", **(extra or {})}
            stdout = json.dumps(body)
        if self._events_path:
            with _EVENTS_LOCK, open(self._events_path, "a") as handle:
                record = {"kind": kind, "returncode": 0 if status == 200 else status, "stdout": stdout, "stderr": stderr, **event_fields(t_start)}
                handle.write(json.dumps(record, sort_keys=True) + "\n")

    def _create_cache(self, head: str, items: list[dict]):
        """A new cache over the system instruction and items, or None if the call failed."""
        self.guard.add(0)
        anchor = self._clock()
        data = {
            "model": f"models/{self.model}",
            "systemInstruction": {"parts": [{"text": head}]},
            "contents": items,
            "ttl": f"{CACHE_TTL_SECONDS}s",
        }
        status, raw, stderr, detail, t_start = self._cache_request("POST", CACHE_URL, data)
        if status is None:
            # The server may have created the cache before the connection failed. Its size is unknown, so charge as if
            # every UTF-8 byte were a token (a byte-fallback tokenizer emits at most one token per byte), plus a margin
            # for each item's role markers, and drop the reference; the cache expires at its TTL.
            texts = [head] + [part["text"] for item in items for part in item["parts"]]
            tokens = sum(len(text.encode("utf-8")) for text in texts) + CACHE_BOUND_ITEM_TOKENS * len(texts)
            cost, extra = self._create_cost(tokens)
            self._cache_event("cache_create", status, json.dumps({"error": detail}), stderr, t_start, cost,
                              {**extra, "cache_tokens_bound": "utf8_bytes"})
            self.guard.add(cost)
            return None
        if status != 200:
            self._cache_event("cache_create", status, raw, stderr, t_start)
            return None
        if raw is None:
            # The server answered but the body was lost, so the cache's size and cost are unknown; stop the run.
            self.guard.trip(f"cache create HTTP 200 reply could not be read: {detail}")
            raise CLIFailure("incomplete_output", f"cache create HTTP 200 reply could not be read: {detail}")
        try:
            body = json.loads(raw)
        except ValueError:
            body = None
        usage = body.get("usageMetadata") if isinstance(body, dict) else None
        tokens = usage.get("totalTokenCount") if isinstance(usage, dict) else None
        if not _is_count(tokens):
            self._cache_event("cache_create", status, raw, stderr, t_start)
            self.guard.trip("cache create HTTP 200 reply without valid token usage, cost unknown")
            raise CLIFailure("incomplete_output", "no valid token usage in the cache create reply")
        cost, extra = self._create_cost(tokens)
        self._cache_event("cache_create", status, raw, stderr, t_start, cost, extra)
        self.guard.add(cost)
        name = body.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"cachedContents/[A-Za-z0-9_-]+", name):
            return None
        return {"name": name, "head": head, "items": list(items), "tokens": tokens, "anchor": anchor}

    def _create_cost(self, tokens: int):
        """Creation is billed as input; storage is charged up front for the whole TTL."""
        prices = MODEL_PRICES[self.model]
        input_cost = tokens * prices["input"] / Decimal(1_000_000)
        storage_cost = tokens * prices["storage_hour"] * CACHE_TTL_SECONDS / Decimal(3600) / Decimal(1_000_000)
        extra = {
            "cache_tokens": tokens, "cache_input_cost": float(input_cost),
            "cache_storage_cost": float(storage_cost), "cache_storage_seconds": CACHE_TTL_SECONDS,
        }
        return float(input_cost + storage_cost), extra

    def _refresh_cache(self, cache: dict) -> bool:
        """Set the cache's TTL back to CACHE_TTL_SECONDS; False if the call failed."""
        self.guard.add(0)
        anchor = self._clock()
        status, raw, stderr, detail, t_start = self._cache_request(
            "PATCH", CACHE_ITEM_URL.format(name=cache["name"]), {"ttl": f"{CACHE_TTL_SECONDS}s"},
        )
        if status is not None and status != 200:
            self._cache_event("cache_storage", status, raw, stderr, t_start)
            return False
        # The cache lived at least TTL past the old anchor and now lives TTL past a moment before this reply, so the
        # extension bought is at most the time from the old anchor to now; charging that never undercounts storage.
        # A lost connection may still have applied the PATCH, so it is charged the same and the cache is then dropped.
        seconds = Decimal(str(max(self._clock() - cache["anchor"], 0)))
        cost = float(cache["tokens"] * MODEL_PRICES[self.model]["storage_hour"] * seconds / Decimal(3600) / Decimal(1_000_000))
        extra = {"cache_tokens": cache["tokens"], "cache_storage_cost": cost, "cache_storage_seconds": float(seconds)}
        if status is None:
            raw = json.dumps({"error": detail})
        self._cache_event("cache_storage", status, raw, stderr, t_start, cost, extra)
        self.guard.add(cost)
        if status is None:
            return False
        cache["anchor"] = anchor
        return True

    def _delete_cache(self, name: str) -> None:
        """Best effort; a cache left behind expires at its TTL, whose storage is already charged."""
        status, raw, stderr, _, t_start = self._cache_request("DELETE", CACHE_ITEM_URL.format(name=name))
        if status is not None:
            self._cache_event("cache_delete", status, raw, stderr, t_start)

    def _estimate(self, head: str, contents: list[dict]) -> int:
        """Rough prompt tokens: the last reply's promptTokenCount plus chars/4 of the items added since."""
        if self._last is not None:
            last_head, last_contents, last_prompt = self._last
            k = len(last_contents)
            if last_head == head and contents[:k] == last_contents:
                return last_prompt + _chars(contents[k:]) // 4
        return (len(head) + _chars(contents)) // 4

    def _cached_payload(self, head: str, contents: list[dict], config: dict):
        """The request that uses the cache, or None to send the full request; the model sees the same input either way."""
        cache = self._cache
        n = len(contents)
        if cache is not None:
            k = len(cache["items"])
            if cache["head"] != head or k >= n or contents[:k] != cache["items"]:
                # The cache no longer is a prefix of this request.
                self._cache = None
                self._delete_cache(cache["name"])
                cache = None
        if cache is None or _chars(contents[len(cache["items"]):]) / 4 > RECACHE_TOKENS:
            if self._estimate(head, contents) >= CACHE_MIN_TOKENS:
                old = cache
                # The newest item stays uncached.
                cache = self._create_cache(head, contents[: n - 1])
                self._cache = cache
                if old is not None:
                    self._delete_cache(old["name"])
        elif self._clock() - cache["anchor"] > CACHE_REFRESH_SECONDS:
            if not self._refresh_cache(cache):
                self._cache = cache = None
        if cache is None:
            return None
        # With an explicit cache the system instruction lives in the cache (docs/caching examples send only contents).
        return {"cachedContent": cache["name"], "contents": contents[len(cache["items"]):], "generationConfig": config}

    def invoke(self, system: str, prompt: str) -> Invocation:
        return self.invoke_items(system, [{"role": "user", "content": prompt}])

    def invoke_items(self, head: str, items: list[dict]) -> Invocation:
        contents = [{"role": ROLES[item["role"]], "parts": [{"text": item["content"]}]} for item in items]
        config = {
            "maxOutputTokens": self.max_tokens,
            "thinkingConfig": {"thinkingLevel": self.reasoning_effort.upper()},
        }
        full = {"systemInstruction": {"parts": [{"text": head}]}, "contents": contents, "generationConfig": config}
        payload = self._cached_payload(head, contents, config) or full
        detail = "no reply"
        attempt = 0
        while attempt < 4:
            # The final failure kind follows the last attempt; model_format means the model's own output failed.
            kind = "cli_error"
            self.guard.add(0)
            # The key travels in the header only, never in the URL.
            req = request.Request(
                URL.format(model=self.model),
                data=json.dumps(payload).encode(),
                headers={"x-goog-api-key": self._key, "Content-Type": "application/json"},
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
                if status is not None:
                    # The server answered but the body was lost, so this call's cost is unknown; stop the run.
                    self.guard.trip(f"HTTP {status} reply could not be read: {detail}")
                    raise CLIFailure("incomplete_output", f"HTTP {status} reply could not be read: {detail}")
            except ValueError as exc:
                raise CLIFailure("cli_error", self._scrub(str(exc))) from None

            if raw is not None:
                raw = self._scrub_body(raw)
                try:
                    body = json.loads(raw)
                except ValueError:
                    body = None
                usage = body.get("usageMetadata") if isinstance(body, dict) else None
                cost = price_cost(usage, self.model)
                if cost is not None:
                    # Gemini reports no cost; the saved body carries the price-table cost that SpendGuard reads on resume.
                    # usageMetadata stays as sent; usage repeats its counts beside the cost, plus the names metrics reads.
                    thoughts = usage.get("thoughtsTokenCount", 0)
                    usage = {
                        **usage, "cost": cost, "cost_source": "price_table",
                        "input_tokens": usage["promptTokenCount"],
                        "input_tokens_details": {"cached_tokens": usage.get("cachedContentTokenCount", 0)},
                        "output_tokens": usage.get("candidatesTokenCount", 0) + thoughts,
                        "output_tokens_details": {"reasoning_tokens": thoughts},
                    }
                    body["usage"] = usage
                    raw = json.dumps(body)
                if self._events_path:
                    with _EVENTS_LOCK, open(self._events_path, "a") as handle:
                        record = {"returncode": 0 if status == 200 else status, "stdout": raw, "stderr": stderr, **event_fields(t_start)}
                        handle.write(json.dumps(record, sort_keys=True) + "\n")
                auth = status in (401, 402, 403) or (status == 400 and KEY_ERROR_TEXT.search(raw) is not None)
                try:
                    if cost is not None:
                        self.guard.add(cost)
                except CLIFailure:
                    if not auth:
                        raise
                if auth:
                    raise CLIFailure("auth_or_quota", f"HTTP {status}: {raw[-300:]}")
                if "cachedContent" in payload and status in (400, 404) and CACHE_GONE_TEXT.search(raw):
                    # The cache expired or is missing: drop it and resend this turn in full, without using an attempt.
                    self._cache = None
                    payload = full
                    continue
                detail = f"HTTP {status}: {raw[-300:]}"
                # A direct 400 or 404 means our request is wrong and is not retried; only a context-window 400 is the model's failure.
                retry = status == 429 or 500 <= status < 600
                if status == 200:
                    if not isinstance(body, dict):
                        # A paid 200 whose cost cannot be counted stops the run.
                        self.guard.trip("unparsable HTTP 200 reply, cost unknown")
                        raise CLIFailure("cli_error", "unparsable Gemini reply")
                    if body.get("error") is not None:
                        error_detail = self._scrub(json.dumps(body.get("error")))
                        detail, kind, retry = f"the response failed: {error_detail}", "incomplete_output", True
                    else:
                        if cost is None:
                            self.guard.trip("HTTP 200 reply without valid token usage, cost unknown")
                            raise CLIFailure("incomplete_output", "no valid token usage in the reply")
                        served = body.get("modelVersion")
                        if not self._served_model_ok(served):
                            raise CLIFailure("model_mismatch", self._scrub(f"asked for {self.model}, got {served}"))
                        prompt = usage["promptTokenCount"]
                        cached = usage.get("cachedContentTokenCount", 0)
                        self._last = (head, contents, prompt)
                        is_first = self._first_call
                        self._first_call = False
                        if cached > 0:
                            self.guard.reset_cache_misses()
                        elif "cachedContent" in payload or (not is_first and prompt >= 4096):
                            if self.guard.record_cache_miss() >= 3:
                                reason = f"3 consecutive cache misses: {usage}"
                                self.guard.trip(reason)
                                raise CLIFailure("budget", f"tripped: {reason}")
                        feedback = body.get("promptFeedback")
                        block_reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
                        if block_reason:
                            raise CLIFailure("model_refusal", self._scrub(f"prompt blocked: {block_reason}"))
                        candidates = body.get("candidates")
                        if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
                            raise CLIFailure("incomplete_output", "malformed Gemini output")
                        finish_reason = candidates[0].get("finishReason")
                        if finish_reason in REFUSAL_FINISH:
                            raise CLIFailure("model_refusal", self._scrub(f"model refused: finishReason {finish_reason}"))
                        if finish_reason == "MAX_TOKENS":
                            detail, kind, retry = "finishReason is MAX_TOKENS", "model_format", True
                        elif finish_reason in TEXT_FINISH:
                            try:
                                text = _output_text(candidates[0])
                            except (AttributeError, KeyError, TypeError):
                                raise CLIFailure("incomplete_output", "malformed Gemini output") from None
                            if text.strip():
                                return Invocation(text, {"usage": usage, "model": served}, [])
                            detail, kind, retry = "the model returned no text", "model_format", True
                        else:
                            detail = f"unexpected finishReason: {self._scrub(str(finish_reason))}"
                            kind, retry = "incomplete_output", True
                if not retry:
                    raise CLIFailure("context_limit" if is_gemini_context_limit_error(status, body) else "cli_error", detail)
            if attempt < len(RETRY_WAITS):
                self._sleep(RETRY_WAITS[attempt])
            attempt += 1
        raise CLIFailure(kind, detail)

    def complete(self, request: str) -> str:
        invocation = self.invoke_items(*render_items(request))
        if invocation.violations:
            raise VoidEpisode(invocation.violations)
        return reply_to_response(invocation.text, invocation.usage)
