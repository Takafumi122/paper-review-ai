"""Short explanations cannot borrow another item's selected quotations."""

import asyncio
import time
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.application.knowledge_review import (
    KnowledgeEvidenceFailure,
    review_knowledge_items,
)
from paper_review.config import Settings
from paper_review.domain.understanding import Evidence, KnowledgeItem
from paper_review.infrastructure.database.models import AnalysisJob, Paper, StageExecution
from paper_review.llm.gateway import ExecutionFailure
from paper_review.worker import Worker


def setup_worker(engine):
    assert engine.url.database is not None
    worker = Worker(engine, Settings(database_path=Path(engine.url.database)), pipeline_mode="full")
    with Session(engine) as session:
        session.add(Paper(id="paper", external_ids={}))
        session.flush()
        job = AnalysisJob(paper_id="paper", arxiv_revision="1v1", fingerprint="review-test",
                          status="building_knowledge", owner=worker.owner,
                          lease_until=time.time() + 600)
        session.add(job)
        session.commit()
        jid = job.id

    return worker, jid


def item(index, identifier=None):
    return KnowledgeItem(id=identifier or f"k{index}", concept="必要な操作",
                         from_step_id="a", to_step_id="b", necessity_score=3,
                         why_needed="接続に必要", short_explanation=f"操作{index}。",
                         evidence=[Evidence(span_id=f"s{index}", quote=f"Operation {index}.")])


def judge(calls, *, decision="supported", borrow=False):
    async def call(schema, stage_id, task, instruction, spans, extra, validator, **kwargs):
        assert task == "contribution_review" and kwargs["require_all_sources"]
        assert kwargs["bind_evidence"] is False
        calls.append((kwargs["logical_call_id"], spans, extra))
        output = {"items": [{"item_id": part["item_id"], "claims": [{
            "text": part["summary"], "decision": decision,
            "evidence_span_ids": ([spans[-1]["span_id"]] if borrow else
                                  part["selected_source_ids"]) if decision == "supported" else [],
            "reason": "選択引用の支持を確認",
        }]} for part in extra["items"]]}
        review = schema.model_validate(output)
        validator(review)
        return review
    return call


def test_five_items_reviewed_in_two_calls_and_reused_without_another_call(engine):
    worker, jid = setup_worker(engine)
    # Generated IDs cannot collide with internal review labels.
    items = [item(i, identifier) for i, identifier in enumerate(
        ["problem", "item_1", "third", "fourth", "fifth"]
    )]
    calls = []
    asyncio.run(review_knowledge_items(worker, jid, items, judge(calls)))
    assert [c[0] for c in calls] == ["knowledge-evidence-review-0-v1",
                                   "knowledge-evidence-review-1-v1"]
    assert [len(c[2]["items"]) for c in calls] == [4, 1]
    asyncio.run(review_knowledge_items(worker, jid, items, judge(calls)))
    assert len(calls) == 2
    with Session(engine) as session:
        stages = list(session.scalars(select(StageExecution).order_by(StageExecution.name)))
        for stage in stages:
            assert stage.status == "completed" and stage.checkpoint is not None
            assert stage.checkpoint["supported"]
        assert stages[0].checkpoint is not None
        assert stages[0].checkpoint["item_ids"] == [i.id for i in items[:4]]


@pytest.mark.parametrize("decision", ["unsupported", "uncertain", "wrong_scope"])
def test_semantic_rejection_is_durable_and_not_retried(engine, decision):
    worker, jid = setup_worker(engine)
    calls = []
    for _ in range(2):
        with pytest.raises(KnowledgeEvidenceFailure, match="knowledge_evidence_not_supported"):
            asyncio.run(review_knowledge_items(
                worker, jid, [item(0)], judge(calls, decision=decision),
            ))
    assert len(calls) == 1
    changed = item(0).model_copy(update={"short_explanation": "別の操作。"})
    with pytest.raises(KnowledgeEvidenceFailure, match="knowledge_review_input_changed"):
        asyncio.run(review_knowledge_items(worker, jid, [changed], judge(calls)))
    assert len(calls) == 1


def test_another_items_quote_cannot_support_a_claim_even_when_in_the_request(engine):
    worker, jid = setup_worker(engine)
    calls = []
    with pytest.raises(ValueError, match="reasoning_review_unselected_evidence"):
        asyncio.run(review_knowledge_items(
            worker, jid, [item(0), item(1)], judge(calls, borrow=True),
        ))
    extra = calls[0][2]
    assert extra["items"][0]["selected_source_ids"] == ["s0"]
    assert {s["span_id"] for s in calls[0][1]} == {"s0", "s1"}
    with Session(engine) as session:
        stage = session.scalar(select(StageExecution))
        assert stage is not None and stage.status == "failed" and stage.checkpoint is None


def test_empty_or_excess_items_do_not_start_reviews_and_budget_failure_is_retained(engine):
    worker, jid = setup_worker(engine)
    calls = []
    asyncio.run(review_knowledge_items(worker, jid, [], judge(calls)))
    for invalid in ([item(i) for i in range(6)], [item(0), item(0)]):
        with pytest.raises(KnowledgeEvidenceFailure):
            asyncio.run(review_knowledge_items(worker, jid, invalid, judge(calls)))
    assert calls == []

    async def denied(*args, **kwargs):
        raise ExecutionFailure("budget_exceeded")

    with pytest.raises(ExecutionFailure, match="budget_exceeded"):
        asyncio.run(review_knowledge_items(worker, jid, [item(0)], denied))
    with Session(engine) as session:
        stage = session.scalar(select(StageExecution))
        assert stage is not None and stage.status == "failed"
        assert stage.error_code == "ExecutionFailure" and stage.checkpoint is None
