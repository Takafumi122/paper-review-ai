"""Source-bound review gates prevent automatic graph calls and preferred-answer retries."""

import json

import pytest

from paper_review.application import lora_stage2_verification as verification


def test_preflight_has_new_top10_identity_coverage_and_bounded_budget():
    plan = verification.plan()
    assert plan["selection_policy_version"] == "lower-bound-v1"
    assert plan["max_logical_calls"] == 2 and plan["max_attempts"] == 6
    assert plan["new_s2_calls"] == 0 and plan["production_gates_changed"] is False
    assert plan["prior_used_or_reserved_usd"] + plan["max_additional_reservation_usd"] <= 1.6
    rows = plan["source_preview"]["candidates"]
    assert len(rows) == 10 and all(r["direct_source_ids"] for r in rows)
    assert plan["source_preview"]["input_token_estimate"] <= 16000


@pytest.fixture
def window(settings, monkeypatch):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    plan = {"source_preview": {"transmitted_source_ids": ["s"]}}
    monkeypatch.setattr(verification, "plan", lambda: plan)
    (output / "prepared-plan.json").write_text(json.dumps(plan))
    (output / "preflight-review.json").write_text(json.dumps({
        "decision": "pass_for_relevance_diagnostic", "plan_sha256": verification.digest(plan),
    }))
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    calls = []

    async def inputs():
        return {"aid": "2106.09685"}, "new-production-analysis", {"frozen": True}

    async def run(cases, **kwargs):
        calls.append(kwargs)
        return {"status": "completed", "lineage": {"status": "ranked_diagnostic_only"}}

    monkeypatch.setattr(verification, "inputs", inputs)
    monkeypatch.setattr(verification.parent, "_lineage", run)
    monkeypatch.setattr(verification, "relevance_selection", lambda: {"validated": "selection"})
    return output, plan, calls


def test_relevance_uses_new_analysis_and_graph_requires_review(window):
    output, _, calls = window
    assert verification.evaluate(160)["status"] == "completed"
    assert len(calls) == 1 and calls[0]["relevance_only"] is True
    assert calls[0]["analysis_override"] == "new-production-analysis"
    assert calls[0]["accepted_contributions"] == {"frozen": True}
    assert calls[0]["total_budget_usd"] == 1.6
    assert verification.evaluate(160)["status"] == "awaiting_source_review"
    assert len(calls) == 1 and not (output / "graph.lock").exists()


@pytest.mark.parametrize("stale", [None, "relevance_sha256", "selection_sha256", "plan_sha256"])
def test_graph_requires_matching_review_and_reuses_selection(window, stale):
    output, plan, calls = window
    result = verification.evaluate(160)
    review = {"decision": "pass_for_scoped_graph_diagnostic",
              "relevance_sha256": verification.digest(result),
              "selection_sha256": verification.digest(verification.relevance_selection()),
              "plan_sha256": verification.digest(plan)}
    if stale:
        review[stale] = "wrong"
    (output / "relevance-review.json").write_text(json.dumps(review))
    actual = verification.evaluate(160)
    assert actual["status"] == ("blocked" if stale else "completed")
    assert len(calls) == (1 if stale else 2)
    if not stale:
        assert calls[1]["accepted_relevance_selection"] == {"validated": "selection"}
        assert calls[1]["relevance_only"] is False
        assert verification.evaluate(160)["status"] == "completed"
        assert len(calls) == 2


def test_failed_relevance_never_retries_or_starts_graph(window):
    output, _, calls = window
    (output / "relevance-results.json").write_text('{"status":"stopped"}')
    assert verification.evaluate(160)["status"] == "blocked"
    assert not calls
    assert verification.evaluate(50)["status"] == "blocked"


def test_missing_preflight_review_never_sends(window):
    output, _, calls = window
    (output / "preflight-review.json").unlink()
    assert verification.evaluate(160)["reason"] == "scoped_source_review_required"
    assert not calls
