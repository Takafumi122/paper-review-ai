"""One frozen, agent-curated real-paper merge diagnostic. Never calls S2."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path

from paper_review.application import merge_guard_verification as previous
from paper_review.application.contribution_review import audit_contributions
from paper_review.application.full_analysis import BASE_PROMPT
from paper_review.application.lineage_merge_review import review_lineage_merge
from paper_review.config import ROOT, Settings
from paper_review.domain.models import CandidatePaper, Paper
from paper_review.domain.understanding import (
    Contributions,
    LineageProposalDraft,
    validate_evidence,
    validate_lineage,
)
from paper_review.infrastructure.database import models as db
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.gateway import LLMGateway
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING
from paper_review.worker import Worker

OUTPUT = ROOT / "evaluation/reports/real-merge-and-s2-prep-2026-11-03"
DATABASE = ROOT / ".work/real-merge-2026-11-03.db"
VERSION = "real-merge-2026-11-03"


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def inputs():
    value = json.loads((OUTPUT / "real-paper-input.json").read_text())
    sources = {s["span_id"]: s for s in value["sources"]}
    # Independently check each selected substring against persisted original sections.
    with sqlite3.connect(f"file:{ROOT / '.work/app.db'}?mode=ro", uri=True) as conn:
        for s in sources.values():
            row = conn.execute("select text from paper_sections where id=?", (s["section_id"],))
            original = row.fetchone()[0]
            if (hashlib.sha256(original.encode()).hexdigest() != s["section_sha256"]
                    or original[int(s["start_offset"]):int(s["end_offset"])] != s["text"]):
                raise ValueError("real_source_changed")
    target = Paper.model_validate(value["target"])
    candidates = {c["paper_id"]: CandidatePaper.model_validate(c) for c in value["candidates"]}
    contributions = Contributions.model_validate(value["contributions"])
    for evidence in [contributions.problem_evidence,
                     *(c.evidence for c in contributions.contributions)]:
        validate_evidence(evidence, sources)
    proposal = LineageProposalDraft.model_validate(value["proposal"])
    edges = validate_lineage(proposal, target, candidates, sources, contributions=contributions)
    return contributions, proposal, edges


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.DATABASE}?mode=ro", uri=True) as conn:
        return previous.prior_cost() + sum(
            r if s in {"reserved", "unknown"} else e or 0
            for s, r, e in conn.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def plan() -> dict:
    inputs()
    tasks = ("contribution_review", "lineage_merge_review")
    reserve = sum(ROUTING[t].max_input_tokens * RATES[ROUTING[t].model][0]
                  + ROUTING[t].max_output_tokens * RATES[ROUTING[t].model][1] for t in tasks) * 3
    if prior_cost() + reserve > 1.6:
        raise ValueError("insufficient_shared_budget")
    paths = [OUTPUT / "real-paper-input.json", ROOT / ".work/s2-http-gate.json",
             ROOT / ".work/s2-rate-limited.json"]
    paths += [ROOT / "src/paper_review" / p for p in (
        "application/real_merge_verification.py", "application/roadmap_verification.py",
        "application/contribution_review.py", "application/lineage_merge_review.py",
        "domain/understanding.py", "llm/gateway.py", "llm/provider.py",
    )]
    return {"version": VERSION, "max_logical_calls": 2, "max_attempts": 6,
            "max_new_reservation_usd": reserve, "prior_used_or_reserved_usd": prior_cost(),
            "cumulative_ceiling_usd": 1.6, "new_s2_calls": 0,
            "new_graph_generation_calls": 0, "new_relevance_calls": 0,
            "production_flags_changed": False, "independent_gold": False,
            "sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in paths}}


async def run() -> dict:
    contributions, proposal, edges = inputs()
    return await review_case(
        contributions, proposal, edges, database=DATABASE, version=VERSION,
        budget_usd=plan()["max_new_reservation_usd"], output=OUTPUT,
    )


async def review_case(
    contributions: Contributions, proposal: LineageProposalDraft, edges: list[dict], *,
    database: Path, version: str, budget_usd: float, output: Path,
    accepted_contribution_review: dict | None = None,
) -> dict:
    """Shared bounded review core; a valid negative verdict is never regenerated."""
    assert proposal.merge_support is not None
    output.mkdir(parents=True, exist_ok=True)

    def save(name: str, value: dict) -> None:
        (output / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    config = Settings(database_path=database, provider="openai", initial_budget_usd=budget_usd)
    engine = make_engine(config)
    db.Base.metadata.create_all(engine)
    worker = Worker(engine, config, gateway=LLMGateway(engine, config, OpenAIProvider()))
    worker.gateway.pipeline_version = version
    jid = sid = None
    try:
        with transaction(engine) as session:
            pid = proposal.merge_support.node_id
            if session.get(db.Paper, pid) is None:
                session.add(db.Paper(id=pid, external_ids={}))
                session.flush()
            job = db.AnalysisJob(paper_id=pid, arxiv_revision=version, fingerprint=version,
                                 status="building_lineage", owner=worker.owner,
                                 lease_until=time.time() + 600)
            session.add(job)
            session.flush()
            stage = db.StageExecution(job_id=job.id, name="building_lineage", input_hash=version)
            session.add(stage)
            session.flush()
            jid, sid = job.id, stage.id

        async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None,
                       **kwargs):
            assert task in {"contribution_review", "lineage_merge_review"}
            if task == "contribution_review" and accepted_contribution_review is not None:
                value = schema.model_validate(accepted_contribution_review)
                assert validator is not None
                validator(value)
                return value
            return await worker.gateway.execute(
                job_id=jid, stage_id=stage_id, logical_call_id=f"{version}-{task}",
                task_type=task, output_schema=schema, owner=worker.owner,
                context_spans=(*spans, {"span_id": "task-data",
                                       "text": json.dumps(extra or {}, ensure_ascii=False)}),
                instructions=BASE_PROMPT + instruction, validate_output=validator,
                bind_evidence=False,
            )

        review = await audit_contributions(contributions, sid, call)
        save("contribution-review.json", review.model_dump())
        if not review.supported:
            with transaction(engine) as session:
                saved = session.get(db.AnalysisJob, jid)
                saved_stage = session.get(db.StageExecution, sid)
                assert saved is not None and saved_stage is not None
                saved.status = saved_stage.status = "failed"
                saved.owner = None
                saved_stage.finished_at = time.time()
            return {"status": "stopped", "reason": "contribution_review_not_supported"}
        verdict, bindings = await review_lineage_merge(proposal, contributions, sid, call)
        save("merge-review.json", dict(**verdict.model_dump(), supported=verdict.supported,
                                       evidence_bindings=bindings))
        result = {"status": "completed", "merge_supported": verdict.supported,
                  "scope": "agent_curated_real_source_diagnostic_only",
                  "edges": edges if verdict.supported else [],
                  "production_flags_changed": False, "new_s2_calls": 0}
        with transaction(engine) as session:
            saved = session.get(db.AnalysisJob, jid)
            saved_stage = session.get(db.StageExecution, sid)
            assert saved is not None and saved_stage is not None
            saved.status = saved_stage.status = "completed"
            saved.owner = None
            saved_stage.checkpoint = result
            saved_stage.finished_at = time.time()
        return result
    except Exception:
        if jid is not None and sid is not None:
            with transaction(engine) as session:
                saved = session.get(db.AnalysisJob, jid)
                saved_stage = session.get(db.StageExecution, sid)
                assert saved is not None and saved_stage is not None
                saved.status = saved_stage.status = "failed"
                saved.owner = None
                saved_stage.finished_at = time.time()
        raise
    finally:
        engine.dispose()


def evaluate(budget_cents: int) -> dict:
    if budget_cents != 160:
        return {"status": "blocked", "reason": "explicit_160_cent_authorization_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        value = json.loads(destination.read_text())
        return value if value["status"] == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review",
        }
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        write("results.json", {"status": "started"})
        try:
            value = asyncio.run(run())
        except Exception as exc:
            value = {"status": "stopped", "reason": "runner_failed_review_reservations",
                     "error_type": type(exc).__name__}
        write("results.json", value)
        return value
