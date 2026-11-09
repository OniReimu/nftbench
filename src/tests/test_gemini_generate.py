"""The Gemini generateContent backend and its evaluate flags. Every HTTP reply here is local fake data."""

from __future__ import annotations

import http.client as http_client
import io
import json
import os
from pathlib import Path
from urllib import error as url_error

import pytest

from nftbench import evaluate
from nftbench.agents.cli import CLIFailure, render_items
from nftbench.agents.gemini_generate import EFFORTS, GeminiGenerateBackend, price_cost
from nftbench.agents.openrouter import SpendGuard

MODEL = "gemini-3.8-flash"
KEY = "test-only-google-key-do-not-leak"
URL = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
FINISH = json.dumps({"name": "finish_task", "arguments": {"summary": "done"}})
INFEASIBLE = json.dumps({"name": "declare_infeasible", "arguments": {"reason": "mock infeasible"}})
USAGE = {
    "promptTokenCount": 1000,
    "cachedContentTokenCount": 200,
    "candidatesTokenCount": 100,
    "thoughtsTokenCount": 300,
    "totalTokenCount": 1400,
}
# (800 * 0.75 + 200 * 0.075 + (100 + 300) * 3.75) / 1e6 = (600 + 15 + 1500) / 1e6
COST = 0.002115
USAGE_MISS = {"promptTokenCount": 5000, "candidatesTokenCount": 100, "totalTokenCount": 5100}
USAGE_HIT = {"promptTokenCount": 6000, "cachedContentTokenCount": 4000, "candidatesTokenCount": 100, "totalTokenCount": 6100}


def body(text=FINISH, *, model=MODEL, finish="STOP", usage="default", thought="thinking summary", block=None):
    parts = []
    if thought is not None:
        parts.append({"text": thought, "thought": True})
    if text is not None:
        parts.append({"text": text})
    reply = {
        "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": finish, "index": 0}],
        "usageMetadata": USAGE if usage == "default" else usage,
        "modelVersion": model,
        "responseId": "resp-01",
    }
    if block is not None:
        reply["candidates"] = []
        reply["promptFeedback"] = {"blockReason": block}
    return reply


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req, *, timeout):
        self.calls.append((req, timeout))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        status, data = reply
        raw = json.dumps(data) if not isinstance(data, str) else data
        if status >= 400:
            raise url_error.HTTPError(req.full_url, status, "fake HTTP error", {}, io.BytesIO(raw.encode()))
        response = io.BytesIO(raw.encode())
        response.status = status
        return response


def make_backend(tmp_path, monkeypatch, replies, *, cap=10, guard=None, model=MODEL, effort="high"):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    fake = FakeOpener(replies)
    waits = []
    backend = GeminiGenerateBackend(
        model, guard=guard or SpendGuard(cap), reasoning_effort=effort,
        events_path=tmp_path / "events.jsonl", opener=fake, sleep=waits.append,
    )
    return backend, fake, waits


def google_error(code, message, status):
    return {"error": {"code": code, "message": message, "status": status}}


def test_request_body_shape(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body())], effort="medium")
    request_json = json.dumps({
        "messages": [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User question"},
            {"role": "assistant", "content": "Assistant answer"},
            {"role": "tool", "content": "Tool result"},
        ],
        "tools": [{"name": "finish_task"}],
    })
    result = json.loads(backend.complete(request_json))
    req, timeout = fake.calls[0]
    head, items = render_items(request_json)
    assert req.full_url == URL and timeout == 600
    assert KEY not in req.full_url and "key=" not in req.full_url
    assert req.get_header("X-goog-api-key") == KEY
    assert req.get_header("Content-type") == "application/json"
    assert req.get_header("Authorization") is None
    sent = json.loads(req.data)
    assert sent == {
        "systemInstruction": {"parts": [{"text": head}]},
        "contents": [
            {"role": "user", "parts": [{"text": items[0]["content"]}]},
            {"role": "model", "parts": [{"text": items[1]["content"]}]},
            {"role": "user", "parts": [{"text": items[2]["content"]}]},
        ],
        "generationConfig": {"maxOutputTokens": 32000, "thinkingConfig": {"thinkingLevel": "MEDIUM"}},
    }
    config = sent["generationConfig"]
    for forbidden in ("temperature", "topP", "topK", "seed", "candidateCount", "stopSequences"):
        assert forbidden not in config
    for forbidden in ("tools", "toolConfig", "safetySettings", "cachedContent", "model"):
        assert forbidden not in sent
    assert result["tool_calls"][0]["name"] == "finish_task"
    assert result["usage"]["model"] == MODEL
    assert result["usage"]["usage"]["cost"] == pytest.approx(COST)
    assert waits == []


def test_efforts_are_the_documented_levels():
    assert EFFORTS == ("low", "medium", "high")


@pytest.mark.parametrize("effort", ["minimal", "max", "xhigh", "none"])
def test_unknown_effort_raises_at_construction(monkeypatch, effort):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    with pytest.raises(ValueError):
        GeminiGenerateBackend(MODEL, guard=SpendGuard(10), reasoning_effort=effort)


def test_unknown_model_raises_at_construction(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    with pytest.raises(ValueError) as exc:
        GeminiGenerateBackend("gemini-9-ultra", guard=SpendGuard(10), reasoning_effort="high")
    assert "unknown model: gemini-9-ultra" in str(exc.value)


def test_missing_key_is_auth_or_quota(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(CLIFailure) as exc:
        GeminiGenerateBackend(MODEL, guard=SpendGuard(10), reasoning_effort="high")
    assert exc.value.kind == "auth_or_quota"


def test_thought_parts_are_ignored(tmp_path, monkeypatch):
    reply = body()
    reply["candidates"][0]["content"]["parts"] = [
        {"text": "Internal thought", "thought": True},
        {"text": '{"name": "finish_task", '},
        {"text": "More thought", "thought": True, "thoughtSignature": "c2ln"},
        {"text": '"arguments": {"summary": "done"}}'},
    ]
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    assert backend.invoke("sys", "prompt").text == '{"name": "finish_task", "arguments": {"summary": "done"}}'


@pytest.mark.parametrize("candidates", [
    None,
    [],
    ["not a dict"],
    [{"finishReason": "STOP"}],
    [{"finishReason": "STOP", "content": "not a dict"}],
    [{"finishReason": "STOP", "content": {"parts": "not a list"}}],
    [{"finishReason": "STOP", "content": {"parts": ["not a dict"]}}],
    [{"finishReason": "STOP", "content": {"parts": [{"text": 123}]}}],
    [{"finishReason": "STOP", "content": {"parts": [{"functionCall": {"args": {}}}]}}],
])
def test_malformed_body_raises_incomplete_output(tmp_path, monkeypatch, candidates):
    reply = {"usageMetadata": USAGE, "modelVersion": MODEL}
    if candidates is not None:
        reply["candidates"] = candidates
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "incomplete_output"
    assert len(fake.calls) == 1


def test_no_usage_raises_incomplete_output(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage={"candidatesTokenCount": 3}))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "incomplete_output"


@pytest.mark.parametrize("served", ["gemini-3.6-flash", "gemini-3.8-flash-lite", "gemini-3.8-pro", None])
def test_model_mismatch_raises_immediately(tmp_path, monkeypatch, served):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(model=served))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_mismatch"
    assert f"asked for {MODEL}, got {served}" in exc.value.detail
    assert len(fake.calls) == 1 and waits == []


def test_numbered_version_of_the_model_is_accepted(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(model=f"{MODEL}-001"))])
    assert backend.invoke("sys", "prompt").text == FINISH


def test_max_tokens_retries_then_succeeds(tmp_path, monkeypatch):
    cut = body(text=None, finish="MAX_TOKENS")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, cut), (200, body())])
    assert backend.invoke("sys", "prompt").text == FINISH
    assert len(fake.calls) == 2 and waits == [5]


def test_max_tokens_exhausting_retries_raises_model_format(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(finish="MAX_TOKENS"))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_format" and "MAX_TOKENS" in exc.value.detail
    assert len(fake.calls) == 4 and waits == [5, 20, 60]


def test_empty_text_retries_with_model_format(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(text="  ")), (200, body())])
    assert backend.invoke("sys", "prompt").text == FINISH
    assert len(fake.calls) == 2 and waits == [5]


def test_stop_with_no_parts_is_empty_text(tmp_path, monkeypatch):
    reply = body()
    reply["candidates"][0]["content"] = {"role": "model"}
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_format"


@pytest.mark.parametrize("finish", ["SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT", "SPII"])
def test_safety_finish_raises_model_refusal_no_retry(tmp_path, monkeypatch, finish):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(text=None, finish=finish))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_refusal" and finish in exc.value.detail
    assert len(fake.calls) == 1 and waits == []


def test_prompt_block_raises_model_refusal_no_retry(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(block="PROHIBITED_CONTENT"))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_refusal" and "PROHIBITED_CONTENT" in exc.value.detail
    assert len(fake.calls) == 1 and waits == []


def test_unexpected_finish_reason_retries_as_incomplete_output(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(finish="OTHER"))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "incomplete_output" and "OTHER" in exc.value.detail
    assert len(fake.calls) == 4 and waits == [5, 20, 60]


@pytest.mark.parametrize("status", [429, 500, 503, 504])
def test_retryable_status_then_200(tmp_path, monkeypatch, status):
    reply = google_error(status, "try again", "UNAVAILABLE")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(status, reply), (200, body())])
    assert backend.invoke("sys", "prompt").text == FINISH
    assert len(fake.calls) == 2 and waits == [5]


def test_connection_error_retries(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [url_error.URLError("reset"), (200, body())])
    assert backend.invoke("sys", "prompt").text == FINISH
    assert len(fake.calls) == 2 and waits == [5]


@pytest.mark.parametrize("status,reply", [
    (401, google_error(401, "unauthenticated", "UNAUTHENTICATED")),
    (402, google_error(402, "prepay credits depleted", "FAILED_PRECONDITION")),
    (403, google_error(403, "Your project has been denied access.", "PERMISSION_DENIED")),
    (400, google_error(400, "API key not valid. Please pass a valid API key.", "INVALID_ARGUMENT")),
])
def test_auth_or_quota_no_retry(tmp_path, monkeypatch, status, reply):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(status, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "auth_or_quota"
    assert len(fake.calls) == 1 and waits == []


def test_context_limit_400_is_context_limit(tmp_path, monkeypatch):
    reply = google_error(
        400, "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576).", "INVALID_ARGUMENT",
    )
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(400, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "context_limit"
    assert len(fake.calls) == 1 and waits == []


def test_other_400_is_cli_error_no_retry(tmp_path, monkeypatch):
    reply = google_error(400, "Invalid JSON payload received.", "INVALID_ARGUMENT")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(400, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "cli_error"
    assert len(fake.calls) == 1 and waits == []


def test_cost_with_and_without_cached_tokens():
    assert price_cost(USAGE, MODEL) == pytest.approx(COST)
    # No cached field: all 10,000 prompt tokens at 0.75; 1,000 candidates at 3.75.
    assert price_cost({"promptTokenCount": 10_000, "candidatesTokenCount": 1_000}, MODEL) == pytest.approx(0.01125)
    # Thoughts are billed as output: 0 candidates + 2,000 thoughts = 2,000 * 3.75 / 1e6.
    assert price_cost({"promptTokenCount": 0, "thoughtsTokenCount": 2_000}, MODEL) == pytest.approx(0.0075)
    # Fully cached prompt: 4,000 * 0.075 / 1e6.
    assert price_cost({"promptTokenCount": 4_000, "cachedContentTokenCount": 4_000}, MODEL) == pytest.approx(0.0003)


def test_cost_rejects_unusable_usage():
    assert price_cost({"candidatesTokenCount": 10}, MODEL) is None
    assert price_cost({"promptTokenCount": "10"}, MODEL) is None
    assert price_cost({"promptTokenCount": True}, MODEL) is None
    assert price_cost({"promptTokenCount": 10, "cachedContentTokenCount": 11}, MODEL) is None
    assert price_cost({"promptTokenCount": 10, "thoughtsTokenCount": -1}, MODEL) is None
    assert price_cost({"promptTokenCount": 10, "candidatesTokenCount": None}, MODEL) is None
    assert price_cost("not a dict", MODEL) is None
    assert price_cost(USAGE, "gemini-unknown") is None


def test_spend_guard_resume_reads_injected_cost(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body())])
    backend.invoke("sys", "prompt")
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(events) == 1
    saved = json.loads(events[0]["stdout"])
    assert saved["usage"]["cost"] == pytest.approx(COST)
    assert saved["usage"]["cost_source"] == "price_table"
    # Every raw count is kept, both in the untouched usageMetadata and beside the cost.
    assert saved["usageMetadata"] == USAGE
    assert all(saved["usage"][key] == value for key, value in USAGE.items())
    resumed = SpendGuard(10, output_dir=tmp_path)
    assert resumed.total == pytest.approx(COST)


def test_spend_cap_stops_the_run(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body())], cap=0.003)
    backend.invoke("sys", "prompt 1")
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 2")
    assert exc.value.kind == "budget"


def test_three_consecutive_misses_trip_guard(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))])
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0
    backend.invoke("sys", "prompt 2")
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 4")
    assert exc.value.kind == "budget" and "tripped: 3 consecutive cache misses" in exc.value.detail
    with pytest.raises(CLIFailure) as exc_add:
        backend.guard.add(0)
    assert exc_add.value.kind == "budget"


def test_cache_hit_resets_consecutive_miss_count(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))])
    backend.invoke("sys", "prompt 1")
    backend.invoke("sys", "prompt 2")
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2
    backend._opener = FakeOpener([(200, body(usage=USAGE_HIT))])
    backend.invoke("sys", "prompt 4")
    assert backend.guard.consecutive_misses == 0
    backend._opener = FakeOpener([(200, body(usage=USAGE_MISS))])
    backend.invoke("sys", "prompt 5")
    assert backend.guard.consecutive_misses == 1


def test_first_call_and_small_prompts_never_count_as_misses(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))])
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0
    backend._opener = FakeOpener([(200, body(usage={"promptTokenCount": 4095, "candidatesTokenCount": 10}))])
    for _ in range(4):
        backend.invoke("sys", "small prompt")
    assert backend.guard.consecutive_misses == 0


def test_two_backends_sharing_guard_accumulate_misses(tmp_path, monkeypatch):
    guard = SpendGuard(10)
    first, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], guard=guard)
    second, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], guard=guard)
    first.invoke("sys", "a1")
    first.invoke("sys", "a2")
    second.invoke("sys", "b1")
    second.invoke("sys", "b2")
    assert guard.consecutive_misses == 2
    with pytest.raises(CLIFailure) as exc:
        first.invoke("sys", "a3")
    assert exc.value.kind == "budget"


def test_key_scrubbed_from_events_and_exceptions(tmp_path, monkeypatch):
    escaped = json.dumps({"error": {"code": 500, "message": "".join(f"\\u{ord(c):04x}" for c in KEY)}})
    replies = [(500, f"error with key {KEY} inside"), (500, escaped), (500, json.dumps(KEY)), (500, "plain")]
    backend, _, _ = make_backend(tmp_path, monkeypatch, replies)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert KEY not in str(exc.value) and KEY not in exc.value.detail
    events = (tmp_path / "events.jsonl").read_text()
    assert KEY not in events and "[GOOGLE_API_KEY]" in events
    # The \\u-escaped spelling is decoded and scrubbed too.
    assert KEY not in json.dumps([json.loads(json.loads(line)["stdout"]) for line in events.splitlines()[1:2]])

    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(text=f"leak {KEY}", model=f"{KEY}"))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_mismatch" and KEY not in exc.value.detail and "[GOOGLE_API_KEY]" in exc.value.detail


def test_google_key_is_a_secret_env():
    assert ("GOOGLE_API_KEY", "[GOOGLE_API_KEY]") in evaluate.SECRET_ENVS
    assert "gemini" in evaluate.API_LAYOUT_PROVIDERS


# --- evaluate.py integration and CLI tests ---

class GeminiEvalFake:
    def __init__(self, content=INFEASIBLE, finish="STOP"):
        self.content = content
        self.finish = finish
        self.urls = []
        self.payloads = []
        self.headers = []

    def __call__(self, req, *, timeout):
        self.urls.append(req.full_url)
        self.payloads.append(json.loads(req.data))
        self.headers.append(req.get_header("X-goog-api-key"))
        response = io.BytesIO(json.dumps(body(self.content, finish=self.finish)).encode())
        response.status = 200
        return response


@pytest.fixture
def fake_gemini_urlopen(monkeypatch):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    for name in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "NFTB_ENDPOINT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    fake = GeminiEvalFake()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def gemini_args(out_dir, *extra, effort="high", max_usd="1", model=MODEL, instances="F1a_04"):
    argv = ["--tier", "synthetic", "--instances", instances, "--model", model, "--gemini", "--out", str(out_dir)]
    if effort is not None:
        argv += ["--reasoning-effort", effort]
    if max_usd is not None:
        argv += ["--max-usd", max_usd]
    return [*argv, *extra]


def test_gemini_run_records_identity_and_needs_no_other_key(tmp_path, fake_gemini_urlopen):
    out_dir = tmp_path / "gemini_out"
    assert evaluate.main(gemini_args(out_dir)) == 0
    assert fake_gemini_urlopen.urls and all(url == URL for url in fake_gemini_urlopen.urls)
    for payload in fake_gemini_urlopen.payloads:
        assert payload["generationConfig"] == {"maxOutputTokens": 32000, "thinkingConfig": {"thinkingLevel": "HIGH"}}
        assert set(payload) == {"systemInstruction", "contents", "generationConfig"}
    run = json.loads((out_dir / "run.json").read_text())
    assert run["provider"] == "gemini" and run["model"] == MODEL
    assert run["sampling"] == {"reasoning_effort": "high", "max_tokens": 32000}
    assert run["prompt_layout"] == "items_v1"
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tasks_scored"] == 1 and summary["overall"]["success_count"] == 1
    saved = [json.loads(json.loads(line)["stdout"]) for line in (out_dir / "events.jsonl").read_text().splitlines()]
    assert saved and all(s["usage"]["cost_source"] == "price_table" for s in saved)


def test_gemini_refusal_is_scored_model_failure_not_infra_error(tmp_path, fake_gemini_urlopen):
    fake_gemini_urlopen.finish = "SAFETY"
    out_dir = tmp_path / "refusal_out"
    assert evaluate.main(gemini_args(out_dir)) == 0
    result = json.loads((out_dir / "F1a_04" / "result.json").read_text())
    assert result["status"] == "scored" and result["success"] is False
    assert result["outcome"] == "format_error" and result["error_kind"] == "model_refusal"
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["complete"] is True and summary["tasks_errored"] == 0
    assert summary["overall"]["outcomes"]["format_error"] == 1


def test_gemini_key_not_written_to_outputs(tmp_path, fake_gemini_urlopen):
    fake_gemini_urlopen.content = json.dumps({"name": "declare_infeasible", "arguments": {"reason": f"key {KEY}"}})
    out_dir = tmp_path / "key_out"
    assert evaluate.main(gemini_args(out_dir)) == 0
    assert fake_gemini_urlopen.headers and all(h == KEY for h in fake_gemini_urlopen.headers)
    for root, _, files in os.walk(out_dir):
        for name in files:
            assert KEY not in (Path(root) / name).read_text(encoding="utf-8", errors="ignore"), name


def test_gemini_missing_key_is_refused(tmp_path, monkeypatch, fake_gemini_urlopen, capsys):
    monkeypatch.delenv("GOOGLE_API_KEY")
    assert evaluate.main(gemini_args(tmp_path / "nokey")) == 1
    assert "GOOGLE_API_KEY is not set in the environment" in capsys.readouterr().err
    assert fake_gemini_urlopen.payloads == []


@pytest.mark.parametrize("kwargs,extra", [
    ({"effort": None}, ()),
    ({"effort": "minimal"}, ()),
    ({"effort": "max"}, ()),
    ({"effort": "xhigh"}, ()),
    ({"effort": "none"}, ()),
    ({"max_usd": None}, ()),
    ({"max_usd": "0"}, ()),
    ({"max_usd": "-1"}, ()),
    ({}, ("--seed", "1")),
    ({}, ("--temperature", "0.7")),
    ({}, ("--top-p", "0.9")),
    ({}, ("--openrouter-provider", "Google")),
    ({}, ("--endpoint", "http://gpu-node-07:8000")),
    ({}, ("--openai",)),
    ({}, ("--anthropic",)),
    ({}, ("--price-per-mtok", "1,1,1")),
    ({}, ("--enable-thinking",)),
])
def test_gemini_flag_combinations_refused(tmp_path, fake_gemini_urlopen, kwargs, extra):
    out_dir = tmp_path / "bad_flags"
    assert evaluate.main(gemini_args(out_dir, *extra, **kwargs)) != 0
    assert not (out_dir / "run.json").exists()
    assert fake_gemini_urlopen.payloads == []


def test_gemini_requires_model(tmp_path, fake_gemini_urlopen, capsys):
    argv = ["--tier", "synthetic", "--instances", "F1a_04", "--agent", "do_nothing", "--gemini", "--out", str(tmp_path / "x")]
    assert evaluate.main(argv) == 1
    assert "--gemini requires --model" in capsys.readouterr().err
    assert fake_gemini_urlopen.payloads == []


def test_gemini_resume_refused_on_changed_effort(tmp_path, fake_gemini_urlopen):
    out_dir = tmp_path / "resume"
    assert evaluate.main(gemini_args(out_dir, effort="high")) == 0
    before = (out_dir / "run.json").read_text()
    calls = len(fake_gemini_urlopen.payloads)
    assert evaluate.main(gemini_args(out_dir, effort="low")) == 1
    assert (out_dir / "run.json").read_text() == before
    assert len(fake_gemini_urlopen.payloads) == calls


def test_200_without_usage_trips_the_guard(tmp_path, monkeypatch):
    guard = SpendGuard(10)
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage={"candidatesTokenCount": 3}))], guard=guard)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "incomplete_output"
    with pytest.raises(CLIFailure) as later:
        guard.add(0)
    assert later.value.kind == "budget" and "without valid token usage" in later.value.detail


def test_unparsable_200_trips_the_guard(tmp_path, monkeypatch):
    guard = SpendGuard(10)
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, "{not json")], guard=guard)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "cli_error"
    with pytest.raises(CLIFailure) as later:
        guard.add(0)
    assert later.value.kind == "budget"


class _UnreadableResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        raise http_client.IncompleteRead(b'{"candidates": [')


def test_unreadable_200_trips_the_guard_and_does_not_retry(tmp_path, monkeypatch):
    guard = SpendGuard(10)
    backend, _, waits = make_backend(tmp_path, monkeypatch, [(200, body())], guard=guard)
    calls = []
    backend._opener = lambda req, *, timeout: calls.append(req) or _UnreadableResponse()
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "incomplete_output" and len(calls) == 1 and waits == []
    with pytest.raises(CLIFailure) as later:
        guard.add(0)
    assert later.value.kind == "budget" and "could not be read" in later.value.detail


def test_malformed_function_call_with_text_is_the_reply(tmp_path, monkeypatch):
    usage = {"promptTokenCount": 2077, "totalTokenCount": 2077}
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(finish="MALFORMED_FUNCTION_CALL", usage=usage, thought=None))])
    assert backend.invoke("sys", "prompt").text == FINISH and len(fake.calls) == 1


def test_malformed_function_call_without_text_retries_as_model_format(tmp_path, monkeypatch):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(text="", finish="MALFORMED_FUNCTION_CALL")), (200, body())])
    assert backend.invoke("sys", "prompt").text == FINISH and len(fake.calls) == 2


def test_one_function_call_part_becomes_the_named_reply(tmp_path, monkeypatch):
    reply = body(text=None)
    reply["candidates"][0]["content"]["parts"].append(
        {"functionCall": {"name": "get_logs", "args": {"address": "0xabc"}, "id": "call_1"}, "thoughtSignature": "sig"})
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    assert json.loads(backend.invoke("sys", "prompt").text) == {"name": "get_logs", "arguments": {"address": "0xabc"}}


def test_two_function_call_parts_leave_no_reply(tmp_path, monkeypatch):
    reply = body(text=None)
    reply["candidates"][0]["content"]["parts"] += [{"functionCall": {"name": "get_logs", "args": {}}},
                                                   {"functionCall": {"name": "read_contract", "args": {}}}]
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, reply), (200, body())])
    assert backend.invoke("sys", "prompt").text == FINISH and len(fake.calls) == 2


# --- explicit context caching ---

CACHES = "https://generativelanguage.googleapis.com/v1beta/cachedContents"
BIG = "x" * 80_000  # about 20,000 tokens at chars/4
HEAD = "system head"


def cache_reply(name, tokens):
    return {"name": name, "model": f"models/{MODEL}", "usageMetadata": {"totalTokenCount": tokens}, "expireTime": "2026-11-08T00:05:00Z"}


class CacheFake:
    """Routes requests by method and URL; each route answers from its own reply list (the last reply repeats)."""

    def __init__(self, **routes):
        self.routes = {kind: list(replies) for kind, replies in routes.items()}
        self.calls = []

    def __call__(self, req, *, timeout):
        method = req.get_method()
        if req.full_url == URL:
            kind = "generate"
        elif method == "POST" and req.full_url == CACHES:
            kind = "create"
        else:
            kind = {"PATCH": "patch", "DELETE": "delete"}[method]
        data = json.loads(req.data) if req.data else None
        self.calls.append((kind, req.full_url, data, req.get_header("X-goog-api-key")))
        replies = self.routes[kind]
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        status, data = reply
        raw = json.dumps(data) if not isinstance(data, str) else data
        if status >= 400:
            raise url_error.HTTPError(req.full_url, status, "fake HTTP error", {}, io.BytesIO(raw.encode()))
        response = io.BytesIO(raw.encode())
        response.status = status
        return response

    def of(self, kind):
        return [call for call in self.calls if call[0] == kind]


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def cache_backend(tmp_path, monkeypatch, fake, *, guard=None):
    monkeypatch.setenv("GOOGLE_API_KEY", KEY)
    clock, waits = Clock(), []
    backend = GeminiGenerateBackend(
        MODEL, guard=guard or SpendGuard(10), reasoning_effort="high",
        events_path=tmp_path / "events.jsonl", opener=fake, sleep=waits.append, clock=clock,
    )
    return backend, clock, waits


def gen(prompt, cached=0):
    usage = {"promptTokenCount": prompt, "candidatesTokenCount": 10, "totalTokenCount": prompt + 10}
    if cached:
        usage["cachedContentTokenCount"] = cached
    return (200, body(usage=usage))


def contents_of(items):
    return [{"role": "user" if i["role"] == "user" else "model", "parts": [{"text": i["content"]}]} for i in items]


def u(text):
    return {"role": "user", "content": text}


def a(text):
    return {"role": "assistant", "content": text}


def assert_model_input_unchanged(fake, items):
    """The live cache's system instruction and contents plus the sent contents equal the full request item by item."""
    sent = fake.of("generate")[-1][2]
    full = contents_of(items)
    if "cachedContent" not in sent:
        assert sent["systemInstruction"] == {"parts": [{"text": HEAD}]} and sent["contents"] == full
        return
    created = {f"cachedContents/c{i + 1}": call[2] for i, call in enumerate(fake.of("create"))}
    cache = created[sent["cachedContent"]]
    assert "systemInstruction" not in sent
    assert cache["systemInstruction"] == {"parts": [{"text": HEAD}]}
    assert cache["model"] == f"models/{MODEL}" and cache["ttl"] == "300s"
    assert cache["contents"] + sent["contents"] == full
    assert sent["generationConfig"] == {"maxOutputTokens": 32000, "thinkingConfig": {"thinkingLevel": "HIGH"}}


def events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


def test_small_first_turns_send_the_full_request_without_cache(tmp_path, monkeypatch):
    fake = CacheFake(generate=[gen(1000)])
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u("hello")]
    backend.invoke_items(HEAD, items)
    items += [a("call"), u("small tool result")]
    backend.invoke_items(HEAD, items)
    assert [call[0] for call in fake.calls] == ["generate", "generate"]
    assert_model_input_unchanged(fake, items)
    assert backend._cache is None


def test_cache_create_reuse_and_recreate(tmp_path, monkeypatch):
    fake = CacheFake(
        generate=[gen(1000), gen(21_000, cached=900), gen(21_200, cached=21_100), gen(21_400, cached=21_100)],
        create=[(200, cache_reply("cachedContents/c1", 900)), (200, cache_reply("cachedContents/c2", 21_100))],
        delete=[(200, {})],
    )
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u("task")]
    backend.invoke_items(HEAD, items)
    assert fake.of("create") == []

    # The large tool result: one create over everything but the newest item, which alone is sent beside the cache.
    items += [a("get_logs"), u(BIG)]
    backend.invoke_items(HEAD, items)
    assert len(fake.of("create")) == 1
    assert fake.of("create")[0][2]["contents"] == contents_of(items[:-1])
    sent = fake.of("generate")[-1][2]
    assert sent["cachedContent"] == "cachedContents/c1" and sent["contents"] == contents_of(items[-1:])
    assert_model_input_unchanged(fake, items)

    # The uncached suffix now holds the big result (above 16,000 tokens): recreate, then delete the old cache.
    items += [a("read_contract"), u("small")]
    backend.invoke_items(HEAD, items)
    assert len(fake.of("create")) == 2
    assert fake.of("delete") == [("delete", f"{CACHES}/c1", None, KEY)]
    sent = fake.of("generate")[-1][2]
    assert sent["cachedContent"] == "cachedContents/c2" and sent["contents"] == contents_of(items[-1:])
    assert_model_input_unchanged(fake, items)

    # A small later turn reuses the cache.
    items += [a("balance_of"), u("small again")]
    backend.invoke_items(HEAD, items)
    assert len(fake.of("create")) == 2 and len(fake.of("delete")) == 1 and fake.of("patch") == []
    sent = fake.of("generate")[-1][2]
    assert sent["cachedContent"] == "cachedContents/c2" and sent["contents"] == contents_of(items[-3:])
    assert_model_input_unchanged(fake, items)
    # The key travels in the header of every cache call and never in a URL.
    assert all(call[3] == KEY and KEY not in call[1] and "key=" not in call[1] for call in fake.calls)


def test_ttl_refresh_after_120_seconds(tmp_path, monkeypatch):
    fake = CacheFake(
        generate=[gen(21_000, cached=20_000)],
        create=[(200, cache_reply("cachedContents/c1", 20_000))],
        patch=[(200, cache_reply("cachedContents/c1", 20_000))],
    )
    backend, clock, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    assert len(fake.of("create")) == 1
    clock.now += 100
    items += [a("call"), u("r2")]
    backend.invoke_items(HEAD, items)
    assert fake.of("patch") == []
    clock.now += 21  # 121 s since the create
    items += [a("call"), u("r3")]
    backend.invoke_items(HEAD, items)
    assert fake.of("patch") == [("patch", f"{CACHES}/c1", {"ttl": "300s"}, KEY)]
    assert fake.of("generate")[-1][2]["cachedContent"] == "cachedContents/c1"
    assert_model_input_unchanged(fake, items)
    storage = [e for e in events(tmp_path) if e.get("kind") == "cache_storage"]
    assert len(storage) == 1 and storage[0]["returncode"] == 0
    # The extension bought is charged: 20,000 tokens * 0.50 USD per MTok-hour * 121 s.
    assert json.loads(storage[0]["stdout"])["usage"]["cost"] == pytest.approx(20_000 * 0.50 * 121 / 3600 / 1e6)
    clock.now += 60
    items += [a("call"), u("r4")]
    backend.invoke_items(HEAD, items)
    assert len(fake.of("patch")) == 1


def test_refresh_failure_falls_back_to_the_full_request(tmp_path, monkeypatch):
    fake = CacheFake(
        generate=[gen(21_000, cached=20_000), gen(21_200)],
        create=[(200, cache_reply("cachedContents/c1", 20_000))],
        patch=[(500, google_error(500, "internal", "INTERNAL"))],
    )
    backend, clock, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    clock.now += 200
    items += [a("call"), u("r2")]
    backend.invoke_items(HEAD, items)
    assert len(fake.of("patch")) == 1 and backend._cache is None
    assert "cachedContent" not in fake.of("generate")[-1][2]
    assert_model_input_unchanged(fake, items)


def test_create_failure_falls_back_to_the_full_request(tmp_path, monkeypatch):
    fake = CacheFake(generate=[gen(21_000)], create=[(500, google_error(500, "internal", "INTERNAL"))])
    backend, _, waits = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    assert backend.invoke_items(HEAD, items).text == FINISH
    assert [call[0] for call in fake.calls] == ["create", "generate"] and waits == []
    assert "cachedContent" not in fake.of("generate")[0][2]
    assert_model_input_unchanged(fake, items)
    assert backend._cache is None
    failed = [e for e in events(tmp_path) if e.get("kind") == "cache_create"]
    assert len(failed) == 1 and failed[0]["returncode"] == 500
    assert "usage" not in json.loads(failed[0]["stdout"])
    # A lost connection on create also falls back.
    fake = CacheFake(generate=[gen(21_000)], create=[url_error.URLError("reset")])
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake)
    assert backend.invoke_items(HEAD, items).text == FINISH
    assert "cachedContent" not in fake.of("generate")[0][2] and backend._cache is None


def test_expired_cache_404_drops_the_cache_and_resends_in_full(tmp_path, monkeypatch):
    gone = google_error(404, "CachedContent not found (or permission denied)", "NOT_FOUND")
    fake = CacheFake(
        generate=[gen(21_000, cached=20_000), (404, gone), gen(21_200)],
        create=[(200, cache_reply("cachedContents/c1", 20_000))],
    )
    backend, _, waits = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    items += [a("call"), u("r2")]
    assert backend.invoke_items(HEAD, items).text == FINISH
    kinds = [call[0] for call in fake.calls]
    assert kinds == ["create", "generate", "generate", "generate"] and waits == []
    assert fake.of("generate")[1][2]["cachedContent"] == "cachedContents/c1"
    assert "cachedContent" not in fake.of("generate")[2][2]
    assert_model_input_unchanged(fake, items)
    assert backend._cache is None


def test_cache_costs_are_charged_written_and_recounted_on_resume(tmp_path, monkeypatch):
    fake = CacheFake(
        generate=[gen(21_000, cached=20_000)],
        create=[(200, cache_reply("cachedContents/c1", 20_000))],
        patch=[(200, cache_reply("cachedContents/c1", 20_000))],
    )
    guard = SpendGuard(10)
    backend, clock, _ = cache_backend(tmp_path, monkeypatch, fake, guard=guard)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    clock.now += 150
    items += [a("call"), u("r2")]
    backend.invoke_items(HEAD, items)
    lines = events(tmp_path)
    create = [e for e in lines if e.get("kind") == "cache_create"]
    assert len(create) == 1 and create[0]["returncode"] == 0
    usage = json.loads(create[0]["stdout"])["usage"]
    # Creation: 20,000 tokens at the 0.75 input price; storage up front for 300 s at 0.50 per MTok-hour.
    create_cost = 20_000 * 0.75 / 1e6 + 20_000 * 0.50 * 300 / 3600 / 1e6
    assert usage["cost"] == pytest.approx(create_cost) and usage["cost_source"] == "price_table"
    assert usage["cache_input_cost"] == pytest.approx(0.015)
    assert usage["cache_storage_cost"] == pytest.approx(20_000 * 0.50 * 300 / 3600 / 1e6)
    assert json.loads(create[0]["stdout"])["name"] == "cachedContents/c1"
    storage_cost = 20_000 * 0.50 * 150 / 3600 / 1e6
    # Each generateContent reply keeps the existing formula: 1,000 uncached at 0.75, 20,000 cached at 0.075.
    gen_cost = (1_000 * 0.75 + 20_000 * 0.075 + 10 * 3.75) / 1e6
    assert guard.total == pytest.approx(create_cost + storage_cost + 2 * gen_cost)
    assert SpendGuard(10, output_dir=tmp_path).total == pytest.approx(guard.total)


def test_cache_create_200_without_usage_trips_the_guard(tmp_path, monkeypatch):
    fake = CacheFake(generate=[gen(21_000)], create=[(200, {"name": f"cachedContents/{KEY}"})])
    guard = SpendGuard(10)
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake, guard=guard)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke_items(HEAD, [u(BIG), a("call"), u("r1")])
    assert exc.value.kind == "incomplete_output" and fake.of("generate") == []
    with pytest.raises(CLIFailure) as later:
        guard.add(0)
    assert later.value.kind == "budget" and "cache create" in later.value.detail
    text = (tmp_path / "events.jsonl").read_text()
    assert KEY not in text and "[GOOGLE_API_KEY]" in text


def test_cached_request_reporting_no_cached_tokens_is_a_miss(tmp_path, monkeypatch):
    fake = CacheFake(generate=[gen(21_000)], create=[(200, cache_reply("cachedContents/c1", 20_000))])
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)  # the first call of the backend, yet it sent cachedContent
    assert "cachedContent" in fake.of("generate")[0][2]
    assert backend.guard.consecutive_misses == 1
    items += [a("call"), u("r2")]
    backend.invoke_items(HEAD, items)
    assert backend.guard.consecutive_misses == 2
    items += [a("call"), u("r3")]
    with pytest.raises(CLIFailure) as exc:
        backend.invoke_items(HEAD, items)
    assert exc.value.kind == "budget" and "3 consecutive cache misses" in exc.value.detail


def test_key_scrubbed_from_cache_events_and_errors(tmp_path, monkeypatch):
    leak = google_error(500, f"bad key {KEY}", "INTERNAL")
    fake = CacheFake(
        generate=[gen(21_000, cached=20_000), gen(21_200)],
        create=[(200, cache_reply("cachedContents/c1", 20_000)), (500, leak)],
        patch=[(500, leak)],
        delete=[(500, leak)],
    )
    backend, clock, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    clock.now += 200
    items += [a("call"), u("r2")]
    backend.invoke_items(HEAD, items)  # the refresh fails, the turn goes in full
    items += [a("call"), u(BIG)]
    backend.invoke_items(HEAD, items)  # create fails
    kinds = {e.get("kind") for e in events(tmp_path)}
    assert {"cache_create", "cache_storage"} <= kinds
    text = (tmp_path / "events.jsonl").read_text()
    assert KEY not in text and text.count("[GOOGLE_API_KEY]") >= 2


class _TruncatedBody(io.BytesIO):
    def read(self, *args):
        raise http_client.IncompleteRead(b"partial", 50)


def test_unreadable_cache_error_body_falls_back(tmp_path, monkeypatch):
    broken = url_error.HTTPError(CACHES, 503, "unavailable", {}, _TruncatedBody())
    fake = CacheFake(generate=[gen(21_000)], create=[broken])
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    assert backend.invoke_items(HEAD, items).text == FINISH
    assert "cachedContent" not in fake.of("generate")[0][2] and backend._cache is None


def test_lost_create_connection_charges_a_byte_bound(tmp_path, monkeypatch):
    guard = SpendGuard(10)
    fake = CacheFake(generate=[gen(21_000)], create=[url_error.URLError("reset")])
    backend, _, _ = cache_backend(tmp_path, monkeypatch, fake, guard=guard)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    texts = [HEAD] + [i["content"] for i in items[:-1]]
    tokens = sum(len(text.encode("utf-8")) for text in texts) + 16 * len(texts)
    bound = tokens * (0.75 + 0.50 * 300 / 3600) / 1e6
    created = [e for e in events(tmp_path) if e.get("kind") == "cache_create"]
    assert len(created) == 1 and json.loads(created[0]["stdout"])["usage"]["cost"] == pytest.approx(bound)
    assert SpendGuard(10, output_dir=tmp_path).total == pytest.approx(guard.total)
    assert guard.total >= bound


def test_lost_refresh_connection_charges_the_extension_and_drops_the_cache(tmp_path, monkeypatch):
    fake = CacheFake(
        generate=[gen(21_000, cached=20_000), gen(21_200)],
        create=[(200, cache_reply("cachedContents/c1", 20_000))],
        patch=[url_error.URLError("reset")],
    )
    backend, clock, _ = cache_backend(tmp_path, monkeypatch, fake)
    items = [u(BIG), a("call"), u("r1")]
    backend.invoke_items(HEAD, items)
    clock.now += 200
    items += [a("call"), u("r2")]
    backend.invoke_items(HEAD, items)
    assert backend._cache is None and "cachedContent" not in fake.of("generate")[-1][2]
    storage = [e for e in events(tmp_path) if e.get("kind") == "cache_storage"]
    assert len(storage) == 1
    assert json.loads(storage[0]["stdout"])["usage"]["cost"] == pytest.approx(20_000 * 0.50 * 200 / 3600 / 1e6)
