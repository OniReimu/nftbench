"""The OpenAI-compatible endpoint backend and its evaluate flags. Every HTTP reply here is local fake data."""

from __future__ import annotations

import io
import json
import os
import threading
from pathlib import Path
from urllib import error as url_error

import pytest

from nftbench import evaluate
from nftbench.agents.cli import CURRENT_TASK, CLIFailure, render_items
from nftbench.agents.openai_compat import OpenAICompatBackend
from nftbench.agents.openrouter import OpenRouterBackend, SpendGuard

MODEL = "Qwen/Qwen3-32B"
BASE = "http://gpu-node-07:8000"
TOKEN = "test-only-endpoint-token"
FINISH = json.dumps({"name": "finish_task", "arguments": {"summary": "done"}})
INFEASIBLE = json.dumps({"name": "declare_infeasible", "arguments": {"reason": "mock infeasible"}})


def body(text=FINISH, *, model=MODEL, usage="default", finish_reason="stop"):
    reply = {"model": model, "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish_reason}]}
    if usage == "default":
        usage = {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    if usage is not None:
        reply["usage"] = usage
    return reply


class FakeOpener:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def __call__(self, req, *, timeout):
        self.calls.append((req, timeout))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        status, data = reply
        raw = json.dumps(data) if not isinstance(data, str) else data
        if status >= 400:
            raise url_error.HTTPError(req.full_url, status, "fake HTTP error", {}, io.BytesIO(raw.encode()))
        response = io.BytesIO(raw.encode())
        response.status = status
        return response


def make_backend(tmp_path, replies, **kwargs):
    fake = FakeOpener(replies)
    waits = []
    options = {"temperature": 0.6, "top_p": 0.95, "seed": 7}
    options.update(kwargs)
    backend = OpenAICompatBackend(BASE, MODEL, events_path=tmp_path / "events.jsonl", opener=fake, sleep=waits.append, **options)
    return backend, fake, waits


def test_successful_turn_sends_rendered_items_and_sampling(tmp_path, monkeypatch):
    monkeypatch.delenv("NFTB_ENDPOINT_TOKEN", raising=False)
    backend, fake, waits = make_backend(tmp_path, [(200, body())])
    request = json.dumps({"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "tools": []})
    result = json.loads(backend.complete(request))
    req, timeout = fake.calls[0]
    head, items = render_items(request)
    assert req.full_url == f"{BASE}/v1/chat/completions"
    assert req.get_header("Authorization") is None
    assert timeout == 600
    assert json.loads(req.data) == {
        "model": MODEL, "messages": [{"role": "system", "content": head}, *items],
        "max_tokens": 32000, "temperature": 0.6, "top_p": 0.95, "seed": 7,
    }
    assert result["tool_calls"][0]["name"] == "finish_task"
    assert result["usage"]["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
    assert waits == []
    record = json.loads((tmp_path / "events.jsonl").read_text())
    assert set(record) == {"returncode", "stdout", "stderr", "instance_id", "t_start", "t_end"}
    assert record["returncode"] == 0 and record["stdout"] == json.dumps(body())


def test_seed_omitted_when_not_given_and_trailing_slash_ignored(tmp_path, monkeypatch):
    monkeypatch.delenv("NFTB_ENDPOINT_TOKEN", raising=False)
    fake = FakeOpener([(200, body())])
    backend = OpenAICompatBackend(BASE + "/", MODEL, temperature=0.0, top_p=1.0, max_tokens=512, opener=fake, sleep=lambda _: None)
    backend.invoke("S", "P")
    req, _ = fake.calls[0]
    assert req.full_url == f"{BASE}/v1/chat/completions"
    sent = json.loads(req.data)
    assert "seed" not in sent and sent["max_tokens"] == 512 and sent["temperature"] == 0.0


class CostOpener(FakeOpener):
    """FakeOpener whose replies carry the usage.cost OpenRouter requires."""

    def __call__(self, req, *, timeout):
        self.calls.append((req, timeout))
        response = io.BytesIO(json.dumps(body(usage={"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.001})).encode())
        response.status = 200
        return response


def test_messages_match_the_openrouter_backend_for_the_same_request(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy_openrouter_key_for_layout")
    request = json.dumps({
        "messages": [
            {"role": "system", "content": "S"},
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_state", "arguments": {}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{\"balance\": 1}"},
        ],
        "tools": [{"name": "read_state", "description": "d", "parameters": {"type": "object", "properties": {}}}],
    })
    compat_opener = FakeOpener([(200, body())])
    OpenAICompatBackend(BASE, MODEL, opener=compat_opener, sleep=lambda _: None).complete(request)
    openrouter_opener = CostOpener([])
    OpenRouterBackend(
        MODEL, guard=SpendGuard(1.0), opener=openrouter_opener, sleep=lambda _: None, extra={"reasoning": {"effort": "high"}},
    ).complete(request)
    compat = json.loads(compat_opener.calls[0][0].data)
    openrouter = json.loads(openrouter_opener.calls[0][0].data)
    assert compat["messages"] == openrouter["messages"]
    assert [m["role"] for m in compat["messages"]] == ["system", "user", "assistant", "user"]
    assert compat["max_tokens"] == openrouter["max_tokens"] == 32000


def test_unset_sampling_is_left_to_the_server_and_extra_is_merged(tmp_path, monkeypatch):
    monkeypatch.delenv("NFTB_ENDPOINT_TOKEN", raising=False)
    fake = FakeOpener([(200, body())])
    extra = {"chat_template_kwargs": {"enable_thinking": True}}
    OpenAICompatBackend(BASE, MODEL, extra=extra, opener=fake, sleep=lambda _: None).invoke("S", "P")
    sent = json.loads(fake.calls[0][0].data)
    assert sent == {
        "model": MODEL, "messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "P"}],
        "max_tokens": 32000, "chat_template_kwargs": {"enable_thinking": True},
    }


# The body vLLM v0.20.0 builds for a prompt past max-model-len (renderers/params.py _token_len_check,
# entrypoints/utils.py create_error_response), and the unwrapped shape older vLLM servers sent.
VLLM_CONTEXT_400 = {"error": {
    "message": "This model's maximum context length is 40960 tokens. However, you requested 32000 output tokens and "
    "your prompt contains 9123 input tokens, for a total of 41123 tokens. Please reduce the length of the input prompt "
    "or the number of requested output tokens. (parameter=input_tokens, value=9123)",
    "type": "BadRequestError", "param": "input_tokens", "code": 400,
}}
VLLM_FLAT_CONTEXT_400 = {
    "object": "error", "message": "This model's maximum context length is 4096 tokens. However, you requested 4500 "
    "tokens (500 in the messages, 4000 in the completion). Please reduce the length of the messages or completion.",
    "type": "BadRequestError", "param": None, "code": 400,
}
VLLM_OTHER_400 = {"error": {
    "message": "max_tokens=32000 cannot be greater than max_model_len=max_total_tokens=16384. Please request fewer "
    "output tokens. (parameter=max_tokens, value=32000)",
    "type": "BadRequestError", "param": "max_tokens", "code": 400,
}}


@pytest.mark.parametrize("reply,kind", [
    ((400, VLLM_CONTEXT_400), "context_limit"),
    ((400, VLLM_FLAT_CONTEXT_400), "context_limit"),
    ((200, VLLM_CONTEXT_400), "context_limit"),
    ((400, VLLM_OTHER_400), "cli_error"),
    ((400, {"object": "error", "message": "bad request", "type": "BadRequestError", "code": 400}), "cli_error"),
])
def test_context_overflow_is_context_limit_and_other_400s_are_cli_errors(tmp_path, reply, kind):
    backend, fake, waits = make_backend(tmp_path, [reply])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == kind
    assert len(fake.calls) == 1 and waits == []


@pytest.mark.parametrize("usage", [
    None,
    {"completion_tokens": 7},
    {"prompt_tokens": 11},
    {"prompt_tokens": 11.0, "completion_tokens": 7},
    {"prompt_tokens": True, "completion_tokens": 7},
    {"prompt_tokens": 11, "completion_tokens": "7"},
])
def test_missing_token_usage_is_incomplete_output(tmp_path, usage):
    backend, fake, waits = make_backend(tmp_path, [(200, body(usage=usage))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "incomplete_output"
    assert len(fake.calls) == 1 and waits == []


def test_model_mismatch(tmp_path):
    backend, fake, waits = make_backend(tmp_path, [(200, body(model="some/other-model"))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_mismatch"
    assert len(fake.calls) == 1 and waits == []


def test_length_finish_is_retried_then_succeeds(tmp_path):
    backend, fake, waits = make_backend(tmp_path, [(200, body("partial", finish_reason="length")), (200, body())])
    assert backend.invoke("S", "P").text == FINISH
    assert len(fake.calls) == 2 and waits == [5]


def test_retried_attempt_tokens_are_reported(tmp_path):
    cut = body("partial", finish_reason="length", usage={"prompt_tokens": 100, "completion_tokens": 32000})
    backend, fake, waits = make_backend(tmp_path, [(200, cut), (200, body())])
    usage = backend.invoke("S", "P").usage["usage"]
    assert usage["prompt_tokens"] == 11 and usage["completion_tokens"] == 7
    assert usage["retried"] == {"attempts": 1, "prompt_tokens": 100, "completion_tokens": 32000}


def test_no_retried_field_without_retries(tmp_path):
    backend, fake, waits = make_backend(tmp_path, [(200, body())])
    assert "retried" not in backend.invoke("S", "P").usage["usage"]


def test_length_finish_exhaustion_is_model_format(tmp_path):
    backend, fake, waits = make_backend(tmp_path, [(200, body("partial", finish_reason="length"))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_format"
    assert len(fake.calls) == 4 and waits == [5, 20, 60]


def test_empty_reply_and_server_errors_are_retried(tmp_path):
    replies = [(503, "unavailable"), (200, body("   ")), (200, {"error": {"message": "busy"}}), (200, body())]
    backend, fake, waits = make_backend(tmp_path, replies)
    assert backend.invoke("S", "P").text == FINISH
    assert waits == [5, 20, 60]
    records = [json.loads(line) for line in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert [record["returncode"] for record in records] == [503, 0, 0, 0]


def test_single_parsed_tool_call_becomes_reply_text(tmp_path):
    reply = body(None)
    reply["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "finish_task", "arguments": '{"summary": "done"}'}}]
    backend, _, _ = make_backend(tmp_path, [(200, reply)])
    assert json.loads(backend.invoke("S", "P").text) == {"name": "finish_task", "arguments": {"summary": "done"}}


def test_token_sent_as_bearer_and_scrubbed_from_saved_bodies_and_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("NFTB_ENDPOINT_TOKEN", TOKEN)
    escaped = "".join(f"\\u{ord(c):04x}" for c in TOKEN)
    replies = [
        (500, f'{{"error": "bad token {escaped}"}}'),
        (200, body(json.dumps({"name": "finish_task", "arguments": {"summary": f"saw {TOKEN}"}}))),
    ]
    backend, fake, _ = make_backend(tmp_path, replies)
    invocation = backend.invoke("S", "P")
    assert fake.calls[0][0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in invocation.text and "[NFTB_ENDPOINT_TOKEN]" in invocation.text
    events = (tmp_path / "events.jsonl").read_text()
    assert TOKEN not in events and escaped not in events

    backend, _, _ = make_backend(tmp_path, [(200, body(model=f"model-{TOKEN}"))])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_mismatch" and TOKEN not in failure.value.detail


def test_concurrent_tasks_append_whole_scrubbed_event_lines(tmp_path, monkeypatch):
    monkeypatch.setenv("NFTB_ENDPOINT_TOKEN", TOKEN)
    events = tmp_path / "events.jsonl"
    together = threading.Barrier(2)
    # Each reply is large so that unlocked appends from two threads could interleave.
    text = json.dumps({"name": "finish_task", "arguments": {"summary": f"{TOKEN} " + "x" * 200_000}})

    def opener(req, *, timeout):
        together.wait(timeout=10)
        if req.get_header("Authorization") and not hasattr(threading.current_thread(), "failed"):
            threading.current_thread().failed = True
            raise url_error.HTTPError(req.full_url, 503, f"busy {TOKEN}", {}, io.BytesIO(f"busy {TOKEN}".encode()))
        response = io.BytesIO(json.dumps(body(text)).encode())
        response.status = 200
        return response

    def run(instance_id):
        CURRENT_TASK.set(instance_id)
        OpenAICompatBackend(BASE, MODEL, events_path=events, opener=opener, sleep=lambda _: None).invoke("S", "P")

    threads = [threading.Thread(target=run, args=(iid,)) for iid in ("F1a_01", "F1a_04")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    raw = events.read_text()
    assert TOKEN not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert len(records) == 4
    for record in records:
        assert set(record) == {"returncode", "stdout", "stderr", "instance_id", "t_start", "t_end"}
    assert sorted((r["instance_id"], r["returncode"]) for r in records) == [
        ("F1a_01", 0), ("F1a_01", 503), ("F1a_04", 0), ("F1a_04", 503),
    ]
    assert all("[NFTB_ENDPOINT_TOKEN]" in r["stderr"] for r in records if r["returncode"] == 503)


def test_endpoint_run_writes_events(tmp_path, fake_urlopen):
    out_dir = tmp_path / "events_out"
    assert evaluate.main(endpoint_args(out_dir)) == 0
    records = [json.loads(line) for line in (out_dir / "events.jsonl").read_text().splitlines()]
    assert records and all(r["instance_id"] == "F1a_04" and r["returncode"] == 0 for r in records)


def test_auth_failure_stops_immediately(tmp_path):
    backend, fake, waits = make_backend(tmp_path, [(401, {"error": "unauthorised"})])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "auth_or_quota"
    assert len(fake.calls) == 1 and waits == []


# evaluate.py: endpoint mode with a fake urlopen.

class EvalFake:
    def __init__(self, model=MODEL, content=INFEASIBLE, usage="default"):
        self.model, self.content, self.usage = model, content, usage
        self.urls = []
        self.payloads = []
        self.headers = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        self.payloads.append(json.loads(req.data))
        self.headers.append(req.get_header("Authorization"))
        response = io.BytesIO(json.dumps(body(self.content, model=self.model, usage=self.usage)).encode())
        response.status = 200
        return response


def endpoint_args(out_dir, *, endpoint=BASE, temperature="0.6", top_p="0.95", extra=()):
    sampling = [*(("--temperature", temperature) if temperature is not None else ()), *(("--top-p", top_p) if top_p is not None else ())]
    return [
        "--tier", "synthetic", "--instances", "F1a_04", "--model", MODEL, "--endpoint", endpoint,
        *sampling, "--out", str(out_dir), *extra,
    ]


@pytest.fixture
def fake_urlopen(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("NFTB_ENDPOINT_TOKEN", raising=False)
    fake = EvalFake()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def test_endpoint_run_needs_no_openrouter_key_and_records_identity(tmp_path, fake_urlopen):
    out_dir = tmp_path / "endpoint_out"
    ret = evaluate.main(endpoint_args(out_dir, extra=("--seed", "3", "--max-tokens", "4096")))
    assert ret == 0
    assert fake_urlopen.urls and all(url == f"{BASE}/v1/chat/completions" for url in fake_urlopen.urls)
    assert all(p["temperature"] == 0.6 and p["top_p"] == 0.95 and p["seed"] == 3 and p["max_tokens"] == 4096
               for p in fake_urlopen.payloads)
    run = json.loads((out_dir / "run.json").read_text())
    assert run["provider"] == "endpoint" and run["model"] == MODEL
    assert run["sampling"] == {"temperature": 0.6, "top_p": 0.95, "seed": 3, "max_tokens": 4096}
    assert BASE not in json.dumps(run)
    summary = json.loads((out_dir / "summary.json").read_text())
    assert summary["tasks_scored"] == 1 and summary["overall"]["success_count"] == 1


def test_endpoint_run_without_sampling_flags_sends_and_records_only_what_is_given(tmp_path, fake_urlopen):
    out_dir = tmp_path / "defaults"
    assert evaluate.main(endpoint_args(out_dir, temperature=None, top_p=None)) == 0
    assert fake_urlopen.payloads
    assert all(not {"temperature", "top_p", "seed", "chat_template_kwargs"} & set(p) for p in fake_urlopen.payloads)
    assert all(p["messages"][0]["role"] == "system" for p in fake_urlopen.payloads)
    run = json.loads((out_dir / "run.json").read_text())
    assert run["sampling"] == {"max_tokens": 32000}
    assert run["prompt_layout"] == "items_v1"


def test_enable_thinking_sends_chat_template_kwargs_and_is_recorded(tmp_path, fake_urlopen):
    out_dir = tmp_path / "thinking"
    assert evaluate.main(endpoint_args(out_dir, temperature=None, top_p=None, extra=("--enable-thinking",))) == 0
    assert fake_urlopen.payloads
    assert all(p["chat_template_kwargs"] == {"enable_thinking": True} for p in fake_urlopen.payloads)
    assert json.loads((out_dir / "run.json").read_text())["sampling"] == {"enable_thinking": True, "max_tokens": 32000}
    calls = len(fake_urlopen.urls)
    assert evaluate.main(endpoint_args(out_dir, temperature=None, top_p=None)) == 1
    assert len(fake_urlopen.urls) == calls


def test_endpoint_run_dir_without_prompt_layout_is_refused(tmp_path, fake_urlopen):
    legacy = tmp_path / "blob"
    legacy.mkdir()
    before = json.dumps({
        "tier": "synthetic", "selection": ["F1a_04"], "model": MODEL, "provider": "endpoint",
        "sampling": {"temperature": 0.6, "top_p": 0.95, "max_tokens": 32000},
    })
    (legacy / "run.json").write_text(before)
    assert evaluate.main(endpoint_args(legacy)) == 1
    assert (legacy / "run.json").read_text() == before
    assert fake_urlopen.urls == []


def test_endpoint_reply_without_token_usage_errors_the_task(tmp_path, monkeypatch, fake_urlopen):
    fake_urlopen.usage = None
    out_dir = tmp_path / "no_usage"
    assert evaluate.main(endpoint_args(out_dir)) == 1
    result = json.loads((out_dir / "F1a_04" / "result.json").read_text())
    assert result["status"] == "error" and "incomplete_output" in result["error"]


@pytest.mark.parametrize("changed", [
    {"temperature": "0.7"},
    {"temperature": None},
    {"top_p": "0.9"},
    {"extra": ("--enable-thinking",)},
    {"extra": ("--seed", "1")},
    {"extra": ("--max-tokens", "1000")},
])
def test_resume_refused_on_changed_sampling(tmp_path, fake_urlopen, changed):
    out_dir = tmp_path / "resume"
    assert evaluate.main(endpoint_args(out_dir)) == 0
    before = (out_dir / "run.json").read_text()
    calls = len(fake_urlopen.urls)
    assert evaluate.main(endpoint_args(out_dir, **changed)) == 1
    assert (out_dir / "run.json").read_text() == before
    assert len(fake_urlopen.urls) == calls


def test_resume_allowed_on_changed_endpoint_url(tmp_path, fake_urlopen):
    out_dir = tmp_path / "resume_url"
    assert evaluate.main(endpoint_args(out_dir)) == 0
    (out_dir / "F1a_04" / "result.json").write_text("not json")
    assert evaluate.main(endpoint_args(out_dir, endpoint="http://gpu-node-12:8123")) == 0
    assert fake_urlopen.urls[-1] == "http://gpu-node-12:8123/v1/chat/completions"
    assert json.loads((out_dir / "F1a_04" / "result.json").read_text())["status"] == "scored"


def test_resume_refused_across_providers_and_legacy_dir_is_openrouter(tmp_path, monkeypatch, fake_urlopen):
    legacy = tmp_path / "legacy"
    legacy.mkdir()
    record = {"tier": "synthetic", "selection": ["F1a_04"], "model": MODEL, "prompt_layout": "items_v1"}
    (legacy / "run.json").write_text(json.dumps(record))
    assert evaluate.main(endpoint_args(legacy)) == 1

    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy_openrouter_key_for_resume")
    fake_urlopen.usage = {"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.001}
    assert evaluate.main(["--tier", "synthetic", "--instances", "F1a_04", "--model", MODEL, "--max-usd", "1", "--out", str(legacy)]) == 0
    assert json.loads((legacy / "run.json").read_text())["provider"] == "openrouter"

    endpoint_dir = tmp_path / "endpoint_first"
    assert evaluate.main(endpoint_args(endpoint_dir)) == 0
    assert evaluate.main(["--tier", "synthetic", "--instances", "F1a_04", "--model", MODEL, "--max-usd", "1", "--out", str(endpoint_dir)]) == 1


def test_endpoint_token_not_written_to_outputs(tmp_path, monkeypatch, fake_urlopen):
    monkeypatch.setenv("NFTB_ENDPOINT_TOKEN", TOKEN)
    fake_urlopen.content = json.dumps({"name": "declare_infeasible", "arguments": {"reason": f"token {TOKEN}"}})
    out_dir = tmp_path / "token_out"
    assert evaluate.main(endpoint_args(out_dir)) == 0
    assert fake_urlopen.headers and all(h == f"Bearer {TOKEN}" for h in fake_urlopen.headers)
    for root, _, files in os.walk(out_dir):
        for name in files:
            assert TOKEN not in (Path(root) / name).read_text(encoding="utf-8", errors="ignore"), name


@pytest.mark.parametrize("argv_extra,uses_endpoint,ok", [
    (("--concurrency", "4"), False, True),
    (("--concurrency", "5"), False, False),
    (("--concurrency", "32"), True, True),
    (("--concurrency", "33"), True, False),
    (("--concurrency", "0"), True, False),
])
def test_concurrency_limits_per_provider(tmp_path, monkeypatch, fake_urlopen, argv_extra, uses_endpoint, ok):
    out_dir = tmp_path / "conc"
    if uses_endpoint:
        argv = endpoint_args(out_dir, extra=argv_extra)
    else:
        monkeypatch.setenv("OPENROUTER_API_KEY", "dummy_openrouter_key_for_concurrency")
        fake_urlopen.usage = {"prompt_tokens": 11, "completion_tokens": 7, "cost": 0.001}
        argv = ["--tier", "synthetic", "--instances", "F1a_04", "--model", MODEL, "--max-usd", "1", "--out", str(out_dir), *argv_extra]
    assert (evaluate.main(argv) == 0) is ok
    assert (out_dir / "run.json").exists() is ok


def test_concurrency_above_four_refused_for_built_in_agents(tmp_path):
    out_dir = tmp_path / "agent_conc"
    argv = ["--tier", "synthetic", "--instances", "F1a_04", "--agent", "reference", "--concurrency", "8", "--out", str(out_dir)]
    assert evaluate.main(argv) == 1


@pytest.mark.parametrize("argv", [
    ["--agent", "reference", "--temperature", "0.6"],
    ["--agent", "reference", "--seed", "1"],
    ["--agent", "reference", "--endpoint", BASE, "--temperature", "0.6", "--top-p", "0.95"],
    ["--agent", "reference", "--enable-thinking"],
    ["--model", MODEL, "--enable-thinking", "--max-usd", "1"],
    ["--model", MODEL, "--endpoint", BASE, "--temperature", "0.6", "--top-p", "0.95", "--max-tokens", "0"],
])
def test_endpoint_flag_combinations_refused(tmp_path, fake_urlopen, argv):
    out_dir = tmp_path / "bad_flags"
    assert evaluate.main(["--tier", "synthetic", "--instances", "F1a_04", "--out", str(out_dir), *argv]) == 1
    assert not (out_dir / "run.json").exists()
    assert fake_urlopen.urls == []


def _choice_error(code):
    reply = body(finish_reason="error")
    reply["choices"][0]["error"] = {"code": code, "message": "unexpected tokens remaining in message header"}
    return (200, reply)


@pytest.mark.parametrize("replies,kind", [
    ([(200, body("  "))] * 4, "model_format"),
    ([(200, body("partial", finish_reason="length"))] * 4, "model_format"),
    ([_choice_error(400)] * 4, "model_format"),
    ([_choice_error(502)] * 4, "incomplete_output"),
    ([_choice_error(429)] * 4, "incomplete_output"),
    ([(429, {"error": "busy"})] * 4, "cli_error"),
    ([_choice_error(400)] * 3 + [(503, "unavailable")], "cli_error"),
    ([(503, "unavailable")] * 3 + [(200, body(""))], "model_format"),
])
def test_final_kind_marks_model_format_failures_by_last_attempt(tmp_path, replies, kind):
    backend, fake, waits = make_backend(tmp_path, replies + [replies[-1]])
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == kind and len(fake.calls) == 4


def test_choice_error_is_retried_then_succeeds(tmp_path):
    backend, fake, waits = make_backend(tmp_path, [_choice_error(400), (200, body())])
    assert backend.invoke("S", "P").text == FINISH
    assert len(fake.calls) == 2 and waits == [5]


@pytest.mark.parametrize("content", [0, False, {}, []])
def test_non_string_content_is_a_malformed_body(tmp_path, content):
    reply = body()
    reply["choices"][0]["message"]["content"] = content
    backend, fake, waits = make_backend(tmp_path, [(200, reply)] * 4)
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "incomplete_output" and len(fake.calls) == 1


def test_length_cut_tool_arguments_are_model_format(tmp_path):
    cut = body("", finish_reason="length")
    cut["choices"][0]["message"]["tool_calls"] = [{"type": "function", "function": {"name": "finish_task", "arguments": '{"summary":'}}]
    backend, fake, waits = make_backend(tmp_path, [(200, cut)] * 4)
    with pytest.raises(CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == "model_format" and len(fake.calls) == 4
