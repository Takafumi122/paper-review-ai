import asyncio
import time
from dataclasses import replace
from typing import Any, TypedDict

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from tests_support import create_job

from paper_review.infrastructure.database.models import LLMExecution, StageExecution
from paper_review.llm.gateway import ExecutionFailure, LLMGateway, validate_display_references
from paper_review.llm.provider import (
    EXTRACTION,
    ROUTING,
    FakeLLMProvider,
    FakeOutput,
    ProviderFailure,
    ProviderTimeout,
)
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


def test_internal_citation_labels_cannot_leak_into_display_but_quotes_stay_original():
    for text in ["説明（S4、S28）", "Claim (S26)", "Claim [E2]", "根拠: S1、S26", "Source: S2"]:
        with pytest.raises(ValueError, match="unresolved_inline_reference"):
            validate_display_references({"explanation": text})
    validate_display_references(
        {
            "explanation": "S2 is a provider; O(n) is complexity.",
            "evidence": [{"span_id": "s", "quote": "Original (S1)"}],
        }
    )
    validate_display_references(
        {"reader_explanation_evidence": [{"span_id": "s", "quote": "Original (S1)"}]}
    )
    with pytest.raises(ValueError, match="unresolved_inline_reference"):
        validate_display_references({"reader_explanation": "説明 (S1)"})


def test_both_reasoning_part_citations_bind_exactly_without_generated_quotes():
    from paper_review.llm.gateway import bind_source_evidence

    sources = {"S0": "Reported fact.", "S1": "Supporting premise."}
    pointers = {"evidence": [{"span_id": "S0"}], "reader_explanation_evidence": [{"span_id": "S1"}]}
    bound = bind_source_evidence(pointers, sources, original_ids={"S0": "fact", "S1": "premise"})
    assert bound == {
        "evidence": [{"span_id": "fact", "quote": sources["S0"]}],
        "reader_explanation_evidence": [{"span_id": "premise", "quote": sources["S1"]}],
    }
    for bad in [{"span_id": "S1", "quote": "Invented quote"}, {"span_id": "unknown"}]:
        with pytest.raises(ValueError):
            bind_source_evidence({"reader_explanation_evidence": [bad]}, sources)


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
        task_type="evidence_extraction",
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
        assert all(e.metadata_json["timeout_origin"] == "gateway" for e in executions)
        assert all(e.metadata_json["timeout_phase"] is None for e in executions)
        assert len({c.client_request_id for c in provider.calls}) == 3


@pytest.mark.parametrize("transport_timeout", [False, True])
def test_timeout_diagnosis_retains_cost_and_distinguishes_clock_elapsed(
    engine, settings, monkeypatch, transport_timeout,
):
    clock = [time.time()]

    class TimeoutProvider(FakeLLMProvider):
        async def generate(self, request):
            self.calls.append(request)
            clock[0] += 400
            if transport_timeout:
                raise ProviderTimeout("read")
            raise TimeoutError("private source must not be recorded")

    provider = TimeoutProvider()
    gateway, kwargs = prepare(engine, settings.model_copy(update={"lease_seconds": 900}), provider)
    monkeypatch.setattr("paper_review.llm.gateway.time.time", lambda: clock[0])
    with pytest.raises(ExecutionFailure, match="task_deadline"):
        asyncio.run(gateway.execute(**kwargs))
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None and len(provider.calls) == 1
        assert execution.cost_status == "unknown" and execution.reserved_usd > 0
        assert execution.estimated_cost_usd is None and execution.output is None
        meta = execution.metadata_json
        assert meta["timeout_origin"] == ("transport" if transport_timeout else "provider")
        assert meta["timeout_phase"] == ("read" if transport_timeout else None)
        assert meta["wall_elapsed_seconds"] == 400
        assert meta["latency_seconds"] < 1
        assert meta["effective_timeout_seconds"] <= meta["attempt_timeout_seconds"] == 30
        assert meta["provider_client_request_id"] == execution.id
        assert provider.calls[0].client_request_id == execution.id
        assert "private source" not in str(meta)


def test_budget_prevents_call(engine, settings):
    provider = FakeLLMProvider()
    gateway, kwargs = prepare(engine, settings, provider)
    gateway.settings = settings.model_copy(update={"initial_budget_usd": 0.00001})
    with pytest.raises(ExecutionFailure, match="budget_exceeded"):
        asyncio.run(gateway.execute(**kwargs))
    assert not provider.calls


def test_production_citation_sol_reservation_respects_remaining_shared_budget(engine, settings):
    provider = FakeLLMProvider()
    gateway, kwargs = prepare(engine, settings, provider)
    gateway.settings = settings.model_copy(update={
        "provider": "openai", "initial_budget_usd": 0.0352196,
    })
    kwargs["task_type"] = "citation_classification"
    policy = ROUTING["citation_classification"]
    assert policy.model == "gpt-6.1-sol"
    assert (policy.max_input_tokens, policy.max_output_tokens) == (8000, 2000)
    assert (policy.timeout, policy.deadline) == (30, 90)
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


def test_deadline_stops_retry(engine, settings, monkeypatch):
    clock = [time.time()]

    class DeadlineProvider(FakeLLMProvider):
        async def generate(self, request):
            clock[0] += 2
            return await super().generate(request)

    provider = DeadlineProvider([ProviderFailure("unavailable", retryable=True)])
    gateway, kwargs = prepare(engine, settings, provider)
    # A real 1ms timer races SQLite/OS scheduling and can expire before any send.
    # Advance wall time at the first provider call to test the retry boundary.
    monkeypatch.setattr("paper_review.llm.gateway.time.time", lambda: clock[0])
    with pytest.raises(ExecutionFailure, match="task_deadline"):
        asyncio.run(gateway.execute(**kwargs, policy=replace(EXTRACTION, deadline=1)))
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


@pytest.mark.parametrize("code", ["output_limit", "incomplete", "invalid_response"])
def test_response_failure_reason_is_retained_without_resend(engine, settings, code):
    provider = FakeLLMProvider([ProviderFailure(code, http_status=200)])
    gateway, kwargs = prepare(engine, settings, provider)
    for _ in range(2):
        with pytest.raises(ExecutionFailure, match=code):
            asyncio.run(gateway.execute(**kwargs))
    assert len(provider.calls) == 1
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None and execution.error_code == code
        assert execution.metadata_json["provider_http_status"] == 200
        assert execution.cost_status == "unknown" and execution.reserved_usd > 0


def test_configuration_http_status_survives_restart_without_retry(engine, settings):
    provider = FakeLLMProvider([ProviderFailure("configuration", http_status=403)])
    gateway, kwargs = prepare(engine, settings, provider)
    with pytest.raises(ExecutionFailure, match="configuration"):
        asyncio.run(gateway.execute(**kwargs))
    restarted = LLMGateway(engine, settings, provider, sleep=no_sleep)
    with pytest.raises(ExecutionFailure, match="configuration"):
        asyncio.run(restarted.execute(**kwargs))
    assert len(provider.calls) == 1
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None
        assert execution.metadata_json["provider_http_status"] == 403
        assert execution.output is None


@pytest.mark.parametrize(
    "reason", ["unverified_evidence_quote_mismatch", "private-body-dummy-secret"]
)
def test_semantic_repair_reason_is_allowlisted_and_output_remains_validated(
    engine, settings, reason
):
    provider = FakeLLMProvider()
    gateway, kwargs = prepare(engine, settings, provider)
    validations = 0

    def validate(output):
        nonlocal validations
        validations += 1
        if validations == 1:
            raise ValueError(reason)

    asyncio.run(gateway.execute(**kwargs, validate_output=validate))
    expected = reason if reason.startswith("unverified_evidence_") else "schema_validation"
    assert provider.calls[1].repair_error == expected
    with Session(engine) as session:
        attempts = list(session.scalars(select(LLMExecution).order_by(LLMExecution.attempt)))
        assert attempts[0].output is None and attempts[1].output is not None
        assert attempts[0].metadata_json["validation_reason"] == (
            reason if expected == reason else None
        )
        assert "private-body-dummy-secret" not in str(attempts[0].metadata_json)


def test_selected_source_is_bound_verbatim_and_generated_quote_is_rejected(engine, settings):
    from paper_review.domain.understanding import Contributions, validate_evidence

    text = "An exact hyphen-\nated claim with unusual spacing."
    base = {
        "problem": "test",
        "contributions": [
            {
                "id": "c",
                "description": "interpretation",
                "evidence": [{"span_id": "S0", "quote": "invented paraphrase"}],
            }
        ],
    }
    repaired = {
        "problem": "test",
        "contributions": [
            {
                "id": "c",
                "description": "interpretation",
                "evidence": [{"span_id": "S0"}],
            }
        ],
    }
    provider = FakeLLMProvider([base, repaired])
    gateway, kwargs = prepare(engine, settings, provider)
    result = asyncio.run(
        gateway.execute(
            job_id=kwargs["job_id"],
            stage_id=kwargs["stage_id"],
            logical_call_id="source-bound",
            task_type="contribution_extraction",
            output_schema=Contributions,
            owner=kwargs["owner"],
            context_spans=({"span_id": "source", "text": text},),
            bind_evidence=True,
            validate_output=lambda value: validate_evidence(
                value.contributions[0].evidence, {"source": {"text": text}}
            ),
        )
    )
    assert result.contributions[0].evidence[0].quote == text
    assert "quote" not in provider.calls[0].output_schema["$defs"]["Evidence"]["properties"]
    assert provider.calls[0].output_schema["$defs"]["Evidence"]["properties"]["span_id"][
        "enum"
    ] == ["S0"]
    assert provider.calls[1].repair_error == "invalid_evidence_reference"
    with Session(engine) as session:
        attempts = list(session.scalars(select(LLMExecution).order_by(LLMExecution.attempt)))
        assert attempts[0].output is None
        assert attempts[1].output == result.model_dump(mode="json")
        assert (
            attempts[1].metadata_json["generation_settings"]["evidence_binding"] == "source-span-v2"
        )


def test_binding_rejects_invented_source_and_cannot_replace_quote():
    from paper_review.llm.gateway import bind_source_evidence

    with pytest.raises(ValueError, match="unverified_evidence_unknown_span"):
        bind_source_evidence({"evidence": [{"span_id": "invented"}]}, {"real": "source"})
    with pytest.raises(ValueError, match="invalid_evidence_reference"):
        bind_source_evidence(
            {"evidence": [{"span_id": "real", "quote": "fabricated"}]}, {"real": "source"}
        )


def test_provider_usage_above_input_limit_stops_and_preserves_known_cost(engine, settings):
    from paper_review.llm.provider import ProviderResponse

    class UsageProvider:
        calls = 0

        async def generate(self, request):
            self.calls += 1
            return ProviderResponse(
                output={"fake": True, "message": "result"},
                request_id="test",
                returned_model=request.model,
                input_tokens=request.max_input_tokens + 1,
            )

    provider = UsageProvider()
    gateway, kwargs = prepare(engine, settings, provider)
    with pytest.raises(ExecutionFailure, match="input_limit_exceeded"):
        asyncio.run(gateway.execute(**kwargs))
    assert provider.calls == 1
    with Session(engine) as session:
        execution = session.scalar(select(LLMExecution))
        assert execution is not None and execution.output is None
        assert execution.cost_status == "estimated" and execution.estimated_cost_usd
        assert execution.metadata_json["retryable"] is False


def test_constrained_edge_and_source_refs_restore_provenance_and_cache_context(engine, settings):
    from paper_review.domain.understanding import KnowledgeBatch

    payload = {
        "items": [
            {
                "id": "k",
                "concept": "正規化",
                "edge_id": "E0",
                "necessity_score": 3,
                "why_needed": "理由",
                "short_explanation": "仕組み",
                "evidence": [{"span_id": "S0"}],
            }
        ]
    }
    provider = FakeLLMProvider([payload])
    gateway, kwargs = prepare(engine, settings, provider)
    args: dict[str, Any] = {
        **kwargs,
        "output_schema": KnowledgeBatch,
        "bind_evidence": True,
        "knowledge_edges": {"E0": ("left", "right")},
        "context_spans": ({"span_id": "original-section:p2:c400", "text": "original text"},),
    }
    result = asyncio.run(gateway.execute(**args))
    item = result.items[0]
    assert (item.from_step_id, item.to_step_id) == ("left", "right")
    assert item.evidence[0].span_id == "original-section:p2:c400"
    assert item.evidence[0].quote == "original text"
    schema = provider.calls[0].output_schema["$defs"]["KnowledgeItem"]
    assert schema["properties"]["edge_id"]["enum"] == ["E0"]
    assert "from_step_id" not in schema["properties"]
    args["knowledge_edges"] = {"E0": ("left", "different")}
    with pytest.raises(ExecutionFailure, match="logical_call_input_changed"):
        asyncio.run(gateway.execute(**args))
    assert len(provider.calls) == 1


def test_unknown_edge_is_rejected_instead_of_inventing_a_transition():
    from paper_review.llm.gateway import bind_knowledge_edges

    with pytest.raises(ValueError, match="knowledge_edge_missing"):
        bind_knowledge_edges({"items": [{"edge_id": "invented"}]}, {"E0": ("a", "b")})
    with pytest.raises(ValueError, match="knowledge_edge_missing"):
        bind_knowledge_edges(
            {"items": [{"edge_id": "E0", "from_step_id": "a"}]}, {"E0": ("a", "b")}
        )
