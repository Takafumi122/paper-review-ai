"""One live URL-to-HTTP-result check with a predeclared USD 1 job ceiling."""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

import httpx

from paper_review.application import identity_selection_verification as lineage
from paper_review.application.contribution_verification import ledger_cost
from paper_review.config import ROOT

OUTPUT = ROOT / "evaluation/reports/fresh-url-v27-2026-11-02"
APP_DATABASE = ROOT / ".work/app.db"
ARXIV_URL = "https://arxiv.org/abs/2305.14314"
PAPER_ID = "arxiv:2305.14314"
API = "http://127.0.0.1:8000"


def check_health(value: dict, version: str = "full-v27", budget_cap: float = 1) -> None:
    if value.get("pipeline_version") != version:
        raise ValueError("full_v27_restart_required")
    models = value.get("models", {})
    if any(models.get(task) != "gpt-6.1-sol" for task in (
        "contribution_extraction", "contribution_review",
    )):
        raise ValueError("production_model_mismatch")
    budget = value.get("limits", {}).get("initial_analysis_budget_usd")
    if (isinstance(budget, bool) or not isinstance(budget, (float, int))
            or not 0 < budget <= budget_cap):
        raise ValueError("job_budget_must_not_exceed_one_dollar")
    if any(value.get("lineage_readiness", {}).values()):
        raise ValueError("unexpected_lineage_readiness")


def prior_cost() -> float:
    current = ledger_cost(lineage.DATABASE) if lineage.DATABASE.exists() else 0
    return lineage.prior_cost() + current


def evaluate(
    budget_cents: int = 50, client: httpx.Client | None = None, *,
    output_dir: Path | None = None, expected_version: str = "full-v27", job_budget: float = 1,
    parent_job_id: str | None = None, prior_usage: Callable[[], float] | None = None,
    authorized_cumulative_usd: float = 1.50,
) -> dict:
    output = OUTPUT if output_dir is None else output_dir
    prior_usage = prior_cost if prior_usage is None else prior_usage
    if (authorized_cumulative_usd not in {1.50, 1.60}
            or budget_cents != round(authorized_cumulative_usd * 100)):
        return {"status": "blocked", "reason": "explicit_150_cent_authorization_required"}
    result_file = output / "results.json"
    if result_file.exists():
        return json.loads(result_file.read_text())
    accepted_file = output / "accepted-job.json"
    if (output / "active.lock").exists():
        return {"status": "blocked", "reason": "active_or_interrupted_review_job_before_resume"}
    if accepted_file.exists():
        return {"status": "blocked", "reason": "accepted_job_requires_polling_not_repost"}
    if not 0 < job_budget <= 1 or prior_usage() + job_budget > authorized_cumulative_usd:
        return {"status": "blocked", "reason": "shared_budget_insufficient_for_job"}
    with sqlite3.connect(f"file:{APP_DATABASE}?mode=ro", uri=True) as connection:
        history = connection.execute(
            "select count(*) from analysis_jobs where paper_id=?", (PAPER_ID,),
        ).fetchone()[0]
        parent = connection.execute(
            "select status from analysis_jobs where id=? and paper_id=?",
            (parent_job_id, PAPER_ID),
        ).fetchone() if parent_job_id is not None else None
        active = connection.execute(
            "select count(*) from analysis_jobs where status not in "
            "('completed','partial_completed','failed')"
        ).fetchone()[0]
    allowed_history = (history == 1 and parent is not None and parent[0] == "failed"
                       if parent_job_id else history == 0)
    if not allowed_history or active:
        return {"status": "blocked", "reason": "target_has_history_or_worker_is_busy"}
    owned = client is None
    client = client or httpx.Client(base_url=API, timeout=20, trust_env=False)
    try:
        health = client.get("/health")
        health.raise_for_status()
        check_health(health.json(), expected_version, job_budget)
        output.mkdir(parents=True, exist_ok=True)
        with (output / "active.lock").open("x"):
            (output / "live-plan.json").write_text(json.dumps({
                "arxiv_url": ARXIV_URL, "paper_id": PAPER_ID, "prior_analysis_count": history,
                "job_budget_usd": job_budget,
                "cumulative_budget_usd": authorized_cumulative_usd,
                "prior_used_or_reserved_usd": prior_usage(), "health": health.json(),
                "parent_failed_job_id": parent_job_id,
                "max_analysis_posts": 1, "no_detail_generation": True,
            }, indent=2) + "\n")
            # Mark intent first. A lost POST response must never cause automatic resubmission.
            accepted_file.write_text('{"status":"post_started_response_not_yet_known"}\n')
            response = client.post("/analyses", json={"arxiv_url": ARXIV_URL})
            response.raise_for_status()
            accepted = response.json()
            accepted_file.write_text(json.dumps(accepted, indent=2) + "\n")
            jid = accepted["job_id"]
            started = time.monotonic()
            while time.monotonic() - started < 1200:
                response = client.get(f"/analyses/{jid}")
                response.raise_for_status()
                value = response.json()
                summary = {k: value.get(k) for k in (
                    "job_id", "paper_id", "status", "current_stage", "feature_status",
                    "error_code", "missing_reasons", "cost", "timing",
                )}
                (output / "progress.json").write_text(json.dumps(summary, indent=2) + "\n")
                if value["status"] in {"completed", "partial_completed", "failed"}:
                    endpoints = {path: client.get(path).status_code for path in (
                        f"/analyses/{jid}/lineage", f"/analyses/{jid}/reasoning",
                    )}
                    result = value.get("result") or {}
                    report = {"status": "completed", "analysis": summary,
                              "analysis_url": f"{API}/#analysis={jid}",
                              "paper": result.get("paper"), "endpoints": endpoints,
                              "source_count": len(result.get("sources", [])),
                              "reasoning_steps": len(result.get("reasoning", {}).get("steps", [])),
                              "knowledge_items": len(result.get("knowledge", {}).get("items", [])),
                              "lineage": result.get("lineage"),
                              "note": "Check completion includes failed/partial analyses."}
                    result_file.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
                    return report
                time.sleep(5)
            return {"status": "stopped", "reason": "poll_timeout_keep_job_no_resubmission"}
    finally:
        if owned:
            client.close()


if __name__ == "__main__":
    value = evaluate(150)
    print(json.dumps({"status": value["status"], "reason": value.get("reason"),
                      "analysis_status": value.get("analysis", {}).get("status")}))
