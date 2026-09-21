import asyncio
from dataclasses import replace
from typing import TypedDict

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests_support import create_job

from paper_review.infrastructure.database.models import LLMExecution, StageExecution
from paper_review.llm.gateway import ExecutionFailure, LLMGateway
from paper_review.llm.provider import EXTRACTION, FakeLLMProvider, FakeOutput, ProviderFailure
from paper_review.worker import Worker


class CallArguments(TypedDict):
    job_id: str
    stage_id: str
    logical_call_id: str
    task_type: str
    output_schema: type[FakeOutput]
    owner: str


async def no_sleep(_):
    pass


def prepare(engine, settings, provider):
    job_id = create_job(engine, settings)
    worker = Worker(engine, settings)
    assert worker.claim() == job_id
    with Session(engine) as session:
        stage = session.scalar(select(StageExecution).where(StageExecution.job_id == job_id))
        assert stage is not None
        stage_id = stage.id
    gateway = LLMGateway(engine, settings, provider, sleep=no_sleep)
    kwargs = CallArguments(
        job_id=job_id,
        stage_id=stage_id,
        logical_call_id="test",
        task_type="contribution_extraction",
        output_schema=FakeOutput,
        owner=worker.owner,
    )
    return gateway, kwargs


def test_validation_repair_and_cache(engine, settings):
    provider = FakeLLMProvider([{"fake": "bad"}, {"fake": True, "message": "完了"}])
    gateway, kwargs = prepare(engine, settings, provider)
    assert asyncio.run(gateway.execute(**kwargs)).message == "完了"
    assert asyncio.run(gateway.execute(**kwargs)).message == "完了"
    assert len(provider.calls) == 2
    assert provider.calls[1].repair_error == "schema_validation"
    with Session(engine) as session:
        executions = list(session.scalars(select(LLMExecution).order_by(LLMExecution.attempt)))
        assert executions[0].output is None
        assert executions[0].estimated_cost_usd is not None
        assert executions[0].estimated_cost_usd > 0
        assert executions[1].metadata_json["cache_hit"] is True
        assert executions[1].metadata_json["price_table_version"] == "fake-v1"
        assert executions[1].metadata_json["requested_model"] == "gpt-5-nano"


def test_attempt_limit_survives_restart(engine, settings):
    provider = FakeLLMProvider([{}, {}, {}])
    gateway, kwargs = prepare(engine, settings, provider)
    with pytest.raises(ExecutionFailure, match="attempt_limit"):
        asyncio.run(gateway.execute(**kwargs))
    restarted = LLMGateway(engine, settings, provider, sleep=no_sleep)
    with pytest.raises(ExecutionFailure, match="attempt_limit"):
        asyncio.run(restarted.execute(**kwargs))
    assert len(provider.calls) == 3


def test_timeout_keeps_unknown_reservation(engine, settings):
    provider = FakeLLMProvider(delay=0.05)
    gateway, kwargs = prepare(engine, settings, provider)
    with pytest.raises(ExecutionFailure, match="attempt_limit"):
        asyncio.run(gateway.execute(**kwargs, policy=replace(EXTRACTION, timeout=0.001)))
    with Session(engine) as session:
        executions = list(session.scalars(select(LLMExecution)))
        assert len(executions) == 3
        assert all(
            e.cost_status == "unknown" and e.reserved_usd > 0 and e.estimated_cost_usd is None
            for e in executions
        )


def test_budget_prevents_call(engine, settings):
    provider = FakeLLMProvider()
    gateway, kwargs = prepare(engine, settings, provider)
    gateway.settings = settings.model_copy(update={"initial_budget_usd": 0.00001})
    with pytest.raises(ExecutionFailure, match="budget_exceeded"):
        asyncio.run(gateway.execute(**kwargs))
    assert not provider.calls


def test_unknown_cost_does_not_restore_budget(engine, settings):
    provider = FakeLLMProvider([TimeoutError()])
    gateway, kwargs = prepare(engine, settings, provider)
    gateway.settings = settings.model_copy(update={"initial_budget_usd": 0.013})
    with pytest.raises(ExecutionFailure, match="budget_exceeded"):
        asyncio.run(gateway.execute(**kwargs))
    assert len(provider.calls) == 1


@pytest.mark.parametrize("code", ["refusal", "configuration", "invalid_input"])
def test_non_retryable(engine, settings, code):
    provider = FakeLLMProvider([ProviderFailure(code, retryable=True)])
    gateway, kwargs = prepare(engine, settings, provider)
    with pytest.raises(ExecutionFailure, match=code):
        asyncio.run(gateway.execute(**kwargs))
    with pytest.raises(ExecutionFailure, match=code):
        asyncio.run(gateway.execute(**kwargs))
    assert len(provider.calls) == 1


def test_fingerprint_change_rejects_cache(engine, settings):
    provider = FakeLLMProvider()
    gateway, kwargs = prepare(engine, settings, provider)
    asyncio.run(gateway.execute(**kwargs))
    with pytest.raises(ExecutionFailure, match="logical_call_input_changed"):
        asyncio.run(gateway.execute(**kwargs, policy=replace(EXTRACTION, max_output_tokens=1000)))
    assert len(provider.calls) == 1


def test_interrupted_reservation_committed_when_budget_blocks(engine, settings):
    provider = FakeLLMProvider([TimeoutError()])
    gateway, kwargs = prepare(engine, settings, provider)
    gateway.settings = settings.model_copy(update={"initial_budget_usd": 0.013})
    with pytest.raises(ExecutionFailure, match="budget_exceeded"):
        asyncio.run(gateway.execute(**kwargs))
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None
        execution.status = "running"
        execution.cost_status = "reserved"
        session.commit()
    with pytest.raises(ExecutionFailure, match="budget_exceeded"):
        asyncio.run(gateway.execute(**kwargs))
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None
        assert execution.status == "interrupted"
        assert execution.cost_status == "unknown"
    assert len(provider.calls) == 1


def test_deadline_stops_retry(engine, settings):
    provider = FakeLLMProvider(delay=0.1)
    gateway, kwargs = prepare(engine, settings, provider)
    with pytest.raises(ExecutionFailure, match="task_deadline"):
        asyncio.run(gateway.execute(**kwargs, policy=replace(EXTRACTION, deadline=0.001)))
    assert len(provider.calls) == 1


def test_provider_error_sanitized_and_not_retried_on_restart(engine, settings, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="paper_review")
    provider = FakeLLMProvider([RuntimeError("PRIVATE PAPER BODY SENTINEL")])
    gateway, kwargs = prepare(engine, settings, provider)
    for _ in range(2):
        with pytest.raises(ExecutionFailure, match="provider_error"):
            asyncio.run(gateway.execute(**kwargs))
    assert len(provider.calls) == 1
    assert "PRIVATE PAPER BODY SENTINEL" not in caplog.text
    assert '"retry_count": 0' in caplog.text
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None
        assert execution.error_code == "provider_error"
        assert execution.output is None
