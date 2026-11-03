"""The paired ranking window is bounded, frozen and cannot automatically repeat."""

import json

from paper_review.application import ranking_quality_verification as evaluation


def test_frozen_window_requires_budget_and_reuses_completed_results(settings, monkeypatch):
    root = settings.database_path.parent
    output, accepted = root / "output", root / "accepted"
    output.mkdir()
    accepted.mkdir()
    monkeypatch.setattr(evaluation, "OUTPUT", output)
    monkeypatch.setattr(evaluation.relation_before, "OUTPUT", accepted)
    monkeypatch.setattr(evaluation, "plan", lambda: {"frozen": True})
    monkeypatch.setattr(evaluation, "prior_cost", lambda: .70963016)
    monkeypatch.setattr(evaluation.classification, "load_cases", lambda: [])
    monkeypatch.setattr(evaluation, "frozen_batches", lambda: {"paper": [["C0"]]})
    monkeypatch.setattr(evaluation, "relevance_sources", lambda: ["s"])
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    (accepted / "results.json").write_text('{"lineage":{"accepted":"unchanged"}}')
    calls = []

    async def classify(*args, **kwargs):
        calls.append("classify")
        assert kwargs["total_budget_usd"] == 1.6
        assert kwargs["fixed_context_batches"] == {"paper": [["C0"]]}
        return {"status": "completed", "cases": []}

    async def relate(*args, **kwargs):
        calls.append("relate")
        assert kwargs["relevance_only"] is True and kwargs["total_budget_usd"] == 1.6
        assert kwargs["frozen_relevance_source_ids"] == ["s"]
        assert kwargs["accepted_contributions"] == {"accepted": "unchanged"}
        return {"status": "completed", "lineage": {"status": "ranked_diagnostic_only"}}

    monkeypatch.setattr(evaluation.classification, "_run", classify)
    monkeypatch.setattr(evaluation.parent, "_lineage", relate)
    assert evaluation.evaluate()["reason"] == "explicit_160_cent_authorization_required"
    assert evaluation.evaluate(160)["reason"] == "prepared_plan_required_or_changed"
    (output / "prepared-plan.json").write_text('{"frozen":true}')
    result = evaluation.evaluate(160)
    assert result["status"] == "completed" and calls == ["classify", "relate"]
    assert result["production_flags_changed"] is False
    assert evaluation.evaluate(160) == result and calls == ["classify", "relate"]
    (output / "results.json").write_text(json.dumps({"status": "stopped"}))
    assert evaluation.evaluate(160)["reason"] == "previous_window_requires_review"
    assert calls == ["classify", "relate"]
