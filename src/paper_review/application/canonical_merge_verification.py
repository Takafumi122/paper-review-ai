"""Frozen regression window for canonical two-line integration, including shared CIDs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import real_merge_verification as previous
from paper_review.config import ROOT
from paper_review.domain.models import CandidatePaper, Paper
from paper_review.domain.understanding import (
    Contributions,
    LineageProposalDraft,
    validate_evidence,
    validate_lineage,
)
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/canonical-merge-v35-2026-11-03"
DATABASE = ROOT / ".work/canonical-merge-v35.db"
VERSION = "canonical-merge-v35"


def write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def used_here() -> float:
    if not DATABASE.exists():
        return 0
    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as conn:
        return sum(r if s in {"reserved", "unknown"} else e or 0 for s, r, e in conn.execute(
            "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
        ))


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.DATABASE}?mode=ro", uri=True) as conn:
        return previous.prior_cost() + sum(
            r if s in {"reserved", "unknown"} else e or 0 for s, r, e in conn.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def inputs() -> list[dict]:
    cases = json.loads((OUTPUT / "inputs.json").read_text())["cases"]
    for case in cases:
        value = Contributions.model_validate(case["contributions"])
        sources = {s["span_id"]: s for s in case["sources"]}
        for evidence in [value.problem_evidence, *(c.evidence for c in value.contributions)]:
            validate_evidence(evidence, sources)
        case["validated_edges"] = validate_lineage(
            LineageProposalDraft.model_validate(case["proposal"]),
            Paper.model_validate(case["target"]),
            {c["paper_id"]: CandidatePaper.model_validate(c) for c in case["candidates"]},
            sources, contributions=value,
        )
    return cases


def plan() -> dict:
    cases = inputs()
    tasks = ["lineage_merge_review"] * len(cases)
    tasks += ["contribution_review"] * sum(
        c["accepted_contribution_review"] is None for c in cases
    )
    reserve = sum(ROUTING[t].max_input_tokens * RATES[ROUTING[t].model][0]
                  + ROUTING[t].max_output_tokens * RATES[ROUTING[t].model][1] for t in tasks) * 3
    if prior_cost() + reserve > 1.6:
        raise ValueError("insufficient_shared_budget")
    paths = [OUTPUT / "inputs.json", ROOT / ".work/s2-http-gate.json",
             ROOT / ".work/s2-rate-limited.json"]
    paths += [ROOT / "src/paper_review" / n for n in (
        "application/canonical_merge_verification.py", "application/real_merge_verification.py",
        "application/lineage_merge_review.py", "application/contribution_review.py",
        "application/roadmap_verification.py", "application/full_analysis.py",
        "domain/understanding.py", "llm/provider.py", "llm/gateway.py",
    )]
    return {"version": VERSION, "max_logical_calls": len(tasks), "max_attempts": len(tasks)*3,
            "max_new_reservation_usd": reserve, "cumulative_ceiling_usd": 1.6,
            "prior_used_or_reserved_usd": prior_cost(), "new_s2_calls": 0,
            "new_graph_generation_calls": 0, "new_relevance_calls": 0,
            "production_flags_changed": False, "independent_gold": False,
            "cases": [{"name": c["name"], "expected_supported": c["expected_supported"]}
                      for c in cases],
            "sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in paths}}


async def run() -> dict:
    cases = []
    reserve = plan()["max_new_reservation_usd"]
    for case in inputs():
        result = await previous.review_case(
            Contributions.model_validate(case["contributions"]),
            LineageProposalDraft.model_validate(case["proposal"]), case["validated_edges"],
            database=DATABASE, version=f"{VERSION}-{case['name']}",
            budget_usd=min(reserve-used_here(), 1.6-prior_cost()-used_here()),
            output=OUTPUT / case["name"],
            accepted_contribution_review=case["accepted_contribution_review"],
        )
        cases.append({"name": case["name"], "expected_supported": case["expected_supported"],
                      **result})
        write("case-results.json", {"cases": cases})
        if (result["status"] != "completed"
                or result["merge_supported"] != case["expected_supported"]):
            return {"status": "stopped", "reason": "case_requires_review_no_preference_retry",
                    "cases": cases}
    return {"status": "completed", "cases": cases, "new_s2_calls": 0,
            "production_flags_changed": False}


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
