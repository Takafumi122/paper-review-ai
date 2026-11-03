"""No live calls: production classifier, isolated budget and operator safeguards."""

import gzip
import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from paper_review.api.app import create_app
from paper_review.application import citation_quality_evaluation as evaluation
from paper_review.application.stage1_shortlist import Stage1Analysis
from paper_review.domain.models import CandidatePaper, CandidateSet, Paper, Reference
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.llm.provider import ProviderResponse


@pytest.fixture
def pilot(settings, monkeypatch):
    root = settings.database_path.parent
    monkeypatch.setattr(evaluation, "OUTPUT", root / "evaluation")
    monkeypatch.setattr(evaluation, "DATABASE", settings.database_path)
    monkeypatch.setattr(evaluation, "prior_reservation", lambda: 0)
    fixture = root / "fixture.gz"
    fixture.write_bytes(gzip.compress(b"[]"))
    target = Paper(id="target", title="Synthetic target")
    inventory = CandidateSet(target_paper_id="target", candidates=[CandidatePaper(
        paper_id="candidate", title="Prior method", resolution_status="resolved",
        source_references=[Reference(
            id="ref", paper_id="target", revision_id="rev", ref_order=2,
            raw_text="Reference", citation_key="[2]",
        )],
    )])
    text = "We use the method [2]."
    cases = [{"aid": "synthetic", "source_job": "source", "target": target.model_dump(),
              "inventory": inventory.model_dump(), "fixture": fixture,
              "spans": [{"span_id": "source", "paper_id": "target", "revision": "rev",
                         "section_id": "section", "section": "Method", "text": text,
                         "start_offset": "0", "end_offset": str(len(text))}]}]
    monkeypatch.setattr(evaluation, "load_cases", lambda: cases)

    async def frozen_analysis(target, inventory, client):
        return Stage1Analysis(target=target, inventory=inventory, ranking=rank_stage1_candidates(
            target, inventory, target_references_fully_resolved=False,
        ))

    monkeypatch.setattr(evaluation, "build_stage1_shortlist", frozen_analysis)
    evaluation.OUTPUT.mkdir()
    (evaluation.OUTPUT / "prepared-plan.json").write_text(json.dumps(evaluation.plan(cases)))
    return cases


def test_missing_key_or_interrupted_window_never_sends(pilot, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert evaluation.evaluate()["reason"] == "OPENAI_API_KEY_missing"
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    (evaluation.OUTPUT / "sol-active.lock").touch()
    assert evaluation.evaluate()["reason"] == "active_or_interrupted"


def test_sol_first_with_provenance_and_cached_completion(pilot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            assert request.model in {"gpt-6.1-sol", "gpt-5-nano"}
            return ProviderResponse(output={"decisions": [{
                "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                "classification": "core_foundation",
                "support_id": json.loads(s["support_options"])[0]["support_id"],
                "reason": "手法を使用",
            } for s in request.context_spans]}, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(evaluation, "OpenAIProvider", Provider)
    result = evaluation.evaluate()
    assert result["status"] == "completed" and len(calls) == 1
    metric = result["cases"][0]["after"][0]["score"]["component_details"]["citation_importance"]
    assert metric["source_ids"] == ["source"] and metric["value"] == 1
    assert evaluation.evaluate() == result and len(calls) == 1
    assert calls[0].model == "gpt-6.1-sol"
    (evaluation.OUTPUT / "sol-review.json").write_text(json.dumps({
        "accepted_for_comparison": True,
        "sol_result_sha256": hashlib.sha256(
            (evaluation.OUTPUT / "sol-results.json").read_bytes()
        ).hexdigest(),
    }))
    nano = evaluation.evaluate("nano")
    assert nano["status"] == "completed" and len(calls) == 2
    assert calls[1].model == "gpt-5-nano"
    assert evaluation.evaluate("nano") == nano and len(calls) == 2


def test_nano_requires_source_reviewed_baseline(pilot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    assert evaluation.evaluate("nano")["reason"] == "source_reviewed_sol_baseline_required"
    assert not (evaluation.OUTPUT / "nano-active.lock").exists()


def test_budget_fails_closed_without_provider_call(pilot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(evaluation, "BUDGET", 0.000001)
    (evaluation.OUTPUT / "prepared-plan.json").write_text(json.dumps(evaluation.plan(pilot)))

    class Provider:
        async def generate(self, request):
            raise AssertionError("budget must reject first")

    monkeypatch.setattr(evaluation, "OpenAIProvider", Provider)
    assert evaluation.evaluate()["status"] == "stopped"


def test_operator_endpoint_is_opt_in_loopback_and_summary_only(engine, settings, monkeypatch):
    path = "/verification/citation-contexts"
    monkeypatch.delenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", raising=False)
    assert path not in {getattr(route, "path", "") for route in create_app(settings, engine).routes}
    monkeypatch.setenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", "1")
    monkeypatch.setattr(evaluation, "evaluate", lambda: {"status": "completed", "cases": [1, 2]})
    with TestClient(create_app(settings, engine)) as api:
        assert api.post(path).json() == {
            "status": "completed", "reason": None, "cases_finished": 2,
        }
        assert api.post(path + "/unknown").status_code == 422
    with TestClient(create_app(settings, engine), client=("192.0.2.1", 1234)) as api:
        assert api.post(path).status_code == 403
