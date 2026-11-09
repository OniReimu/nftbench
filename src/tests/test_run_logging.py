"""Timing, attempt and task fields in transcripts and events.jsonl; they change no request and no replay."""

import json
import threading
from pathlib import Path

import pytest

from helpers import ALICE, sweep
from nftbench.agents.cli import CURRENT_TASK, CLIFailure, count_attempt
from nftbench.metrics import transcript_stats
from nftbench.agents.llm import ScriptedBackend
from nftbench.oracle import load_oracle
from nftbench.runner import replay_transcript, run_llm
from nftbench.tasks import Environment
from test_cli_backend import FINISH, make_openrouter, openrouter_body
from test_llm import reference_script, response

NEW_RESPONSE_FIELDS = ("t_start", "t_end", "latency_s", "attempts")
NEW_END_FIELDS = ("t_episode_start", "t_episode_end")
SAVED_RUN = Path.home() / "Data" / "nft_bench" / "runs" / "main_gpt-6.1-sol-high_real_code" / "seed0"


class CountingBackend(ScriptedBackend):
    """A scripted backend that reports two attempts per turn, as a retried HTTP backend would."""

    def complete(self, request: str) -> str:
        count_attempt()
        count_attempt()
        return super().complete(request)


def records(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines()]


def test_transcript_records_timing_and_attempts(tmp_path):
    path = tmp_path / "t.jsonl"
    with Environment("A1") as env:
        run_llm(env, CountingBackend([response("get_account_state", address=ALICE), response("finish_task", summary="x")]),
                transcript_path=path)
    saved = records(path)
    responses = [r for r in saved if r["type"] == "response"]
    assert len(responses) == 2
    for record in responses:
        assert record["t_start"] <= record["t_end"]
        assert record["latency_s"] == pytest.approx(record["t_end"] - record["t_start"])
        assert record["attempts"] == 2
    tool_records = [r for r in saved if r["type"] == "message" and r["message"]["role"] == "tool"]
    assert len(tool_records) == 2 and all(r["tool_latency_s"] >= 0 for r in tool_records)
    # The latency stays outside the message, so it never reaches a request.
    assert all("tool_latency_s" not in r["message"] for r in tool_records)
    end = saved[-1]
    assert end["type"] == "end" and end["t_episode_start"] <= responses[0]["t_start"] <= end["t_episode_end"]


def test_scripted_turns_record_no_attempts(tmp_path):
    path = tmp_path / "t.jsonl"
    with Environment("A1") as env:
        run_llm(env, ScriptedBackend([response("finish_task", summary="x")]), transcript_path=path)
    assert "attempts" not in next(r for r in records(path) if r["type"] == "response")


def strip_new_fields(path, out):
    lines = []
    for record in records(path):
        drop = {"response": NEW_RESPONSE_FIELDS, "end": NEW_END_FIELDS, "message": ("tool_latency_s",)}.get(record["type"], ())
        lines.append(json.dumps({k: v for k, v in record.items() if k not in drop}, sort_keys=True))
    Path(out).write_text("\n".join(lines) + "\n")


def test_old_and_new_transcripts_replay_to_the_same_report(tmp_path):
    new = tmp_path / "new.jsonl"
    old = tmp_path / "old.jsonl"
    with Environment("A1") as env:
        script = reference_script(env, load_oracle("A1")) + [response("submit_transaction", tx=sweep(env))]
        first = run_llm(env, CountingBackend(script), transcript_path=new)
        first_fingerprint = env.fingerprint()
    assert any("t_start" in r for r in records(new))
    strip_new_fields(new, old)
    assert not any("t_start" in r or "tool_latency_s" in r for r in records(old))
    assert replay_transcript(new) == (first, first_fingerprint)
    assert replay_transcript(old) == (first, first_fingerprint)


@pytest.mark.skipif(not SAVED_RUN.is_dir(), reason="the saved gpt-6.1-sol-high real-code run is not on this machine")
@pytest.mark.parametrize("instance_id", ["R01_locked", "M03_rc08_approve_disabled", "M09_rc17_bob_listed"])
def test_saved_transcript_requests_are_byte_identical(instance_id):
    """ReplayBackend refuses any request whose sha256 differs from the saved record, so a replay that
    completes means the logging change left every request of the default condition byte-identical."""
    transcript = SAVED_RUN / instance_id / "transcript.jsonl"
    saved = records(transcript)
    assert not any("t_start" in r for r in saved)
    report, _ = replay_transcript(transcript)
    assert report == json.loads((SAVED_RUN / instance_id / "result.json").read_text())["report"]


def test_events_carry_the_task_of_each_thread(tmp_path, monkeypatch):
    replies = [(200, openrouter_body()) for _ in range(4)]
    backend, _, _ = make_openrouter(tmp_path, monkeypatch, replies)
    request = json.dumps({"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}], "tools": []})
    barrier = threading.Barrier(4)

    def task(instance_id):
        CURRENT_TASK.set(instance_id)
        barrier.wait()
        assert json.loads(backend.complete(request))["tool_calls"][0]["name"] == json.loads(FINISH)["name"]

    threads = [threading.Thread(target=task, args=(f"T{i}",)) for i in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    events = records(tmp_path / "events.jsonl")
    assert sorted(e["instance_id"] for e in events) == ["T0", "T1", "T2", "T3"]
    assert all(e["t_start"] <= e["t_end"] for e in events)


def test_retries_count_as_attempts_of_one_turn(tmp_path, monkeypatch):
    backend, _, _ = make_openrouter(tmp_path, monkeypatch, [(429, {"error": "slow down"}), (200, openrouter_body())])
    path = tmp_path / "t.jsonl"
    with Environment("A1") as env:
        run_llm(env, backend, transcript_path=path)
    first = next(r for r in records(path) if r["type"] == "response")
    assert first["attempts"] == 2
    assert [e["instance_id"] for e in records(tmp_path / "events.jsonl")] == [None, None]


class FailingBackend:
    """Answers the first turn, then fails the second after three attempts, as a format-error reply does."""

    def __init__(self, first):
        self.first = first

    def complete(self, request):
        count_attempt()
        if self.first:
            reply, self.first = self.first, None
            return reply
        for _ in range(2):
            count_attempt()
        raise CLIFailure("model_format", "the model returned no text")


def test_a_failed_turn_keeps_its_attempts_and_timing(tmp_path):
    path = tmp_path / "t.jsonl"
    with Environment("A1") as env:
        with pytest.raises(CLIFailure):
            run_llm(env, FailingBackend(response("get_account_state", address=ALICE)), transcript_path=path)
    saved = records(path)
    failed = saved[-1]
    assert failed["type"] == "failed_turn" and failed["attempts"] == 3
    assert (failed["error_type"], failed["error_kind"]) == ("CLIFailure", "model_format")
    assert failed["t_start"] <= failed["t_end"] == failed["t_episode_end"] and failed["t_episode_start"] <= failed["t_start"]
    assert [r["type"] for r in saved].count("response") == 1 and not any(r["type"] == "end" for r in saved)
    stats = transcript_stats(path, [])
    assert stats["attempts"] == 4 and stats["wall_source"] == "failed_turn"
    assert stats["wall_s"] == pytest.approx(failed["t_episode_end"] - failed["t_episode_start"])
