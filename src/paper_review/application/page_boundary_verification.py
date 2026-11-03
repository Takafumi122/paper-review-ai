"""Bounded fixed-section regression; no new academic API data or ranking policy."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import citation_quality_evaluation as classification
from paper_review.application import citation_year_verification as previous
from paper_review.application import roadmap_verification as roadmap
from paper_review.application.citation_contexts import (
    CITATION_PROMPT,
    CitationBatch,
    context_batches,
    prepare_contexts,
    request_context,
)
from paper_review.application.full_analysis import source_spans
from paper_review.application.stage1_shortlist import apply_context_classifications
from paper_review.config import ROOT
from paper_review.domain.models import CandidateSet, PaperSection
from paper_review.llm.token_budget import estimate_input_tokens

OUTPUT = ROOT / "evaluation/reports/page-boundary-v31-2026-11-02"
DATABASE = ROOT / ".work/page-boundary-v31.db"
VERSION = "page-boundary-v31"


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.DATABASE}?mode=ro", uri=True) as connection:
        return previous.prior_cost() + sum(
            r if status in {"reserved", "unknown"} else e or 0
            for status, r, e in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def inputs() -> list[dict]:
    before_attention = json.loads((previous.previous.OUTPUT / "classify-results.json").read_text())
    before_lora = json.loads((previous.OUTPUT / "comparison.json").read_text())
    cases = []
    for case in classification.load_cases():
        with sqlite3.connect(f"file:{ROOT / '.work/app.db'}?mode=ro", uri=True) as connection:
            connection.row_factory = sqlite3.Row
            sections = [PaperSection.model_validate(dict(row) | {
                "span_info": json.loads(row["span_info"]) if row["span_info"] else None,
            }) for row in connection.execute(
                "select id,revision_id,paper_id,section_index,title,text,"
                "page_start,page_end,span_info "
                "from paper_sections where revision_id=? order by section_index",
                (case["spans"][0]["revision"],),
            )]
        repaired = source_spans(sections)
        by_id = {s["span_id"]: s for s in repaired}
        if any(by_id.get(s["span_id"]) != s for s in case["spans"]):
            raise ValueError("old_source_changed")
        old = before_lora["audit"] if case["aid"] == "2106.09685" else next(
            c["audit"] for c in before_attention["cases"] if c["aid"] == case["aid"]
        )
        contexts = prepare_contexts(CandidateSet.model_validate(case["inventory"]), repaired)
        known = {previous.signature(c) for c in old["contexts"]}
        changed = [c for c in contexts if previous.signature(c) not in known]
        batches = context_batches(changed)
        if (any(not c["support_options"] for c in changed)
                or {c["context_id"] for b in batches for c in b}
                != {c["context_id"] for c in changed}):
            raise ValueError("changed_contexts_cannot_fit_batch_budget")
        cases.append({"case": dict(case, spans=repaired), "original_case": case,
                      "contexts": contexts, "old_audit": old, "changed": changed,
                      "batches": batches,
                      "section_hashes": {s.id: hashlib.sha256(s.text.encode()).hexdigest()
                                         for s in sections}})
    return cases


def plan() -> dict:
    cases = inputs()
    calls = sum(len(c["batches"]) for c in cases)
    if not 0 < calls <= 4:
        raise ValueError("unexpected_total_call_count")
    paths = [ROOT / "src/paper_review" / name for name in (
        "application/full_analysis.py", "application/citation_contexts.py",
        "application/citation_year_verification.py", "application/page_boundary_verification.py",
        "application/stage1_shortlist.py", "domain/stage1_scoring.py",
    )]
    paths += [roadmap.live_s2.RUNTIME_LIMIT, ROOT / ".work/s2-http-gate.json",
              previous.OUTPUT / "comparison.json",
              previous.previous.OUTPUT / "classify-results.json"]
    return {
        "version": VERSION, "cumulative_ceiling_usd": 1.6,
        "prior_used_or_reserved_usd": prior_cost(), "model": "gpt-6.1-sol",
        "max_logical_calls": calls, "max_attempts": calls * 3,
        "new_s2_calls": 0, "new_graph_calls": 0, "production_policy_changed": False,
        "artifact_and_code_sha256": {str(p.relative_to(ROOT)):
                                     hashlib.sha256(p.read_bytes()).hexdigest() for p in paths},
        "cases": [{"aid": c["case"]["aid"],
                   "original_sources_sha256": classification.digest(c["original_case"]["spans"]),
                   "repaired_sources_sha256": classification.digest(c["case"]["spans"]),
                   "section_hashes": c["section_hashes"],
                   "inventory_sha256": classification.digest(c["case"]["inventory"]),
                   "fixture_sha256": hashlib.sha256(c["case"]["fixture"].read_bytes()).hexdigest(),
                   "changed_context_ids": [r["context_id"] for r in c["changed"]],
                   "fixed_batches": [[r["context_id"] for r in b] for b in c["batches"]],
                   "input_tokens_estimates": [estimate_input_tokens(
                       CitationBatch.model_json_schema(), request_context(b), CITATION_PROMPT,
                   ) for b in c["batches"]],
                   "context_count_before": len(c["old_audit"]["contexts"]),
                   "context_count_after": len(c["contexts"])} for c in cases],
    }


async def compare(fresh: dict) -> dict:
    comparisons = []
    for item in inputs():
        case, old = item["case"], item["old_audit"]
        audit = next(c["audit"] for c in fresh["cases"] if c["aid"] == case["aid"])
        merged = previous.merge(item["contexts"], old, audit,
                                reused_origin="citation-context-v8", new_origin=VERSION)
        base = await roadmap.replay(case)
        before = apply_context_classifications(base, old)
        after = apply_context_classifications(base, merged)

        def rows(analysis):
            return [{"id": r.candidate.paper_id, "title": r.candidate.title,
                     "canonical_rank": r.rank, "score": r.score.model_dump()}
                    for r in analysis.ranking.ranked_candidates]

        comparisons.append({"aid": case["aid"], "audit": merged,
                            "before": rows(before), "after": rows(after),
                            "new_separator_sources": [
                                s for s in case["spans"]
                                if s.get("source_kind") == "stored_separator"
                            ]})
    return {"status": "completed", "cases": comparisons, "quality_gate": "held",
            "production_policy_changed": False}


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
        write("results.json", {"status": "started"})
        cases = inputs()
        try:
            fresh = asyncio.run(classification._run(
                [c["case"] for c in cases], OUTPUT / "classify-results.json", "sol",
                database=DATABASE, prior_cost=prior_cost, total_budget_usd=1.6,
                control_ids={c["case"]["aid"]: {r["context_id"] for r in c["changed"]}
                             for c in cases},
                fixed_context_batches={c["case"]["aid"]: [
                    [r["context_id"] for r in b] for b in c["batches"]
                ] for c in cases}, version=VERSION,
            ))
            write("classify-results.json", fresh)
            result = asyncio.run(compare(fresh)) if fresh["status"] == "completed" else fresh
        except Exception as exc:
            result = {"status": "stopped", "reason": "runner_failed_review_reservations",
                      "error_type": type(exc).__name__}
        write("results.json", result)
        return result
