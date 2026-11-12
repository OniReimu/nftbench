"""The released clarification-question audit reproduces audit.json from its own files."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("question_audit", REPO_ROOT / "scripts" / "question_audit.py")
QA = importlib.util.module_from_spec(spec)
spec.loader.exec_module(QA)


def test_audit_reproduces_reference():
    res, reference = QA.recompute(QA.AUDIT_DIR)
    assert QA.mismatches(res, reference) == []
    assert (res["n_items"], res["agreement"]["three_way"]["agree"]) == (93, 92)
    models = res["models"].values()
    assert sum(m["identifies_successes"] for m in models) == 83
    assert sum(m["clarify_successes"] for m in models) == 87
    assert round(res["models"]["gpt-oss-120b"]["S_q"], 3) == 0.407
    assert round(res["models"]["Mistral Small 4"]["S_q"], 3) == 0.330


def test_changed_label_is_reported(tmp_path):
    for f in QA.AUDIT_DIR.iterdir():
        if f.is_file():
            (tmp_path / f.name).write_bytes(f.read_bytes())
    adj = tmp_path / "adjudication.jsonl"
    adj.write_text(adj.read_text(encoding="utf-8").replace('"identifies"', '"other"'), encoding="utf-8")
    assert QA.mismatches(*QA.recompute(tmp_path))


def test_changed_reference_field_is_reported(tmp_path):
    for f in QA.AUDIT_DIR.iterdir():
        if f.is_file():
            (tmp_path / f.name).write_bytes(f.read_bytes())
    ref = (tmp_path / "audit.json")
    doc = json.loads(ref.read_text(encoding="utf-8"))
    doc["agreement"]["identifies_vs_not"]["agree"] = 0
    ref.write_text(json.dumps(doc), encoding="utf-8")
    assert QA.mismatches(*QA.recompute(tmp_path)) == [
        "audit.json.agreement.identifies_vs_not.agree: recomputed 92, audit.json 0"
    ]
