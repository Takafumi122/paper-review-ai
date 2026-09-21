"""Persistent Phase 0 fake execution policy, with a single retry owner."""

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, select

from paper_review.config import Settings
from paper_review.infrastructure.database.models import AnalysisJob, LLMExecution, StageExecution
from paper_review.infrastructure.database.session import transaction
from paper_review.infrastructure.observability import record
from paper_review.llm.provider import (
    ROUTING,
    LLMProvider,
    LLMRequest,
    ProviderFailure,
    TaskPolicy,
)


class ExecutionFailure(RuntimeError):
    pass


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


class LLMGateway:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        provider: LLMProvider,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.engine = engine
        self.settings = settings
        self.provider = provider
        self.sleep = sleep

    async def execute[T: BaseModel](
        self,
        *,
        job_id: str,
        stage_id: str,
        logical_call_id: str,
        task_type: str,
        output_schema: type[T],
        owner: str,
        policy: TaskPolicy | None = None,
    ) -> T:
        configured = ROUTING.get(task_type)
        if configured is None:
            raise ExecutionFailure("unknown_task")
        limits = policy or configured
        if limits.model != configured.model:
            raise ExecutionFailure("invalid_model")
        schema = output_schema.model_json_schema()
        # A previous owner may have lost a response. Commit recovery before guards
        # raise, so unknown cost remains visible even if no next attempt can start.
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            if (
                job is None
                or job.owner != owner
                or job.lease_until is None
                or job.lease_until <= time.time()
            ):
                raise ExecutionFailure("lease_lost")
            interrupted = session.scalars(
                select(LLMExecution).where(
                    LLMExecution.job_id == job_id, LLMExecution.status == "running"
                )
            )
            for previous in interrupted:
                previous.status = "interrupted"
                previous.cost_status = "unknown"
                previous.error_code = "response_lost"
                previous.finished_at = time.time()
        repair_error = None
        for _ in range(3):
            with transaction(self.engine) as session:
                job = session.get(AnalysisJob, job_id)
                if (
                    job is None
                    or job.owner != owner
                    or job.lease_until is None
                    or job.lease_until <= time.time()
                ):
                    raise ExecutionFailure("lease_lost")
                stage = session.get(StageExecution, stage_id)
                if stage is None or stage.job_id != job_id:
                    raise ExecutionFailure("invalid_stage")
                input_hash = digest(
                    {
                        "revision": job.arxiv_revision,
                        "stage_input": stage.input_hash,
                        "context_spans": [],
                    }
                )
                prompt_hash = digest("Phase 0 fake readiness check; no paper analysis")
                fingerprint = digest(
                    {
                        "input": input_hash,
                        "schema": schema,
                        "task": task_type,
                        "provider": "fake",
                        "model": limits.model,
                        "settings": "fake_deterministic",
                        "prompt": prompt_hash,
                        "prompt_version": "1",
                        "schema_version": "1",
                        "pipeline_version": "fake-pipeline-v1",
                        "parser_version": "fake-v1",
                        "scoring_version": "unresolved",
                        "language": "ja",
                        "policy": vars(limits),
                    }
                )
                attempts = list(
                    session.scalars(
                        select(LLMExecution)
                        .where(
                            LLMExecution.job_id == job_id,
                            LLMExecution.logical_call_id == logical_call_id,
                        )
                        .order_by(LLMExecution.attempt)
                    )
                )
                for previous in attempts:
                    if previous.metadata_json["fingerprint"] != fingerprint:
                        raise ExecutionFailure("logical_call_input_changed")
                    if previous.status == "completed" and previous.output is not None:
                        metadata = dict(previous.metadata_json)
                        metadata["cache_hit"] = True
                        previous.metadata_json = metadata
                        return output_schema.model_validate(previous.output)
                if attempts and attempts[-1].error_code in {
                    "refusal",
                    "configuration",
                    "invalid_input",
                }:
                    raise ExecutionFailure(attempts[-1].error_code)
                if attempts and attempts[-1].metadata_json.get("retryable") is False:
                    raise ExecutionFailure(attempts[-1].error_code or "provider_error")
                deadline_at = (
                    attempts[0].started_at if attempts else time.time()
                ) + limits.deadline
                if len(attempts) >= 3:
                    raise ExecutionFailure("attempt_limit")
                if time.time() >= deadline_at:
                    raise ExecutionFailure("task_deadline")
                all_attempts = list(
                    session.scalars(select(LLMExecution).where(LLMExecution.job_id == job_id))
                )
                category = "jit_detail" if task_type == "knowledge_detail" else "initial_analysis"
                cap = 3 if category == "jit_detail" else 30
                if len(all_attempts) >= cap:
                    raise ExecutionFailure("job_attempt_limit")
                budget = (
                    self.settings.detail_budget_usd
                    if category == "jit_detail"
                    else self.settings.initial_budget_usd
                )
                used = sum(
                    e.reserved_usd
                    if e.cost_status in {"reserved", "unknown"}
                    else e.estimated_cost_usd or 0
                    for e in all_attempts
                )
                # Synthetic USD-per-token rates. No claim about real provider pricing.
                input_rate, output_rate = (
                    (0.000001, 0.000002) if limits.model == "gpt-5-nano" else (0.000002, 0.000004)
                )
                reservation = (
                    limits.max_input_tokens * input_rate + limits.max_output_tokens * output_rate
                )
                if used + reservation > budget:
                    raise ExecutionFailure("budget_exceeded")
                metadata = {
                    "provider": "fake",
                    "requested_model": limits.model,
                    "returned_model": None,
                    "provider_request_id": None,
                    "generation_settings": {"fake_deterministic": True},
                    "prompt_version": "1",
                    "prompt_hash": prompt_hash,
                    "schema_version": "1",
                    "pipeline_version": "fake-pipeline-v1",
                    "scoring_version": "unresolved",
                    "input_hash": input_hash,
                    "source_ids": [],
                    "fingerprint": fingerprint,
                    "task_type": task_type,
                    "cache_hit": False,
                    "currency": "USD",
                    "price_table_version": "fake-v1",
                    "simulated": True,
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "latency_seconds": None,
                    "retry_reason": attempts[-1].error_code if attempts else None,
                }
                execution = LLMExecution(
                    job_id=job_id,
                    paper_id=job.paper_id,
                    stage_id=stage_id,
                    logical_call_id=logical_call_id,
                    attempt=len(attempts) + 1,
                    reserved_usd=reservation,
                    metadata_json=metadata,
                    cost_category=category,
                )
                session.add(execution)
                session.flush()
                execution_id = execution.id
                request = LLMRequest(
                    job_id=job_id,
                    paper_id=job.paper_id,
                    stage_id=stage_id,
                    logical_call_id=logical_call_id,
                    task_type=task_type,
                    model=limits.model,
                    prompt_hash=prompt_hash,
                    input_hash=input_hash,
                    output_schema=schema,
                    max_input_tokens=limits.max_input_tokens,
                    max_output_tokens=limits.max_output_tokens,
                    attempt_timeout=limits.timeout,
                    task_deadline=deadline_at,
                    cost_category=category,
                    remaining_budget=budget - used - reservation,
                    repair_error=repair_error,
                )
            started = time.monotonic()
            response = None
            validated = None
            error = None
            retryable = True
            try:
                response = await asyncio.wait_for(
                    self.provider.generate(request),
                    timeout=min(limits.timeout, max(0, deadline_at - time.time())),
                )
                validated = output_schema.model_validate(response.output)
            except TimeoutError:
                error = "timeout"
            except ValidationError:
                error = "schema_validation"
            except ProviderFailure as exc:
                error = (
                    exc.code
                    if exc.code
                    in {
                        "refusal",
                        "configuration",
                        "invalid_input",
                        "rate_limit",
                        "unavailable",
                    }
                    else "provider_error"
                )
                retryable = exc.retryable and error not in {
                    "refusal",
                    "configuration",
                    "invalid_input",
                }
            except Exception:
                # Provider exception text may contain source content; never persist it.
                error = "provider_error"
                retryable = False
            with transaction(self.engine) as session:
                saved = session.get(LLMExecution, execution_id)
                assert saved is not None
                saved.finished_at = time.time()
                saved.error_code = error
                saved.status = "completed" if validated is not None else "failed"
                saved.cost_status = "estimated" if response else "unknown"
                metadata = dict(saved.metadata_json)
                metadata["latency_seconds"] = time.monotonic() - started
                metadata["retryable"] = retryable
                metadata["validation_status"] = "valid" if validated is not None else error
                if response is not None:
                    metadata.update(
                        {
                            "input_tokens": response.input_tokens,
                            "cached_input_tokens": response.cached_input_tokens,
                            "output_tokens": response.output_tokens,
                            "reasoning_tokens": response.reasoning_tokens,
                            "returned_model": response.returned_model,
                            "provider_request_id": response.request_id,
                        }
                    )
                    saved.estimated_cost_usd = (
                        response.input_tokens * input_rate + response.output_tokens * output_rate
                    )
                saved.metadata_json = metadata
                saved.output = validated.model_dump(mode="json") if validated is not None else None
            record(
                "llm_finished",
                job_id=job_id,
                stage_id=stage_id,
                attempt_id=execution_id,
                status="completed" if validated is not None else "failed",
                error_code=error,
                input_tokens=response.input_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                estimated_cost_usd=saved.estimated_cost_usd,
                retry_count=len(attempts),
            )
            if validated is not None:
                return validated
            if not retryable:
                raise ExecutionFailure(error or "provider_error")
            repair_error = error
            # No sleep after final attempt; durable count remains authoritative on restart.
            if len(attempts) < 2:
                delay = 2 ** len(attempts) + random.uniform(0, 0.5)
                await self.sleep(min(delay, max(0, deadline_at - time.time())))
        raise ExecutionFailure("attempt_limit")
