"""Frozen-source year-alias repair; one Sol batch, zero academic API requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import citation_quality_evaluation as classification
from paper_review.application import ranking_quality_verification as previous
from paper_review.application import roadmap_verification as roadmap
from paper_review.application.citation_contexts import (
    CITATION_PROMPT,
    CitationBatch,
    prepare_contexts,
    request_context,
)
from paper_review.application.stage1_shortlist import apply_context_classifications
from paper_review.config import ROOT
from paper_review.domain.models import CandidateSet
from paper_review.llm.token_budget import estimate_input_tokens

OUTPUT = ROOT / "evaluation/reports/citation-years-v30-2026-11-02"
DATABASE = ROOT / ".work/citation-years-v30.db"
VERSION = "citation-years-v30"


def _write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.DATABASE}?mode=ro", uri=True) as connection:
        return previous.prior_cost() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def signature(context: dict) -> tuple:
    return (context["candidate_id"], tuple(context["source_ids"]),
            context.get("text_sha256") or hashlib.sha256(context["text"].encode()).hexdigest(),
            tuple(context["citation_anchors"]))


def inputs() -> tuple[dict, list[dict], dict, list[dict]]:
    case = next(c for c in classification.load_cases() if c["aid"] == "2106.09685")
    contexts = prepare_contexts(CandidateSet.model_validate(case["inventory"]), case["spans"])
    audit = next(c["audit"] for c in json.loads(
        (previous.OUTPUT / "classify-results.json").read_text()
    )["cases"] if c["aid"] == case["aid"])
    known = {signature(c) for c in audit["contexts"]}
    changed = [c for c in contexts if signature(c) not in known]
    return case, contexts, audit, changed


def plan() -> dict:
    case, contexts, audit, changed = inputs()
    estimate = estimate_input_tokens(
        CitationBatch.model_json_schema(), request_context(changed), CITATION_PROMPT,
    )
    if not 0 < len(changed) <= 12 or any(not c["support_options"] for c in changed):
        raise ValueError("unexpected_changed_contexts")
    if estimate > 8000:
        raise ValueError("changed_context_batch_exceeds_input_budget")
    return {
        "version": VERSION, "authorized_cumulative_usd": 1.6,
        "prior_used_or_reserved_usd": prior_cost(), "new_s2_requests": 0,
        "new_graph_calls": 0, "max_logical_calls": 1, "max_attempts": 3,
        "model": "gpt-6.1-sol", "input_tokens_estimate": estimate,
        "selected_context_ids": [c["context_id"] for c in changed],
        "old_context_count": len(audit["contexts"]), "new_context_count": len(contexts),
        "selected_candidate_ids": [c["candidate_id"] for c in changed],
        "case_sha256": classification.digest({k: v for k, v in case.items() if k != "fixture"}),
        "fixture_sha256": hashlib.sha256(case["fixture"].read_bytes()).hexdigest(),
        "baseline_sha256": hashlib.sha256(
            (previous.OUTPUT / "classify-results.json").read_bytes()
        ).hexdigest(),
        "selected_context_sha256": classification.digest(request_context(changed)),
        "code_sha256": {
            name: hashlib.sha256((ROOT / "src/paper_review" / name).read_bytes()).hexdigest()
            for name in ("domain/citation_identity.py", "application/citation_contexts.py",
                         "application/citation_year_verification.py",
                         "application/stage1_shortlist.py", "domain/stage1_scoring.py")
        },
        "s2_stop_sha256": hashlib.sha256(roadmap.live_s2.RUNTIME_LIMIT.read_bytes()).hexdigest(),
        "shared_gate_sha256": hashlib.sha256(
            (ROOT / ".work/s2-http-gate.json").read_bytes()
        ).hexdigest(),
        "production_flags_changed": False,
    }


def merge(
    contexts: list[dict], old: dict, fresh: dict, *,
    reused_origin: str = previous.VERSION, new_origin: str = VERSION,
) -> dict:
    """Never reuse positional C IDs after insertion; bind original text and anchors."""
    known = {signature(c): c for c in old["contexts"]}
    new = {signature(c): c for c in fresh["contexts"]}
    rows = []
    for context in contexts:
        key = signature(context)
        source = new.get(key, known.get(key))
        if source is None:
            raise ValueError("citation_reuse_identity_mismatch")
        row = {**source, **{k: v for k, v in context.items() if k != "text"},
               "text_sha256": hashlib.sha256(context["text"].encode()).hexdigest(),
               "source_context_id": source["context_id"],
               "evaluation_origin": new_origin if key in new else source.get(
                   "evaluation_origin", reused_origin,
               )}
        if source.get("support_id") is not None:
            options = [p for p in context["support_options"]
                       if p["source_ranges"] == source["evidence_source_ranges"]
                       and context["text"][p["start"]:p["end"]] == source["supporting_quote"]]
            if len(options) != 1:
                raise ValueError("citation_reuse_quote_mismatch")
            row["support_id"] = options[0]["support_id"]
        rows.append(row)
    if len(new) != sum(signature(c) in new for c in contexts):
        raise ValueError("unexpected_fresh_citation_identity")
    return {"version": new_origin, "contexts": rows, "errors": fresh["errors"],
            "scope": "fixed_source_incremental_evaluation",
            "reused_contexts": sum(signature(c) not in new for c in contexts),
            "new_or_changed_contexts": len(new)}


async def _compare(fresh: dict) -> dict:
    case, contexts, old, _ = inputs()
    merged = merge(contexts, old, fresh)
    baseline = await roadmap.replay(case)
    before = apply_context_classifications(baseline, old)
    after = apply_context_classifications(baseline, merged)
    old_rows = {r.candidate.paper_id: r for r in before.ranking.ranked_candidates}
    rows = [{
        "candidate_id": r.candidate.paper_id, "title": r.candidate.title,
        "rank_before": old_rows[r.candidate.paper_id].rank, "rank_after": r.rank,
        "score_before": old_rows[r.candidate.paper_id].score.model_dump(),
        "score_after": r.score.model_dump(),
    } for r in after.ranking.ranked_candidates]
    return {
        "audit": merged, "ranking": rows, "weights_unchanged": before.ranking.weights
        == after.ranking.weights, "top_k": after.ranking.shortlist_limit,
        "candidate_inventory_unchanged": before.inventory == after.inventory,
        "top10_missing_before": sum(r.score.citation_importance is None
                                    for r in before.ranking.ranked_candidates[:10]),
        "top10_missing_after": sum(r.score.citation_importance is None
                                   for r in after.ranking.ranked_candidates[:10]),
        "quality_gate": "held", "production_flags_changed": False,
    }


def evaluate(budget_cents: int) -> dict:
    if budget_cents != 160:
        return {"status": "blocked", "reason": "explicit_160_cent_authorization_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review",
        }
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if prior_cost() >= 1.6:
        return {"status": "blocked", "reason": "shared_budget"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        _write("results.json", {"status": "started"})
        case, _, _, changed = inputs()
        selected = [c["context_id"] for c in changed]
        try:
            fresh = asyncio.run(classification._run(
                [case], OUTPUT / "classify-results.json", "sol", database=DATABASE,
                prior_cost=prior_cost, total_budget_usd=1.6,
                control_ids={case["aid"]: set(selected)},
                fixed_context_batches={case["aid"]: [selected]}, version=VERSION,
            ))
            _write("classify-results.json", fresh)
            if fresh["status"] != "completed":
                _write("results.json", fresh)
                return fresh
            comparison = asyncio.run(_compare(fresh["cases"][0]["audit"]))
            _write("comparison.json", comparison)
            result = {"status": "completed", "cases": fresh["cases"],
                      "quality_gate": "held", "new_s2_requests": 0}
        except Exception as exc:
            result = {"status": "stopped", "reason": "runner_failed_review_reservations",
                      "error_type": type(exc).__name__}
        _write("results.json", result)
        return result
