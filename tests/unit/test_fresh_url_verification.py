"""A fresh URL check creates at most one job and enforces runtime model/cost gates."""

import json
import sqlite3

import httpx
import pytest

from paper_review.application import fresh_url_verification as evaluation


def health():
    return {"pipeline_version": "full-v27", "models": {
        "contribution_extraction": "gpt-6.1-sol", "contribution_review": "gpt-6.1-sol",
    }, "limits": {"initial_analysis_budget_usd": 1},
        "lineage_readiness": {"specter2_verified": False, "stage1_audited": False}}


@pytest.mark.parametrize("fault", ["version", "model", "budget", "readiness"])
def test_fresh_url_preflight_fails_closed(fault):
    value = health()
    if fault == "version":
        value["pipeline_version"] = "full-v24"
    elif fault == "model":
        value["models"]["contribution_extraction"] = "gpt-5-nano"
    elif fault == "budget":
        value["limits"]["initial_analysis_budget_usd"] = 2
    else:
        value["lineage_readiness"]["stage1_audited"] = True
    with pytest.raises(ValueError):
        evaluation.check_health(value)


def test_fresh_url_once_and_ambiguous_post_never_resubmitted(settings, monkeypatch):
    root = settings.database_path.parent
    monkeypatch.setattr(evaluation, "APP_DATABASE", settings.database_path)
    monkeypatch.setattr(evaluation, "OUTPUT", root / "results")
    monkeypatch.setattr(evaluation, "prior_cost", lambda: .45)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("create table analysis_jobs(paper_id text,status text)")
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/health":
            return httpx.Response(200, json=health())
        if request.method == "POST":
            assert json.loads(request.content) == {"arxiv_url": evaluation.ARXIV_URL}
            return httpx.Response(202, json={"job_id": "j", "paper_id": evaluation.PAPER_ID})
        return httpx.Response(200, json={"status": "partial_completed", "job_id": "j",
            "paper_id": evaluation.PAPER_ID, "result": {"sources": [{"text": "not in report"}],
            "lineage": {"status": "blocked", "reason": "stage1_quality_audit_pending"},
            "reasoning": {"steps": [{"id": "s"}]}, "knowledge": {"items": []}}})

    with httpx.Client(base_url=evaluation.API, transport=httpx.MockTransport(handle)) as client:
        assert evaluation.evaluate(client=client)["reason"] == (
            "explicit_150_cent_authorization_required"
        )
        result = evaluation.evaluate(150, client)
        assert result["analysis"]["status"] == "partial_completed"
        assert result["source_count"] == 1 and result["reasoning_steps"] == 1
        assert "not in report" not in (evaluation.OUTPUT / "results.json").read_text()
        assert evaluation.evaluate(150, client) == result
        assert sum(method == "POST" for method, _ in calls) == 1
        (evaluation.OUTPUT / "results.json").unlink()
        assert evaluation.evaluate(150, client)["reason"].startswith("active_or_interrupted")
        assert sum(method == "POST" for method, _ in calls) == 1


@pytest.mark.parametrize("parent_status,ceiling,job_budget", [
    ("failed", 1.50, .90), ("completed", 1.50, .90), ("failed", 1.60, 1),
])
def test_changed_schema_followup_preserves_parent_and_caps_remaining_budget(
    settings, monkeypatch, parent_status, ceiling, job_budget,
):
    root = settings.database_path.parent
    monkeypatch.setattr(evaluation, "APP_DATABASE", settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("create table analysis_jobs(id text,paper_id text,status text)")
        connection.execute("insert into analysis_jobs values(?,?,?)",
                           ("parent", evaluation.PAPER_ID, parent_status))
    posts = []

    def handle(request):
        if request.url.path == "/health":
            value = health()
            value["pipeline_version"] = "full-v28"
            value["limits"]["initial_analysis_budget_usd"] = job_budget
            return httpx.Response(200, json=value)
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(202, json={"job_id": "new", "paper_id": evaluation.PAPER_ID})
        return httpx.Response(200, json={"status": "failed", "job_id": "new", "result": {}})

    with httpx.Client(base_url=evaluation.API, transport=httpx.MockTransport(handle)) as client:
        output = root / "followup"
        result = evaluation.evaluate(
            round(ceiling * 100), client, output_dir=output, expected_version="full-v28",
            job_budget=job_budget, authorized_cumulative_usd=ceiling,
            parent_job_id="parent", prior_usage=lambda: .55100416,
        )
    if parent_status == "failed":
        assert len(posts) == 1 and result["status"] == "completed"
        plan = json.loads((output / "live-plan.json").read_text())
        assert plan["job_budget_usd"] == job_budget
        assert plan["cumulative_budget_usd"] == ceiling
        assert plan["parent_failed_job_id"] == "parent"
    else:
        assert not posts and result["status"] == "blocked"
    with sqlite3.connect(settings.database_path) as connection:
        assert connection.execute("select status from analysis_jobs where id='parent'").fetchone()[
            0
        ] == parent_status
