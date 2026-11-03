"""Evaluation routing and staged gates: dummy keys, synthetic outputs, no network."""

import asyncio
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests_support import create_job

from paper_review.application import model_evaluation as evaluation
from paper_review.domain.understanding import Contributions
from paper_review.infrastructure.database.models import LLMExecution, StageExecution
from paper_review.llm.gateway import LLMGateway, digest
from paper_review.llm.provider import ROUTING, FakeLLMProvider, FakeOutput
from paper_review.worker import Worker


@pytest.mark.parametrize(
    "model,rates",
    [
        ("gpt-6.1-sol", (2, 10)),
        ("gpt-6-luna", (0.1, 0.5)),
        ("gpt-6.1-terra", (1, 5)),
    ],
)
def test_evaluation_model_priced_without_changing_production(engine, settings, model, rates):
    config = settings.model_copy(update={"provider": "openai"})
    provider = FakeLLMProvider()
    jid = create_job(engine, settings)
    worker = Worker(engine, settings)
    assert worker.claim() == jid
    with Session(engine) as session:
        stage = session.scalar(select(StageExecution).where(StageExecution.job_id == jid))
        assert stage is not None
        sid = stage.id
    gateway = LLMGateway(engine, config, provider, evaluation_model=model)
    asyncio.run(
        gateway.execute(
            job_id=jid,
            stage_id=sid,
            logical_call_id="comparison",
            task_type="contribution_extraction",
            output_schema=FakeOutput,
            owner=worker.owner,
        )
    )
    assert provider.calls[0].model == model
    assert ROUTING["contribution_extraction"].model == "gpt-6.1-sol"
    with Session(engine) as session:
        record = session.scalar(select(LLMExecution))
        assert record is not None
        assert record.estimated_cost_usd == pytest.approx(
            (100 * rates[0] + 20 * rates[1]) / 1_000_000
        )
        assert record.metadata_json["pipeline_version"] == "model-evaluation-v1"


def test_eval_controls_reject_other_model(engine, settings):
    with pytest.raises(ValueError, match="unsupported_evaluation_model"):
        LLMGateway(engine, settings, FakeLLMProvider(), evaluation_model="unpriced")


@pytest.fixture
def pilot(settings, monkeypatch):
    out = settings.database_path.parent / "model-pilot"
    monkeypatch.setattr(evaluation, "OUTPUT", out)
    monkeypatch.setattr(evaluation, "DATABASE", settings.database_path.parent / "pilot.db")
    case = evaluation.Case(
        "case",
        "arxiv:test",
        "synthetic",
        "contribution_extraction",
        Contributions,
        "Extract",
        ({"span_id": "source", "text": "Fact"},),
    )
    monkeypatch.setattr(evaluation, "cases", lambda: [case])
    return out, case


def test_missing_key_does_not_reserve_provider_call(pilot, monkeypatch):
    out, _ = pilot
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert evaluation.evaluate("sol")["reason"] == "OPENAI_API_KEY_missing"
    assert not (out / "sol.json").exists()
    assert not (out / "active.lock").exists()


def test_no_cheaper_model_before_bound_source_review(pilot, monkeypatch):
    out, _ = pilot
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    assert evaluation.evaluate("luna")["reason"] == "source_review_required"
    plan = json.loads((out / "plan.json").read_text())
    output = {"problem": "fixture", "contributions": []}
    (out / "sol.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "cases": [{"case_id": "case", "status": "validated", "output": output}],
            }
        )
    )
    review = {
        "plan_sha256": digest(plan),
        "reviewer_kind": "agent_provisional",
        "cases": [
            {
                "case_id": "case",
                "decision": "pass",
                "source_checked": False,
                "notes": "must independently check scope",
                "output_sha256": digest(output),
            }
        ],
    }
    (out / "sol-review.json").write_text(json.dumps(review))
    assert evaluation.evaluate("luna")["reason"] == "no_source_reviewed_passes"
    review["cases"][0]["source_checked"] = True
    (out / "sol-review.json").write_text(json.dumps(review))
    called = []

    async def fake_run(phase, suite, destination):
        called.append([c.id for c in suite])
        return {"status": "completed", "cases": []}

    monkeypatch.setattr(evaluation, "_run", fake_run)
    assert evaluation.evaluate("luna")["status"] == "completed"
    assert evaluation.evaluate("luna")["status"] == "completed"
    assert called == [["case"]]  # persisted result prevents another send


def test_mocked_evaluation_stores_validated_output_and_usage(pilot, monkeypatch):
    out, _ = pilot
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    monkeypatch.setattr(
        evaluation,
        "OpenAIProvider",
        lambda: FakeLLMProvider([{"problem": "fixture", "contributions": []}]),
    )
    result = evaluation.evaluate("sol")
    assert result["status"] == "completed"
    assert result["cases"][0]["status"] == "validated"
    assert result["quality_acceptance"] == "not_reviewed"
    assert "dummy-key" not in (out / "sol.json").read_text()


def test_cross_process_lock_blocks_sends(pilot):
    out, _ = pilot
    out.mkdir()
    (out / "active.lock").touch()
    assert evaluation.evaluate("sol")["reason"] == "evaluation_active_or_interrupted"
