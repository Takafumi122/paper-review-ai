"""One bounded classification comparison on frozen real sources and SPECTER2 fixtures."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.application.citation_contexts import (
    CITATION_PROMPT,
    CitationBatch,
    classify_contexts,
    context_batches,
    prepare_contexts,
    request_context,
)
from paper_review.application.stage1_shortlist import (
    apply_context_classifications,
    build_stage1_shortlist,
)
from paper_review.config import ROOT, Settings
from paper_review.domain.models import CandidateSet, Paper
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.infrastructure.database import models as db
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.gateway import LLMGateway, digest
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.worker import Worker

OUTPUT = ROOT / "evaluation/reports/citation-context-controls-v2-2026-11-02"
DATABASE = ROOT / ".work/citation-context-controls-v2.db"
CONTROL_IDS = {"1706.03762": {"C12", "C45"}, "2106.09685": {"C5", "C21"}}
SOURCES = (
    ("1706.03762", "0330fab2-e4f6-4beb-a856-ad03b4951b20", "stage1-live-2026-10-30"),
    ("2106.09685", "25762f7f-0d5a-438e-ab30-2c04b1ae7954", "stage1-live-2026-11-01"),
)
BUDGET = 0.10


def prior_reservation() -> float:
    """Keep the failed v2 attempt inside the same total authorized ceiling."""
    total = 0.0
    for name in ("citation-context-quality-v2.db", "citation-context-controls-v1.db"):
        path = ROOT / ".work" / name
        if not path.exists():
            continue
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
            total += sum(
                reserved if status in {"reserved", "unknown"} else estimated or 0
                for status, reserved, estimated in connection.execute(
                    "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
                )
            )
    return total


def load_cases() -> list[dict]:
    engine = make_engine(Settings(database_path=ROOT / ".work/app.db"))
    try:
        with Session(engine) as session:
            cases = []
            for aid, jid, fixture_dir in SOURCES:
                job = session.get(db.AnalysisJob, jid)
                if not job or not job.result or job.feature_status.get("reasoning") != "completed":
                    raise ValueError("source_job_missing")
                inventory = json.loads((
                    ROOT / f"evaluation/reports/candidate-inventory/{aid}-result.json"
                ).read_text())
                fixture = ROOT / f"evaluation/reports/{fixture_dir}/{aid}-embeddings.json.gz"
                cases.append({
                    "aid": aid, "source_job": jid, "target": inventory["target"],
                    "inventory": inventory["inventory"], "spans": job.result["sources"],
                    "fixture": fixture,
                })
            return cases
    finally:
        engine.dispose()


def plan(cases: list[dict]) -> dict:
    return {
        "version": "citation-context-controls-v2", "budget_usd": BUDGET,
        "prior_reserved_or_used_usd": prior_reservation(),
        "max_logical_calls": 4, "max_http_attempts": 12, "new_s2_requests": 0,
        "model_order": ["gpt-6.1-sol", "gpt-5-nano"],
        "controls": {k: sorted(v) for k, v in CONTROL_IDS.items()},
        "automatic_quality_acceptance": False,
        "cases": [{
            "aid": c["aid"], "source_job": c["source_job"],
            "input_sha256": digest({k: v for k, v in c.items() if k != "fixture"}),
            "fixture_sha256": hashlib.sha256(c["fixture"].read_bytes()).hexdigest(),
            "batches_sha256": digest([
                request_context(batch) for batch in context_batches([
                    context for context in prepare_contexts(
                        CandidateSet.model_validate(c["inventory"]), c["spans"]
                    ) if c["aid"] not in CONTROL_IDS
                    or context["context_id"] in CONTROL_IDS[c["aid"]]
                ])
            ]),
        } for c in cases],
        "prompt_sha256": digest(CITATION_PROMPT),
        "schema_sha256": digest(CitationBatch.model_json_schema()),
    }


def evaluate(phase: str = "sol") -> dict:
    if phase not in {"sol", "nano"}:
        return {"status": "blocked", "reason": "unknown_phase"}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{phase}-results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_run_requires_review"
        }
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if phase == "nano":
        baseline = OUTPUT / "sol-results.json"
        review = OUTPUT / "sol-review.json"
        if not baseline.exists() or not review.exists():
            return {"status": "blocked", "reason": "source_reviewed_sol_baseline_required"}
        assessment = json.loads(review.read_text())
        if json.loads(baseline.read_text()).get("status") != "completed" or not assessment.get(
            "accepted_for_comparison"
        ) or assessment.get(
            "sol_result_sha256"
        ) != hashlib.sha256(baseline.read_bytes()).hexdigest():
            return {"status": "blocked", "reason": "source_reviewed_sol_baseline_required"}
    try:
        reservation = (OUTPUT / f"{phase}-active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with reservation:
        cases = load_cases()
        frozen = plan(cases)
        prepared = OUTPUT / "prepared-plan.json"
        if not prepared.exists() or json.loads(prepared.read_text()) != frozen:
            return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
        (OUTPUT / "plan.json").write_text(json.dumps(frozen, indent=2) + "\n")
        destination.write_text('{"status":"started","cases":[]}\n')
        result = asyncio.run(_run(cases, destination, phase))
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result


async def _run(
    cases: list[dict], destination: Path, phase: str, *,
    database: Path | None = None, prior_cost: Callable[[], float] | None = None,
    total_budget_usd: float | None = None,
    control_ids: dict[str, set[str]] | None = None,
    fixed_context_batches: dict[str, list[list[str]]] | None = None,
    version: str = "citation-controls-v2",
) -> dict:
    ceiling = BUDGET if total_budget_usd is None else total_budget_usd
    config = Settings(
        database_path=database or DATABASE, provider="openai", initial_budget_usd=ceiling,
    )
    engine = make_engine(config)
    db.Base.metadata.create_all(engine)
    rows = []
    try:
        for case in cases:
            with Session(engine) as session:
                used = (prior_cost or prior_reservation)() + sum(
                    e.reserved_usd if e.cost_status in {"reserved", "unknown"}
                    else e.estimated_cost_usd or 0
                    for e in session.scalars(select(db.LLMExecution))
                )
            if used >= ceiling:
                return {"status": "stopped", "reason": "global_budget", "cases": rows}
            limits = config.model_copy(update={"initial_budget_usd": ceiling - used})
            worker = Worker(engine, limits, gateway=LLMGateway(
                engine, limits, OpenAIProvider(),
                evaluation_model="gpt-6.1-sol" if phase == "sol" else "gpt-5-nano",
            ))
            worker.gateway.pipeline_version = version
            target = Paper.model_validate(case["target"])
            inventory = CandidateSet.model_validate(case["inventory"])
            payload = json.loads(gzip.decompress(case["fixture"].read_bytes()))
            # Replay an existing real fixture, never acquire new S2 data or keys.
            async with SemanticScholarClient(
                transport=httpx.MockTransport(
                    lambda _, payload=payload: httpx.Response(200, json=payload)
                ),
                api_key="offline-replay", rate_limiter=RateLimiter(0), max_attempts=1,
            ) as scholar:
                baseline = await build_stage1_shortlist(target, inventory, scholar)
            with transaction(engine) as session:
                if session.get(db.Paper, target.id) is None:
                    session.add(db.Paper(id=target.id, external_ids={}))
                session.flush()
                job = db.AnalysisJob(
                    paper_id=target.id, arxiv_revision=f"{version}-{phase}",
                    fingerprint=digest((case["aid"], phase, version)), status="ranking_candidates",
                    owner=worker.owner, lease_until=time.time() + 600,
                )
                session.add(job)
                session.flush()
                stage = db.StageExecution(
                    job_id=job.id, name="ranking_candidates", input_hash=job.fingerprint
                )
                session.add(stage)
                session.flush()
                jid, sid = job.id, stage.id
            audit = await classify_contexts(
                worker, jid, sid, baseline.inventory, case["spans"],
                selected_context_ids=(CONTROL_IDS if control_ids is None else control_ids).get(
                    case["aid"]
                ),
                fixed_context_batches=(fixed_context_batches or {}).get(case["aid"]),
            )
            result = apply_context_classifications(baseline, audit)

            def ranking_summary(analysis):
                return [{
                    "id": row.candidate.paper_id, "title": row.candidate.title,
                    "rank": row.rank, "score": row.score.model_dump(),
                } for row in analysis.ranking.ranked_candidates]

            rows.append({"aid": case["aid"], "job_id": jid, "audit": audit,
                         "before": ranking_summary(baseline), "after": ranking_summary(result)})
            with transaction(engine) as session:
                job = session.get(db.AnalysisJob, jid)
                assert job is not None
                job.status = "failed" if audit["errors"] else "completed"
                job.owner = None
            status = "stopped" if audit["errors"] else "started"
            destination.write_text(json.dumps({"status": status, "cases": rows}, indent=2) + "\n")
            if audit["errors"]:
                return {"status": "stopped", "reason": "classification_unavailable", "cases": rows}
        return {"status": "completed", "cases": rows}
    finally:
        engine.dispose()
