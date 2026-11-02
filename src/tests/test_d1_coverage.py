"""Coverage rules of the D1 scripts. Fixtures are unit tests only and never enter data/.

Standalone, with no project install:
  uv run --isolated --no-project --with pytest --with httpx --with pycryptodome pytest --noconftest -p no:cacheprovider src/tests/test_d1_coverage.py
D1_SCRIPTS_DIR points the tests at another copy of the scripts.
"""
from __future__ import annotations

import importlib, json, os, sys
from pathlib import Path

import pytest

SCRIPTS = Path(os.environ.get("D1_SCRIPTS_DIR", Path(__file__).resolve().parents[2] / "scripts"))
sys.path.insert(0, str(SCRIPTS))
census = importlib.import_module("d1_census_6551")
candidates = importlib.import_module("d1_candidates")

URL = "https://endpoint.test"
HB_ADDR = census.CHAINS["ethereum"]["heartbeat"]["addresses"][0]
HB_TOPIC = census.CHAINS["ethereum"]["heartbeat"]["topics"][0]
UNREQUESTED = "0x" + "ab" * 32


def log(block: int, topic: str = HB_TOPIC, address: str = HB_ADDR, removed: bool = False, index: int = 0) -> dict:
    return {"address": address, "topics": [topic], "data": "0x", "blockNumber": hex(block), "logIndex": hex(index), "transactionHash": "0x" + "11" * 32, "removed": removed}


def endpoint(cap: int = 1000):
    return census.Endpoint(census.E(URL, 1000, 0, cap=cap))


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(census.time, "sleep", lambda _: None)


def answer_with(monkeypatch, logs: list) -> None:
    fake = lambda *a, **k: ("OK", logs)  # noqa: E731
    monkeypatch.setattr(census, "rpc", fake)
    monkeypatch.setattr(candidates, "rpc", fake)


def window_answer(start: int, end: int, cap: int = 1000):
    return candidates.get_sub_range([endpoint(cap)], end, start, {HB_TOPIC})


def census_answer(start: int, end: int, cap: int = 1000):
    c = census.Census.__new__(census.Census)
    c.cfg, c.client = census.CHAINS["ethereum"], None
    return c.ep_get_logs(endpoint(cap), start, end)


SINGLE_BLOCK_AT_CAP = [log(100, index=i) for i in range(3)]
BAD_ANSWERS = {
    "out_of_range_heartbeat": [log(5000)],
    "removed_heartbeat": [log(100, removed=True)],
    "unrequested_topic": [log(100, topic=UNREQUESTED)],
}


def test_capped_singleton_rejected_in_windows(monkeypatch):
    answer_with(monkeypatch, SINGLE_BLOCK_AT_CAP)
    with pytest.raises(LookupError):
        window_answer(100, 100, cap=3)


def test_capped_singleton_rejected_in_census(monkeypatch):
    answer_with(monkeypatch, SINGLE_BLOCK_AT_CAP)
    with pytest.raises(RuntimeError):
        census_answer(100, 100, cap=3)


@pytest.mark.parametrize("case", BAD_ANSWERS)
def test_invalid_heartbeat_rejected_in_windows(monkeypatch, case):
    answer_with(monkeypatch, BAD_ANSWERS[case])
    with pytest.raises(LookupError):
        window_answer(100, 200)


@pytest.mark.parametrize("case", BAD_ANSWERS)
def test_invalid_heartbeat_rejected_in_census(monkeypatch, case):
    answer_with(monkeypatch, BAD_ANSWERS[case])
    with pytest.raises(RuntimeError):
        census_answer(100, 200)


def finalized(monkeypatch, tmp_path, answered: list, checks_log_block: int | None = None, cap_blocks: list | None = None) -> dict:
    """A real finalize over a fixture checkpoint: nothing mocked between the controls and the complete flag."""
    data = tmp_path / "data"
    (data / "census_6551").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(census, "ROOT", tmp_path)
    monkeypatch.setattr(census, "DATA", data)
    monkeypatch.setattr(census, "write_summary", lambda: None)
    c = census.Census.__new__(census.Census)
    c.chain, c.cfg, c.smoke, c.client = "ethereum", census.CHAINS["ethereum"], None, None
    c.raw, c.out_path = data / "raw", data / "census_6551" / "ethereum.json"
    c.endpoints = [endpoint()]
    lo, hi = min(x[0] for x in answered), max(x[1] for x in answered)
    c.ckpt = {"chain_id": 1, "covered": [x[:2] for x in answered], "answered": answered, "pinned": {"number": hi, "hash": "0x0", "queried_at_utc": "t"}, "runs": [], "wall_seconds": 0, "ep_stats": {},
              "single_block_at_cap": cap_blocks or [], "first_code_block": lo, "method": "fixture", "sourcify_deployment_block": None}
    monkeypatch.setattr(c, "bounds", lambda: (lo, hi))
    monkeypatch.setattr(c, "month_boundaries", lambda a, b: {"2026-10": a})
    monkeypatch.setattr(c, "save_checkpoint", lambda: None)
    monkeypatch.setattr(c, "cross_check", lambda logs: [])
    saved = {}
    if checks_log_block is not None:
        entry = log(checks_log_block, topic=census.EXPECTED_TOPIC0, address=census.REGISTRY)
        entry["topics"] = [census.EXPECTED_TOPIC0, "0x" + "00" * 32, "0x" + "00" * 32, "0x" + "00" * 32]
        entry["data"] = "0x" + "00" * 96
        monkeypatch.setattr(c, "ep_get_logs", lambda ep, a, b: [entry])
        saved = {(entry["transactionHash"], 0): entry}
    monkeypatch.setattr(c, "load_logs", lambda: (saved, len(saved), 0))
    return c.finalize()


def write_audit(tmp_path, ranges: list, below: int, **overrides) -> None:
    path = tmp_path / "data" / "census_6551" / "ethereum_reread_audit.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"endpoint": URL, "below": below, "via": "https://via.test", "validator": "judge_answer", "finished": True, "matched_ranges": len(ranges),
                                "mismatched_ranges": 0, "unanswered_ranges": 0, "ranges": [{"range": r, "match": True} for r in ranges], **overrides}))


def test_endpoint_with_zero_checks_blocks_complete(monkeypatch, tmp_path):
    out = finalized(monkeypatch, tmp_path, [[100, 200, URL, 0, 5]])
    assert out["positive_controls"][0]["registry_log_checks"] == []
    assert out["complete"] is False
    assert out["positive_controls"][0]["control_status"] == "inconclusive"


def test_clean_audit_stands_in_below_its_height_and_is_named(monkeypatch, tmp_path):
    write_audit(tmp_path, [[100, 200]], below=1000)
    out = finalized(monkeypatch, tmp_path, [[100, 200, URL, 0, 5]])
    assert out["complete"] is True
    assert out["positive_controls"][0]["substitute"]["reread_audit"] == "data/census_6551/ethereum_reread_audit.json"
    assert out["positive_controls"][0]["substitute"]["stands_in_only_below_block"] == 1000


def test_audit_does_not_reach_above_its_height(monkeypatch, tmp_path):
    write_audit(tmp_path, [[100, 999]], below=1000)
    out = finalized(monkeypatch, tmp_path, [[100, 999, URL, 0, 5], [1000, 1100, URL, 0, 5]])
    assert out["complete"] is False
    assert out["positive_controls"][0]["control_status"] == "inconclusive"


def test_endpoint_above_audit_height_passes_only_with_its_own_check(monkeypatch, tmp_path):
    write_audit(tmp_path, [[100, 999]], below=1000)
    out = finalized(monkeypatch, tmp_path, [[100, 999, URL, 0, 5], [1000, 1100, URL, 1, 5]], checks_log_block=1050)
    assert out["complete"] is True
    assert out["positive_controls"][0]["control_status"] == "own_checks_above_audit_height_and_audit_substitute_below"


@pytest.mark.parametrize("field", [{"finished": False}, {"mismatched_ranges": 1}, {"unanswered_ranges": 1}])
def test_unclean_audit_is_no_substitute(monkeypatch, tmp_path, field):
    write_audit(tmp_path, [[100, 200]], below=1000, **field)
    assert finalized(monkeypatch, tmp_path, [[100, 200, URL, 0, 5]])["complete"] is False


def test_capped_block_needs_two_endpoint_evidence_and_output_names_it(monkeypatch, tmp_path):
    answered = [[100, 200, URL, 1, 5]]
    out = finalized(monkeypatch, tmp_path, answered, checks_log_block=150, cap_blocks=[[150, 3, URL]])
    assert out["complete"] is False and out["single_block_at_cap"][0]["covered_only_through"] is None
    evidence = tmp_path / "data" / "census_6551" / "ethereum_block_150.json"
    evidence.write_text(json.dumps({"sources": [{"endpoint": "https://a.test", "status": "OK", "registry_logs": 1}, {"endpoint": "https://b.test", "status": "OK", "registry_logs": 1}]}))
    out = finalized(monkeypatch, tmp_path, answered, checks_log_block=150, cap_blocks=[[150, 3, URL]])
    assert out["complete"] is True and out["single_block_at_cap"][0]["covered_only_through"] == "data/census_6551/ethereum_block_150.json"


def test_valid_heartbeat_is_accepted(monkeypatch):
    answer_with(monkeypatch, [log(150)])
    assert window_answer(100, 200)[0] == 200
    assert census_answer(100, 200) == []


def test_capped_larger_range_is_split_not_rejected(monkeypatch):
    def rpc(client, url, method, params):
        span = int(params[0]["toBlock"], 16) - int(params[0]["fromBlock"], 16) + 1
        return "OK", ([log(int(params[0]["fromBlock"], 16), index=i) for i in range(3)] if span > 50 else [log(int(params[0]["fromBlock"], 16))])
    monkeypatch.setattr(candidates, "rpc", rpc)
    monkeypatch.setattr(census, "rpc", rpc)
    assert window_answer(100, 200, cap=3)[0] < 200
    assert census_answer(100, 200, cap=3) == []
