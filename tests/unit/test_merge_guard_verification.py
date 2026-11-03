"""Negative-control failures stop the window; Graph/Relevance are not regenerated for preference."""

import json

import pytest

from paper_review.application import merge_guard_verification as verification


def test_real_source_control_plan_is_bounded_and_does_not_call_s2():
    p = verification.plan()
    assert p["new_s2_calls"] == p["new_relevance_calls"] == p["new_contribution_calls"] == 0
    assert p["max_logical_calls"] == 3 and p["max_attempts"] == 9
    assert p["prior_used_or_reserved_usd"] + p["max_new_reservation_usd"] <= 1.6
    assert p["production_gates_changed"] is False


@pytest.mark.parametrize("negative_rejected", [True, False])
def test_graph_follows_only_a_rejected_negative_control(settings, monkeypatch, negative_rejected):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "plan", lambda: {"bounded": True})
    (output / "prepared-plan.json").write_text('{"bounded":true}')
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(verification.previous, "relevance_selection", lambda: {"accepted": True})
    monkeypatch.setattr(verification, "control", lambda: {"counterfactual": True})

    async def inputs():
        return {"aid": "2106.09685"}, "accepted-new-analysis", {"accepted": "contributions"}

    monkeypatch.setattr(verification.previous, "inputs", inputs)
    calls = []

    async def run(cases, **kwargs):
        calls.append(kwargs)
        if kwargs.get("accepted_graph_proposal"):
            return {"status": "completed", "lineage": {
                "status": "insufficient_evidence" if negative_rejected else "completed",
                "edges": [], "merge_review": {"supported": not negative_rejected},
            }}
        return {"status": "completed", "lineage": {"status": "completed"}}

    monkeypatch.setattr(verification.parent, "_lineage", run)
    result = verification.evaluate(160)
    assert result["status"] == ("completed" if negative_rejected else "stopped")
    assert len(calls) == (2 if negative_rejected else 1)
    for call in calls:
        assert call["analysis_override"] == "accepted-new-analysis"
        assert call["accepted_relevance_selection"] == {"accepted": True}
        assert call["total_budget_usd"] == 1.6
    assert verification.evaluate(160)["status"] == ("completed" if negative_rejected else "blocked")
    assert len(calls) == (2 if negative_rejected else 1)


def test_interrupted_window_stays_reserved(settings, monkeypatch):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    (output / "results.json").write_text(json.dumps({"status": "started"}))
    assert verification.evaluate(160)["status"] == "blocked"
    assert verification.evaluate(50)["status"] == "blocked"
