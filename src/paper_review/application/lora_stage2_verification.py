"""Two bounded phases separated by a source-bound agent review; no runtime readiness changes."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import sqlite3

import httpx

from paper_review.application import identity_selection_verification as accepted
from paper_review.application import page_boundary_verification as previous
from paper_review.application import roadmap_verification as parent
from paper_review.application.full_analysis import BASE_PROMPT, fit_context, generate_lineage
from paper_review.application.stage1_shortlist import (
    apply_context_classifications,
    build_stage1_shortlist,
)
from paper_review.config import ROOT
from paper_review.domain.models import CandidateSet, Paper
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.llm.gateway import prepare_source_request, request_schema
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING
from paper_review.llm.token_budget import estimate_input_tokens

OUTPUT = ROOT / "evaluation/reports/lora-new-top10-v33-2026-11-02"
DATABASE = ROOT / ".work/lora-new-top10-v33.db"
VERSION = "lora-new-top10-v33"


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.DATABASE}?mode=ro", uri=True) as db:
        return previous.prior_cost() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in db.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


async def inputs():
    item = next(i for i in previous.inputs() if i["case"]["aid"] == "2106.09685")
    case = item["case"]
    audit = next(c["audit"] for c in json.loads((previous.OUTPUT / "results.json").read_text())[
        "cases"
    ] if c["aid"] == case["aid"])
    payload = json.loads(gzip.decompress(case["fixture"].read_bytes()))
    async with SemanticScholarClient(
        api_key="offline-replay", rate_limiter=RateLimiter(0), max_attempts=1,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as client:
        analysis = apply_context_classifications(await build_stage1_shortlist(
            Paper.model_validate(case["target"]), CandidateSet.model_validate(case["inventory"]),
            client,
        ), audit)
    checkpoint = json.loads((accepted.OUTPUT / "results.json").read_text())["lineage"]
    return case, analysis, checkpoint


class PreviewReady(Exception):
    def __init__(self, data: dict):
        self.data = data


async def preview(case, analysis, checkpoint) -> dict:
    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None,
                   bind_evidence=True, preserve_source_order=False, **kwargs):
        if task in {"contribution_extraction", "contribution_review"}:
            value = schema.model_validate(checkpoint[
                "contributions" if task == "contribution_extraction" else task
            ])
            assert validator is not None
            validator(value)
            return value
        if task != "contribution_relevance":
            raise ValueError("unexpected_preview_task")
        context = fit_context(
            request_schema(schema, bind_evidence=bind_evidence), BASE_PROMPT + instruction,
            spans, ROUTING[task].max_input_tokens,
            extra_span={"span_id": "task-data",
                        "text": json.dumps(extra or {}, ensure_ascii=False)},
            preserve_source_order=preserve_source_order,
        )
        effective_schema, effective_context, _ = prepare_source_request(
            request_schema(schema, bind_evidence=bind_evidence), context,
        )
        selected = [s for s in context if s["span_id"] != "task-data"]
        rows = []
        for row in analysis.ranking.top_k():
            cid = row.candidate.paper_id
            direct = [s["span_id"] for s in selected
                      if cid in json.loads(s.get("identifies_candidate_ids", "[]"))]
            rows.append({"candidate_id": cid, "title": row.candidate.title, "rank": row.rank,
                         "direct_source_ids": direct,
                         "support_source_ids": [s["span_id"] for s in selected
                                                if cid in json.loads(s.get("candidate_ids", "[]"))],
                         "date": row.candidate.date_provenance.model_dump()
                         if row.candidate.date_provenance else None,
                         "stage1_score": row.score.model_dump()})
        raise PreviewReady({
            "candidates": rows, "transmitted_source_ids": [s["span_id"] for s in selected],
            "prepared_source_count": len(spans), "transmitted_source_count": len(selected),
            "input_token_estimate": estimate_input_tokens(
                effective_schema, effective_context, BASE_PROMPT + instruction,
            ),
            "request_contract_sha256": digest((
                request_schema(schema, bind_evidence=bind_evidence), instruction, context,
            )),
            "contributions_validated_against_current_sources": True,
        })

    try:
        await generate_lineage(analysis.target, analysis, case["spans"], "preview", call,
                               adjustment_policy="identity-v1", relevance_only=True)
    except PreviewReady as ready:
        return ready.data
    raise ValueError("relevance_preview_not_reached")


def plan() -> dict:
    case, analysis, checkpoint = asyncio.run(inputs())
    data = asyncio.run(preview(case, analysis, checkpoint))
    expected = json.loads((ROOT / "evaluation/reports/stage1-lower-bound-v32-2026-11-02/"
                          "results.json").read_text())
    expected_ids = next(c for c in expected["cases"] if c["aid"] == "2106.09685")["ranked"][:10]
    if ([r["candidate_id"] for r in data["candidates"]] != [r["id"] for r in expected_ids]
            or len(data["candidates"]) != 10
            or any(not r["direct_source_ids"] for r in data["candidates"])
            or any(r.score.status != "complete" for r in analysis.ranking.top_k())):
        raise ValueError("scoped_top10_source_prerequisites_failed")
    reservation = sum(
        ROUTING[t].max_input_tokens * RATES[ROUTING[t].model][0]
        + ROUTING[t].max_output_tokens * RATES[ROUTING[t].model][1]
        for t in ["contribution_relevance", "lineage"]
    ) * 3
    if prior_cost() + reservation > 1.6:
        raise ValueError("insufficient_shared_budget_for_bounded_plan")
    files = [parent.live_s2.RUNTIME_LIMIT, ROOT / ".work/s2-http-gate.json",
             accepted.OUTPUT / "results.json", previous.OUTPUT / "results.json"]
    files += [ROOT / "src/paper_review" / name for name in (
        "application/lora_stage2_verification.py", "application/roadmap_verification.py",
        "application/full_analysis.py", "application/lineage_evidence.py",
        "domain/understanding.py", "domain/stage1_scoring.py",
    )]
    return {
        "version": VERSION, "cumulative_ceiling_usd": 1.6,
        "prior_used_or_reserved_usd": prior_cost(), "max_additional_reservation_usd": reservation,
        "max_logical_calls": 2, "max_attempts": 6, "new_s2_calls": 0,
        "production_gates_changed": False, "selection_policy_version": "lower-bound-v1",
        "task_policies": {t: vars(ROUTING[t]) for t in ["contribution_relevance", "lineage"]},
        "source_preview": data,
        "original_sources_sha256": digest(case["spans"]),
        "contribution_checkpoint_sha256": digest({k: checkpoint[k] for k in (
            "contributions", "contribution_review",
        )}),
        "code_and_artifact_sha256": {str(p.relative_to(ROOT)):
                                     hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
        "graph_gate": "exact original and candidate identity; source-bound agent review of "
                      "all nonnull relevance/contribution links; at least one causal predecessor "
                      "with verified earlier date; no readiness or formal gold claim",
    }


def relevance_selection() -> dict:
    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as db:
        rows = db.execute(
            "select output from llm_executions where status='completed' "
            "and logical_call_id=?", (f"{VERSION}-relevance-contribution_relevance",),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("one_accepted_relevance_selection_required")
    return json.loads(rows[0][0])


def evaluate(budget_cents: int) -> dict:
    if budget_cents != 160:
        return {"status": "blocked", "reason": "explicit_160_cent_authorization_required"}
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    source_review = OUTPUT / "preflight-review.json"
    if not source_review.exists():
        return {"status": "blocked", "reason": "scoped_source_review_required"}
    preflight = json.loads(source_review.read_text())
    if (preflight.get("decision") != "pass_for_relevance_diagnostic"
            or preflight.get("plan_sha256") != digest(json.loads(prepared.read_text()))):
        return {"status": "blocked", "reason": "scoped_source_review_stale_or_rejected"}
    phase = "graph" if (OUTPUT / "relevance-results.json").exists() else "relevance"
    if phase == "graph":
        result = json.loads((OUTPUT / "relevance-results.json").read_text())
        if result.get("status") != "completed":
            return {"status": "blocked", "reason": "previous_window_requires_review"}
        review_path = OUTPUT / "relevance-review.json"
        if not review_path.exists():
            return {"status": "awaiting_source_review", "reason": "graph_not_authorized_by_review"}
        review = json.loads(review_path.read_text())
        if (review.get("decision") != "pass_for_scoped_graph_diagnostic"
                or review.get("relevance_sha256") != digest(result)
                or review.get("selection_sha256") != digest(relevance_selection())
                or review.get("plan_sha256") != digest(json.loads(prepared.read_text()))):
            return {"status": "blocked", "reason": "source_review_missing_rejected_or_stale"}
    destination = OUTPUT / f"{phase}-results.json"
    if destination.exists():
        result = json.loads(destination.read_text())
        return result if result.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review",
        }
    try:
        lock = (OUTPUT / f"{phase}.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        write(destination.name, {"status": "started"})
        case, analysis, checkpoint = asyncio.run(inputs())
        try:
            result = asyncio.run(parent._lineage(
                [case], database=DATABASE, prior_usage=prior_cost,
                version=f"{VERSION}-{phase}", total_budget_usd=1.6,
                accepted_contributions=checkpoint, analysis_override=analysis,
                relevance_only=phase == "relevance",
                accepted_relevance_selection=relevance_selection() if phase == "graph" else None,
                frozen_relevance_source_ids=plan()["source_preview"]["transmitted_source_ids"],
            ))
        except Exception as exc:
            result = {"status": "stopped", "reason": "runner_failed_review_reservations",
                      "error_type": type(exc).__name__}
        write(destination.name, result)
        return result
