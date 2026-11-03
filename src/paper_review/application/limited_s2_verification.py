"""Two authenticated S2 requests, then scoped LoRA validation only after success."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time

from paper_review.application import canonical_merge_verification as previous
from paper_review.application import lora_stage2_verification as lora
from paper_review.application import roadmap_verification as roadmap
from paper_review.application.limited_s2_gate import LimitedS2Gate
from paper_review.application.provider_verification import CaptureTransport
from paper_review.application.stage1_shortlist import Stage1Analysis, apply_context_classifications
from paper_review.config import ROOT
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SemanticScholarClient,
    SemanticScholarError,
)
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/limited-s2-lora-2026-11-03"
DATABASE = ROOT / ".work/limited-s2-lora-2026-11-03.db"
FROZEN_INPUTS = ROOT / ".work/limited-s2-lora-inputs-2026-11-03.json"
GATE_PATH = ROOT / ".work/s2-http-gate.json"
HISTORICAL_STOP = ROOT / ".work/s2-rate-limited.json"
VERSION = "limited-s2-lora-2026-11-03"
IDS = ["ARXIV:2106.09685", "ARXIV:1706.03762"]


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prior_cost() -> float:
    return previous.prior_cost() + previous.used_here()


def prepare_inputs() -> None:
    """Freeze offline replay once; never present its execution time as a fresh retrieval."""
    if FROZEN_INPUTS.exists():
        return
    case, analysis, checkpoint = asyncio.run(lora.inputs())
    fixture_hash = hashlib.sha256(case["fixture"].read_bytes()).hexdigest()
    objects = [analysis.target, *analysis.inventory.candidates,
               *(r.candidate for r in analysis.ranking.ranked_candidates),
               *(r.candidate for r in analysis.ranking.unranked_candidates)]
    for paper in objects:
        paper.embedding_provenance.update({
            "data_origin": "cached_provider_fixture_replayed_offline",
            "attempted_at_semantics": "offline replay time, not provider retrieval time",
            "provider_retrieved_at": "unknown", "fixture_sha256": fixture_hash,
        })
    with FROZEN_INPUTS.open("x") as saved:
        json.dump({"case": {"aid": case["aid"], "spans": case["spans"]},
                   "analysis": analysis.model_dump(), "checkpoint": checkpoint}, saved)


def inputs():
    value = json.loads(FROZEN_INPUTS.read_text())
    return value["case"], Stage1Analysis.model_validate(value["analysis"]), value["checkpoint"]


def plan() -> dict:
    contract = json.loads((ROOT / ".work/live-specter2-contract.json").read_text())
    if not contract.get("contract_verified") or contract.get("status") != 200:
        raise ValueError("verified_contract_required")
    if json.loads(GATE_PATH.read_text()).get("stopped") is not True:
        raise ValueError("reviewed_stopped_state_required")
    tasks = ["contribution_relevance", "lineage", "lineage_merge_review"]
    reserve = sum(ROUTING[t].max_input_tokens * RATES[ROUTING[t].model][0]
                  + ROUTING[t].max_output_tokens * RATES[ROUTING[t].model][1] for t in tasks) * 3
    if prior_cost() + reserve > 1.6:
        raise ValueError("insufficient_shared_budget")
    paths = [GATE_PATH, HISTORICAL_STOP, FROZEN_INPUTS,
             ROOT / ".work/live-specter2-contract.json"]
    paths += [lora.OUTPUT / n for n in ("relevance-results.json", "relevance-review.json")]
    paths += [ROOT / "src/paper_review" / n for n in (
        "application/limited_s2_verification.py", "application/limited_s2_gate.py",
        "application/provider_verification.py", "application/roadmap_verification.py",
        "application/full_analysis.py", "application/lineage_merge_review.py",
        "infrastructure/academic_api/semantic_scholar.py",
        "infrastructure/academic_api/s2_request_gate.py", "llm/provider.py", "llm/gateway.py",
    )]
    _, analysis, _ = inputs()
    return {"version": VERSION, "max_http_requests": 2, "attempts_per_request": 1,
            "max_http_retries": 0, "metadata_fields": "paperId,title",
            "embedding_fields": "paperId,title,embedding.specter_v2", "embedding_ids": IDS,
            "interval_seconds": 1.05, "cumulative_ceiling_usd": 1.6,
            "prior_used_or_reserved_usd": prior_cost(), "max_new_llm_reservation_usd": reserve,
            "max_new_llm_calls": 3, "max_new_llm_attempts": 9,
            "scope": "small live connection probe; LoRA ranking/graph with cached candidates",
            "authorization": "user requested limited S2 reconnect then LoRA on success",
            "production_flags_changed": False, "other_key_consumers_verified_idle": False,
            "expected_lora_s2_id": analysis.inventory.target_s2_id,
            "sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in paths}}


async def probe(p: dict) -> tuple[dict, dict | None]:
    gate = LimitedS2Gate(
        GATE_PATH, expected_sha256=p["sha256"][str(GATE_PATH.relative_to(ROOT))],
        archive=OUTPUT / "shared-stop-before.json", window_id=VERSION,
    )
    requests = []
    metadata = None
    papers = None
    for step in ("metadata", "embedding"):
        capture = CaptureTransport()
        error = None
        try:
            async with SemanticScholarClient(
                transport=capture, request_gate=gate, max_attempts=1, max_http_attempts=1,
            ) as client:
                if not client.api_key:
                    return {"status": "blocked", "reason": "s2_key_missing",
                            "http_requests": 0, "requests": requests}, None
                if step == "metadata":
                    metadata = await client.get_paper(IDS[0], fields=p["metadata_fields"])
                else:
                    papers = await client.get_papers(IDS, fields=p["embedding_fields"])
        except (SemanticScholarError, OSError, ValueError) as exc:
            error = type(exc).__name__
        requests.append({"step": step, "http_attempts": capture.attempts,
                         "diagnostics": capture.diagnostics,
                         "x_api_key_used": capture.x_api_key_used, "error_type": error})
        summary = {"status": "started", "http_requests": sum(r["http_attempts"] for r in requests),
                   "requests": requests}
        write("connection-results.json", summary)
        if error or capture.status != 200:
            return {**summary, "status": "stopped", "reason": "s2_request_failed_no_retry",
                    "http_status": capture.status}, None
    assert metadata is not None and papers is not None
    contract = json.loads((ROOT / ".work/live-specter2-contract.json").read_text())
    valid = True
    for pid in IDS:
        paper = papers[pid]
        if (paper is None or paper.embedding is None
                or len(paper.embedding) != contract["dimension"]
                or paper.embedding_response_model != contract["provider_model"]):
            valid = False
    target = papers[IDS[0]]
    attention = papers[IDS[1]]
    if (not valid or target is None or attention is None
            or target.paper_id != metadata.paper_id
            or (p["expected_lora_s2_id"] is not None
                and metadata.paper_id != p["expected_lora_s2_id"])
            or attention.paper_id != contract["paper_id"]):
        return {**summary, "status": "stopped", "reason": "embedding_contract_unverified"}, None
    normalized = {pid: paper.model_dump() for pid, paper in papers.items() if paper is not None}
    write("embedding-papers.json", normalized)
    return {**summary, "status": "completed", "reason": "small_contract_verified",
            "http_status": 200}, normalized


async def lineage(papers: dict) -> dict:
    case, analysis, checkpoint = inputs()
    new = papers[IDS[0]]
    # Only the live Target vector is fresh. Preserve cached candidate provenance.
    target = analysis.target.model_copy(update={
        "embedding": new["embedding"], "embedding_model": new["embedding_model"],
        "embedding_provenance": {
            "source": "https://api.semanticscholar.org/graph/v1/paper/batch",
            "requested_field": "embedding.specter_v2", "status": "available",
            "retrieved_at": str(time.time()), "verification_window": VERSION,
        },
    })
    updated = apply_context_classifications(
        analysis.model_copy(update={"target": target}), analysis.citation_context_audit,
    )
    old_ids = [r.candidate.paper_id for r in analysis.ranking.top_k()]
    new_ids = [r.candidate.paper_id for r in updated.ranking.top_k()]
    same_candidates = set(old_ids) == set(new_ids)
    write("ranking-comparison.json", {
        "selection_policy_version": updated.ranking.selection_policy_version,
        "old_top10": old_ids, "new_top10": new_ids,
        "cached_candidate_inventory_sha256": lora.digest(analysis.inventory.model_dump()),
        "updated_candidate_inventory_sha256": lora.digest(updated.inventory.model_dump()),
        "scope": "live target embedding with cached candidates, citations and full text",
        "before": analysis.ranking.model_dump(), "after": updated.ranking.model_dump(),
        "reuse_relevance": same_candidates,
    })
    return await roadmap._lineage(
        [case], database=DATABASE, prior_usage=prior_cost, version=VERSION,
        total_budget_usd=1.6, analysis_override=updated, accepted_contributions=checkpoint,
        accepted_relevance_selection=lora.relevance_selection() if same_candidates else None,
    )


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
    if not (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY")):
        return {"status": "blocked", "reason": "s2_key_missing"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "openai_key_missing_for_success_path"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    p = json.loads(prepared.read_text())
    with lock:
        write("results.json", {"status": "started"})
        (OUTPUT / "historical-stop.json").write_bytes(HISTORICAL_STOP.read_bytes())
        try:
            connection, papers = asyncio.run(probe(p))
            write("connection-results.json", connection)
            if connection["status"] != "completed" or papers is None:
                result = connection
            else:
                graph = asyncio.run(lineage(papers))
                write("lineage-results.json", graph)
                result = {"status": graph["status"], "reason": "scoped_lineage_diagnostic",
                          "http_requests": connection["http_requests"],
                          "http_status": 200, "production_flags_changed": False}
        except Exception as exc:
            result = {"status": "stopped", "reason": "runner_failed_review_reserved_window",
                      "error_type": type(exc).__name__}
        write("results.json", result)
        return result
