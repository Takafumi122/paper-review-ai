"""Independent cases proceed once; scoped checks and shared authorization remain mandatory."""

import hashlib
import json

import pytest

from paper_review.application import remaining_model_comparison
from paper_review.application import remaining_verification as bundle
from paper_review.llm.gateway import ExecutionFailure


@pytest.mark.parametrize("bundle", [bundle, remaining_model_comparison])
def test_bundle_keeps_negative_case_and_runs_independent_case_once(bundle, settings, monkeypatch):
    root = settings.database_path.parent
    output, parent = root / "new", root / "parent"
    output.mkdir()
    parent.mkdir()
    monkeypatch.setattr(bundle, "OUTPUT", output)
    monkeypatch.setattr(bundle.parent, "OUTPUT", parent)
    monkeypatch.setattr(bundle, "prior_cost", lambda: .30493187)
    monkeypatch.setattr(bundle, "plan", lambda: {"frozen": True})
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    monkeypatch.setattr(bundle, "load_cases", lambda: [])
    classification = parent / "classify-results.json"
    classification.write_text('{"status":"completed"}')
    (parent / "stage1-review.json").write_text(json.dumps({
        "accepted_for_lineage_diagnostic": True,
        "classification_sha256": hashlib.sha256(classification.read_bytes()).hexdigest(),
    }))
    (parent / "s2-results.json").write_text(json.dumps({"reports": [{
        "target": "2106.09685", "gate": "READY FOR QUALITY AUDIT",
    }]}))
    calls = []

    async def evaluate(cases, **kwargs):
        calls.append(kwargs)
        if bundle is remaining_model_comparison:
            assert kwargs["evaluation_model"] == "gpt-6.1-sol"
            assert kwargs["contribution_only"] is True
        if kwargs["target_aid"] == "2106.09685":
            raise ExecutionFailure("attempt_limit")
        assert kwargs["contribution_only"] is True
        return {"status": "completed", "lineage": {"status": "insufficient_evidence"}}

    monkeypatch.setattr(bundle.parent, "_lineage", evaluate)
    assert bundle.evaluate()["reason"] == "explicit_shared_ceiling_required"
    assert bundle.evaluate(50)["reason"] == "prepared_plan_required_or_changed"
    (output / "prepared-plan.json").write_text('{"frozen":true}')
    result = bundle.evaluate(50)
    assert len(calls) == 2 and result["status"] == "completed"
    assert result["cases"][0]["reason"] == "attempt_limit"
    assert result["cases"][1]["lineage"]["status"] == "insufficient_evidence"
    assert bundle.evaluate(50) == result and len(calls) == 2
    (output / "results.json").write_text('{"status":"started"}')
    assert bundle.evaluate(50)["reason"] == "previous_window_requires_review"
    assert len(calls) == 2
