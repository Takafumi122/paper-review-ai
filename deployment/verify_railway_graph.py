"""One-shot, opt-in release canary; credentials stay inside the running service."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paper_review.config import ROOT, Settings
from paper_review.infrastructure.database.models import AnalysisJob, LLMExecution
from paper_review.infrastructure.database.session import make_engine

ARXIV_URL = "https://arxiv.org/abs/2005.11401v4"
MARKER = ROOT / ".work/railway-graph-v44-canary.json"


def summarize(data: dict[str, Any], attempts: int) -> dict[str, Any]:
    result = data.get("result") or {}
    lineage = result.get("lineage") or {}
    cost = data.get("cost") or {}
    accounted = cost.get("estimated_usd", 0) + cost.get("unsettled_reserved_usd", 0)
    passed = (
        data.get("status") == "completed" and bool(lineage.get("edges"))
        and accounted <= 0.50 and attempts <= 30 and not cost.get("has_unknown_cost")
    )
    return {
        "release_canary": "passed" if passed else "failed",
        "job_id": data.get("job_id"), "job_status": data.get("status"),
        "features": data.get("feature_status"), "error_code": data.get("error_code"),
        "edges": len(lineage.get("edges") or []),
        "source_count": len(result.get("sources") or []),
        "attempts": attempts, "cost": cost,
        "duration_seconds": (data.get("timing") or {}).get("total_seconds"),
        "quality_acceptance": "numerical_gate_waived_by_user",
    }


def run() -> dict[str, Any]:
    settings = Settings.from_environment()
    if (settings.initial_budget_usd > 0.50 or settings.detail_budget_usd > 0.10
            or not settings.openalex_local_verified or not settings.openalex_local_audited):
        raise ValueError("release_canary_configuration_not_ready")
    # A crash, redeploy or restart must never enqueue a second paid analysis.
    try:
        with MARKER.open("x") as handle:
            json.dump({"state": "claimed", "arxiv_url": ARXIV_URL}, handle)
    except FileExistsError:
        return {"release_canary": "already_claimed_no_new_job"}
    headers = {"Authorization": "Bearer " + os.environ["PAPER_REVIEW_ACCESS_TOKEN"]}
    base = "http://127.0.0.1:" + os.environ.get("PORT", "8000")
    engine = make_engine(settings)
    with httpx.Client(base_url=base, headers=headers, timeout=15) as client:
        for _ in range(120):
            try:
                if client.get("/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            raise ValueError("release_canary_api_unavailable")
        with Session(engine) as session:
            if session.scalar(select(func.count(AnalysisJob.id))):
                raise ValueError("release_canary_requires_empty_dedicated_db")
        accepted = client.post("/analyses", json={"arxiv_url": ARXIV_URL})
        accepted.raise_for_status()
        job_id = accepted.json()["job_id"]
        MARKER.write_text(json.dumps({"state": "submitted", "job_id": job_id}))
        print(json.dumps({"release_canary": "submitted", "job_id": job_id}), flush=True)
        previous_stage = None
        for _ in range(360):
            response = client.get(f"/analyses/{job_id}")
            response.raise_for_status()
            data = response.json()
            if data.get("current_stage") != previous_stage:
                previous_stage = data.get("current_stage")
                print(json.dumps({"release_canary": "progress", "job_id": job_id,
                                  "stage": previous_stage}), flush=True)
            if data["status"] in {"completed", "partial_completed", "failed"}:
                with Session(engine) as session:
                    attempts = session.scalar(select(func.count(LLMExecution.id)).where(
                        LLMExecution.job_id == job_id)) or 0
                summary = summarize(data, attempts)
                saved = client.get(f"/analyses/{job_id}")
                saved.raise_for_status()
                summary["saved_result_stable"] = saved.json().get("result") == data.get("result")
                summary["saved_result_sha256"] = hashlib.sha256(json.dumps(
                    data.get("result"), sort_keys=True).encode()).hexdigest()
                if not summary["saved_result_stable"]:
                    summary["release_canary"] = "failed"
                peak = Path("/sys/fs/cgroup/memory.peak")
                if peak.exists():
                    summary["container_peak_bytes"] = int(peak.read_text())
                MARKER.write_text(json.dumps(summary, indent=2))
                return summary
            time.sleep(3)
    raise ValueError("release_canary_poll_deadline")


if __name__ == "__main__":
    try:
        report = run()
    except Exception as exc:
        # Exception bodies/URLs may contain paper content or credentials.
        report = {"release_canary": "failed", "error_type": type(exc).__name__}
    print(json.dumps(report), flush=True)
