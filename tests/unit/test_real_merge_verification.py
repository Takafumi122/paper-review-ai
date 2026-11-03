import json

import pytest

from paper_review.application import real_merge_verification as verification
from paper_review.domain.models import CandidatePaper, Paper
from paper_review.domain.understanding import Contributions, LineageProposalDraft, validate_lineage


def test_frozen_rag_source_candidate_passes_structural_checks():
    value = json.loads((verification.OUTPUT / "real-paper-input.json").read_text())
    proposal = LineageProposalDraft.model_validate(value["proposal"])
    edges = validate_lineage(
        proposal, Paper.model_validate(value["target"]),
        {c["paper_id"]: CandidatePaper.model_validate(c) for c in value["candidates"]},
        {s["span_id"]: s for s in value["sources"]},
        contributions=Contributions.model_validate(value["contributions"]),
    )
    assert len(edges) == 2
    assert all(e["status"] == "supported" for e in edges)
    # A structural pass is deliberately not a semantic/gold assertion.
    assert "not_independent_gold" in value["scope"]


@pytest.mark.parametrize("supported", [True, False])
def test_one_shot_verdict_never_retries_to_force_positive(settings, monkeypatch, supported):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "plan", lambda: {"frozen": True})
    (output / "prepared-plan.json").write_text('{"frozen":true}')
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    calls = []

    async def run():
        calls.append(1)
        return {"status": "completed", "merge_supported": supported}

    monkeypatch.setattr(verification, "run", run)
    assert verification.evaluate(160)["merge_supported"] is supported
    assert verification.evaluate(160)["merge_supported"] is supported
    assert len(calls) == 1


def test_interrupted_or_changed_window_does_not_send(settings, monkeypatch):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "plan", lambda: {"frozen": True})
    assert verification.evaluate(50)["status"] == "blocked"
    assert verification.evaluate(160)["status"] == "blocked"
    (output / "results.json").write_text('{"status":"started"}')
    assert verification.evaluate(160)["reason"] == "previous_window_requires_review"
