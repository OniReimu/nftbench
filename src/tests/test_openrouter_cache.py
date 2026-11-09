"""Explicit Gemini cache breakpoints on the OpenRouter backend; every HTTP response below is local fake data."""

from __future__ import annotations

import copy
import io
import json

import pytest

from nftbench import evaluate
from nftbench.agents import openrouter
from nftbench.agents.gemini_generate import RECACHE_TOKENS
from nftbench.agents.openrouter import CACHE_EXPIRE_SECONDS, CACHE_MIN_TOKENS, OpenRouterBackend, SpendGuard

MODEL = "google/gemini-3.8-flash"
REPLY = json.dumps({"name": "declare_infeasible", "arguments": {"reason": "mock"}})
MARK = {"type": "ephemeral"}


class Fake:
    def __init__(self, model=MODEL):
        self.model = model
        self.payloads = []

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data))
        body = {
            "model": self.model,
            "choices": [{"message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
            "usage": {"cost": 0.001, "prompt_tokens": 10, "completion_tokens": 5},
        }
        response = io.BytesIO(json.dumps(body).encode())
        response.status = 200
        return response


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def make(tmp_path, monkeypatch, *, cache=True, name="events.jsonl"):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-openrouter-key")
    fake, clock = Fake(), Clock()
    backend = OpenRouterBackend(
        MODEL, guard=SpendGuard(10), events_path=tmp_path / name, opener=fake, sleep=lambda _: None, cache=cache, clock=clock,
    )
    return backend, fake, clock


def item(role, chars):
    return {"role": role, "content": "x" * chars}


def marks(payload):
    """Indices into the items (messages after the system one) that carry cache_control."""
    found = []
    for index, message in enumerate(payload["messages"][1:]):
        if isinstance(message["content"], list):
            assert message["content"] == [{"type": "text", "text": message["content"][0]["text"], "cache_control": MARK}]
            found.append(index)
    return found


def events(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


HEAD = "S"
THRESHOLD_CHARS = CACHE_MIN_TOKENS * 4 - len(HEAD)


def test_no_breakpoint_below_the_write_minimum(tmp_path, monkeypatch):
    backend, fake, _ = make(tmp_path, monkeypatch)
    items = [item("user", THRESHOLD_CHARS - 1)]
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[0]) == []
    assert fake.payloads[0]["messages"][1:] == items
    assert events(tmp_path / "events.jsonl")[0]["cache_breakpoint"] is None


def test_breakpoint_placed_on_the_last_item_at_the_threshold(tmp_path, monkeypatch):
    backend, fake, _ = make(tmp_path, monkeypatch)
    items = [item("user", 10), item("assistant", 10), item("user", THRESHOLD_CHARS - 20)]
    before = copy.deepcopy(items)
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[0]) == [2]
    assert fake.payloads[0]["messages"][3]["content"][0]["text"] == items[2]["content"]
    assert fake.payloads[0]["messages"][1:3] == items[:2]
    assert items == before
    assert events(tmp_path / "events.jsonl")[0]["cache_breakpoint"] == 2


def test_breakpoint_stays_on_the_same_bytes_under_the_suffix_bound(tmp_path, monkeypatch):
    backend, fake, clock = make(tmp_path, monkeypatch)
    items = [item("user", THRESHOLD_CHARS)]
    backend.invoke_items(HEAD, items)
    first = fake.payloads[0]["messages"][1]
    for turn in range(3):
        clock.now += 60
        items = [*items, item("assistant", 100), item("user", RECACHE_TOKENS)]
        backend.invoke_items(HEAD, items)
        assert marks(fake.payloads[-1]) == [0]
        assert json.dumps(fake.payloads[-1]["messages"][1]) == json.dumps(first)
    assert [e["cache_breakpoint"] for e in events(tmp_path / "events.jsonl")] == [0, 0, 0, 0]


def test_breakpoint_moves_to_the_last_item_past_the_suffix_bound(tmp_path, monkeypatch):
    backend, fake, _ = make(tmp_path, monkeypatch)
    items = [item("user", THRESHOLD_CHARS)]
    backend.invoke_items(HEAD, items)
    items = [*items, item("assistant", 4), item("user", RECACHE_TOKENS * 4 - 4)]
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[-1]) == [0]
    items = [*items, item("assistant", 4), item("user", 4)]
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[-1]) == [4]
    assert [e["cache_breakpoint"] for e in events(tmp_path / "events.jsonl")] == [0, 0, 4]


def test_breakpoint_moves_after_the_cache_lifetime(tmp_path, monkeypatch):
    backend, fake, clock = make(tmp_path, monkeypatch)
    items = [item("user", THRESHOLD_CHARS)]
    backend.invoke_items(HEAD, items)
    clock.now += CACHE_EXPIRE_SECONDS
    items = [*items, item("assistant", 4), item("user", 4)]
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[-1]) == [0]
    clock.now += 1
    items = [*items, item("assistant", 4), item("user", 4)]
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[-1]) == [4]
    # The moved breakpoint's lifetime counts from when it moved.
    clock.now += CACHE_EXPIRE_SECONDS
    items = [*items, item("assistant", 4), item("user", 4)]
    backend.invoke_items(HEAD, items)
    assert marks(fake.payloads[-1]) == [4]


def test_episodes_never_share_a_breakpoint(tmp_path, monkeypatch):
    first, first_fake, _ = make(tmp_path, monkeypatch, name="a.jsonl")
    second, second_fake, _ = make(tmp_path, monkeypatch, name="b.jsonl")
    first.invoke_items(HEAD, [item("user", THRESHOLD_CHARS)])
    second.invoke_items(HEAD, [item("user", 10)])
    assert marks(first_fake.payloads[0]) == [0] and marks(second_fake.payloads[0]) == []
    # A backend reused for another conversation drops a breakpoint that is not on that conversation's prefix.
    first.invoke_items(HEAD, [item("assistant", 10)])
    assert marks(first_fake.payloads[-1]) == []
    first.invoke_items(HEAD, [item("user", THRESHOLD_CHARS)])
    first.invoke_items("T", [item("user", THRESHOLD_CHARS), item("assistant", 4), item("user", 4)])
    assert marks(first_fake.payloads[-1]) == [2]


@pytest.mark.parametrize("cache", [False, True])
def test_request_bytes_unchanged_without_a_breakpoint(tmp_path, monkeypatch, cache):
    backend, fake, _ = make(tmp_path, monkeypatch, cache=cache)
    items = [item("user", THRESHOLD_CHARS - (1 if cache else -100000))]
    backend.invoke_items(HEAD, items)
    assert fake.payloads[0] == {
        "model": MODEL, "messages": [{"role": "system", "content": HEAD}, *items], "max_tokens": 32000, "usage": {"include": True},
    }
    assert ("cache_breakpoint" in events(tmp_path / "events.jsonl")[0]) is cache


# evaluate.py plumbing


@pytest.fixture
def google_fake(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy_openrouter_key_for_cache")
    fake = Fake()
    monkeypatch.setattr("urllib.request.urlopen", fake)
    return fake


def args(out_dir, *extra, model=MODEL, instances="F1a_04"):
    return ["--tier", "synthetic", "--instances", instances, "--model", model, "--max-usd", "1", "--out", str(out_dir), *extra]


def test_flag_is_recorded_and_reaches_one_backend_per_episode(tmp_path, google_fake, monkeypatch):
    built = []

    class Recording(OpenRouterBackend):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            built.append(self)

    monkeypatch.setattr(evaluate, "OpenRouterBackend", Recording)
    out_dir = tmp_path / "cached"
    assert evaluate.main(args(out_dir, "--openrouter-cache", "--concurrency", "2", instances="F1a_04,F1a_05")) == 0
    assert json.loads((out_dir / "run.json").read_text())["openrouter_cache"] is True
    assert len(built) == 2 and len(set(map(id, built))) == 2 and all(b.cache for b in built)
    assert all("cache_breakpoint" in record for record in events(out_dir / "events.jsonl"))

    plain = tmp_path / "plain"
    assert evaluate.main(args(plain)) == 0
    assert "openrouter_cache" not in json.loads((plain / "run.json").read_text())
    assert not built[-1].cache


@pytest.mark.parametrize("argv", [
    args("OUT", "--openrouter-cache", model="qwen/qwen3.8-flash"),
    ["--tier", "synthetic", "--instances", "F1a_04", "--agent", "reference", "--out", "OUT", "--openrouter-cache"],
    args("OUT", "--openrouter-cache", "--endpoint", "http://gpu-node-07:8000"),
])
def test_flag_refused_without_an_openrouter_google_model(tmp_path, google_fake, argv):
    out_dir = tmp_path / "bad"
    argv = [str(out_dir) if a == "OUT" else a for a in argv]
    assert evaluate.main(argv) == 1
    assert not (out_dir / "run.json").exists()
    assert google_fake.payloads == []


@pytest.mark.parametrize("first,second", [(("--openrouter-cache",), ()), ((), ("--openrouter-cache",))])
def test_resume_refused_when_the_flag_changes(tmp_path, google_fake, first, second):
    out_dir = tmp_path / "resume"
    assert evaluate.main(args(out_dir, *first)) == 0
    before = (out_dir / "run.json").read_text()
    calls = len(google_fake.payloads)
    assert evaluate.main(args(out_dir, *second)) == 1
    assert (out_dir / "run.json").read_text() == before
    assert len(google_fake.payloads) == calls
    assert evaluate.main(args(out_dir, *first)) == 0


# Gemini's MALFORMED_FUNCTION_CALL finish, which OpenRouter reports as finish_reason "error"


class ChoiceFake(Fake):
    def __init__(self, choice):
        super().__init__()
        self.choice = choice

    def __call__(self, req, timeout=None):
        self.payloads.append(json.loads(req.data))
        body = {"model": MODEL, "choices": [self.choice],
                "usage": {"cost": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
        response = io.BytesIO(json.dumps(body).encode())
        response.status = 200
        return response


def malformed(**changes):
    choice = {"finish_reason": "error", "native_finish_reason": "MALFORMED_FUNCTION_CALL",
              "message": {"role": "assistant", "content": REPLY}}
    for key, value in changes.items():
        if key == "content":
            if value is ...:
                del choice["message"]["content"]
            else:
                choice["message"]["content"] = value
        else:
            choice[key] = value
    return choice


def finish_backend(tmp_path, monkeypatch, choice):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-only-openrouter-key")
    fake = ChoiceFake(choice)
    backend = OpenRouterBackend(MODEL, guard=SpendGuard(10), events_path=tmp_path / "events.jsonl", opener=fake, sleep=lambda _: None)
    return backend, fake


def test_malformed_function_call_with_text_is_the_reply(tmp_path, monkeypatch):
    backend, fake = finish_backend(tmp_path, monkeypatch, malformed())
    invocation = backend.invoke("S", "P")
    assert invocation.text == REPLY and len(fake.payloads) == 1
    assert invocation.usage == {"usage": {"cost": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}
    assert json.loads(backend.complete(json.dumps({"messages": [{"role": "system", "content": "S"}], "tools": []})))["tool_calls"][0]["name"] == (
        "declare_infeasible")


@pytest.mark.parametrize("choice,kind", [
    (malformed(native_finish_reason="OTHER"), "incomplete_output"),
    (malformed(native_finish_reason=None), "incomplete_output"),
    (malformed(error={"code": 502, "message": "upstream"}), "incomplete_output"),
    (malformed(content=""), "incomplete_output"),
    (malformed(content="  "), "incomplete_output"),
    (malformed(content=None), "incomplete_output"),
    (malformed(content=...), "incomplete_output"),
    (malformed(content=["not", "text"]), "incomplete_output"),
    (malformed(content={"text": REPLY}), "incomplete_output"),
])
def test_other_error_finishes_still_fail(tmp_path, monkeypatch, choice, kind):
    backend, fake = finish_backend(tmp_path, monkeypatch, choice)
    with pytest.raises(openrouter.CLIFailure) as failure:
        backend.invoke("S", "P")
    assert failure.value.kind == kind
