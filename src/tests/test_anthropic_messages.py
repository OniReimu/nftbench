"""The Anthropic Messages backend and its evaluate flags. Every HTTP reply here is local fake data."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from urllib import error as url_error

import pytest

from nftbench import evaluate
from nftbench.agents.anthropic_messages import (
    AnthropicMessagesBackend,
    price_cost,
    MODEL_PRICES,
    EFFORTS,
)
from nftbench.agents.cli import CLIFailure, render_items
from nftbench.agents.openrouter import SpendGuard

MODEL = "claude-opus-5-5"
HAIKU_MODEL = "claude-haiku-5-5"
KEY = "test-only-anthropic-key-do-not-leak"
FINISH = json.dumps({"name": "finish_task", "arguments": {"summary": "done"}})
INFEASIBLE = json.dumps({"name": "declare_infeasible", "arguments": {"reason": "mock infeasible"}})
USAGE = {
    "input_tokens": 1000,
    "cache_creation_input_tokens": 200,
    "cache_read_input_tokens": 100,
    "output_tokens": 500,
}
# For Opus: (1000 * 4.00 + 200 * 5.00 + 100 * 0.20 + 500 * 20.00) / 1e6 = 15020 / 1e6 = 0.01502
OPUS_COST = 0.01502


def body(
    text=FINISH,
    *,
    model=MODEL,
    stop_reason="end_turn",
    stop_details=None,
    usage="default",
    error=None,
    thinking="thinking content",
):
    content = []
    if thinking is not None:
        content.append({"type": "thinking", "thinking": thinking, "signature": "sig123"})
    if text is not None:
        content.append({"type": "text", "text": text})
    reply = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "error": error,
    }
    if stop_details is not None:
        reply["stop_details"] = stop_details
    reply["usage"] = USAGE if usage == "default" else usage
    return reply


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req, *, timeout):
        self.calls.append((req, timeout))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if callable(reply):
            reply = reply()
        if isinstance(reply, Exception):
            raise reply
        status, data = reply
        raw = json.dumps(data) if not isinstance(data, str) else data
        if status >= 400:
            raise url_error.HTTPError(req.full_url, status, "fake HTTP error", {}, io.BytesIO(raw.encode()))
        response = io.BytesIO(raw.encode())
        response.status = status
        return response


def make_backend(tmp_path, monkeypatch, replies, *, cap=10, guard=None, model=MODEL, **kwargs):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    fake = FakeOpener(replies)
    waits = []
    options = {"reasoning_effort": "high"}
    options.update(kwargs)
    backend = AnthropicMessagesBackend(
        model,
        guard=guard or SpendGuard(cap),
        events_path=tmp_path / "events.jsonl",
        opener=fake,
        sleep=waits.append,
        **options,
    )
    return backend, fake, waits


def test_request_body_shape(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body())])
    request_json = json.dumps({
        "messages": [
            {"role": "system", "content": "System prompt"},
            {"role": "user", "content": "User question"},
            {"role": "assistant", "content": "Assistant answer"},
        ],
        "tools": [{"name": "finish_task"}],
    })
    result = json.loads(backend.complete(request_json))
    req, timeout = fake.calls[0]
    head, items = render_items(request_json)
    assert req.full_url == "https://api.anthropic.com/v1/messages" and timeout == 600
    assert req.get_header("X-api-key") == KEY
    assert req.get_header("Anthropic-version") == "2023-06-01"
    assert req.get_header("Content-type") == "application/json"
    sent = json.loads(req.data)
    assert sent == {
        "model": MODEL,
        "max_tokens": 32000,
        "system": head,
        "messages": items,
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "cache_control": {"type": "ephemeral"},
    }
    # Explicitly ensure forbidden keys are not present
    for forbidden in ("temperature", "top_p", "top_k", "fallbacks", "tool_choice", "tools", "seed"):
        assert forbidden not in sent
    assert result["tool_calls"][0]["name"] == "finish_task"
    assert result["usage"]["model"] == MODEL
    assert result["usage"]["usage"]["cost"] == pytest.approx(OPUS_COST)
    assert waits == []


def test_text_extraction_ignoring_thinking_blocks(tmp_path, monkeypatch):
    reply = {
        "id": "msg_01",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [
            {"type": "thinking", "thinking": "Internal thought process", "signature": "sig"},
            {"type": "redacted_thinking", "data": "redacted"},
            {"type": "text", "text": '{"name": "finish_task", '},
            {"type": "thinking", "thinking": "More thinking"},
            {"type": "text", "text": '"arguments": {"summary": "done"}}'},
        ],
        "stop_reason": "end_turn",
        "usage": USAGE,
    }
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    inv = backend.invoke("sys", "prompt")
    assert inv.text == '{"name": "finish_task", "arguments": {"summary": "done"}}'


@pytest.mark.parametrize("malformed", [
    {},
    {"content": "not a list"},
    {"content": ["not a dict"]},
    {"content": [{"type": "text"}]},
    {"content": [{"type": "text", "text": 123}]},
    {"content": [{"type": "unknown_type", "text": "foo"}]},
])
def test_malformed_body_raises_incomplete_output(tmp_path, monkeypatch, malformed):
    reply = {"id": "msg_01", "model": MODEL, "stop_reason": "end_turn", "usage": USAGE, **malformed}
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "incomplete_output"


def test_model_mismatch_raises_immediately(tmp_path, monkeypatch):
    reply = body(model="claude-haiku-5-5")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply)], model=MODEL)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_mismatch"
    assert "asked for claude-opus-5-5, got claude-haiku-5-5" in exc.value.detail
    assert len(fake.calls) == 1
    assert waits == []


def test_max_tokens_retries_then_succeeds(tmp_path, monkeypatch):
    reply1 = body(stop_reason="max_tokens")
    reply2 = body(stop_reason="end_turn")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply1), (200, reply2)])
    inv = backend.invoke("sys", "prompt")
    assert len(fake.calls) == 2
    assert waits == [5]
    assert inv.text == FINISH


def test_max_tokens_exhausting_retries_raises_model_format(tmp_path, monkeypatch):
    reply = body(stop_reason="max_tokens")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_format"
    assert "max_tokens" in exc.value.detail
    assert len(fake.calls) == 4
    assert waits == [5, 20, 60]


def test_empty_text_retries_with_model_format(tmp_path, monkeypatch):
    reply1 = body(text="")
    reply2 = body(text=FINISH)
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply1), (200, reply2)])
    inv = backend.invoke("sys", "prompt")
    assert len(fake.calls) == 2
    assert waits == [5]
    assert inv.text == FINISH


def test_refusal_raises_model_refusal_no_retry(tmp_path, monkeypatch):
    reply = body(stop_reason="refusal", stop_details={"type": "safety_block", "category": "harmful"})
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "model_refusal"
    assert "safety_block" in exc.value.detail
    assert len(fake.calls) == 1
    assert waits == []


def test_429_then_200(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(429, "rate limited"), (200, body())])
    inv = backend.invoke("sys", "prompt")
    assert len(fake.calls) == 2
    assert waits == [5]
    assert inv.text == FINISH


def test_529_overloaded_retries_then_succeeds(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(529, "overloaded"), (200, body())])
    inv = backend.invoke("sys", "prompt")
    assert len(fake.calls) == 2
    assert waits == [5]
    assert inv.text == FINISH


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_or_quota_no_retry(tmp_path, monkeypatch, status):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(status, f"error {status}")])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "auth_or_quota"
    assert len(fake.calls) == 1
    assert waits == []


def test_prompt_too_long_400_is_context_limit(tmp_path, monkeypatch):
    err_body = {
        "type": "error",
        "error": {
            "type": "invalid_request_error",
            "message": "prompt is too long: 250000 tokens > 200000 maximum",
        },
    }
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(400, err_body)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "context_limit"
    assert len(fake.calls) == 1
    assert waits == []


def test_cost_opus():
    usage = {
        "input_tokens": 10_000,
        "cache_creation_input_tokens": 5_000,
        "cache_read_input_tokens": 20_000,
        "output_tokens": 1_000,
    }
    # Opus: in 4.00, write 5.00, read 0.20, out 20.00
    # (10000*4.00 + 5000*5.00 + 20000*0.20 + 1000*20.00) / 1e6
    # = (40000 + 25000 + 4000 + 20000) / 1e6 = 89000 / 1e6 = 0.089
    cost = price_cost(usage, "claude-opus-5-5")
    assert cost == pytest.approx(0.089)


def test_cost_haiku_below_and_above_100k():
    # Below 100k: prompt_length = 50k + 30k + 20k = 100k <= 100k
    # Tier 1: in 0.10, write 0.125, read 0.01, out 0.50
    usage_below = {
        "input_tokens": 50_000,
        "cache_creation_input_tokens": 30_000,
        "cache_read_input_tokens": 20_000,
        "output_tokens": 5_000,
    }
    # (50000*0.10 + 30000*0.125 + 20000*0.01 + 5000*0.50) / 1e6
    # = (5000 + 3750 + 200 + 2500) / 1e6 = 11450 / 1e6 = 0.01145
    cost_below = price_cost(usage_below, "claude-haiku-5-5")
    assert cost_below == pytest.approx(0.01145)

    # Above 100k pushed over by cache_read: prompt_length = 50k + 30k + 20001 = 100001 > 100k
    # Tier 2: in 0.50, write 0.625, read 0.05, out 2.50
    usage_pushed = {
        "input_tokens": 50_000,
        "cache_creation_input_tokens": 30_000,
        "cache_read_input_tokens": 20_001,
        "output_tokens": 5_000,
    }
    # (50000*0.50 + 30000*0.625 + 20001*0.05 + 5000*2.50) / 1e6
    # = (25000 + 18750 + 1000.05 + 12500) / 1e6 = 57250.05 / 1e6 = 0.05725005
    cost_pushed = price_cost(usage_pushed, "claude-haiku-5-5")
    assert cost_pushed == pytest.approx(0.05725005)

    # Direct large prompt above 100k
    usage_above = {
        "input_tokens": 120_000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 2_000,
    }
    # (120000*0.50 + 2000*2.50) / 1e6 = (60000 + 5000) / 1e6 = 0.065
    cost_above = price_cost(usage_above, "claude-haiku-5-5")
    assert cost_above == pytest.approx(0.065)


def test_cost_missing_cache_fields_and_invalid_values():
    # Missing cache fields count as 0
    usage = {"input_tokens": 100_000, "output_tokens": 10_000}
    # Opus: (100000*4.00 + 10000*20.00) / 1e6 = (400000 + 200000) / 1e6 = 0.6
    assert price_cost(usage, "claude-opus-5-5") == pytest.approx(0.6)

    # Non-count values return None
    assert price_cost({"input_tokens": "100", "output_tokens": 10}, "claude-opus-5-5") is None
    assert price_cost({"input_tokens": -1, "output_tokens": 10}, "claude-opus-5-5") is None
    assert price_cost({"input_tokens": 10, "output_tokens": True}, "claude-opus-5-5") is None
    assert price_cost({"input_tokens": 10, "output_tokens": 10, "cache_creation_input_tokens": None}, "claude-opus-5-5") is None
    assert price_cost({"input_tokens": 10, "output_tokens": 10, "cache_read_input_tokens": -5}, "claude-opus-5-5") is None
    assert price_cost("not a dict", "claude-opus-5-5") is None
    assert price_cost(usage, "unknown-model") is None
    assert price_cost(usage, {"input": 1}) is None
    assert price_cost(usage, (1, 1, 1, 1)) is None
    assert price_cost(usage, [1, 1, 1, 1]) is None


def test_unknown_model_raises_at_construction(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    with pytest.raises(ValueError) as exc:
        AnthropicMessagesBackend("claude-unknown-9", guard=SpendGuard(10), reasoning_effort="high")
    assert "unknown model: claude-unknown-9" in str(exc.value)


def test_spend_guard_resume_reads_injected_cost(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body())])
    backend.invoke("sys", "prompt")
    # Read events.jsonl to confirm injected cost
    events = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert len(events) == 1
    saved_body = json.loads(events[0]["stdout"])
    assert saved_body["usage"]["cost"] == pytest.approx(OPUS_COST)
    assert saved_body["usage"]["cost_source"] == "price_table"
    # Ensure all four token counts are saved
    assert all(k in saved_body["usage"] for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens"))

    # New SpendGuard on the same directory should recount the cost
    resumed_guard = SpendGuard(10, output_dir=tmp_path)
    assert resumed_guard.total == pytest.approx(OPUS_COST)


def test_key_hygiene_scrubbed_from_outputs_and_exceptions(tmp_path, monkeypatch):
    # Test key scrubbing in body error
    err_body = f"error with key {KEY} inside"
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(500, err_body)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert KEY not in str(exc.value)
    assert KEY not in exc.value.detail
    assert "[ANTHROPIC_API_KEY]" in exc.value.detail

    # Test key scrubbing in events.jsonl
    events_text = (tmp_path / "events.jsonl").read_text()
    assert KEY not in events_text


# --- evaluate.py integration and CLI tests ---

class AnthropicEvalFake:
    def __init__(self, content=INFEASIBLE, model=MODEL, effort="high", usage=None, stop_reason="end_turn", stop_details=None):
        self.content = content
        self.model = model
        self.effort = effort
        self.usage = usage
        self.stop_reason = stop_reason
        self.stop_details = stop_details
        self.urls = []
        self.payloads = []
        self.headers = []

    def __call__(self, req, *, timeout):
        self.urls.append(req.full_url)
        self.payloads.append(json.loads(req.data))
        self.headers.append(req.get_header("X-api-key"))
        reply = body(
            self.content,
            model=self.model,
            stop_reason=self.stop_reason,
            stop_details=self.stop_details,
            usage=self.usage or USAGE,
        )
        response = io.BytesIO(json.dumps(reply).encode())
        response.status = 200
        return response


@pytest.fixture
def fake_anthropic_urlopen(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("NFTB_ENDPOINT_TOKEN", raising=False)
    fake = AnthropicEvalFake()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def anthropic_args(out_dir, *extra, effort="high", max_usd="1", model=MODEL, instances="F1a_04"):
    argv = ["--tier", "synthetic", "--instances", instances, "--model", model, "--anthropic", "--out", str(out_dir)]
    if effort is not None:
        argv += ["--reasoning-effort", effort]
    if max_usd is not None:
        argv += ["--max-usd", max_usd]
    return [*argv, *extra]


def test_anthropic_run_records_identity_and_needs_no_other_key(tmp_path, fake_anthropic_urlopen):
    out_dir = tmp_path / "anthropic_out"
    assert evaluate.main(anthropic_args(out_dir)) == 0
    assert fake_anthropic_urlopen.urls and all(url == "https://api.anthropic.com/v1/messages" for url in fake_anthropic_urlopen.urls)
    for payload in fake_anthropic_urlopen.payloads:
        assert payload["thinking"] == {"type": "adaptive"}
        assert payload["output_config"] == {"effort": "high"}
        assert payload["cache_control"] == {"type": "ephemeral"}
        assert payload["max_tokens"] == 32000
        assert not {"temperature", "top_p", "seed", "fallbacks", "tool_choice", "tools"} & set(payload)
        assert isinstance(payload["system"], str)
        assert isinstance(payload["messages"], list)
    run = json.loads((out_dir / "run.json").read_text())
    assert run["provider"] == "anthropic" and run["model"] == MODEL
    assert run["sampling"] == {"reasoning_effort": "high", "max_tokens": 32000}
    assert run["prompt_layout"] == "items_v1"
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tasks_scored"] == 1 and summary["overall"]["success_count"] == 1
    saved = [json.loads(json.loads(line)["stdout"]) for line in (out_dir / "events.jsonl").read_text().splitlines()]
    assert saved and all(s["usage"]["cost_source"] == "price_table" for s in saved)


def test_anthropic_refusal_is_scored_model_failure_not_infra_error(tmp_path, fake_anthropic_urlopen):
    fake_anthropic_urlopen.stop_reason = "refusal"
    fake_anthropic_urlopen.stop_details = {"type": "safety_block", "category": "illicit"}
    out_dir = tmp_path / "refusal_out"
    # Model failure of the episode means evaluate.main completes the run (exit code 0) and records scored format_error
    assert evaluate.main(anthropic_args(out_dir)) == 0
    result = json.loads((out_dir / "F1a_04" / "result.json").read_text())
    assert result["status"] == "scored"
    assert result["success"] is False
    assert result["outcome"] == "format_error"
    assert result["error_kind"] == "model_refusal"
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["complete"] is True
    assert summary["tasks_scored"] == 1
    assert summary["tasks_errored"] == 0
    assert summary["overall"]["outcomes"]["format_error"] == 1


def test_anthropic_key_not_written_to_outputs(tmp_path, fake_anthropic_urlopen):
    fake_anthropic_urlopen.content = json.dumps({"name": "declare_infeasible", "arguments": {"reason": f"key {KEY}"}})
    out_dir = tmp_path / "key_out"
    assert evaluate.main(anthropic_args(out_dir)) == 0
    assert fake_anthropic_urlopen.headers and all(h == KEY for h in fake_anthropic_urlopen.headers)
    for root, _, files in os.walk(out_dir):
        for name in files:
            assert KEY not in (Path(root) / name).read_text(encoding="utf-8", errors="ignore"), name


def test_anthropic_missing_key_is_refused(tmp_path, monkeypatch, fake_anthropic_urlopen, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    assert evaluate.main(anthropic_args(tmp_path / "nokey")) == 1
    assert "ANTHROPIC_API_KEY is not set in the environment" in capsys.readouterr().err
    assert fake_anthropic_urlopen.payloads == []


def test_anthropic_reasoning_effort_max_allowed(tmp_path, fake_anthropic_urlopen):
    out_dir = tmp_path / "max_effort"
    assert evaluate.main(anthropic_args(out_dir, effort="max")) == 0
    run = json.loads((out_dir / "run.json").read_text())
    assert run["sampling"]["reasoning_effort"] == "max"


def test_openai_rejects_max_effort(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    argv = ["--tier", "synthetic", "--instances", "F1a_04", "--model", "gpt-4", "--openai",
            "--reasoning-effort", "max", "--price-per-mtok", "1,1,1", "--max-usd", "1", "--out", str(tmp_path / "openai_max")]
    assert evaluate.main(argv) == 1
    assert "reasoning-effort must be one of" in capsys.readouterr().err


def test_openrouter_rejects_max_effort(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", KEY)
    argv = ["--tier", "synthetic", "--instances", "F1a_04", "--model", "openai/gpt-4",
            "--reasoning-effort", "max", "--max-usd", "1", "--out", str(tmp_path / "openrouter_max")]
    assert evaluate.main(argv) == 1
    assert "reasoning-effort must be one of" in capsys.readouterr().err


@pytest.mark.parametrize("kwargs,extra", [
    ({"effort": None}, ()),
    ({"effort": "extreme"}, ()),
    ({"effort": "none"}, ()),
    ({"effort": "minimal"}, ()),
    ({"max_usd": None}, ()),
    ({"max_usd": "0"}, ()),
    ({"max_usd": "-1"}, ()),
    ({}, ("--seed", "1")),
    ({}, ("--temperature", "0.7")),
    ({}, ("--top-p", "0.9")),
    ({}, ("--openrouter-provider", "Anthropic")),
    ({}, ("--endpoint", "http://gpu-node-07:8000")),
    ({}, ("--openai",)),
    ({}, ("--price-per-mtok", "1,1,1")),
])
def test_anthropic_flag_combinations_refused(tmp_path, fake_anthropic_urlopen, kwargs, extra):
    out_dir = tmp_path / "bad_flags"
    assert evaluate.main(anthropic_args(out_dir, *extra, **kwargs)) != 0
    assert not (out_dir / "run.json").exists()
    assert fake_anthropic_urlopen.payloads == []


def test_anthropic_resume_with_same_identity_is_allowed(tmp_path, fake_anthropic_urlopen):
    out_dir = tmp_path / "same"
    assert evaluate.main(anthropic_args(out_dir)) == 0
    (out_dir / "F1a_04" / "result.json").write_text("not json")
    assert evaluate.main(anthropic_args(out_dir)) == 0
    assert json.loads((out_dir / "F1a_04" / "result.json").read_text())["status"] == "scored"


def test_anthropic_resume_refused_on_changed_effort(tmp_path, fake_anthropic_urlopen):
    out_dir = tmp_path / "resume"
    assert evaluate.main(anthropic_args(out_dir, effort="high")) == 0
    before = (out_dir / "run.json").read_text()
    calls = len(fake_anthropic_urlopen.payloads)
    assert evaluate.main(anthropic_args(out_dir, effort="medium")) == 1
    assert (out_dir / "run.json").read_text() == before
    assert len(fake_anthropic_urlopen.payloads) == calls


USAGE_MISS = {
    "input_tokens": 5000,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "output_tokens": 100,
}


def test_spend_guard_trip_direct():
    guard = SpendGuard(10)
    guard.trip("manual reason")
    assert guard.tripped is True
    assert guard.tripped_reason == "manual reason"
    with pytest.raises(CLIFailure) as exc:
        guard.add(1)
    assert exc.value.kind == "budget"
    assert exc.value.detail == "tripped: manual reason"


def test_three_consecutive_misses_trip_guard_and_next_add_raises(tmp_path, monkeypatch):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))])
    # Call 1: first call by this backend instance, not a miss
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Call 2: prompt >= 4096 and cache_read == 0 -> miss 1
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1

    # Call 3: prompt >= 4096 and cache_read == 0 -> miss 2
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2

    # Call 4: prompt >= 4096 and cache_read == 0 -> miss 3 trips guard and raises CLIFailure
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 4")
    assert exc.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc.value.detail
    assert backend.guard.consecutive_misses == 3

    # The next add() on the guard raises
    with pytest.raises(CLIFailure) as exc_add:
        backend.guard.add(0)
    assert exc_add.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc_add.value.detail


def test_cache_hit_resets_consecutive_miss_count(tmp_path, monkeypatch):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))])
    # Call 1: first call (not a miss)
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Call 2: miss 1
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1

    # Call 3: miss 2
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2

    # Call 4: cache hit with cache_read > 0 resets the count
    hit_reply = body(usage={
        "input_tokens": 2000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 4000,
        "output_tokens": 100,
    })
    backend._opener = FakeOpener([(200, hit_reply)])
    backend.invoke("sys", "prompt 4")
    assert backend.guard.consecutive_misses == 0

    # Call 5: miss 1 again (guard not tripped)
    backend._opener = FakeOpener([(200, body(usage=USAGE_MISS))])
    backend.invoke("sys", "prompt 5")
    assert backend.guard.consecutive_misses == 1


def test_first_call_and_small_prompts_never_count_as_misses(tmp_path, monkeypatch):
    small_usage = {
        "input_tokens": 1000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "output_tokens": 100,
    }
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))])
    # Episode's first call with prompt >= 4096 and cache_read == 0: never counts as miss
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Small prompts (< 4096) with cache_read == 0: never count as misses
    backend._opener = FakeOpener([(200, body(usage=small_usage))])
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 0

    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 0


def test_two_backends_sharing_guard_accumulate_misses(tmp_path, monkeypatch):
    guard = SpendGuard(10)
    backend1, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], guard=guard)
    backend2, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], guard=guard)

    # backend1 first call: not a miss
    backend1.invoke("sys", "b1 prompt 1")
    assert guard.consecutive_misses == 0

    # backend1 second call: miss 1
    backend1.invoke("sys", "b1 prompt 2")
    assert guard.consecutive_misses == 1

    # backend2 first call: not a miss
    backend2.invoke("sys", "b2 prompt 1")
    assert guard.consecutive_misses == 1

    # backend2 second call: miss 2
    backend2.invoke("sys", "b2 prompt 2")
    assert guard.consecutive_misses == 2

    # backend1 third call: miss 3 trips guard
    with pytest.raises(CLIFailure) as exc:
        backend1.invoke("sys", "b1 prompt 3")
    assert exc.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc.value.detail
    assert guard.consecutive_misses == 3

    # Next add() from backend2 or directly on guard raises
    with pytest.raises(CLIFailure) as exc2:
        backend2.guard.add(0)
    assert exc2.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc2.value.detail


class FakeClock:
    def __init__(self, start=1000.0):
        self.time = start

    def __call__(self):
        return self.time

    def advance(self, seconds):
        self.time += seconds


def test_three_consecutive_misses_with_gaps_over_270s_do_not_trip(tmp_path, monkeypatch):
    clock = FakeClock(1000.0)
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], clock=clock)
    # First call: first call of episode, not a miss
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Miss 1 with gap > 270 s
    clock.advance(300)
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 0

    # Miss 2 with gap > 270 s
    clock.advance(300)
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 0

    # Miss 3 with gap > 270 s
    clock.advance(300)
    backend.invoke("sys", "prompt 4")
    assert backend.guard.consecutive_misses == 0
    assert backend.guard.tripped is False


def test_three_consecutive_misses_with_short_gaps_trip_guard_fake_clock(tmp_path, monkeypatch):
    clock = FakeClock(1000.0)
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], clock=clock)
    # First call: not a miss
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Miss 1 with short gap (<= 270 s)
    clock.advance(10)
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1

    # Miss 2 with short gap (<= 270 s)
    clock.advance(10)
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2

    # Miss 3 with short gap (<= 270 s) trips guard
    clock.advance(10)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 4")
    assert exc.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc.value.detail
    assert backend.guard.consecutive_misses == 3
    assert backend.guard.tripped is True


def test_long_gap_miss_between_two_short_gap_misses_neither_resets_nor_increments(tmp_path, monkeypatch):
    clock = FakeClock(1000.0)
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], clock=clock)
    # First call
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Short-gap miss 1
    clock.advance(10)
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1

    # Long-gap miss (> 270 s): neither resets nor increments
    clock.advance(300)
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 1

    # Short-gap miss 2 (10 s from Call 3's attempt) -> increments to 2
    clock.advance(10)
    backend.invoke("sys", "prompt 4")
    assert backend.guard.consecutive_misses == 2

    # Short-gap miss 3 -> trips
    clock.advance(10)
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 5")
    assert exc.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc.value.detail
    assert backend.guard.consecutive_misses == 3


def test_long_gap_hit_neither_resets_nor_increments_counter(tmp_path, monkeypatch):
    clock = FakeClock(1000.0)
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], clock=clock)
    # First call
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Short-gap miss 1
    clock.advance(10)
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1

    # Long-gap hit (> 270 s): neither resets nor increments
    clock.advance(300)
    hit_reply = body(usage={
        "input_tokens": 2000,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 4000,
        "output_tokens": 100,
    })
    backend._opener = FakeOpener([(200, hit_reply)])
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 1

    # Short-gap miss 2
    clock.advance(10)
    backend._opener = FakeOpener([(200, body(usage=USAGE_MISS))])
    backend.invoke("sys", "prompt 4")
    assert backend.guard.consecutive_misses == 2


def test_retry_after_timeout_longer_than_270s_does_not_trip_guard(tmp_path, monkeypatch):
    clock = FakeClock(1000.0)
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], clock=clock)
    # First call of episode: not counted as miss
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0
    assert backend._last_cache_touch == 1000.0

    # Two counted short-gap misses
    clock.advance(10)
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1
    assert backend._last_cache_touch == 1010.0

    clock.advance(10)
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2
    assert backend._last_cache_touch == 1020.0

    # Next invoke: attempt 0 starts at 1030.0, times out after 301 s (no 200)
    clock.advance(10)

    def on_timeout():
        clock.advance(301)
        raise TimeoutError("connection timed out")

    backend._opener = FakeOpener([on_timeout, (200, body(usage=USAGE_MISS))])
    # Retry starts at 1030 + 301 = 1331.0, which is > 270 s after last 200 attempt (1020.0).
    # Cache_read is 0 -> expired, so not counted and guard does not trip.
    inv = backend.invoke("sys", "prompt 4")
    assert inv.text == FINISH
    assert backend.guard.consecutive_misses == 2
    assert backend.guard.tripped is False
    assert backend._last_cache_touch == 1331.0

    # A subsequent short-gap miss (<= 270 s from the retry at 1331.0) trips the guard
    clock.advance(10)
    backend._opener = FakeOpener([(200, body(usage=USAGE_MISS))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 5")
    assert exc.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc.value.detail
    assert backend.guard.consecutive_misses == 3
    assert backend.guard.tripped is True


def test_retry_after_timeout_within_270s_counts_as_miss_and_trips_guard(tmp_path, monkeypatch):
    clock = FakeClock(1000.0)
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body(usage=USAGE_MISS))], clock=clock)
    # First call
    backend.invoke("sys", "prompt 1")
    assert backend.guard.consecutive_misses == 0

    # Short-gap miss 1
    clock.advance(10)
    backend.invoke("sys", "prompt 2")
    assert backend.guard.consecutive_misses == 1

    # Short-gap miss 2
    clock.advance(10)
    backend.invoke("sys", "prompt 3")
    assert backend.guard.consecutive_misses == 2
    assert backend._last_cache_touch == 1020.0

    # Next invoke: attempt 0 times out after only 5 s (total gap 10 + 5 = 15 s <= 270 s)
    clock.advance(10)

    def on_short_timeout():
        clock.advance(5)
        raise TimeoutError("connection timed out")

    backend._opener = FakeOpener([on_short_timeout, (200, body(usage=USAGE_MISS))])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt 4")
    assert exc.value.kind == "budget"
    assert "tripped: 3 consecutive cache misses" in exc.value.detail
    assert backend.guard.consecutive_misses == 3
    assert backend.guard.tripped is True


def test_stop_reason_model_context_window_exceeded_raises_context_limit_no_retry(tmp_path, monkeypatch):
    reply = body(stop_reason="model_context_window_exceeded")
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "context_limit"
    assert "model context window exceeded" in exc.value.detail
    assert len(fake.calls) == 1
    assert waits == []


def test_stop_reason_model_context_window_exceeded_with_details_raises_context_limit_no_retry(tmp_path, monkeypatch):
    reply = body(stop_reason="model_context_window_exceeded", stop_details={"type": "context_exceeded"})
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as exc:
        backend.invoke("sys", "prompt")
    assert exc.value.kind == "context_limit"
    assert "context_exceeded" in exc.value.detail
    assert len(fake.calls) == 1
    assert waits == []


def test_anthropic_context_window_exceeded_in_evaluate_maps_to_scored_turn_limit(tmp_path, fake_anthropic_urlopen):
    fake_anthropic_urlopen.stop_reason = "model_context_window_exceeded"
    out_dir = tmp_path / "context_out"
    assert evaluate.main(anthropic_args(out_dir)) == 0
    result = json.loads((out_dir / "F1a_04" / "result.json").read_text())
    assert result["status"] == "scored"
    assert result["success"] is False
    assert result["outcome"] == "turn_limit"
    assert result["error_kind"] == "context_limit"
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["complete"] is True
    assert summary["tasks_scored"] == 1
    assert summary["tasks_errored"] == 0
    assert summary["overall"]["outcomes"]["turn_limit"] == 1
    assert len(fake_anthropic_urlopen.urls) == 1
