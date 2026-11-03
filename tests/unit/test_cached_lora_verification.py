import json

import pytest

from paper_review.application import cached_lora_verification as verification


def test_plan_is_frozen_and_reserves_only_graph_and_optional_merge_review():
    p = verification.plan()
    assert p == verification.plan()
    assert p["new_s2_calls"] == p["new_contribution_calls"] == p["new_relevance_calls"] == 0
    assert p["max_llm_calls"] == 2 and p["max_llm_attempts"] == 6
    assert p["prior_used_or_reserved_usd"] + p["max_new_reservation_usd"] <= 1.6


@pytest.mark.parametrize("graph_status", ["completed", "insufficient_evidence"])
def test_cached_path_reuses_inputs_and_never_regenerates_valid_negative(
    settings, monkeypatch, graph_status,
):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "plan", lambda: {"frozen": True})
    (output / "prepared-plan.json").write_text('{"frozen":true}')
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.setattr(verification.frozen, "inputs", lambda: (
        {"aid": "2106.09685"}, "frozen-analysis", {"accepted": "contributions"},
    ))
    monkeypatch.setattr(
        verification.accepted, "relevance_selection", lambda: {"frozen": "relevance"},
    )
    calls = []

    async def run(cases, **kwargs):
        calls.append(kwargs)
        return {"status": "completed", "lineage": {"status": graph_status}}

    monkeypatch.setattr(verification.roadmap, "_lineage", run)
    assert verification.evaluate(160)["lineage"]["status"] == graph_status
    assert verification.evaluate(160)["lineage"]["status"] == graph_status
    assert len(calls) == 1
    assert calls[0]["analysis_override"] == "frozen-analysis"
    assert calls[0]["accepted_contributions"] == {"accepted": "contributions"}
    assert calls[0]["accepted_relevance_selection"] == {"frozen": "relevance"}
    assert "accepted_graph_proposal" not in calls[0]


def test_failed_window_cannot_resume(settings, monkeypatch):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    assert verification.evaluate(50)["status"] == "blocked"
    (output / "results.json").write_text(json.dumps({"status": "started"}))
    assert verification.evaluate(160)["reason"] == "previous_window_requires_review"
