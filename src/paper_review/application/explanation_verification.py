"""One changed-schema QLoRA analysis, preserving the failed parent's ledger and result."""

import sqlite3

from paper_review.application import fresh_url_verification as previous
from paper_review.config import ROOT

OUTPUT = ROOT / "evaluation/reports/explanation-context-v28-2026-11-02"
PARENT_JOB_ID = "35f63155-d2b6-4c39-9536-ab9367d199a5"


def prior_cost() -> float:
    with sqlite3.connect(f"file:{previous.APP_DATABASE}?mode=ro", uri=True) as connection:
        spent = sum(reserved if status in {"reserved", "unknown"} else estimated or 0
                    for status, reserved, estimated in connection.execute(
                        "select cost_status,reserved_usd,estimated_cost_usd from llm_executions "
                        "where job_id=?", (PARENT_JOB_ID,),
                    ))
    return previous.prior_cost() + spent


def evaluate() -> dict:
    return previous.evaluate(
        160, output_dir=OUTPUT, expected_version="full-v28", job_budget=1,
        parent_job_id=PARENT_JOB_ID, prior_usage=prior_cost,
        authorized_cumulative_usd=1.60,
    )


if __name__ == "__main__":
    import json

    result = evaluate()
    print(json.dumps({"status": result["status"], "reason": result.get("reason"),
                      "analysis_status": result.get("analysis", {}).get("status")}))
