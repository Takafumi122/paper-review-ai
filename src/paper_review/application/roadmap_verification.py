"""Bounded operator bundle: source/ranking audit, S2 window, then scoped Lineage diagnostic.

All production readiness settings and prior stop records remain untouched.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.application import citation_quality_evaluation as base
from paper_review.application import citation_quality_recovery as previous
from paper_review.application import stage1_verification as live_s2
from paper_review.application.citation_contexts import (
    CITATION_PROMPT,
    CitationBatch,
    context_batches,
    prepare_contexts,
    request_context,
)
from paper_review.application.full_analysis import (
    BASE_PROMPT,
    fit_context,
    generate_lineage,
    generate_reviewed_contributions,
)
from paper_review.application.stage1_shortlist import (
    Stage1Analysis,
    apply_context_classifications,
    build_stage1_shortlist,
)
from paper_review.config import ROOT, Settings
from paper_review.domain.models import CandidateSet, Paper
from paper_review.domain.stage1_scoring import SCORING_VERSION
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.infrastructure.database import models as db
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.gateway import ExecutionFailure, LLMGateway, request_schema
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING
from paper_review.worker import Worker

OUTPUT = ROOT / "evaluation/reports/roadmap-1-3-v21-2026-11-02"
DATABASE = ROOT / ".work/roadmap-1-3-v21.db"
CEILING = 0.50


def run_operator(phase: str, budget_cents: int = 15) -> dict:
    """Fresh fixed runner inherits the service environment; no keys in arguments/output.

    Evaluator fixes can be loaded without repeatedly restarting the user's keyed
    API/Worker. Production processes and readiness settings are not hot-reloaded.
    """
    if phase not in {"s2", "classify", "lineage", "reconnect"}:
        return {"status": "blocked", "reason": "unknown_phase"}
    if phase == "reconnect" and budget_cents != 160:
        return {"status": "blocked", "reason": "explicit_bundle_budget_required"}
    if budget_cents != 50 and not (phase in {"lineage", "reconnect"} and budget_cents == 160):
        return {"status": "blocked", "reason": "explicit_bundle_budget_required"}
    try:
        process = subprocess.run(
            [sys.executable, "-m", "paper_review.application.roadmap_verification",
             phase, "--budget-cents", str(budget_cents)],
            cwd=ROOT, capture_output=True, text=True, timeout=1200, check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "stopped", "reason": "runner_timeout_review_reservations"}
    if process.returncode != 0:
        return {"status": "stopped", "reason": "runner_failed"}
    try:
        summary = json.loads(process.stdout.splitlines()[-1])
        return {key: summary.get(key) for key in (
            "status", "reason", "http_status", "cases_finished",
        )}
    except (ValueError, IndexError, AttributeError):
        return {"status": "stopped", "reason": "runner_summary_invalid"}


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.EXPANDED_DATABASE}?mode=ro", uri=True) as connection:
        return previous.expanded_prior_cost() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def plan(cases: list[dict]) -> dict:
    rows = []
    for case in cases:
        contexts = prepare_contexts(CandidateSet.model_validate(case["inventory"]), case["spans"])
        batches = context_batches([c for c in contexts if c["support_options"]])
        rows.append({
            "aid": case["aid"], "input_sha256": base.digest({
                k: v for k, v in case.items() if k != "fixture"
            }), "fixture_sha256": hashlib.sha256(case["fixture"].read_bytes()).hexdigest(),
            "batches_sha256": base.digest([request_context(b) for b in batches]),
            "contexts_found": len(contexts), "planned_contexts": sum(map(len, batches)),
            "logical_calls": len(batches),
        })
    return {
        "version": "roadmap-1-3-v21", "pipeline_version": "full-v21",
        "scoring_version": SCORING_VERSION, "proposed_total_budget_usd": CEILING,
        "prior_reserved_or_used_usd": prior_cost(), "cases": rows,
        "prompt_sha256": base.digest(CITATION_PROMPT),
        "schema_sha256": base.digest(CitationBatch.model_json_schema()),
        "max_classification_logical_calls": 8, "max_lineage_logical_calls": 3,
        "max_llm_http_attempts": 33, "s2_targets": list(live_s2.TARGETS),
        "max_s2_http_attempts": 2, "s2_stop_on_first_failure": True,
        "lineage_target": "2106.09685", "production_gates_changed": False,
        "lineage_requires_hash_bound_source_review": True,
        "source_code_sha256": {
            name: hashlib.sha256((ROOT / "src/paper_review" / name).read_bytes()).hexdigest()
            for name in ("application/roadmap_verification.py", "application/citation_contexts.py",
                         "domain/citation_identity.py", "domain/stage1_scoring.py",
                         "domain/understanding.py")
        },
        "lineage_core_sha256": hashlib.sha256(
            Path(__file__).with_name("full_analysis.py").read_bytes()
        ).hexdigest(),
        "task_policies": {task: vars(ROUTING[task]) for task in (
            "citation_classification", "contribution_extraction",
            "contribution_relevance", "lineage",
        )},
        "configured_usd_per_token": {
            model: list(RATES[model]) for model in {ROUTING[t].model for t in (
                "citation_classification", "contribution_extraction",
                "contribution_relevance", "lineage",
            )}
        },
    }


def evaluate(phase: str, budget_cents: int = 15) -> dict:
    if phase not in {"s2", "classify", "lineage"}:
        return {"status": "blocked", "reason": "unknown_phase"}
    if budget_cents != 50:
        return {"status": "blocked", "reason": "explicit_bundle_budget_required"}
    destination = OUTPUT / f"{phase}-results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review"
        }
    cases = base.load_cases()
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan(cases):
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if phase == "lineage":
        baseline, review, connection = (
            OUTPUT / "classify-results.json", OUTPUT / "stage1-review.json",
            OUTPUT / "s2-results.json",
        )
        if not all(p.exists() for p in (baseline, review, connection)):
            return {"status": "blocked", "reason": "scoped_stage1_and_live_s2_review_required"}
        assessment = json.loads(review.read_text())
        s2 = json.loads(connection.read_text())
        ready = any(
            r.get("target") == "2106.09685" and r.get("gate") in {
                "READY FOR QUALITY AUDIT", "QUALITY AUDIT RESULT",
            }
            for r in s2.get("reports", [])
        )
        if not ready or not (
            assessment.get("accepted_for_lineage_diagnostic") is True
            and assessment.get("classification_sha256")
            == hashlib.sha256(baseline.read_bytes()).hexdigest()
            and json.loads(baseline.read_text()).get("status") == "completed"
        ):
            return {"status": "blocked", "reason": "scoped_stage1_and_live_s2_review_required"}
    if not os.environ.get("OPENAI_API_KEY") and phase != "s2":
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    try:
        lock = (OUTPUT / f"{phase}-active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        destination.write_text('{"status":"started"}\n')
        try:
            with (OUTPUT / f"{phase}-plan.json").open("x") as phase_plan:
                phase_plan.write(json.dumps({
                    **plan(cases), "phase": phase, "authorized_total_budget_usd": CEILING,
                    "operator_budget_cents": budget_cents,
                }, indent=2) + "\n")
            if phase == "s2":
                if live_s2.RUNTIME_LIMIT.exists():
                    (OUTPUT / "prior-runtime-stop.json").write_bytes(
                        live_s2.RUNTIME_LIMIT.read_bytes()
                    )
                result = asyncio.run(live_s2._evaluate(output=OUTPUT))
            elif phase == "classify":
                result = asyncio.run(base._run(
                    cases, destination, "sol", database=DATABASE, prior_cost=prior_cost,
                    total_budget_usd=CEILING, control_ids={},
                ))
            else:
                result = asyncio.run(_lineage(cases))
        except Exception as exc:
            # Never persist provider exception text, source bodies, or raw responses.
            result = {"status": "stopped", "reason": (
                str(exc) if isinstance(exc, ExecutionFailure) else type(exc).__name__
            )}
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result


async def replay(case: dict):
    payload = json.loads(gzip.decompress(case["fixture"].read_bytes()))
    async with SemanticScholarClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
        api_key="offline-replay", rate_limiter=RateLimiter(0), max_attempts=1,
    ) as client:
        return await build_stage1_shortlist(
            Paper.model_validate(case["target"]), CandidateSet.model_validate(case["inventory"]),
            client, selection_policy="observed-renormalized-v1",
        )


async def _lineage(
    cases: list[dict], *, input_directory: Path | None = None,
    database: Path | None = None, prior_usage: Callable[[], float] | None = None,
    version: str = "roadmap-lineage-v21",
    target_aid: str = "2106.09685",
    contribution_only: bool = False,
    evaluation_model: str | None = None,
    accepted_contributions: dict | None = None,
    relevance_only: bool = False,
    frozen_relevance_source_ids: list[str] | None = None,
    total_budget_usd: float | None = None,
    analysis_override: Stage1Analysis | None = None,
    accepted_relevance_selection: dict | None = None,
    accepted_graph_proposal: dict | None = None,
) -> dict:
    ceiling = CEILING if total_budget_usd is None else total_budget_usd
    input_directory = OUTPUT if input_directory is None else input_directory
    database = DATABASE if database is None else database
    prior_usage = prior_cost if prior_usage is None else prior_usage
    case = next(c for c in cases if c["aid"] == target_aid)
    if analysis_override is None:
        classification = json.loads((input_directory / "classify-results.json").read_text())
        audit = next(c["audit"] for c in classification["cases"] if c["aid"] == case["aid"])
        analysis = apply_context_classifications(await replay(case), audit)
    else:
        analysis = analysis_override
    engine = make_engine(Settings(database_path=database))
    db.Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            used = prior_usage() + sum(
                e.reserved_usd if e.cost_status in {"reserved", "unknown"}
                else e.estimated_cost_usd or 0
                for e in session.scalars(select(db.LLMExecution))
            )
        if used >= ceiling:
            return {"status": "stopped", "reason": "shared_budget"}
        config = Settings(
            database_path=database, provider="openai", initial_budget_usd=ceiling-used,
        )
        worker = Worker(engine, config, gateway=LLMGateway(
            engine, config, OpenAIProvider(), evaluation_model=evaluation_model,
        ))
        worker.gateway.pipeline_version = version
        with transaction(engine) as session:
            if session.get(db.Paper, analysis.target.id) is None:
                session.add(db.Paper(id=analysis.target.id, external_ids={}))
                session.flush()
            job = db.AnalysisJob(
                paper_id=analysis.target.id, arxiv_revision=version,
                fingerprint=version, status="building_lineage",
                owner=worker.owner, lease_until=time.time() + 600,
            )
            session.add(job)
            session.flush()
            stage = db.StageExecution(
                job_id=job.id, name="building_lineage", input_hash=job.fingerprint,
            )
            session.add(stage)
            session.flush()
            jid, sid = job.id, stage.id

        async def call(
            schema, stage_id, task, instruction, spans, extra=None, validator=None,
            preserve_source_order=False,
            bind_evidence=True, require_all_sources=False,
        ):
            if accepted_graph_proposal is not None and task == "lineage":
                value = schema.model_validate(accepted_graph_proposal)
                assert validator is not None
                validator(value)
                return value
            if accepted_relevance_selection is not None and task == "contribution_relevance":
                value = schema.model_validate(accepted_relevance_selection)
                assert validator is not None
                validator(value)
                return value
            if accepted_contributions is not None and task in {
                "contribution_extraction", "contribution_review",
            }:
                key = "contributions" if task == "contribution_extraction" else task
                value = schema.model_validate(accepted_contributions[key])
                assert validator is not None
                # Re-run original-source, item-coverage and selected-evidence checks.
                # The frozen accepted semantic verdict is reused, never regenerated.
                validator(value)
                return value
            if task == "contribution_relevance" and frozen_relevance_source_ids is not None:
                originals = {s["span_id"]: s for s in spans}
                if (len(set(frozen_relevance_source_ids)) != len(frozen_relevance_source_ids)
                        or any(sid not in originals for sid in frozen_relevance_source_ids)):
                    raise ValueError("frozen_relevance_sources_mismatch")
                spans = [originals[sid] for sid in frozen_relevance_source_ids]
                require_all_sources = True
            extra_span = {
                "span_id": "task-data", "text": json.dumps(extra or {}, ensure_ascii=False),
            }
            context = (*spans, extra_span) if require_all_sources else fit_context(
                request_schema(schema, bind_evidence=bind_evidence), BASE_PROMPT + instruction,
                spans, ROUTING[task].max_input_tokens,
                extra_span=extra_span,
                preserve_source_order=preserve_source_order,
            )
            return await worker.gateway.execute(
                job_id=jid, stage_id=stage_id, logical_call_id=f"{version}-{task}",
                task_type=task, output_schema=schema, owner=worker.owner,
                context_spans=context, instructions=BASE_PROMPT + instruction,
                validate_output=validator, bind_evidence=bind_evidence,
            )

        result = None
        try:
            if contribution_only:
                value, review = await generate_reviewed_contributions(case["spans"], sid, call)
                result = {"status": "completed" if review.supported else "insufficient_evidence",
                          "contributions": value.model_dump(),
                          "contribution_review": review.model_dump()}
            else:
                result = await generate_lineage(
                    analysis.target, analysis, case["spans"], sid, call,
                    adjustment_policy="identity-v1",
                    relevance_only=relevance_only,
                )
        finally:
            with transaction(engine) as session:
                saved_job = session.get(db.AnalysisJob, jid)
                saved_stage = session.get(db.StageExecution, sid)
                assert saved_job is not None and saved_stage is not None
                saved_job.status = "completed" if result is not None else "failed"
                saved_job.owner = None
                saved_stage.status = saved_job.status
                saved_stage.checkpoint = result
                saved_stage.finished_at = time.time()
        return {"status": "completed", "scope": "reviewed_diagnostic_only", "lineage": result,
                "production_gates_changed": False,
                "reused_reviewed_contributions": accepted_contributions is not None}
    finally:
        engine.dispose()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fixed local verification phases; no key arguments",
    )
    parser.add_argument("phase", choices=["s2", "classify", "lineage", "reconnect"])
    parser.add_argument("--budget-cents", type=int, choices=[15, 50, 160], default=15)
    args = parser.parse_args()
    if args.phase == "reconnect":
        from paper_review.application.limited_s2_verification import evaluate as reconnect

        value = reconnect(args.budget_cents)
    elif args.phase == "lineage":
        from paper_review.application.cached_lora_verification import (
            evaluate as evaluate_followup,
        )

        value = evaluate_followup(args.budget_cents)
    else:
        value = evaluate(args.phase, args.budget_cents)
    print(json.dumps({"status": value["status"], "reason": value.get("reason"),
                      "http_status": value.get("http_status"),
                      "cases_finished": len(value.get("cases", []))}))
