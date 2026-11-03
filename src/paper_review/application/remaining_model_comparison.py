"""Fixed Sol generation comparison against the closed Nano bundle; no graph or fallback."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from paper_review.application import remaining_verification as previous
from paper_review.application import roadmap_verification as parent
from paper_review.application.citation_quality_evaluation import load_cases
from paper_review.application.contribution_verification import ledger_cost
from paper_review.config import ROOT
from paper_review.llm.gateway import ExecutionFailure
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/remaining-sol-v25-2026-11-02"
DATABASE = ROOT / ".work/remaining-sol-v25.db"
VERSION = "remaining-sol-v25"


def prior_cost() -> float:
    return previous.prior_cost() + ledger_cost(previous.DATABASE)


def plan() -> dict:
    return {
        "version": VERSION, "pipeline_version": "full-v24", "cumulative_budget_usd": .50,
        "prior_used_or_reserved_usd": prior_cost(), "new_s2_requests": 0,
        "new_classification_requests": 0, "max_logical_calls": 4, "max_http_attempts": 12,
        "evaluation_generation_model": "gpt-6.1-sol", "automatic_fallback": False,
        "targets": {"2106.09685": "contributions_only", "1706.03762": "contributions_only"},
        "production_flags_changed": False,
        "parent_artifacts_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [previous.OUTPUT / "results.json", *[
                parent.OUTPUT / name for name in (
                    "classify-results.json", "stage1-review.json", "s2-results.json",
                )
            ]]
        },
        "source_code_sha256": {
            name: hashlib.sha256((ROOT / "src/paper_review" / name).read_bytes()).hexdigest()
            for name in ("application/remaining_model_comparison.py",
                         "application/contribution_review.py",
                         "application/contribution_evidence.py", "application/full_analysis.py",
                         "application/roadmap_verification.py", "domain/understanding.py",
                         "llm/gateway.py", "llm/provider.py")
        },
        "task_policies": {task: {**vars(ROUTING[task]), "model": "gpt-6.1-sol"} for task in (
            "contribution_extraction", "contribution_review",
        )},
    }


def evaluate(budget_cents: int = 15) -> dict:
    if budget_cents != 50:
        return {"status": "blocked", "reason": "explicit_shared_ceiling_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        result = json.loads(destination.read_text())
        return result if result.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review",
        }
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    classification = parent.OUTPUT / "classify-results.json"
    review = json.loads((parent.OUTPUT / "stage1-review.json").read_text())
    s2 = json.loads((parent.OUTPUT / "s2-results.json").read_text())
    if not (review.get("accepted_for_lineage_diagnostic") is True
            and review.get("classification_sha256") == hashlib.sha256(
                classification.read_bytes()
            ).hexdigest()
            and any(row.get("target") == "2106.09685" and row.get("gate") in {
                "READY FOR QUALITY AUDIT", "QUALITY AUDIT RESULT",
            } for row in s2.get("reports", []))):
        return {"status": "blocked", "reason": "scoped_source_review_required"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if prior_cost() >= .50:
        return {"status": "blocked", "reason": "shared_budget"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        (OUTPUT / "live-plan.json").write_text(json.dumps(plan(), indent=2) + "\n")
        destination.write_text('{"status":"started"}\n')
        cases = load_cases()
        results = []
        for aid in ("2106.09685", "1706.03762"):
            try:
                result = asyncio.run(parent._lineage(
                    cases, input_directory=parent.OUTPUT, database=DATABASE,
                    prior_usage=prior_cost, version=f"{VERSION}-{aid}", target_aid=aid,
                    contribution_only=True, evaluation_model="gpt-6.1-sol",
                ))
            except Exception as exc:
                result = {"status": "stopped", "reason": (
                    str(exc) if isinstance(exc, ExecutionFailure) else type(exc).__name__
                )}
            row = {"aid": aid, **result}
            results.append(row)
            (OUTPUT / f"{aid}-results.json").write_text(json.dumps(
                row, ensure_ascii=False, indent=2,
            ) + "\n")
        # Completed refers to the bundle, including any explicitly stopped/insufficient cases.
        result = {"status": "completed", "cases": results, "production_flags_changed": False}
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
