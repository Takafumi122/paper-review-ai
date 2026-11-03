"""Diagnostic mechanics with dummy credentials; no live semantic-quality claims."""

import json

import pytest

from paper_review.application import reasoning_review_evaluation as evaluation
from paper_review.domain.understanding import Evidence, ReasoningStep
from paper_review.llm.provider import ProviderResponse


@pytest.fixture
def diagnostic(settings, monkeypatch):
    monkeypatch.setattr(evaluation, "OUTPUT", settings.database_path.parent / "diagnostic")
    monkeypatch.setattr(evaluation, "DATABASE", settings.database_path)
    monkeypatch.setattr(
        evaluation,
        "historical_step",
        lambda: ReasoningStep(
            id="5",
            kind="experiment",
            summary="翻訳性能を検証するため、学習した。",
            evidence=[Evidence(span_id="selected", quote="We trained on a dataset.")],
        ),
    )
    return evaluation.cases()


def test_fixed_labels_separate_ambiguous_cases_from_scored_expectations(diagnostic):
    assert len(diagnostic) == 12
    assert [c.expected for c in diagnostic].count("accept") == 5
    assert [c.expected for c in diagnostic].count("reject") == 5
    assert [c.expected for c in diagnostic].count("adjudication_required") == 2
    cut = next(c for c in diagnostic if c.id == "comparison_truncated")
    continued = next(c for c in diagnostic if c.id == "comparison_continued")
    assert cut.step.summary == continued.step.summary
    assert len(cut.step.evidence) == 1 and len(continued.step.evidence) == 2
    summary = evaluation.summarize(
        [
            {"status": "reviewed", "expected": "accept", "accepted": False},
            {"status": "reviewed", "expected": "reject", "accepted": True},
            {"status": "reviewed", "expected": "adjudication_required", "accepted": False},
            {"status": "failed", "expected": "accept"},
        ]
    )
    assert summary == {
        "scored_cases": 2,
        "expected_accept_rejected": 1,
        "expected_reject_accepted": 1,
        "unscored_borderline_cases": 1,
    }


def test_missing_credentials_and_interrupted_runs_never_call_provider(diagnostic, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert evaluation.evaluate()["reason"] == "OPENAI_API_KEY_missing"
    assert not (evaluation.OUTPUT / "results.json").exists()
    (evaluation.OUTPUT / "results.json").write_text('{"status":"started","cases":[]}')
    assert evaluation.evaluate()["reason"] == "previous_run_requires_review"


def test_diagnostic_uses_production_reviewer_without_labels_and_reuses_finished_results(
    diagnostic, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    suite = [diagnostic[0], diagnostic[1], diagnostic[-1]]
    monkeypatch.setattr(evaluation, "cases", lambda: suite)
    requests = []

    class Stub:
        async def generate(self, request):
            requests.append(request)
            assert request.task_type == "reasoning_review"
            assert request.generation_settings["instructions"] == evaluation.REASONING_REVIEW_PROMPT
            task = json.loads(
                next(s["text"] for s in request.context_spans if s["span_id"] == "task-data")
            )
            assert set(task) == {"kind", "summary"}
            return ProviderResponse(
                output={
                    "claims": [
                        {
                            "text": task["summary"],
                            "decision": "supported",
                            "evidence_span_ids": [request.context_spans[0]["span_id"]],
                            "reason": "合成テスト用の固定応答",
                        }
                    ]
                },
                request_id="mock",
                returned_model=request.model,
            )

    monkeypatch.setattr(evaluation, "OpenAIProvider", Stub)
    result = evaluation.evaluate()
    assert result["status"] == "completed" and len(requests) == 3
    assert result["summary"]["expected_reject_accepted"] == 1
    assert result["summary"]["unscored_borderline_cases"] == 1
    assert all(r["attempts"][0]["requested_model"] == "gpt-6.1-sol" for r in result["cases"])
    assert evaluation.evaluate() == result and len(requests) == 3


def test_budget_is_enforced_before_provider_call(diagnostic, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setattr(evaluation, "cases", lambda: diagnostic[:1])
    monkeypatch.setattr(evaluation, "TOTAL_BUDGET_USD", 0.000001)

    class NeverCall:
        async def generate(self, request):
            raise AssertionError("budget must stop this call")

    monkeypatch.setattr(evaluation, "OpenAIProvider", NeverCall)
    result = evaluation.evaluate()
    assert result["status"] == "stopped"
    assert result["cases"][0]["status"] == "failed"
    assert result["cases"][0]["attempts"] == []


def test_changed_frozen_plan_stops_before_any_provider_work(diagnostic, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    evaluation.OUTPUT.mkdir()
    (evaluation.OUTPUT / "prepared-plan.json").write_text('{"version":"different"}')
    assert evaluation.evaluate()["reason"] == "frozen_plan_changed"
    assert not (evaluation.OUTPUT / "results.json").exists()
