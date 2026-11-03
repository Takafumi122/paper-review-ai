"""Paired selected-evidence audit of old/new LoRA contributions; no graph/S2 calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from paper_review.application import lineage_followup as previous
from paper_review.application.citation_quality_evaluation import load_cases
from paper_review.application.contribution_evidence import CONTRIBUTION_PROMPT, contribution_sources
from paper_review.application.full_analysis import BASE_PROMPT, fit_context
from paper_review.application.lineage_evidence import validate_contribution_language
from paper_review.application.reasoning_review import REASONING_REVIEW_PROMPT
from paper_review.config import ROOT, Settings
from paper_review.domain.understanding import (
    Contributions,
    ReasoningStep,
    ReasoningStepReview,
    validate_evidence,
    validate_reasoning_review,
)
from paper_review.infrastructure.database import models as db
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.gateway import ExecutionFailure, LLMGateway, request_schema
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/contribution-evidence-v23-2026-11-02"
DATABASE = ROOT / ".work/contribution-evidence-v23.db"
VERSION = "contribution-evidence-v23"


def ledger_cost(path: Path) -> float:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return sum(reserved if status in {"reserved", "unknown"} else estimated or 0
                   for status, reserved, estimated in connection.execute(
                       "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
                   ))


def prior_cost() -> float:
    return previous.prior_cost() + ledger_cost(previous.DATABASE)


def plan() -> dict:
    return {
        "version": VERSION, "cumulative_budget_usd": .50,
        "prior_used_or_reserved_usd": prior_cost(), "new_s2_requests": 0,
        "new_classification_requests": 0, "new_graph_requests": 0,
        "max_logical_calls": 7, "max_http_attempts": 21,
        "target": "2106.09685", "production_flags_changed": False,
        "extraction_policy": vars(ROUTING["contribution_extraction"]),
        "paired_review_policy": vars(ROUTING["reasoning_review"]),
        "review_scope": "Each old/new description against ONLY its own selected quotations",
        "parent_result_sha256": hashlib.sha256(
            (previous.OUTPUT / "results.json").read_bytes()
        ).hexdigest(),
        "source_code_sha256": {
            name: hashlib.sha256((ROOT / "src/paper_review" / name).read_bytes()).hexdigest()
            for name in ("application/contribution_evidence.py",
                         "application/contribution_verification.py", "application/full_analysis.py",
                         "application/reasoning_review.py", "domain/understanding.py",
                         "llm/gateway.py", "application/roadmap_verification.py")
        },
    }


async def _evaluate() -> dict:
    case = next(c for c in load_cases() if c["aid"] == "2106.09685")
    spans = case["spans"]
    sources = {s["span_id"]: s for s in spans}
    old = Contributions.model_validate(json.loads(
        (previous.OUTPUT / "results.json").read_text()
    )["lineage"]["contributions"])
    engine = make_engine(Settings(database_path=DATABASE))
    db.Base.metadata.create_all(engine)
    try:
        remaining = .50 - prior_cost() - ledger_cost(DATABASE)
        settings = Settings(database_path=DATABASE, provider="openai", initial_budget_usd=remaining)
        gateway = LLMGateway(engine, settings, OpenAIProvider())
        gateway.pipeline_version = VERSION
        owner = VERSION
        with transaction(engine) as session:
            paper = db.Paper(id="arxiv:2106.09685", external_ids={})
            session.add(paper)
            session.flush()
            job = db.AnalysisJob(paper_id=paper.id, arxiv_revision=VERSION, fingerprint=VERSION,
                                 status="building_lineage", owner=owner,
                                 lease_until=time.time() + 1200)
            session.add(job)
            session.flush()
            stage = db.StageExecution(job_id=job.id, name=VERSION, input_hash=VERSION)
            session.add(stage)
            session.flush()
            jid, sid = job.id, stage.id

        def check(value):
            validate_contribution_language(value)
            ids = [c.id for c in value.contributions]
            if len(ids) != len(set(ids)):
                raise ValueError("duplicate_contribution_id")
            for contribution in value.contributions:
                validate_evidence(contribution.evidence, sources)

        context = fit_context(
            request_schema(Contributions, bind_evidence=True), BASE_PROMPT + CONTRIBUTION_PROMPT,
            contribution_sources(spans), ROUTING["contribution_extraction"].max_input_tokens,
            preserve_source_order=True,
        )
        new = await gateway.execute(
            job_id=jid, stage_id=sid, logical_call_id=f"{VERSION}-extract",
            task_type="contribution_extraction", output_schema=Contributions, owner=owner,
            context_spans=context, instructions=BASE_PROMPT + CONTRIBUTION_PROMPT,
            validate_output=check, bind_evidence=True,
        )
        # Save accepted extraction before auditing; never retain rejected provider output.
        (OUTPUT / "extraction.json").write_text(json.dumps(
            {"contributions": new.model_dump(), "sent_source_ids": [s["span_id"] for s in context]},
            ensure_ascii=False, indent=2,
        ) + "\n")
        reviews = []
        # Fixed order, same reviewer/schema/selected-only rubric for both versions.
        # A valid negative semantic verdict is recorded once, never repaired/retried.
        for label, value in (("old", old), ("new", new)):
            for i, contribution in enumerate(value.contributions):
                step = ReasoningStep(id=contribution.id, kind="approach",
                                     summary=contribution.description,
                                     evidence=contribution.evidence)
                review_context = tuple({"span_id": e.span_id, "text": e.quote}
                                       for e in step.evidence) + ({
                    "span_id": "task-data", "text": json.dumps(
                        {"kind": "contribution", "summary": step.summary}, ensure_ascii=False,
                    ),
                },)
                review = await gateway.execute(
                    job_id=jid, stage_id=sid, logical_call_id=f"{VERSION}-{label}-{i}",
                    task_type="reasoning_review", output_schema=ReasoningStepReview,
                    owner=owner, context_spans=review_context,
                    instructions=REASONING_REVIEW_PROMPT,
                    validate_output=lambda v, step=step: validate_reasoning_review(v, step),
                )
                reviews.append({"version": label, "contribution_id": contribution.id,
                                "supported": review.supported, "review": review.model_dump()})
                (OUTPUT / "reviews.json").write_text(json.dumps(
                    reviews, ensure_ascii=False, indent=2,
                ) + "\n")
        with transaction(engine) as session:
            saved = session.get(db.AnalysisJob, jid)
            assert saved is not None
            saved.status, saved.owner = "completed", None
            saved_stage = session.get(db.StageExecution, sid)
            assert saved_stage is not None
            saved_stage.status = "completed"
            saved_stage.finished_at = time.time()
            saved_stage.checkpoint = {"reviews": reviews}
        return {"status": "completed", "reviews": reviews,
                "new_all_supported": bool(new.contributions) and all(
                    r["supported"] for r in reviews if r["version"] == "new"
                ), "production_flags_changed": False}
    finally:
        with transaction(engine) as session:
            for saved in session.query(db.AnalysisJob).filter_by(owner=VERSION):
                saved.status, saved.owner = "failed", None
            for saved_stage in session.query(db.StageExecution).filter_by(status="running"):
                saved_stage.status = "failed"
                saved_stage.finished_at = time.time()
        engine.dispose()


def evaluate(budget_cents: int = 15) -> dict:
    if budget_cents != 50:
        return {"status": "blocked", "reason": "explicit_shared_ceiling_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        result = json.loads(destination.read_text())
        return result if result.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review",
        }
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if prior_cost() >= .50:
        return {"status": "blocked", "reason": "shared_budget"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        (OUTPUT / "live-plan.json").write_text(json.dumps(plan(), indent=2) + "\n")
        destination.write_text('{"status":"started"}\n')
        try:
            result = asyncio.run(_evaluate())
        except Exception as exc:
            result = {"status": "stopped", "reason": (
                str(exc) if isinstance(exc, ExecutionFailure) else type(exc).__name__
            )}
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
