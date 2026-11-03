"""Lineage continuation using frozen Sol-reviewed contributions; no repeat generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from paper_review.application import remaining_model_comparison as previous
from paper_review.application import roadmap_verification as parent
from paper_review.application.citation_quality_evaluation import load_cases
from paper_review.application.contribution_verification import ledger_cost
from paper_review.config import ROOT
from paper_review.llm.gateway import ExecutionFailure
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/lineage-continuation-v26-2026-11-02"
DATABASE = ROOT / ".work/lineage-continuation-v26.db"
VERSION = "lineage-continuation-v26"


def prior_cost() -> float:
    return previous.prior_cost() + ledger_cost(previous.DATABASE)


def plan() -> dict:
    return {
        "version": VERSION, "cumulative_budget_usd": 0.50,
        "prior_used_or_reserved_usd": prior_cost(), "new_s2_requests": 0,
        "new_classification_requests": 0, "max_logical_calls": 2, "max_http_attempts": 6,
        "accepted_checkpoint_sha256": hashlib.sha256(
            (previous.OUTPUT / "2106.09685-results.json").read_bytes()
        ).hexdigest(), "repeat_generation_or_review": False,
        "target": "2106.09685", "production_flags_changed": False,
        "parent_artifacts_sha256": {
            name: hashlib.sha256((parent.OUTPUT / name).read_bytes()).hexdigest()
            for name in ("classify-results.json", "stage1-review.json", "s2-results.json",
                         "fresh-versus-frozen-embeddings.json", "lineage-results.json")
        },
        "source_code_sha256": {
            name: hashlib.sha256((ROOT / "src/paper_review" / name).read_bytes()).hexdigest()
            for name in ("application/full_analysis.py", "application/lineage_evidence.py",
                         "application/lineage_continuation.py",
                         "application/roadmap_verification.py",
                         "domain/understanding.py", "llm/gateway.py")
        },
        "task_policies": {task: vars(ROUTING[task]) for task in (
            "contribution_relevance", "lineage",
        )},
    }


def evaluate(budget_cents: int = 15) -> dict:
    if budget_cents != 50:
        return {"status": "blocked", "reason": "explicit_shared_ceiling_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        result = json.loads(destination.read_text())
        return result if result.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review"
        }
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    baseline = parent.OUTPUT / "classify-results.json"
    review = json.loads((parent.OUTPUT / "stage1-review.json").read_text())
    connection = json.loads((parent.OUTPUT / "s2-results.json").read_text())
    if not (
        review.get("accepted_for_lineage_diagnostic") is True
        and review.get("classification_sha256") == hashlib.sha256(baseline.read_bytes()).hexdigest()
        and json.loads(baseline.read_text()).get("status") == "completed"
        and any(row.get("target") == "2106.09685" and row.get("gate") in {
            "READY FOR QUALITY AUDIT", "QUALITY AUDIT RESULT",
        } for row in connection.get("reports", []))
    ):
        return {"status": "blocked", "reason": "scoped_parent_source_review_required"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if prior_cost() >= 0.50:
        return {"status": "blocked", "reason": "shared_budget"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        (OUTPUT / "live-plan.json").write_text(json.dumps(plan(), indent=2) + "\n")
        destination.write_text('{"status":"started"}\n')
        try:
            accepted = json.loads((previous.OUTPUT / "2106.09685-results.json").read_text())
            if accepted.get("lineage", {}).get("status") != "completed":
                raise ExecutionFailure("reviewed_checkpoint_required")
            result = asyncio.run(parent._lineage(
                load_cases(), input_directory=parent.OUTPUT, database=DATABASE,
                prior_usage=prior_cost, version=VERSION,
                accepted_contributions=accepted["lineage"],
            ))
        except Exception as exc:
            result = {"status": "stopped", "reason": (
                str(exc) if isinstance(exc, ExecutionFailure) else type(exc).__name__
            )}
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
