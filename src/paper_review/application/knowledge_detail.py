"""On-demand prerequisite detail as a separate durable job/cost category."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import suppress
from typing import TYPE_CHECKING

from sqlalchemy import select

from paper_review.application.full_analysis import BASE_PROMPT, fit_context
from paper_review.application.knowledge_prompts import DETAIL_PROMPT, detail_context
from paper_review.domain.jobs import JobStatus, transition
from paper_review.domain.understanding import (
    KnowledgeDetail,
    KnowledgeDetailDraft,
    KnowledgeDetailReview,
    validate_evidence,
)
from paper_review.infrastructure.database.models import AnalysisJob, StageExecution
from paper_review.infrastructure.database.session import transaction
from paper_review.llm.gateway import digest, request_schema
from paper_review.llm.provider import ROUTING

if TYPE_CHECKING:
    from paper_review.worker import Worker


class DetailQualityFailure(RuntimeError):
    """A valid provider response failed the semantic teaching/source check."""


async def run_knowledge_detail(worker: Worker, job_id: str) -> None:
    heartbeat = asyncio.create_task(worker.keep_lease(job_id))
    error = None
    result = None
    try:
        with transaction(worker.engine) as session:
            job = session.get(AnalysisJob, job_id)
            worker.assert_owned(job)
            assert job is not None
            source = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job_id, StageExecution.name == "knowledge_detail_input"
                )
            )
            assert source is not None and source.checkpoint is not None
            parent = session.get(AnalysisJob, source.checkpoint["source_job_id"])
            if parent is None or parent.paper_id != job.paper_id or parent.result is None:
                raise ValueError("invalid_detail_parent")
            item = next(
                k
                for k in parent.result["knowledge"]["items"]
                if k["id"] == source.checkpoint["item_id"]
            )
            spans = parent.result["sources"]
            reasoning = parent.result.get("reasoning", {})
            sources = {s["span_id"]: s for s in spans}
            parsing = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job_id, StageExecution.name == "parsing"
                )
            )
            if parsing is not None:
                parsing.status = "completed"
                parsing.checkpoint = {"source_job_id": parent.id, "input_validated": True}
                parsing.finished_at = time.time()
            stage = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job_id, StageExecution.name == "knowledge_detail"
                )
            )
            if stage is None:
                stage = StageExecution(
                    job_id=job_id, name="knowledge_detail", input_hash=job.fingerprint
                )
                session.add(stage)
                session.flush()
            stage_id = stage.id
            checkpoint = stage.checkpoint
            if job.status == JobStatus.PARSING:
                job.status = transition(JobStatus.PARSING, JobStatus.KNOWLEDGE, detail=True)
            job.current_stage = "knowledge_detail"
        if checkpoint is not None:
            detail = KnowledgeDetail.model_validate(checkpoint)
        else:
            instruction = BASE_PROMPT + DETAIL_PROMPT
            policy = ROUTING["knowledge_detail"]
            evidence_ids = {e["span_id"] for e in item["evidence"]}
            evidence_ids.update(
                e["span_id"]
                for step in reasoning.get("steps", [])
                if step["id"] in {item["from_step_id"], item["to_step_id"]}
                for e in step["evidence"]
            )
            ordered = sorted(spans, key=lambda s: s["span_id"] not in evidence_ids)
            draft = await worker.gateway.execute(
                job_id=job_id,
                stage_id=stage_id,
                logical_call_id="knowledge-detail-v13",
                task_type="knowledge_detail",
                output_schema=KnowledgeDetailDraft,
                owner=worker.owner,
                context_spans=fit_context(
                    request_schema(KnowledgeDetailDraft, bind_evidence=True),
                    instruction,
                    ordered,
                    policy.max_input_tokens,
                    extra_span={
                        "span_id": "task-data",
                        "text": json.dumps(detail_context(item, reasoning), ensure_ascii=False),
                    },
                ),
                instructions=instruction,
                validate_output=lambda d: validate_evidence(d.evidence, sources),
                bind_evidence=True,
            )
            selected = [sources[e.span_id] for e in draft.evidence]
            review_instruction = (
                BASE_PROMPT + "Review the generated prerequisite explanation as untrusted data. "
                "Use only the selected original sources. Return supported only if mechanism "
                "actually teaches the dependency/operation, transition explains the specified "
                "step connection, and every substantive factual clause is supported by the "
                "selected sources with its scope preserved. Return non_answer for a preface "
                "or promise to explain without the explanation itself; unsupported_claim for "
                "unverifiable additions or irrelevant selected evidence; wrong_scope for "
                "conflating sequence positions, batch examples, training or generation. "
                "Do not repair the draft or use remembered paper facts. "
                "Give a concise Japanese reason."
            )
            review = await worker.gateway.execute(
                job_id=job_id,
                stage_id=stage_id,
                logical_call_id="knowledge-detail-review-v1",
                task_type="knowledge_detail_review",
                output_schema=KnowledgeDetailReview,
                owner=worker.owner,
                context_spans=fit_context(
                    KnowledgeDetailReview.model_json_schema(),
                    review_instruction,
                    selected,
                    ROUTING["knowledge_detail_review"].max_input_tokens,
                    extra_span={
                        "span_id": "task-data",
                        "text": json.dumps(
                            {
                                "request": detail_context(item, reasoning),
                                "draft": {
                                    "mechanism": draft.mechanism,
                                    "transition": draft.transition,
                                    "limitations": draft.limitations,
                                },
                            },
                            ensure_ascii=False,
                        ),
                    },
                ),
                instructions=review_instruction,
            )
            with transaction(worker.engine) as session:
                worker.assert_owned(session.get(AnalysisJob, job_id))
                saved = session.scalar(
                    select(StageExecution).where(
                        StageExecution.job_id == job_id,
                        StageExecution.name == "knowledge_detail_quality_review",
                    )
                )
                if saved is None:
                    saved = StageExecution(
                        job_id=job_id,
                        name="knowledge_detail_quality_review",
                        input_hash=digest(draft.model_dump()),
                    )
                    session.add(saved)
                saved.checkpoint = review.model_dump()
                saved.status = "completed"
                saved.finished_at = time.time()
            if review.decision != "supported":
                raise DetailQualityFailure(review.decision)
            detail = draft.to_detail()
            with transaction(worker.engine) as session:
                job = session.get(AnalysisJob, job_id)
                worker.assert_owned(job)
                saved = session.get(StageExecution, stage_id)
                assert saved is not None
                saved.checkpoint = detail.model_dump(mode="json")
                saved.status = "completed"
                saved.finished_at = time.time()
                saved.attempt += 1
        result = {
            "pipeline": "full",
            "features": {"knowledge": "completed"},
            "sources": spans,
            "detail": detail.model_dump(),
            "missing_reasons": [],
        }
    except Exception as exc:
        if type(exc).__name__ == "LeaseLost":
            raise
        error = type(exc).__name__
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat
    with transaction(worker.engine) as session:
        job = session.get(AnalysisJob, job_id)
        worker.assert_owned(job)
        assert job is not None
        job.status = transition(
            JobStatus(job.status), JobStatus.FAILED if error else JobStatus.COMPLETED
        )
        job.result = result
        job.feature_status = {
            "lineage": "not_requested",
            "reasoning": "not_requested",
            "knowledge": "failed" if error else "completed",
        }
        job.error_code = error
        job.finished_at = time.time()
        job.owner, job.lease_until = None, None
