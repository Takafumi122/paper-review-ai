"""Durable short-explanation reviews using only each item's selected quotes."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from paper_review.application.contribution_review import ContributionReview, audit_contributions
from paper_review.domain.understanding import (
    Contribution,
    Contributions,
    KnowledgeItem,
    ReasoningStep,
    ReasoningStepReview,
    validate_reasoning_review,
)
from paper_review.infrastructure.database.models import AnalysisJob, StageExecution
from paper_review.infrastructure.database.session import transaction
from paper_review.llm.gateway import digest

if TYPE_CHECKING:
    from paper_review.worker import Worker


class KnowledgeEvidenceFailure(RuntimeError):
    """A short explanation cannot be published with its selected evidence."""


async def review_knowledge_items(
    worker: Worker,
    job_id: str,
    items: list[KnowledgeItem],
    call: Callable[..., Awaitable[Any]],
) -> None:
    if len(items) > 5 or len({item.id for item in items}) != len(items):
        raise KnowledgeEvidenceFailure("knowledge_item_limit_or_duplicate")
    for index in range(0, len(items), 4):
        batch = items[index:index + 4]
        first, *rest = batch
        # Review labels are code-owned, so an item named 'problem' cannot
        # collide with the review's first-item slot or another generated ID.
        value = Contributions(
            problem=first.short_explanation,
            problem_evidence=first.evidence,
            contributions=[
                Contribution(id=f"item_{i}", description=item.short_explanation,
                             evidence=item.evidence)
                for i, item in enumerate(rest, 1)
            ],
        )
        fingerprint = digest([item.model_dump() for item in batch])
        with transaction(worker.engine) as session:
            worker.assert_owned(session.get(AnalysisJob, job_id))
            saved = session.scalar(select(StageExecution).where(
                StageExecution.job_id == job_id,
                StageExecution.name == f"knowledge_evidence_review_{index // 4}",
            ))
            if saved is None:
                saved = StageExecution(job_id=job_id,
                                       name=f"knowledge_evidence_review_{index // 4}",
                                       input_hash=fingerprint)
                session.add(saved)
                session.flush()
            if saved.input_hash != fingerprint:
                raise KnowledgeEvidenceFailure("knowledge_review_input_changed")
            stage_id, checkpoint = saved.id, saved.checkpoint

        if checkpoint is None:
            async def review_call(*args, review_index=index, **kwargs):
                return await call(
                    *args, **kwargs,
                    logical_call_id=f"knowledge-evidence-review-{review_index // 4}-v1",
                )

            try:
                review = await audit_contributions(value, stage_id, review_call)
            except Exception as exc:
                if type(exc).__name__ == "LeaseLost":
                    raise
                with transaction(worker.engine) as session:
                    worker.assert_owned(session.get(AnalysisJob, job_id))
                    saved = session.get(StageExecution, stage_id)
                    assert saved is not None
                    saved.status = "failed"
                    saved.error_code = type(exc).__name__
                    saved.finished_at = time.time()
                raise
        else:
            review = ContributionReview.model_validate(checkpoint["review"])
            steps = {"problem": ReasoningStep(id="problem", kind="approach",
                        summary=value.problem, evidence=value.problem_evidence)}
            steps.update({c.id: ReasoningStep(id=c.id, kind="approach", summary=c.description,
                                             evidence=c.evidence) for c in value.contributions})
            if (len(review.items) != len(steps)
                    or {item.item_id for item in review.items} != steps.keys()):
                raise KnowledgeEvidenceFailure("knowledge_review_items_changed")
            for item in review.items:
                validate_reasoning_review(ReasoningStepReview(claims=item.claims),
                                          steps[item.item_id])

        if checkpoint is None:
            with transaction(worker.engine) as session:
                worker.assert_owned(session.get(AnalysisJob, job_id))
                saved = session.get(StageExecution, stage_id)
                assert saved is not None
                saved.status = "completed"
                saved.finished_at = time.time()
                saved.checkpoint = {
                    "supported": review.supported,
                    "item_ids": [item.id for item in batch],
                    "review": review.model_dump(),
                }
        if not review.supported:
            raise KnowledgeEvidenceFailure("knowledge_evidence_not_supported")
