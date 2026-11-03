"""Frozen LoRA merge regression, semantic negative control and corrected Graph diagnostic."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import lora_stage2_verification as previous
from paper_review.application import roadmap_verification as parent
from paper_review.config import ROOT
from paper_review.domain.understanding import Contributions, LineageProposalDraft, validate_lineage
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/merge-grounding-v34-2026-11-03"
DATABASE = ROOT / ".work/merge-grounding-v34.db"
VERSION = "merge-grounding-v34"


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.DATABASE}?mode=ro", uri=True) as db:
        return previous.prior_cost() + sum(
            r if s in {"reserved", "unknown"} else e or 0
            for s, r, e in db.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def control() -> dict:
    # Deliberately misassign the adapter/latency branch to C2's empirical 10,000x
    # parameter reduction. Quotes are real; the false mapping is clearly a test.
    old = json.loads((previous.OUTPUT / "graph-results.json").read_text())["lineage"]
    edges = [{k: v for k, v in e.items() if k not in {"confidence", "status"}}
             for e in old["edges"]]
    return {
        "predecessor_ids": [n["id"] for n in old["nodes"]], "edges": edges,
        "merge_reason": old["merge_reason"], "missing_reason": None,
        "merge_support": {
            "node_id": old["target_id"], "branches": [
                {"from_paper_id": e["from_paper_id"], "contribution_id": cid,
                 "evidence": e["evidence"]} for e, cid in zip(edges, ["C1", "C2"], strict=True)
            ],
            "integration_evidence": [edges[0]["evidence"][0], *edges[1]["evidence"]],
        },
    }


def plan() -> dict:
    case, analysis, checkpoint = asyncio.run(previous.inputs())
    result = json.loads((previous.OUTPUT / "relevance-results.json").read_text())
    review = json.loads((previous.OUTPUT / "relevance-review.json").read_text())
    old_plan = json.loads((previous.OUTPUT / "prepared-plan.json").read_text())
    if (review["decision"] != "pass_for_scoped_graph_diagnostic"
            or review["relevance_sha256"] != previous.digest(result)
            or review["selection_sha256"] != previous.digest(previous.relevance_selection())
            or old_plan["original_sources_sha256"] != previous.digest(case["spans"])):
        raise ValueError("frozen_relevance_review_required")
    selected_ids = [r["candidate_id"] for r in result["lineage"]["final_ranking"][:5]]
    candidates = {r.candidate.paper_id: r.candidate for r in analysis.ranking.top_k()
                  if r.candidate.paper_id in selected_ids}
    # The negative control must pass mechanical checks before testing semantics.
    validate_lineage(LineageProposalDraft.model_validate(control()), analysis.target, candidates,
                     {s["span_id"]: s for s in case["spans"]},
                     contributions=Contributions.model_validate(checkpoint["contributions"]))
    tasks = ["lineage", "lineage_merge_review", "lineage_merge_review"]
    reserve = sum(ROUTING[t].max_input_tokens * RATES[ROUTING[t].model][0]
                  + ROUTING[t].max_output_tokens * RATES[ROUTING[t].model][1] for t in tasks) * 3
    if prior_cost() + reserve > 1.6:
        raise ValueError("insufficient_shared_budget")
    files = [previous.OUTPUT / n for n in [
        "relevance-results.json", "relevance-review.json", "graph-results.json",
    ]]
    files += [ROOT / n for n in [".work/s2-http-gate.json", ".work/s2-rate-limited.json"]]
    files += [ROOT / "src/paper_review" / n for n in [
        "application/merge_guard_verification.py", "application/roadmap_verification.py",
        "application/full_analysis.py", "application/lineage_merge_review.py",
        "domain/understanding.py", "llm/provider.py", "llm/gateway.py",
    ]]
    return {
        "version": VERSION, "prior_used_or_reserved_usd": prior_cost(),
        "cumulative_ceiling_usd": 1.6, "max_new_reservation_usd": reserve,
        "max_logical_calls": 3, "max_attempts": 9, "new_s2_calls": 0,
        "new_relevance_calls": 0, "new_contribution_calls": 0,
        "production_gates_changed": False, "negative_control_sha256": previous.digest(control()),
        "original_sources_sha256": previous.digest(case["spans"]),
        "scope": "real-source false-mapping control, then corrected graph; no readiness claim",
        "code_and_artifact_sha256": {str(p.relative_to(ROOT)):
                                     hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
    }


def evaluate(budget_cents: int) -> dict:
    if budget_cents != 160:
        return {"status": "blocked", "reason": "explicit_160_cent_authorization_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        saved = json.loads(destination.read_text())
        return saved if saved.get("status") == "completed" else {
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
        case, analysis, checkpoint = asyncio.run(previous.inputs())
        try:
            negative = asyncio.run(parent._lineage(
                [case], database=DATABASE, prior_usage=prior_cost, version=f"{VERSION}-control",
                total_budget_usd=1.6, accepted_contributions=checkpoint,
                analysis_override=analysis,
                accepted_relevance_selection=previous.relevance_selection(),
                accepted_graph_proposal=control(),
            ))
            write("negative-control.json", negative)
            lineage = negative.get("lineage", {})
            if (lineage.get("status") != "insufficient_evidence"
                    or lineage.get("merge_review", {}).get("supported") is not False
                    or lineage.get("edges")):
                result = {"status": "stopped", "reason": "negative_control_not_rejected"}
            else:
                graph = asyncio.run(parent._lineage(
                    [case], database=DATABASE, prior_usage=prior_cost, version=f"{VERSION}-graph",
                    total_budget_usd=1.6, accepted_contributions=checkpoint,
                    analysis_override=analysis,
                    accepted_relevance_selection=previous.relevance_selection(),
                ))
                write("graph-results.json", graph)
                result = {"status": "completed", "negative_control_rejected": True,
                          "graph_status": graph.get("lineage", {}).get("status"),
                          "production_gates_changed": False}
        except Exception as exc:
            result = {"status": "stopped", "reason": "runner_failed_review_reservations",
                      "error_type": type(exc).__name__}
        write("results.json", result)
        return result
