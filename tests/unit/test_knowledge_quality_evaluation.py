"""Isolated fixed-flow evaluation controls with dummy keys and mocked provider."""

import pytest
from fastapi.testclient import TestClient

from paper_review.api.app import create_app
from paper_review.application import knowledge_quality_evaluation as evaluation
from paper_review.application.model_evaluation import Case
from paper_review.domain.understanding import KnowledgeBatch
from paper_review.llm.provider import ProviderResponse


@pytest.fixture
def pilot(settings, monkeypatch):
    monkeypatch.setattr(evaluation, "OUTPUT", settings.database_path.parent / "quality")
    monkeypatch.setattr(evaluation, "DATABASE", settings.database_path)
    cases = [
        Case(
            i,
            i,
            "synthetic",
            "short_knowledge",
            KnowledgeBatch,
            "Explain only selected evidence.",
            ({"span_id": "source", "text": "The update is BA and the original weight is frozen."},),
            {"E0": ("a", "b")},
        )
        for i in ("lora", "attention")
    ]
    monkeypatch.setattr(evaluation, "cases", lambda: (cases, []))
    return cases


def test_quality_missing_key_and_interrupted_results_stop(pilot, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert evaluation.evaluate()["reason"] == "OPENAI_API_KEY_missing"
    (evaluation.OUTPUT / "results.json").write_text('{"status":"started"}')
    assert evaluation.evaluate()["reason"] == "previous_run_requires_review"


def test_fixed_quality_cases_use_current_model_and_cache_completed_output(pilot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            assert request.model == "gpt-6.1-sol" and request.task_type == "short_knowledge"
            return ProviderResponse(
                output={
                    "items": [
                        {
                            "id": "k",
                            "concept": "低ランク分解",
                            "edge_id": "E0",
                            "necessity_score": 3,
                            "why_needed": "更新の理解に必要",
                            "short_explanation": "固定重みとBAによる更新を区別する。",
                            "evidence": [{"span_id": request.context_spans[0]["span_id"]}],
                        }
                    ]
                },
                request_id="mock",
                returned_model=request.model,
            )

    monkeypatch.setattr(evaluation, "OpenAIProvider", Provider)
    result = evaluation.evaluate()
    assert result["status"] == "completed" and len(calls) == 2
    assert all(
        r["displayed_items"][0]["evidence"][0]["span_id"] == "source" for r in result["cases"]
    )
    assert evaluation.evaluate() == result and len(calls) == 2


def test_quality_budget_stops_before_provider_work(pilot, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(evaluation, "TOTAL_BUDGET_USD", 0.000001)

    class Provider:
        async def generate(self, request):
            raise AssertionError("must not reach provider")

    monkeypatch.setattr(evaluation, "OpenAIProvider", Provider)
    result = evaluation.evaluate()
    assert result["status"] == "stopped" and result["cases"][0]["attempts"] == []


def test_quality_operator_endpoint_is_opt_in_local_and_returns_only_summary(
    engine, settings, monkeypatch
):
    monkeypatch.delenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", raising=False)
    app = create_app(settings, engine)
    assert all(getattr(route, "path", None) != "/verification/knowledge" for route in app.routes)
    with TestClient(app) as api:
        assert api.post("/verification/knowledge").status_code in {404, 405}
    monkeypatch.setenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", "1")
    monkeypatch.setattr(
        evaluation,
        "evaluate",
        lambda: {"status": "completed", "cases": [{"private": "not returned"}]},
    )
    with TestClient(create_app(settings, engine)) as api:
        assert api.post("/verification/knowledge").json() == {
            "status": "completed",
            "reason": None,
            "cases_finished": 1,
        }
    with TestClient(create_app(settings, engine), client=("203.0.113.1", 1234)) as api:
        assert api.post("/verification/knowledge").status_code == 403
