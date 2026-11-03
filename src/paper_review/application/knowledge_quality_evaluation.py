"""Two fixed-flow Knowledge cases: change teaching/context, not the reasoning sample."""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.application.full_analysis import BASE_PROMPT, fit_context
from paper_review.application.knowledge_prompts import (
    SHORT_KNOWLEDGE_PROMPT,
    short_knowledge_inputs,
)
from paper_review.application.model_evaluation import Case, _validate
from paper_review.config import ROOT, Settings
from paper_review.domain.understanding import KnowledgeBatch
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    Base,
    LLMExecution,
    Paper,
    StageExecution,
)
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.gateway import LLMGateway, digest, request_schema
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/knowledge-quality-v1-2026-10-31"
DATABASE = ROOT / ".work/knowledge-quality-v1.db"
SOURCE_JOBS = ("25762f7f-0d5a-438e-ab30-2c04b1ae7954", "0330fab2-e4f6-4beb-a856-ad03b4951b20")
TOTAL_BUDGET_USD = 0.30


def cases() -> tuple[list[Case], list[dict[str, Any]]]:
    engine = make_engine(Settings(database_path=ROOT / ".work/app.db"))
    suite, baselines = [], []
    try:
        with Session(engine) as session:
            for jid in SOURCE_JOBS:
                job = session.get(AnalysisJob, jid)
                if (
                    job is None
                    or job.result is None
                    or job.feature_status.get("reasoning") != "completed"
                ):
                    raise ValueError("verified_source_flow_missing")
                result = job.result
                spans, extra, edges = short_knowledge_inputs(result["reasoning"], result["sources"])
                instructions = BASE_PROMPT + SHORT_KNOWLEDGE_PROMPT
                context = fit_context(
                    request_schema(KnowledgeBatch, bind_evidence=True, knowledge_edges=edges),
                    instructions,
                    spans,
                    ROUTING["short_knowledge"].max_input_tokens,
                    extra_span={
                        "span_id": "task-data",
                        "text": json.dumps(extra, ensure_ascii=False),
                    },
                    preserve_source_order=True,
                )
                suite.append(
                    Case(
                        job.paper_id,
                        job.paper_id,
                        "fixed verified full-source flow",
                        "short_knowledge",
                        KnowledgeBatch,
                        instructions,
                        context,
                        edges,
                    )
                )
                baselines.append(
                    {
                        "paper_id": job.paper_id,
                        "source_job_id": jid,
                        "reasoning_sha256": digest(result["reasoning"]),
                        "baseline": result["knowledge"],
                        "available_source_ids": [s["span_id"] for s in result["sources"]],
                    }
                )
        return suite, baselines
    finally:
        engine.dispose()


def evaluate() -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / "results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return (
            old
            if old.get("status") == "completed"
            else {"status": "blocked", "reason": "previous_run_requires_review"}
        )
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    lock = OUTPUT / "active.lock"
    try:
        handle = lock.open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    try:
        handle.close()
        suite, baselines = cases()
        plan = {
            "version": "knowledge-quality-v1",
            "budget_usd": TOTAL_BUDGET_USD,
            "max_http_attempts": 3 * len(suite),
            "model": ROUTING["short_knowledge"].model,
            "cases": [c.manifest() for c in suite],
            "baselines": baselines,
            "automatic_quality_acceptance": False,
        }
        frozen = OUTPUT / "prepared-plan.json"
        if frozen.exists() and json.loads(frozen.read_text()) != plan:
            return {"status": "blocked", "reason": "frozen_plan_changed"}
        (OUTPUT / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        destination.write_text('{"status":"started","cases":[]}\n')
        result = asyncio.run(_run(suite, destination))
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    finally:
        lock.unlink()


async def _run(suite, destination) -> dict[str, Any]:
    config = Settings(database_path=DATABASE, provider="openai")
    engine = make_engine(config)
    Base.metadata.create_all(engine)
    rows = []
    try:
        for case in suite:
            with Session(engine) as session:
                used = sum(
                    e.reserved_usd
                    if e.cost_status in {"reserved", "unknown"}
                    else e.estimated_cost_usd or 0
                    for e in session.scalars(select(LLMExecution))
                )
            if used >= TOTAL_BUDGET_USD:
                return {"status": "stopped", "reason": "global_budget", "cases": rows}
            limits = config.model_copy(update={"initial_budget_usd": TOTAL_BUDGET_USD - used})
            with transaction(engine) as session:
                session.add(Paper(id=case.paper_id, external_ids={}))
                session.flush()
                job = AnalysisJob(
                    paper_id=case.paper_id,
                    arxiv_revision="fixed-knowledge-v1",
                    fingerprint=case.manifest()["input_sha256"],
                    status="building_knowledge",
                    owner="knowledge-evaluator",
                    lease_until=time.time() + 600,
                )
                session.add(job)
                session.flush()
                stage = StageExecution(
                    job_id=job.id, name="building_knowledge", input_hash=job.fingerprint
                )
                session.add(stage)
                session.flush()
                jid, sid = job.id, stage.id
            row: dict[str, Any] = {"paper_id": case.paper_id}
            try:
                output = await LLMGateway(engine, limits, OpenAIProvider()).execute(
                    job_id=jid,
                    stage_id=sid,
                    logical_call_id="knowledge-quality-v1",
                    task_type="short_knowledge",
                    output_schema=KnowledgeBatch,
                    owner="knowledge-evaluator",
                    context_spans=case.context,
                    instructions=case.instruction,
                    bind_evidence=True,
                    knowledge_edges=case.edges,
                    validate_output=lambda value, case=case: _validate(case, value),
                )
                row.update(
                    status="validated",
                    output=output.model_dump(),
                    displayed_items=[
                        i.model_dump() for i in output.items if i.necessity_score == 3
                    ],
                )
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__)
            with transaction(engine) as session:
                job = session.get(AnalysisJob, jid)
                assert job is not None
                job.status = "completed" if row["status"] == "validated" else "failed"
                job.owner = None
                row["attempts"] = [
                    {
                        "status": e.status,
                        "attempt": e.attempt,
                        "cost_status": e.cost_status,
                        "estimated_usd": e.estimated_cost_usd,
                        "reserved_usd": e.reserved_usd,
                        "requested_model": e.metadata_json.get("requested_model"),
                        "returned_model": e.metadata_json.get("returned_model"),
                        "input_tokens": e.metadata_json.get("input_tokens"),
                        "output_tokens": e.metadata_json.get("output_tokens"),
                    }
                    for e in session.scalars(select(LLMExecution).where(LLMExecution.job_id == jid))
                ]
            rows.append(row)
            destination.write_text(
                json.dumps({"status": "running", "cases": rows}, ensure_ascii=False, indent=2)
                + "\n"
            )
            if row["status"] == "failed":
                return {"status": "stopped", "cases": rows}
        return {"status": "completed", "cases": rows}
    finally:
        engine.dispose()
