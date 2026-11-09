"""Frozen recovery never repeats Attention or resets unknown-cost reservations."""

import asyncio
import gzip
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.api.app import create_app
from paper_review.application import citation_quality_evaluation as baseline
from paper_review.application import citation_quality_recovery as recovery
from paper_review.application import roadmap_verification as bundle
from paper_review.application.stage1_shortlist import Stage1Analysis
from paper_review.config import Settings
from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    DateProvenance,
    Paper,
    Reference,
)
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.database.models import LLMExecution
from paper_review.infrastructure.database.session import make_engine
from paper_review.llm.provider import ProviderResponse


@pytest.fixture
def recovery_plan(settings, monkeypatch):
    root = settings.database_path.parent
    monkeypatch.setattr(baseline, "OUTPUT", root / "parent")
    monkeypatch.setattr(recovery, "OUTPUT", root / "recovery")
    monkeypatch.setattr(recovery, "DATABASE", root / "recovery.db")
    monkeypatch.setattr(baseline, "prior_reservation", lambda: 0.0181376)
    monkeypatch.setattr(recovery, "prior_cost", lambda: 0.0590636)
    monkeypatch.setattr(baseline, "CONTROL_IDS", {
        "1706.03762": {"C0"}, "2106.09685": {"C0"},
    })
    fixture = root / "fixture.gz"
    fixture.write_bytes(gzip.compress(b"[]"))
    target = Paper(id="target", title="Synthetic target")
    inventory = CandidateSet(target_paper_id="target", candidates=[CandidatePaper(
        paper_id="candidate", title="Prior method", resolution_status="resolved",
        source_references=[Reference(
            id="ref", paper_id="target", revision_id="rev", ref_order=2,
            raw_text="Reference", citation_key="[2]",
        )],
    )])
    text = "We use the method [2]."
    cases = [{"aid": aid, "source_job": "source", "target": target.model_dump(),
              "inventory": inventory.model_dump(), "fixture": fixture,
              "spans": [{"span_id": "source", "paper_id": "target", "revision": "rev",
                         "section_id": "section", "section": "Method", "text": text,
                         "start_offset": "0", "end_offset": str(len(text))}]}
             for aid in ("1706.03762", "2106.09685")]
    monkeypatch.setattr(baseline, "load_cases", lambda: cases)

    async def frozen_analysis(target, inventory, client):
        return Stage1Analysis(target=target, inventory=inventory, ranking=rank_stage1_candidates(
            target, inventory, target_references_fully_resolved=False,
        ))

    monkeypatch.setattr(baseline, "build_stage1_shortlist", frozen_analysis)
    baseline.OUTPUT.mkdir()
    recovery.OUTPUT.mkdir()
    (baseline.OUTPUT / "prepared-plan.json").write_text(json.dumps(baseline.plan(cases)))
    (baseline.OUTPUT / "sol-results.json").write_text(json.dumps({
        "status": "stopped", "reason": "classification_unavailable", "cases": [
            {"aid": "1706.03762", "audit": {"errors": [], "contexts_completed": 1,
             "contexts": [{"context_id": "C0", "status": "completed"}]}},
            {"aid": "2106.09685", "audit": {"errors": ["unavailable"],
             "contexts_completed": 0, "contexts": []}},
        ],
    }))
    (recovery.OUTPUT / "prepared-plan.json").write_text(json.dumps(recovery.plan(cases)))
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    return cases


def test_sol_reuses_parent_and_nano_needs_hash_bound_review(recovery_plan, monkeypatch):
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            return ProviderResponse(output={"decisions": [{
                "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                "classification": "core_foundation",
                "support_id": json.loads(s["support_options"])[0]["support_id"],
                "reason": "手法を使用",
            } for s in request.context_spans]}, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(baseline, "OpenAIProvider", Provider)
    parent_bytes = (baseline.OUTPUT / "sol-results.json").read_bytes()
    result = recovery.evaluate()
    assert result["status"] == "completed" and len(result["cases"]) == 2
    assert len(calls) == 1 and calls[0].model == "gpt-6.1-sol"
    assert result["cases"][0] == json.loads(parent_bytes)["cases"][0]
    assert (baseline.OUTPUT / "sol-results.json").read_bytes() == parent_bytes
    assert recovery.evaluate() == result and len(calls) == 1
    assert recovery.evaluate("nano")["reason"] == "source_reviewed_sol_baseline_required"
    path = recovery.OUTPUT / "sol-results.json"
    (recovery.OUTPUT / "sol-review.json").write_text(json.dumps({
        "accepted_for_comparison": True, "sol_result_sha256": "wrong",
    }))
    assert recovery.evaluate("nano")["reason"] == "source_reviewed_sol_baseline_required"
    (recovery.OUTPUT / "sol-review.json").write_text(json.dumps({
        "accepted_for_comparison": True,
        "sol_result_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }))
    assert recovery.evaluate("nano")["status"] == "completed"
    assert [c.model for c in calls] == ["gpt-6.1-sol", "gpt-5-nano", "gpt-5-nano"]
    recovery.evaluate("nano")
    assert len(calls) == 3


def test_recovery_timeout_exhausts_reservation_without_repeating(recovery_plan, monkeypatch):
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            raise TimeoutError()

    monkeypatch.setattr(baseline, "OpenAIProvider", Provider)
    assert recovery.evaluate()["status"] == "stopped"
    assert recovery.evaluate()["reason"] == "previous_run_requires_review"
    assert recovery.evaluate("nano")["reason"] == "source_reviewed_sol_baseline_required"
    assert len(calls) == 1
    engine = make_engine(Settings(database_path=recovery.DATABASE))
    try:
        with Session(engine) as session:
            execution = session.scalar(select(LLMExecution))
            assert execution is not None and execution.cost_status == "unknown"
            assert execution.reserved_usd == pytest.approx(0.036)
    finally:
        engine.dispose()


def test_parent_result_changes_block_paid_execution(recovery_plan):
    path = baseline.OUTPUT / "sol-results.json"
    data = json.loads(path.read_text())
    data["diagnostic_change"] = True
    path.write_text(json.dumps(data))
    assert recovery.evaluate()["reason"] == "prepared_plan_required_or_changed"
    assert not recovery.DATABASE.exists()


def test_unknown_and_known_parent_costs_are_preserved(settings, monkeypatch):
    path = settings.database_path.parent / "parent-costs.db"
    monkeypatch.setattr(baseline, "DATABASE", path)
    monkeypatch.setattr(baseline, "prior_reservation", lambda: 0.0181376)
    with sqlite3.connect(path) as conn:
        conn.execute("create table llm_executions (cost_status,reserved_usd,estimated_cost_usd)")
        conn.executemany("insert into llm_executions values (?,?,?)", [
            ("estimated", 0.036, 0.004926), ("unknown", 0.036, None),
        ])
    assert recovery.prior_cost() == pytest.approx(0.0590636)


def test_unavailable_parent_blocks_before_paid_execution(recovery_plan):
    (baseline.OUTPUT / "prepared-plan.json").unlink()
    assert recovery.evaluate()["reason"] == "parent_unavailable_or_changed"
    assert not recovery.DATABASE.exists()


def prepare_sentence_window(settings, monkeypatch, cases):
    root = settings.database_path.parent
    monkeypatch.setattr(recovery, "SENTENCE_OUTPUT", root / "sentences")
    monkeypatch.setattr(recovery, "SENTENCE_DATABASE", root / "sentences.db")
    monkeypatch.setattr(recovery, "sentence_prior_cost", lambda: 0.0647804)
    recovery.SENTENCE_OUTPUT.mkdir()
    (recovery.SENTENCE_OUTPUT / "prepared-plan.json").write_text(
        json.dumps(recovery.sentence_plan(cases))
    )


def test_sentence_window_default_budget_never_increases_or_sends(
    recovery_plan, settings, monkeypatch,
):
    prepare_sentence_window(settings, monkeypatch, recovery_plan)
    assert recovery.evaluate_sentences()["reason"] == "insufficient_shared_budget"
    assert recovery.evaluate_sentences(100)["reason"] == "unsupported_budget"
    assert not recovery.SENTENCE_DATABASE.exists()
    assert not (recovery.SENTENCE_OUTPUT / "sol-active.lock").exists()


def test_sentence_window_explicit_ceiling_is_shared_and_completed_result_is_reused(
    recovery_plan, settings, monkeypatch,
):
    prepare_sentence_window(settings, monkeypatch, recovery_plan)
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            assert request.model == "gpt-6.1-sol"
            return ProviderResponse(output={"decisions": [{
                "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                "classification": "core_foundation",
                "support_id": json.loads(s["support_options"])[0]["support_id"],
                "reason": "手法を使用",
            } for s in request.context_spans]}, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(baseline, "OpenAIProvider", Provider)
    result = recovery.evaluate_sentences(15)
    assert result["status"] == "completed" and len(calls) == 2
    assert recovery.evaluate_sentences(15) == result and len(calls) == 2
    live_plan = json.loads((recovery.SENTENCE_OUTPUT / "live-plan.json").read_text())
    assert live_plan["authorized_total_budget_usd"] == 0.15


@pytest.mark.parametrize("timeout", [False, True])
def test_expanded_window_controls_and_prior_cost_stop_or_reuse(
    recovery_plan, settings, monkeypatch, timeout,
):
    root = settings.database_path.parent
    monkeypatch.setattr(recovery, "EXPANDED_OUTPUT", root / "expanded")
    monkeypatch.setattr(recovery, "EXPANDED_DATABASE", root / "expanded.db")
    monkeypatch.setattr(recovery, "EXPANDED_IDS", {
        "1706.03762": {"C0"}, "2106.09685": {"C0"},
    })
    monkeypatch.setattr(recovery, "expanded_prior_cost", lambda: 0.0749224)
    recovery.EXPANDED_OUTPUT.mkdir()
    (recovery.EXPANDED_OUTPUT / "pre-model-source-review.json").write_text('{}\n')
    (recovery.EXPANDED_OUTPUT / "prepared-plan.json").write_text(
        json.dumps(recovery.expanded_plan(recovery_plan))
    )
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            if timeout:
                raise TimeoutError()
            return ProviderResponse(output={"decisions": [{
                "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                "classification": None, "support_id": None, "reason": "不明",
            } for s in request.context_spans]}, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(baseline, "OpenAIProvider", Provider)
    assert recovery.evaluate_expanded()["reason"] == "explicit_shared_ceiling_required"
    assert not calls
    result = recovery.evaluate_expanded(15)
    assert result["status"] == ("stopped" if timeout else "completed")
    assert len(calls) == 2
    second = recovery.evaluate_expanded(15)
    assert len(calls) == 2
    if timeout:
        assert second["reason"] == "previous_run_requires_review"
        engine = make_engine(Settings(database_path=recovery.EXPANDED_DATABASE))
        try:
            with Session(engine) as session:
                executions = list(session.scalars(select(LLMExecution)))
                assert len(executions) == 2
                assert all(e.cost_status == "unknown" for e in executions)
                assert sum(e.reserved_usd for e in executions) == pytest.approx(0.072)
        finally:
            engine.dispose()
    else:
        assert second == result and all(c.model == "gpt-6.1-sol" for c in calls)


def test_recovery_endpoint_is_opt_in_local_and_summary_only(engine, settings, monkeypatch):
    path = "/verification/citation-context-recovery/{phase}"
    sentence_path = "/verification/citation-sentences"
    monkeypatch.delenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", raising=False)
    assert path not in {getattr(r, "path", "") for r in create_app(settings, engine).routes}
    assert sentence_path not in {
        getattr(r, "path", "") for r in create_app(settings, engine).routes
    }
    monkeypatch.setenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", "1")
    monkeypatch.setattr(recovery, "evaluate", lambda phase: {"status": "completed", "cases": [1]})
    ceilings = []

    def sentence_check(budget_cents):
        ceilings.append(budget_cents)
        return {"status": "blocked", "reason": "insufficient_shared_budget"}

    monkeypatch.setattr(recovery, "evaluate_sentences", sentence_check)
    monkeypatch.setattr(recovery, "evaluate_expanded", sentence_check)
    with TestClient(create_app(settings, engine)) as api:
        assert api.post(path.replace("{phase}", "sol")).json() == {
            "status": "completed", "reason": None, "cases_finished": 1,
        }
        assert api.post(path.replace("{phase}", "unknown")).status_code == 422
        assert api.post(sentence_path).json()["reason"] == "insufficient_shared_budget"
        api.post(sentence_path + "?budget_cents=15")
        assert ceilings == [10, 15]
        api.post(sentence_path + "?scope=expanded&budget_cents=15")
        assert ceilings == [10, 15, 15]
        assert api.post(sentence_path + "?scope=unknown").status_code == 422
    with TestClient(create_app(settings, engine), client=("192.0.2.1", 1234)) as api:
        assert api.post(path.replace("{phase}", "sol")).status_code == 403
        assert api.post(sentence_path).status_code == 403
        assert ceilings == [10, 15, 15]


def prepare_bundle(settings, monkeypatch, cases):
    root = settings.database_path.parent
    monkeypatch.setattr(bundle, "OUTPUT", root / "bundle")
    monkeypatch.setattr(bundle, "DATABASE", root / "bundle.db")
    monkeypatch.setattr(bundle, "prior_cost", lambda: 0.0988284)
    bundle.OUTPUT.mkdir()
    (bundle.OUTPUT / "prepared-plan.json").write_text(json.dumps(bundle.plan(cases)))


def test_bundle_classification_reuses_result_and_never_enables_lineage(
    recovery_plan, settings, monkeypatch,
):
    prepare_bundle(settings, monkeypatch, recovery_plan)
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            return ProviderResponse(output={"decisions": [{
                "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                "classification": None, "support_id": None, "reason": "根拠不足",
            } for s in request.context_spans]}, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(baseline, "OpenAIProvider", Provider)
    assert bundle.evaluate("classify")["reason"] == "explicit_bundle_budget_required"
    assert not calls
    result = bundle.evaluate("classify", 50)
    assert result["status"] == "completed" and len(calls) == 2
    assert json.loads((bundle.OUTPUT / "classify-plan.json").read_text())[
        "authorized_total_budget_usd"
    ] == 0.50
    assert bundle.evaluate("classify", 50) == result and len(calls) == 2
    assert bundle.evaluate("lineage", 50)["reason"] == "scoped_stage1_and_live_s2_review_required"
    assert settings.stage1_audited is False and settings.specter2_verified is False


def test_bundle_s2_failure_retains_stop_and_does_not_resend(
    recovery_plan, settings, monkeypatch,
):
    prepare_bundle(settings, monkeypatch, recovery_plan)
    stop = settings.database_path.parent / "stop.json"
    stop.write_text('{"status":429,"source":"prior-window"}\n')
    original = stop.read_bytes()
    monkeypatch.setattr(bundle.live_s2, "RUNTIME_LIMIT", stop)
    calls = []

    async def failed_connection(**kwargs):
        calls.append(kwargs)
        assert kwargs["output"] == bundle.OUTPUT
        return {"status": "stopped", "http_status": 429, "http_requests": 1, "reports": []}

    monkeypatch.setattr(bundle.live_s2, "_evaluate", failed_connection)
    assert bundle.evaluate("s2", 50)["http_status"] == 429
    assert bundle.evaluate("s2", 50)["reason"] == "previous_window_requires_review"
    assert len(calls) == 1 and stop.read_bytes() == original
    assert (bundle.OUTPUT / "prior-runtime-stop.json").read_bytes() == original


def test_bundle_lineage_rejects_unbound_review_and_incomplete_contract(
    recovery_plan, settings, monkeypatch,
):
    prepare_bundle(settings, monkeypatch, recovery_plan)
    p = bundle.OUTPUT / "classify-results.json"
    p.write_text('{"status":"completed","cases":[]}\n')
    (bundle.OUTPUT / "stage1-review.json").write_text(json.dumps({
        "accepted_for_lineage_diagnostic": True, "classification_sha256": "wrong",
    }))
    s2 = bundle.OUTPUT / "s2-results.json"
    s2.write_text(json.dumps({"reports": [{"target": "2106.09685",
                                         "gate": "READY FOR QUALITY AUDIT"}]}))
    assert bundle.evaluate("lineage", 50)["status"] == "blocked"
    (bundle.OUTPUT / "stage1-review.json").write_text(json.dumps({
        "accepted_for_lineage_diagnostic": True,
        "classification_sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
    }))
    s2.write_text(json.dumps({"reports": [{"target": "2106.09685"}]}))
    assert bundle.evaluate("lineage", 50)["status"] == "blocked"
    assert not bundle.DATABASE.exists()


def test_bundle_operator_route_is_local_opt_in_and_uses_explicit_ceiling(
    engine, settings, monkeypatch,
):
    path = "/verification/roadmap/{phase}"
    monkeypatch.delenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", raising=False)
    assert path not in {getattr(r, "path", "") for r in create_app(settings, engine).routes}
    monkeypatch.setenv("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS", "1")
    calls = []

    def evaluate(phase, budget_cents):
        calls.append((phase, budget_cents))
        return {"status": "blocked", "reason": "test"}

    monkeypatch.setattr(bundle, "run_operator", evaluate)
    with TestClient(create_app(settings, engine)) as api:
        assert api.post(path.replace("{phase}", "s2")).json()["reason"] == "test"
        api.post(path.replace("{phase}", "classify") + "?budget_cents=50")
    assert calls == [("s2", 15), ("classify", 50)]
    with TestClient(create_app(settings, engine), client=("192.0.2.1", 1234)) as api:
        assert api.post(path.replace("{phase}", "s2")).status_code == 403
    assert len(calls) == 2


def test_bundle_fixed_child_runner_never_puts_keys_in_arguments_or_echoes_stderr(monkeypatch):
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command[1:3] == ["-m", "paper_review.application.roadmap_verification"]
        assert command[3:] == ["classify", "--budget-cents", "50"]
        assert "env" not in kwargs and "shell" not in kwargs
        assert kwargs["capture_output"] is True
        return SimpleNamespace(returncode=0, stdout='{"status":"completed","cases_finished":2}\n',
                               stderr="dummy-secret private source")

    monkeypatch.setattr(bundle.subprocess, "run", run)
    assert bundle.run_operator("classify")["reason"] == "explicit_bundle_budget_required"
    assert bundle.run_operator("unknown", 50)["reason"] == "unknown_phase"
    assert not calls
    result = bundle.run_operator("classify", 50)
    assert result["status"] == "completed" and result["cases_finished"] == 2
    assert "dummy-secret" not in str(result) and "private source" not in str(result)


def test_bundle_lineage_adapter_uses_reviewed_sources_and_shared_validator(
    recovery_plan, settings, monkeypatch,
):
    prepare_bundle(settings, monkeypatch, recovery_plan)
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request.task_type)
            evidence = [{"span_id": request.context_spans[0]["span_id"]}]
            if request.task_type == "citation_classification":
                output = {"decisions": [{
                    "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                    "classification": None, "support_id": None, "reason": "不明",
                } for s in request.context_spans]}
            elif request.task_type == "contribution_extraction":
                output = {"problem": "問題", "problem_evidence": evidence, "contributions": [
                    {"id": "c", "description": "手法", "evidence": evidence},
                ]}
            elif request.task_type == "contribution_review":
                items = json.loads(request.context_spans[-1]["text"])["items"]
                output = {"items": [{"item_id": item["item_id"], "claims": [{
                    "text": item["summary"], "decision": "supported",
                    "evidence_span_ids": item["selected_source_ids"], "reason": "引用で支持",
                }]} for item in items]}
            elif request.task_type == "contribution_relevance":
                output = {"evaluations": [{"candidate_id": "candidate", "score": 4,
                    "contribution_ids": ["c"], "reason": "手法を使用",
                    "identifying_evidence": evidence, "evidence": evidence}]}
            elif request.task_type == "lineage_edge_review":
                items = json.loads(request.context_spans[-1]["text"])["edges"]
                output = {"edges": [{"edge_id": item["edge_id"], "claims": [{
                    "text": item["relationship"], "decision": "supported",
                    "evidence_span_ids": item["selected_source_ids"], "reason": "原文で支持",
                }]} for item in items]}
            else:
                assert request.task_type == "lineage"
                output = {"predecessor_ids": ["candidate"], "merge_reason": None,
                          "merge_support": None,
                    "missing_reason": None, "edges": [{"from_paper_id": "candidate",
                    "to_paper_id": "target", "relationship": "手法を使用",
                    "relationship_score": 3, "evidence_confidence": 1.0, "evidence": evidence}]}
            return ProviderResponse(output=output, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(baseline, "OpenAIProvider", Provider)
    monkeypatch.setattr(bundle, "OpenAIProvider", Provider)
    assert bundle.evaluate("classify", 50)["status"] == "completed"
    classification = bundle.OUTPUT / "classify-results.json"
    (bundle.OUTPUT / "stage1-review.json").write_text(json.dumps({
        "accepted_for_lineage_diagnostic": True,
        "classification_sha256": hashlib.sha256(classification.read_bytes()).hexdigest(),
    }))
    (bundle.OUTPUT / "s2-results.json").write_text(json.dumps({"reports": [
        {"target": "2106.09685", "gate": "READY FOR QUALITY AUDIT"},
    ]}))

    async def synthetic_replay(case):
        target = Paper.model_validate(case["target"])
        target.published_at = "2020-01-01"
        target.embedding, target.embedding_model = [1., 0.], "specter2@test"
        inv = CandidateSet.model_validate(case["inventory"])
        cand = inv.candidates[0]
        cand.embedding, cand.embedding_model = [1., 0.], "specter2@test"
        cand.year = 2010
        cand.date_provenance = DateProvenance(
            kind="publication_year", value="2010", precision="year", source_id="synthetic-test",
        )
        return Stage1Analysis(
            target=target, inventory=inv, ranking=rank_stage1_candidates(target, inv),
        )

    monkeypatch.setattr(bundle, "replay", synthetic_replay)
    result = bundle.evaluate("lineage", 50)
    assert result["status"] == "completed" and result["scope"] == "reviewed_diagnostic_only"
    assert result["lineage"]["edges"][0]["confidence"] == pytest.approx(0.85)
    assert calls[-5:] == [
        "contribution_extraction", "contribution_review", "contribution_relevance", "lineage",
        "lineage_edge_review",
    ]
    assert bundle.evaluate("lineage", 50) == result and len(calls) == 7
    assert settings.stage1_audited is False and settings.specter2_verified is False
    before = len(calls)
    continuation = asyncio.run(bundle._lineage(
        recovery_plan, database=settings.database_path.parent / "continued.db",
        prior_usage=lambda: 0.1, version="continued-reviewed-checkpoint",
        accepted_contributions=result["lineage"],
    ))
    assert continuation["reused_reviewed_contributions"] is True
    assert len(calls) == before + 3
    assert calls[-3:] == ["contribution_relevance", "lineage", "lineage_edge_review"]
    assert continuation["lineage"]["contributions"] == result["lineage"]["contributions"]
