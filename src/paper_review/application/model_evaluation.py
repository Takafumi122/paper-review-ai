"""Bounded development pilot: Sol first, reviewed cases only for cheaper models.

This does not promote production routing or provide human quality acceptance.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.application.full_analysis import BASE_PROMPT, fit_context
from paper_review.application.knowledge_prompts import (
    DETAIL_PROMPT,
    SHORT_KNOWLEDGE_PROMPT,
    detail_context,
    short_knowledge_inputs,
)
from paper_review.application.reasoning_prompt import REASONING_PROMPT
from paper_review.config import ROOT, Settings
from paper_review.domain.understanding import (
    Contributions,
    KnowledgeBatch,
    KnowledgeDetailDraft,
    ReasoningFlow,
    ReasoningFlowDraft,
    validate_evidence,
    validate_reasoning,
)
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
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/model-pilot-v3-2026-10-30"
DATABASE = ROOT / ".work/model-evaluation.db"
SOURCE_JOB = "25750cac-d8bf-4de4-bf45-0159fc1444d9"
MODELS = {"sol": "gpt-6.1-sol", "luna": "gpt-6-luna", "terra": "gpt-6.1-terra"}
TOTAL_BUDGET_USD = 2.0
_LOCK = threading.Lock()


@dataclass
class Case:
    id: str
    paper_id: str
    scope: str
    task: str
    schema: type[BaseModel]
    instruction: str
    context: tuple[dict[str, str], ...]
    edges: dict[str, tuple[str, str]] | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "paper_id": self.paper_id,
            "scope": self.scope,
            "task": self.task,
            "input_sha256": digest(
                {
                    "context": self.context,
                    "instruction": self.instruction,
                    "schema": self.schema.model_json_schema(),
                    "edges": self.edges,
                }
            ),
            "source_ids": [s["span_id"] for s in self.context if s["span_id"] != "task-data"],
        }


def cases() -> list[Case]:
    """Freeze retrieved evidence and pre-existing flow, never Sol-generated gold."""
    source_engine = make_engine(Settings(database_path=ROOT / ".work/app.db"))
    try:
        with Session(source_engine) as session:
            job = session.get(AnalysisJob, SOURCE_JOB)
            if job is None or job.result is None:
                raise ValueError("source_analysis_missing")
            original = job.result
    finally:
        source_engine.dispose()
    spans = original["sources"]
    flow = ReasoningFlow.model_validate(original["reasoning"])
    edge_refs = {f"E{i}": (e.from_step_id, e.to_step_id) for i, e in enumerate(flow.edges)}
    result = []

    def add(aid, task, schema, instruction, sources, scope, extra=None, edges=None):
        instruction = BASE_PROMPT + instruction
        context = fit_context(
            request_schema(schema, bind_evidence=True, knowledge_edges=edges),
            instruction,
            sources,
            ROUTING[task].max_input_tokens,
            extra_span={"span_id": "task-data", "text": json.dumps(extra, ensure_ascii=False)}
            if extra is not None
            else None,
            preserve_source_order=task == "short_knowledge",
        )
        result.append(
            Case(f"{aid}-{task}", f"arxiv:{aid}", scope, task, schema, instruction, context, edges)
        )

    reasoning_prompt = REASONING_PROMPT
    extraction_prompt = (
        "Extract the research problem and at most three contributions with supplied evidence. "
        "Distinguish author's claims from demonstrated results; do not add unsupported details."
    )
    for aid in ("1706.03762", "2106.09685", "2210.03629"):
        if aid == "1706.03762":
            sources, scope = spans, "cached full-paper excerpts; development/tuned paper"
        else:
            data = json.loads(
                (ROOT / f"evaluation/reports/candidate-inventory/{aid}-result.json").read_text()
            )
            abstract = data["target"].get("abstract")
            if not abstract:
                raise ValueError("abstract_missing")
            sources = [
                {
                    "span_id": f"{aid}:frozen-abstract:c{i}",
                    "text": abstract[i : i + 400],
                    "section_id": f"{aid}:frozen-abstract",
                }
                for i in range(0, len(abstract), 400)
            ]
            scope = (
                "frozen metadata abstract only; insufficient-evidence test, not full-paper quality"
            )
        add(aid, "contribution_extraction", Contributions, extraction_prompt, sources, scope)
        add(aid, "reasoning", ReasoningFlowDraft, reasoning_prompt, sources, scope)
    knowledge_sources, knowledge_extra, edge_refs = short_knowledge_inputs(
        original["reasoning"], spans
    )
    add(
        "1706.03762",
        "short_knowledge",
        KnowledgeBatch,
        SHORT_KNOWLEDGE_PROMPT,
        knowledge_sources,
        "fixed pre-existing navigation flow; not gold annotation",
        knowledge_extra,
        edge_refs,
    )
    item = original["knowledge"]["items"][0]
    add(
        "1706.03762",
        "knowledge_detail",
        KnowledgeDetailDraft,
        DETAIL_PROMPT,
        spans,
        "fixed prerequisite; parallelism-scope regression",
        detail_context(item, original["reasoning"]),
    )
    return result


def _validate(case: Case, value: BaseModel) -> None:
    sources = {s["span_id"]: s for s in case.context if s["span_id"] != "task-data"}
    if isinstance(value, ReasoningFlowDraft):
        validate_reasoning(value.to_flow(), sources)
    elif isinstance(value, ReasoningFlow):
        validate_reasoning(value, sources)
    elif isinstance(value, Contributions):
        for item in value.contributions:
            validate_evidence(item.evidence, sources)
    elif isinstance(value, KnowledgeBatch):
        for item in value.items:
            if (item.from_step_id, item.to_step_id) not in (case.edges or {}).values():
                raise ValueError("knowledge_edge_missing")
            validate_evidence(item.evidence, sources)
    elif isinstance(value, KnowledgeDetailDraft):
        validate_evidence(value.evidence, sources)


def evaluate(phase: str) -> dict[str, Any]:
    """Serialize CLI/API processes as well as threads; crash lock requires review."""
    if phase not in MODELS:
        raise ValueError("unknown_evaluation_phase")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    active = OUTPUT / "active.lock"
    try:
        handle = active.open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "evaluation_active_or_interrupted"}
    try:
        handle.close()
        return _evaluate_once(phase)
    finally:
        active.unlink()


def _evaluate_once(phase: str) -> dict[str, Any]:
    if phase not in MODELS:
        raise ValueError("unknown_evaluation_phase")
    with _LOCK:
        suite = cases()
        plan = {
            "version": "model-pilot-v3",
            "reasoning_effort": "low",
            "budget_usd": 2.0,
            "cases": [c.manifest() for c in suite],
        }
        OUTPUT.mkdir(parents=True, exist_ok=True)
        plan_path = OUTPUT / "plan.json"
        if plan_path.exists() and json.loads(plan_path.read_text()) != plan:
            return {"status": "blocked", "reason": "input_changed_new_evaluation_required"}
        if not plan_path.exists():
            plan_path.write_text(json.dumps(plan, indent=2) + "\n")
        destination = OUTPUT / f"{phase}.json"
        if destination.exists():
            return json.loads(destination.read_text())
        if phase != "sol":
            review_path = OUTPUT / "sol-review.json"
            if not review_path.exists():
                return {"status": "blocked", "reason": "source_review_required"}
            review = json.loads(review_path.read_text())
            if review.get("reviewer_kind") not in {"human", "agent_provisional"}:
                return {"status": "blocked", "reason": "reviewer_kind_required"}
            if review.get("plan_sha256") != digest(plan):
                return {"status": "blocked", "reason": "review_input_mismatch"}
            baseline = json.loads((OUTPUT / "sol.json").read_text())
            validated = {
                row["case_id"]: row["output"]
                for row in baseline["cases"]
                if row.get("status") == "validated" and isinstance(row.get("output"), dict)
            }
            passed = {
                row["case_id"]
                for row in review.get("cases", [])
                if row.get("decision") == "pass"
                and row["case_id"] in validated
                and row.get("source_checked") is True
                and row.get("notes")
                and row.get("output_sha256") == digest(validated[row["case_id"]])
            }
            suite = [c for c in suite if c.id in passed]
            if not suite:
                return {"status": "blocked", "reason": "no_source_reviewed_passes"}
        if not os.environ.get("OPENAI_API_KEY"):
            return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
        # Fail closed across crashes/restarts instead of accidentally reissuing calls.
        destination.write_text(json.dumps({"status": "started", "cases": []}) + "\n")
        result = asyncio.run(_run(phase, suite, destination))
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result


async def _run(phase: str, suite: list[Case], destination) -> dict[str, Any]:
    settings = Settings(database_path=DATABASE, provider="openai")
    engine = make_engine(settings)
    # Isolated disposable evaluation schema, never the application DB.
    Base.metadata.create_all(engine)
    gateway = LLMGateway(engine, settings, OpenAIProvider(), evaluation_model=MODELS[phase])
    rows = []
    state: dict[str, Any] = {
        "status": "running",
        "model": MODELS[phase],
        "cases": rows,
        "quality_acceptance": "not_reviewed",
    }
    try:
        for case in suite:
            policy = gateway.routing[case.task]
            rates = RATES[policy.model]
            with transaction(engine) as session:
                previous = list(session.scalars(select(LLMExecution)))
                spent = sum(
                    e.reserved_usd
                    if e.cost_status in {"unknown", "reserved"}
                    else e.estimated_cost_usd or 0
                    for e in previous
                )
                maximum = policy.max_input_tokens * rates[0] + policy.max_output_tokens * rates[1]
                if spent + maximum * 3 > TOTAL_BUDGET_USD:
                    return {"status": "stopped", "reason": "budget_guard", "cases": rows}
                if session.get(Paper, case.paper_id) is None:
                    session.add(Paper(id=case.paper_id, external_ids={}))
                    session.flush()
                job = AnalysisJob(
                    paper_id=case.paper_id,
                    arxiv_revision="frozen-pilot-v1",
                    fingerprint=digest({"case": case.manifest(), "phase": phase}),
                    owner="model-evaluator",
                    lease_until=time.time() + 600,
                    status="building_reasoning",
                )
                session.add(job)
                session.flush()
                stage = StageExecution(
                    job_id=job.id, name=case.task, input_hash=case.manifest()["input_sha256"]
                )
                session.add(stage)
                session.flush()
                jid, sid = job.id, stage.id
            started = time.monotonic()
            row: dict[str, Any]
            try:
                value = await gateway.execute(
                    job_id=jid,
                    stage_id=sid,
                    logical_call_id=case.id,
                    task_type=case.task,
                    output_schema=case.schema,
                    owner="model-evaluator",
                    context_spans=case.context,
                    instructions=case.instruction,
                    bind_evidence=True,
                    knowledge_edges=case.edges,
                    validate_output=lambda value, case=case: _validate(case, value),
                )
                row = {"case_id": case.id, "status": "validated", "output": value.model_dump()}
            except Exception as exc:
                row = {"case_id": case.id, "status": "failed", "error_type": type(exc).__name__}
            with transaction(engine) as session:
                job = session.get(AnalysisJob, jid)
                assert job is not None
                job.status = "completed" if row["status"] == "validated" else "failed"
                job.owner = None
                executions = list(
                    session.scalars(select(LLMExecution).where(LLMExecution.job_id == jid))
                )
                row["attempts"] = [
                    {
                        "model": e.metadata_json.get("returned_model"),
                        "input_tokens": e.metadata_json.get("input_tokens"),
                        "output_tokens": e.metadata_json.get("output_tokens"),
                        "estimated_usd": e.estimated_cost_usd,
                        "cost_status": e.cost_status,
                        "error_code": e.error_code,
                    }
                    for e in executions
                ]
            row["elapsed_seconds"] = time.monotonic() - started
            rows.append(row)
            state = {
                "status": "running",
                "model": MODELS[phase],
                "cases": rows,
                "quality_acceptance": "not_reviewed",
            }
            destination.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            if row["status"] == "failed":
                return {**state, "status": "stopped"}
        return {**state, "status": "completed"}
    finally:
        engine.dispose()
