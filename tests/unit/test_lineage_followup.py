"""The follow-up is bounded, frozen and cannot repeat a failed paid window."""

import hashlib
import json
import sqlite3

import pytest

from paper_review.application import lineage_followup as followup


def test_prior_cost_keeps_unknown_reservations(tmp_path, monkeypatch):
    path = tmp_path / "parent.db"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "create table llm_executions(cost_status text,reserved_usd real,"
            "estimated_cost_usd real)"
        )
        connection.executemany("insert into llm_executions values(?,?,?)", [
            ("unknown", .036, None), ("reserved", .01, None), ("estimated", .02, .003),
        ])
    monkeypatch.setattr(followup.parent, "DATABASE", path)
    monkeypatch.setattr(followup.parent, "prior_cost", lambda: .1)
    assert followup.prior_cost() == pytest.approx(.149)


def test_frozen_followup_gates_and_completed_reuse(tmp_path, monkeypatch):
    output = tmp_path / "followup"
    parent = tmp_path / "parent"
    output.mkdir()
    parent.mkdir()
    monkeypatch.setattr(followup, "OUTPUT", output)
    monkeypatch.setattr(followup.parent, "OUTPUT", parent)
    monkeypatch.setattr(followup, "plan", lambda: {"frozen": True})
    monkeypatch.setattr(followup, "prior_cost", lambda: .2585114)
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    assert followup.evaluate()["reason"] == "explicit_shared_ceiling_required"
    assert followup.evaluate(50)["reason"] == "prepared_plan_required_or_changed"
    (output / "prepared-plan.json").write_text('{"frozen":true}')
    baseline = parent / "classify-results.json"
    baseline.write_text('{"status":"completed"}')
    (parent / "stage1-review.json").write_text(json.dumps({
        "accepted_for_lineage_diagnostic": True,
        "classification_sha256": hashlib.sha256(baseline.read_bytes()).hexdigest(),
    }))
    (parent / "s2-results.json").write_text(json.dumps({"reports": [{
        "target": "2106.09685", "gate": "READY FOR QUALITY AUDIT",
    }]}))
    calls = []

    async def lineage(cases, **kwargs):
        calls.append(kwargs)
        return {"status": "completed", "production_gates_changed": False}

    monkeypatch.setattr(followup.parent, "_lineage", lineage)
    monkeypatch.setattr(followup, "load_cases", lambda: [])
    result = followup.evaluate(50)
    assert result["status"] == "completed" and len(calls) == 1
    assert followup.evaluate(50) == result and len(calls) == 1
    (output / "results.json").write_text('{"status":"stopped"}')
    assert followup.evaluate(50)["reason"] == "previous_window_requires_review"
    assert len(calls) == 1
