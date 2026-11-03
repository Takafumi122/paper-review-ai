"""Current production Lineage core against frozen LoRA inputs; no academic API calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import limited_s2_verification as frozen
from paper_review.application import lora_stage2_verification as accepted
from paper_review.application import roadmap_verification as roadmap
from paper_review.config import ROOT
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/cached-lora-v35-2026-11-03"
DATABASE = ROOT / ".work/cached-lora-v35-2026-11-03.db"
VERSION = "cached-lora-v35-2026-11-03"


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prior_cost() -> float:
    total = frozen.prior_cost()
    if frozen.DATABASE.exists():
        with sqlite3.connect(f"file:{frozen.DATABASE}?mode=ro", uri=True) as conn:
            total += sum(r if s in {"reserved", "unknown"} else e or 0
                         for s, r, e in conn.execute(
                             "select cost_status,reserved_usd,estimated_cost_usd "
                             "from llm_executions"
                         ))
    return total


def plan() -> dict:
    case, analysis, _ = frozen.inputs()
    review = json.loads((accepted.OUTPUT / "relevance-review.json").read_text())
    old_result = json.loads((accepted.OUTPUT / "relevance-results.json").read_text())
    selection = accepted.relevance_selection()
    if (review["decision"] != "pass_for_scoped_graph_diagnostic"
            or review["relevance_sha256"] != accepted.digest(old_result)
            or review["selection_sha256"] != accepted.digest(selection)):
        raise ValueError("reviewed_relevance_changed")
    candidate_ids = {r.candidate.paper_id for r in analysis.ranking.top_k()}
    if candidate_ids != {e["candidate_id"] for e in selection["evaluations"]}:
        raise ValueError("frozen_top10_changed")
    if analysis.ranking.selection_policy_version != "lower-bound-v1":
        raise ValueError("production_stage1_policy_required")
    tasks = ["lineage", "lineage_merge_review"]
    reserve = sum(ROUTING[t].max_input_tokens * RATES[ROUTING[t].model][0]
                  + ROUTING[t].max_output_tokens * RATES[ROUTING[t].model][1] for t in tasks) * 3
    if prior_cost() + reserve > 1.6:
        raise ValueError("insufficient_shared_budget")
    paths = [frozen.FROZEN_INPUTS, frozen.GATE_PATH, frozen.HISTORICAL_STOP]
    paths += [accepted.OUTPUT / n for n in ("relevance-review.json", "relevance-results.json")]
    paths += [ROOT / "src/paper_review" / n for n in (
        "application/cached_lora_verification.py", "application/roadmap_verification.py",
        "application/full_analysis.py", "application/lineage_merge_review.py",
        "application/contribution_review.py", "domain/understanding.py",
        "llm/gateway.py", "llm/provider.py",
    )]
    return {"version": VERSION, "pipeline_version": "full-v35",
            "lineage_validation_version": "merge-grounding-v2", "new_s2_calls": 0,
            "new_contribution_calls": 0, "new_relevance_calls": 0,
            "max_llm_calls": 2, "max_llm_attempts": 6,
            "max_new_reservation_usd": reserve, "prior_used_or_reserved_usd": prior_cost(),
            "cumulative_ceiling_usd": 1.6, "production_flags_changed": False,
            "scope": "cached full text, candidate inventory, embeddings, citation decisions",
            "sources_count": len(case["spans"]), "independent_gold": False,
            "sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in paths}}


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
        return {"status": "blocked", "reason": "openai_key_missing"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        write("results.json", {"status": "started"})
        try:
            case, analysis, checkpoint = frozen.inputs()
            result = asyncio.run(roadmap._lineage(
                [case], database=DATABASE, prior_usage=prior_cost, version=VERSION,
                total_budget_usd=1.6, analysis_override=analysis,
                accepted_contributions=checkpoint,
                accepted_relevance_selection=accepted.relevance_selection(),
            ))
            write("lineage-results.json", result)
        except Exception as exc:
            result = {"status": "stopped", "reason": "runner_failed_review_reservations",
                      "error_type": type(exc).__name__}
        write("results.json", result)
        return result
