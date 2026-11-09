"""The OpenAI Responses backend and its evaluate flags. Every HTTP reply here is local fake data."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
from urllib import error as url_error

import pytest

from nftbench import evaluate
from nftbench.agents.cli import CLIFailure, render_items
from nftbench.agents.openai_responses import OpenAIResponsesBackend, price_cost
from nftbench.agents.openrouter import SpendGuard

MODEL = "gpt-6.1-sol"
KEY = "test-only-openai-key-do-not-leak"
PRICES = (1.25, 0.125, 10.0)
FINISH = json.dumps({"name": "finish_task", "arguments": {"summary": "done"}})
INFEASIBLE = json.dumps({"name": "declare_infeasible", "arguments": {"reason": "mock infeasible"}})
USAGE = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 200}, "output_tokens": 500,
         "output_tokens_details": {"reasoning_tokens": 300}, "total_tokens": 1500}
# (800 * 1.25 + 200 * 0.125 + 500 * 10) / 1e6
USAGE_COST = 0.006025


def body(text=FINISH, *, model=MODEL + "-2026-10-01", effort="high", status="completed", reason=None, usage="default",
         error=None):
    output = [{"type": "reasoning", "id": "rs_1", "summary": []}]
    if text is not None:
        output.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text, "annotations": []}]})
    reply = {
        "id": "resp_1", "object": "response", "model": model, "status": status, "error": error,
        "incomplete_details": {"reason": reason} if reason else None, "reasoning": {"effort": effort, "summary": None},
        "output": output,
    }
    reply["usage"] = USAGE if usage == "default" else usage
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


def make_backend(tmp_path, monkeypatch, replies, *, cap=10, guard=None, **kwargs):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    fake = FakeOpener(replies)
    waits = []
    options = {"reasoning_effort": "high", "prices": PRICES}
    options.update(kwargs)
    backend = OpenAIResponsesBackend(
        MODEL, guard=guard or SpendGuard(cap), events_path=tmp_path / "events.jsonl", opener=fake, sleep=waits.append, **options,
    )
    return backend, fake, waits


def events(tmp_path):
    return [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]


def test_request_maps_roles_and_sends_effort_without_sampling_by_default(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body())])
    request = json.dumps({"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"},
                                       {"role": "assistant", "content": "A"}], "tools": []})
    result = json.loads(backend.complete(request))
    req, timeout = fake.calls[0]
    head, items = render_items(request)
    assert items == [{"role": "user", "content": json.dumps({"role": "user", "content": "U"}, indent=1)},
                     {"role": "assistant", "content": json.dumps({"role": "assistant", "content": "A"}, indent=1)}]
    assert req.full_url == "https://api.openai.com/v1/responses" and timeout == 600
    assert req.get_header("Authorization") == f"Bearer {KEY}"
    assert json.loads(req.data) == {
        "model": MODEL, "input": [{"role": "developer", "content": head}, *items],
        "reasoning": {"effort": "high"}, "max_output_tokens": 32000, "store": False,
    }
    assert result["tool_calls"][0]["name"] == "finish_task"
    assert result["usage"]["model"] == MODEL + "-2026-10-01"
    assert result["usage"]["usage"]["cost"] == pytest.approx(USAGE_COST)
    assert waits == []


def test_temperature_top_p_and_max_tokens_are_sent_when_given(tmp_path, monkeypatch):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body())], temperature=0.6, top_p=0.95, max_tokens=4096)
    backend.invoke("S", "P")
    sent = json.loads(fake.calls[0][0].data)
    assert sent["temperature"] == 0.6 and sent["top_p"] == 0.95 and sent["max_output_tokens"] == 4096
    assert "seed" not in sent and "max_tokens" not in sent


def test_output_text_concatenates_message_parts_and_ignores_reasoning(tmp_path, monkeypatch):
    reply = body(None)
    reply["output"] += [
        {"type": "message", "content": [{"type": "output_text", "text": '{"name": "finish_task", '},
                                        {"type": "refusal", "refusal": "no"}]},
        {"type": "reasoning", "content": [{"type": "output_text", "text": "IGNORED"}]},
        {"type": "message", "content": [{"type": "output_text", "text": '"arguments": {"summary": "done"}}'}]},
    ]
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    assert json.loads(backend.invoke("S", "P").text) == {"name": "finish_task", "arguments": {"summary": "done"}}


def test_requires_environment_key(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(CLIFailure) as failure:
        OpenAIResponsesBackend(MODEL, guard=SpendGuard(1), reasoning_effort="high", prices=PRICES)
    assert failure.value.kind == "auth_or_quota"


@pytest.mark.parametrize("served,ok", [
    (MODEL, True),
    (MODEL + "-2026-10-01", True),
    (MODEL + "x", False),
    (MODEL + "-mini", False),
    (MODEL + "-preview", False),
    (MODEL + "-2026-10", False),
    (MODEL + "-mini-2026-10-01", False),
    ("gpt-6.1", False),
    ("gpt-6.1-terra", False),
    (None, False),
])
def test_served_model_must_be_the_requested_one_or_its_snapshot(tmp_path, monkeypatch, served, ok):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(model=served))])
    if ok:
        assert backend.invoke("S", "P").usage["model"] == served
    else:
        with pytest.raises(CLIFailure) as failure:
            backend.invoke("S", "P")
        assert failure.value.kind == "model_mismatch"
        assert len(fake.calls) == 1 and waits == []


@pytest.mark.parametrize("effort", ["medium", None])
def test_echoed_effort_must_match(tmp_path, monkeypatch, effort):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(effort=effort))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "reasoning_mismatch" and len(fake.calls) == 1


def test_missing_reasoning_object_is_an_effort_mismatch(tmp_path, monkeypatch):
    reply = body()
    del reply["reasoning"]
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "reasoning_mismatch"


@pytest.mark.parametrize("reason", ["max_output_tokens", "content_filter"])
def test_incomplete_for_the_models_reasons_is_retried_then_model_format(tmp_path, monkeypatch, reason):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body("partial", status="incomplete", reason=reason))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_format"
    assert len(fake.calls) == 4 and waits == [5, 20, 60]


def test_incomplete_then_complete_succeeds(tmp_path, monkeypatch):
    replies = [(200, body("partial", status="incomplete", reason="max_output_tokens")), (200, body())]
    backend, fake, waits = make_backend(tmp_path, monkeypatch, replies)
    assert backend.invoke("S", "P").text == FINISH
    assert waits == [5]
    # Both attempts are charged.
    assert backend.guard.total == pytest.approx(2 * USAGE_COST)


def test_incomplete_for_another_reason_is_incomplete_output(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body("x", status="incomplete", reason="server_overload"))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "incomplete_output"


@pytest.mark.parametrize("reply", [
    body(None, status="failed", error={"code": "server_error", "message": "boom"}, usage=None),
    body(status="completed", error={"code": "server_error", "message": "boom"}),
    body(None, status="failed", usage=None),
])
def test_failed_response_or_error_object_is_incomplete_output(tmp_path, monkeypatch, reply):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "incomplete_output" and len(fake.calls) == 4


def test_null_error_field_is_not_a_failure(tmp_path, monkeypatch):
    reply = body()
    assert reply["error"] is None
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, reply)])
    assert backend.invoke("S", "P").text == FINISH


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_retried_then_model_format(tmp_path, monkeypatch, text):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(text))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_format" and len(fake.calls) == 4


def test_reply_with_only_reasoning_items_is_model_format(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(None))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_format"


@pytest.mark.parametrize("mutate", [
    lambda r: r["output"][1]["content"][0].update(text=0),
    lambda r: r["output"][1]["content"][0].update(text=None),
    lambda r: r["output"][1]["content"][0].update(text={"x": 1}),
    lambda r: r["output"][1]["content"][0].pop("text"),
    lambda r: r["output"][1].pop("content"),
    lambda r: r["output"][1].update(content=None),
    lambda r: r.update(output={"type": "message"}),
    lambda r: r["output"].append("not an item"),
    lambda r: r["output"][1].update(content={}),
    lambda r: r["output"][1].update(content="text"),
    lambda r: r["output"][1]["content"].append("not a part"),
    lambda r: r.update(output=None),
    lambda r: r.pop("output"),
])
def test_malformed_output_is_incomplete_output_without_retry(tmp_path, monkeypatch, mutate):
    reply = body()
    mutate(reply)
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, reply)])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "incomplete_output" and len(fake.calls) == 1 and waits == []


@pytest.mark.parametrize("usage", [
    None,
    {"output_tokens": 5},
    {"input_tokens": 5},
    {"input_tokens": 5.0, "output_tokens": 5},
    {"input_tokens": True, "output_tokens": 5},
    {"input_tokens": 5, "output_tokens": -1},
    {"input_tokens": 5, "output_tokens": 5, "input_tokens_details": {"cached_tokens": 6}},
    {"input_tokens": 5, "output_tokens": 5, "input_tokens_details": {"cached_tokens": "1"}},
    {"input_tokens": 5, "output_tokens": 5, "input_tokens_details": []},
])
def test_unusable_usage_is_incomplete_output(tmp_path, monkeypatch, usage):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(200, body(usage=usage))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "incomplete_output" and len(fake.calls) == 1


def test_cost_counts_cached_tokens_at_the_cached_price():
    assert price_cost(USAGE, PRICES) == pytest.approx(USAGE_COST)
    no_cache = {"input_tokens": 1000, "output_tokens": 500}
    assert price_cost(no_cache, PRICES) == pytest.approx((1000 * 1.25 + 500 * 10) / 1e6)
    all_cached = {"input_tokens": 1000, "input_tokens_details": {"cached_tokens": 1000}, "output_tokens": 0}
    assert price_cost(all_cached, PRICES) == pytest.approx(1000 * 0.125 / 1e6)
    assert price_cost({"input_tokens": 0, "output_tokens": 0}, PRICES) == 0


def test_event_record_carries_price_table_cost_that_spend_guard_resumes(tmp_path, monkeypatch):
    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body())])
    backend.invoke("S", "P")
    (record,) = events(tmp_path)
    assert set(record) == {"returncode", "stdout", "stderr", "instance_id", "t_start", "t_end"} and record["returncode"] == 0
    saved = json.loads(record["stdout"])
    assert saved["usage"]["cost"] == pytest.approx(USAGE_COST) and saved["usage"]["cost_source"] == "price_table"
    assert saved["usage"]["input_tokens_details"] == {"cached_tokens": 200}
    assert saved["model"] == MODEL + "-2026-10-01"
    assert SpendGuard(1, tmp_path).total == pytest.approx(USAGE_COST)


def test_guard_reaches_cap_and_blocks_later_calls(tmp_path, monkeypatch):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body())], cap=0.01)
    assert backend.invoke("S", "P").text == FINISH
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "budget"
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "budget"
    assert len(fake.calls) == 2


def test_resumed_guard_refuses_before_any_call(tmp_path, monkeypatch):
    first, _, _ = make_backend(tmp_path, monkeypatch, [(200, body())])
    first.invoke("S", "P")
    first.invoke("S", "P")
    backend, fake, _ = make_backend(tmp_path, monkeypatch, [(200, body())], guard=SpendGuard(0.012, tmp_path))
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "budget" and fake.calls == []


@pytest.mark.parametrize("reply", [
    (429, {"error": {"message": "rate limited"}}),
    (500, {"error": {"message": "server"}}),
    (503, "unavailable"),
    url_error.URLError("timed out"),
    TimeoutError("timed out"),
])
def test_rate_limits_server_errors_and_timeouts_retry_then_cli_error(tmp_path, monkeypatch, reply):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [reply])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "cli_error" and len(fake.calls) == 4 and waits == [5, 20, 60]


def test_server_error_then_success(tmp_path, monkeypatch):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(503, "unavailable"), (200, body())])
    assert backend.invoke("S", "P").text == FINISH
    assert waits == [5] and [r["returncode"] for r in events(tmp_path)] == [503, 0]


@pytest.mark.parametrize("status,kind", [(400, "cli_error"), (404, "cli_error"), (401, "auth_or_quota"), (403, "auth_or_quota")])
def test_client_errors_are_infra_and_not_retried(tmp_path, monkeypatch, status, kind):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(status, {"error": {"message": "bad request"}})])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == kind and failure.value.kind != "model_format"
    assert len(fake.calls) == 1 and waits == []


@pytest.mark.parametrize("reply,kind", [
    ({"error": {"message": "Your input exceeds the context window of this model. Please adjust your input and try again.",
                "type": "invalid_request_error", "param": "input", "code": "context_length_exceeded"}}, "context_limit"),
    ({"error": {"message": "This model's maximum context length is 128000 tokens.", "type": "invalid_request_error",
                "param": "input", "code": None}}, "context_limit"),
    ({"error": {"message": "Unsupported parameter: 'temperature'", "type": "invalid_request_error", "param": "temperature",
                "code": "unsupported_parameter"}}, "cli_error"),
])
def test_context_window_400_is_context_limit_without_retry(tmp_path, monkeypatch, reply, kind):
    backend, fake, waits = make_backend(tmp_path, monkeypatch, [(400, reply)])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == kind and len(fake.calls) == 1 and waits == []

@pytest.mark.parametrize("replies,kind", [
    ([(200, body("partial", status="incomplete", reason="max_output_tokens"))] * 3 + [(503, "unavailable")], "cli_error"),
    ([(503, "unavailable")] * 3 + [(200, body(""))], "model_format"),
    ([(200, body(""))] * 3 + [(200, body(None, status="failed", usage=None))], "incomplete_output"),
    ([(200, body(None, status="failed", usage=None))] * 3 + [(200, body("x", status="incomplete", reason="content_filter"))],
     "model_format"),
])
def test_final_kind_follows_the_last_attempt(tmp_path, monkeypatch, replies, kind):
    backend, fake, _ = make_backend(tmp_path, monkeypatch, replies + [replies[-1]])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == kind and len(fake.calls) == 4


def test_key_is_scrubbed_from_replies_events_and_errors(tmp_path, monkeypatch):
    escaped = "".join(f"\\u{ord(c):04x}" for c in KEY)
    replies = [
        (500, f'{{"error": "bad key {escaped}"}}'),
        (200, body(json.dumps({"name": "finish_task", "arguments": {"summary": f"saw {KEY}"}}))),
    ]
    backend, _, _ = make_backend(tmp_path, monkeypatch, replies)
    invocation = backend.invoke("S", "P")
    assert KEY not in invocation.text and "[OPENAI_API_KEY]" in invocation.text
    saved = (tmp_path / "events.jsonl").read_text()
    assert KEY not in saved and escaped not in saved

    backend, _, _ = make_backend(tmp_path, monkeypatch, [(200, body(model=f"model-{KEY}"))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_mismatch" and KEY not in str(failure.value)

    backend, _, _ = make_backend(tmp_path, monkeypatch, [url_error.URLError(KEY)])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert KEY not in str(failure.value)


def test_unknown_effort_is_refused(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    with pytest.raises(ValueError):
        OpenAIResponsesBackend(MODEL, guard=SpendGuard(1), reasoning_effort="extreme", prices=PRICES)


# evaluate.py: --openai mode with a fake urlopen.

class EvalFake:
    def __init__(self):
        self.content, self.effort, self.usage, self.model = INFEASIBLE, "high", "default", MODEL + "-2026-10-01"
        self.urls, self.payloads, self.headers = [], [], []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        self.payloads.append(json.loads(req.data))
        self.headers.append(req.get_header("Authorization"))
        reply = body(self.content, effort=self.effort, usage=self.usage, model=self.model)
        response = io.BytesIO(json.dumps(reply).encode())
        response.status = 200
        return response


@pytest.fixture
def fake_urlopen(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NFTB_ENDPOINT_TOKEN", raising=False)
    fake = EvalFake()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def openai_args(out_dir, *extra, effort="high", prices="1.25,0.125,10", max_usd="1", instances="F1a_04"):
    argv = ["--tier", "synthetic", "--instances", instances, "--model", MODEL, "--openai", "--out", str(out_dir)]
    if effort is not None:
        argv += ["--reasoning-effort", effort]
    if prices is not None:
        argv += ["--price-per-mtok", prices]
    if max_usd is not None:
        argv += ["--max-usd", max_usd]
    return [*argv, *extra]


def test_openai_run_records_identity_and_needs_no_openrouter_key(tmp_path, fake_urlopen):
    out_dir = tmp_path / "openai_out"
    assert evaluate.main(openai_args(out_dir)) == 0
    assert fake_urlopen.urls and all(url == "https://api.openai.com/v1/responses" for url in fake_urlopen.urls)
    for payload in fake_urlopen.payloads:
        assert payload["reasoning"] == {"effort": "high"} and payload["max_output_tokens"] == 32000
        assert payload["store"] is False and not {"temperature", "top_p", "seed"} & set(payload)
        assert [m["role"] for m in payload["input"]] == ["developer", "user"]
    run = json.loads((out_dir / "run.json").read_text())
    assert run["provider"] == "openai" and run["model"] == MODEL
    assert run["sampling"] == {"reasoning_effort": "high", "max_tokens": 32000}
    assert run["prices"] == {"input": 1.25, "cached_input": 0.125, "output": 10.0}
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tasks_scored"] == 1 and summary["overall"]["success_count"] == 1
    saved = [json.loads(json.loads(line)["stdout"]) for line in (out_dir / "events.jsonl").read_text().splitlines()]
    assert saved and all(s["usage"]["cost_source"] == "price_table" for s in saved)


def test_openai_sends_and_records_optional_sampling(tmp_path, fake_urlopen):
    out_dir = tmp_path / "sampled"
    assert evaluate.main(openai_args(out_dir, "--temperature", "0.6", "--top-p", "0.95", "--max-tokens", "4096")) == 0
    for payload in fake_urlopen.payloads:
        assert payload["temperature"] == 0.6 and payload["top_p"] == 0.95 and payload["max_output_tokens"] == 4096
    run = json.loads((out_dir / "run.json").read_text())
    assert run["sampling"] == {"reasoning_effort": "high", "temperature": 0.6, "top_p": 0.95, "max_tokens": 4096}


def test_openai_effort_echo_mismatch_errors_the_task(tmp_path, fake_urlopen):
    fake_urlopen.effort = "medium"
    out_dir = tmp_path / "echo"
    assert evaluate.main(openai_args(out_dir)) == 1
    result = json.loads((out_dir / "F1a_04" / "result.json").read_text())
    assert result["status"] == "error" and result["outcome"] == "infra_error" and "reasoning_mismatch" in result["error"]


def test_openai_empty_text_is_a_scored_format_error(tmp_path, monkeypatch, fake_urlopen):
    monkeypatch.setattr("time.sleep", lambda _: None)
    fake_urlopen.content = ""
    out_dir = tmp_path / "empty"
    assert evaluate.main(openai_args(out_dir)) == 0
    result = json.loads((out_dir / "F1a_04" / "result.json").read_text())
    assert result["status"] == "scored" and result["outcome"] == "format_error"
    assert len(fake_urlopen.payloads) == 4


def test_openai_missing_key_is_refused(tmp_path, monkeypatch, fake_urlopen, capsys):
    monkeypatch.delenv("OPENAI_API_KEY")
    assert evaluate.main(openai_args(tmp_path / "nokey")) == 1
    assert "OPENAI_API_KEY is not set in the environment" in capsys.readouterr().err
    assert fake_urlopen.payloads == []


def test_openai_spend_cap_holds_across_resume_and_may_be_raised(tmp_path, fake_urlopen):
    # Each reply costs 0.6 USD at these prices: 1000 input tokens at 600 USD per million.
    fake_urlopen.usage = {"input_tokens": 1000, "output_tokens": 0}
    out_dir = tmp_path / "capped"
    argv = dict(prices="600,0,0", instances="F1a_04,F1a_01")
    assert evaluate.main(openai_args(out_dir, **argv)) == 1
    assert len(fake_urlopen.payloads) == 2
    assert json.loads((out_dir / "F1a_04" / "result.json").read_text())["status"] == "scored"
    capped = json.loads((out_dir / "F1a_01" / "result.json").read_text())
    assert capped["status"] == "error" and "budget" in capped["error"]
    assert SpendGuard(10, out_dir).total == pytest.approx(1.2)

    assert evaluate.main(openai_args(out_dir, **argv)) == 1
    assert len(fake_urlopen.payloads) == 2

    assert evaluate.main(openai_args(out_dir, max_usd="5", **argv)) == 0
    assert len(fake_urlopen.payloads) == 3
    assert json.loads((out_dir / "F1a_01" / "result.json").read_text())["status"] == "scored"


@pytest.mark.parametrize("changed", [
    {"effort": "medium"},
    {"prices": "1.25,0.125,12"},
    {"extra": ("--temperature", "0.6")},
    {"extra": ("--max-tokens", "1000")},
])
def test_openai_resume_refused_on_changed_identity(tmp_path, fake_urlopen, changed):
    out_dir = tmp_path / "resume"
    assert evaluate.main(openai_args(out_dir)) == 0
    before = (out_dir / "run.json").read_text()
    calls = len(fake_urlopen.payloads)
    extra = changed.pop("extra", ())
    assert evaluate.main(openai_args(out_dir, *extra, **changed)) == 1
    assert (out_dir / "run.json").read_text() == before
    assert len(fake_urlopen.payloads) == calls


def test_openai_resume_with_same_identity_is_allowed(tmp_path, fake_urlopen):
    out_dir = tmp_path / "same"
    assert evaluate.main(openai_args(out_dir)) == 0
    (out_dir / "F1a_04" / "result.json").write_text("not json")
    assert evaluate.main(openai_args(out_dir)) == 0
    assert json.loads((out_dir / "F1a_04" / "result.json").read_text())["status"] == "scored"


def test_openai_run_records_items_layout_and_refuses_a_blob_layout_dir(tmp_path, fake_urlopen):
    out_dir = tmp_path / "layout"
    assert evaluate.main(openai_args(out_dir)) == 0
    run = json.loads((out_dir / "run.json").read_text())
    assert run["prompt_layout"] == "items_v1"
    calls = len(fake_urlopen.payloads)
    for layout in (None, "blob_v0"):
        old = {key: value for key, value in run.items() if key != "prompt_layout"}
        if layout is not None:
            old["prompt_layout"] = layout
        (out_dir / "run.json").write_text(json.dumps(old))
        assert evaluate.main(openai_args(out_dir)) == 1
        assert json.loads((out_dir / "run.json").read_text()) == old
    assert len(fake_urlopen.payloads) == calls


def test_openai_and_openrouter_dirs_do_not_resume_each_other(tmp_path, monkeypatch, fake_urlopen):
    out_dir = tmp_path / "openai_first"
    assert evaluate.main(openai_args(out_dir)) == 0
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy_openrouter_key_for_cross_resume")
    argv = ["--tier", "synthetic", "--instances", "F1a_04", "--model", MODEL, "--max-usd", "1", "--out", str(out_dir)]
    assert evaluate.main(argv) == 1
    assert evaluate.main([*argv, "--reasoning-effort", "high"]) == 1


def test_openai_key_not_written_to_outputs(tmp_path, fake_urlopen):
    fake_urlopen.content = json.dumps({"name": "declare_infeasible", "arguments": {"reason": f"key {KEY}"}})
    out_dir = tmp_path / "key_out"
    assert evaluate.main(openai_args(out_dir)) == 0
    assert fake_urlopen.headers and all(h == f"Bearer {KEY}" for h in fake_urlopen.headers)
    for root, _, files in os.walk(out_dir):
        for name in files:
            assert KEY not in (Path(root) / name).read_text(encoding="utf-8", errors="ignore"), name


@pytest.mark.parametrize("argv_extra,ok", [(("--concurrency", "4"), True), (("--concurrency", "5"), False)])
def test_openai_concurrency_limit(tmp_path, fake_urlopen, argv_extra, ok):
    out_dir = tmp_path / "conc"
    assert (evaluate.main(openai_args(out_dir, *argv_extra)) == 0) is ok
    assert (out_dir / "run.json").exists() is ok


@pytest.mark.parametrize("kwargs,extra", [
    ({"effort": None}, ()),
    ({"prices": None}, ()),
    ({"max_usd": None}, ()),
    ({"max_usd": "0"}, ()),
    ({"prices": "1,2"}, ()),
    ({"prices": "1,2,3,4"}, ()),
    ({"prices": "1,x,3"}, ()),
    ({"prices": "1,-0.1,3"}, ()),
    ({"prices": "1,nan,3"}, ()),
    ({"prices": "1,inf,3"}, ()),
    ({"effort": "extreme"}, ()),
    ({}, ("--seed", "1")),
    ({}, ("--openrouter-provider", "CoreWeave")),
    ({}, ("--endpoint", "http://gpu-node-07:8000")),
])
def test_openai_flag_combinations_refused(tmp_path, fake_urlopen, kwargs, extra):
    out_dir = tmp_path / "bad_flags"
    assert evaluate.main(openai_args(out_dir, *extra, **kwargs)) != 0
    assert not (out_dir / "run.json").exists()
    assert fake_urlopen.payloads == []


@pytest.mark.parametrize("argv", [
    ["--agent", "reference", "--openai"],
    ["--agent", "reference", "--reasoning-effort", "high"],
    ["--agent", "reference", "--price-per-mtok", "1,1,1"],
    ["--model", MODEL, "--max-usd", "1", "--price-per-mtok", "1,1,1"],
    ["--model", MODEL, "--endpoint", "http://gpu-node-07:8000", "--temperature", "0.6", "--top-p", "0.95",
     "--reasoning-effort", "high"],
])
def test_openai_only_flags_refused_elsewhere(tmp_path, monkeypatch, fake_urlopen, argv):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy_openrouter_key_for_flags")
    out_dir = tmp_path / "bad_flags"
    assert evaluate.main(["--tier", "synthetic", "--instances", "F1a_04", "--out", str(out_dir), *argv]) == 1
    assert not (out_dir / "run.json").exists()
    assert fake_urlopen.payloads == []
