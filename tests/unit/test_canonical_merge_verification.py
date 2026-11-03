import asyncio

import pytest

from paper_review.application import canonical_merge_verification as verification
from paper_review.application.contribution_review import audit_contributions
from paper_review.domain.understanding import Contributions


def test_frozen_cases_and_reused_contribution_reviews_are_still_valid():
    cases = verification.inputs()
    assert [c["expected_supported"] for c in cases] == [False, True, True]
    assert all(len(c["validated_edges"]) == 2 for c in cases)
    for case in cases:
        if case["accepted_contribution_review"] is None:
            continue

        async def call(schema, stage_id, task, instruction, spans, extra, validator,
                       review=case["accepted_contribution_review"], **kwargs):
            value = schema.model_validate(review)
            validator(value)
            return value

        result = asyncio.run(audit_contributions(
            Contributions.model_validate(case["contributions"]), "s", call,
        ))
        assert result.supported


@pytest.mark.parametrize("matches", [True, False])
def test_window_stops_on_first_unexpected_verdict_without_repair(settings, monkeypatch, matches):
    cases = verification.inputs()
    monkeypatch.setattr(verification, "inputs", lambda: cases)
    monkeypatch.setattr(verification, "OUTPUT", settings.database_path.parent)
    monkeypatch.setattr(verification, "plan", lambda: {"max_new_reservation_usd": 0.492})
    monkeypatch.setattr(verification, "prior_cost", lambda: 0.95431416)
    monkeypatch.setattr(verification, "used_here", lambda: 0)
    calls = []

    async def run(*args, **kwargs):
        calls.append(kwargs)
        expected = cases[len(calls)-1]["expected_supported"]
        return {"status": "completed", "merge_supported": expected if matches else not expected}

    monkeypatch.setattr(verification.previous, "review_case", run)
    result = asyncio.run(verification.run())
    assert result["status"] == ("completed" if matches else "stopped")
    assert len(calls) == (3 if matches else 1)
    assert all(0 < c["budget_usd"] <= 0.492 for c in calls)


def test_changed_or_interrupted_window_does_not_resume(settings, monkeypatch):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "plan", lambda: {"frozen": True})
    assert verification.evaluate(50)["status"] == "blocked"
    assert verification.evaluate(160)["status"] == "blocked"
    (output / "results.json").write_text('{"status":"started"}')
    assert verification.evaluate(160)["reason"] == "previous_window_requires_review"
