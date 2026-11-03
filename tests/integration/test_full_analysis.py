"""Three-feature composition with synthetic HTTP/LLM fixtures, never live readiness."""

import json
from collections import Counter
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.api.app import create_app
from paper_review.application import full_analysis
from paper_review.domain.jobs import JobStatus
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
    StageExecution,
)
from paper_review.llm.gateway import LLMGateway
from paper_review.llm.provider import ProviderResponse
from paper_review.worker import Worker

TARGET = "arxiv:1706.03762"
CANDIDATE = "b" * 40
QUOTE = "We use normalization [1] to address the problem."
EVIDENCE = [{"span_id": "section:p1:c0", "quote": QUOTE}]


class ScriptedProvider:
    def __init__(self):
        self.calls = Counter()

    async def generate(self, request):
        self.calls[request.task_type] += 1
        if request.task_type == "citation_classification":
            output = {"decisions": [
                {"context_id": s["span_id"], "candidate_id": s["candidate_id"],
                 "classification": "core_foundation",
                 "support_id": json.loads(s["support_options"])[0]["support_id"],
                 "reason": "原文が採用した手法を示す"}
                for s in request.context_spans
            ]}
        elif request.task_type == "contribution_extraction":
            output = {
                "problem": "制約", "problem_evidence": EVIDENCE,
                "contributions": [{"id": "c", "description": "方法", "evidence": EVIDENCE}],
            }
        elif request.task_type == "contribution_review":
            task = json.loads(request.context_spans[-1]["text"])
            output = {"items": [{"item_id": item["item_id"], "claims": [{
                "text": item["summary"], "decision": "supported",
                "evidence_span_ids": item["selected_source_ids"], "reason": "引用で支持",
            }]} for item in task["items"]]}
        elif request.task_type == "contribution_relevance":
            output = {
                "evaluations": [
                    {
                        "candidate_id": CANDIDATE,
                        "score": 4,
                        "contribution_ids": ["c"],
                        "reason": "根拠",
                        "identifying_evidence": EVIDENCE, "evidence": EVIDENCE,
                    }
                ]
            }
        elif request.task_type == "lineage":
            output = {
                "predecessor_ids": [CANDIDATE],
                "merge_reason": None,
                "merge_support": None,
                "missing_reason": None,
                "edges": [
                    {
                        "from_paper_id": CANDIDATE,
                        "to_paper_id": TARGET,
                        "relationship": "制約を解決",
                        "relationship_score": 4,
                        "evidence_confidence": 1.0,
                        "evidence": EVIDENCE,
                    }
                ],
            }
        elif request.task_type == "reasoning":
            output = {
                "steps": [
                    {
                        "id": "a",
                        "kind": "problem",
                        "summary": "問題",
                        "reader_explanation": None,
                        "reader_explanation_evidence": [],
                        "evidence": EVIDENCE,
                    },
                    {
                        "id": "b",
                        "kind": "approach",
                        "summary": "方法",
                        "reader_explanation": None,
                        "reader_explanation_evidence": [],
                        "evidence": EVIDENCE,
                    },
                ],
                "edges": [{"from_step_id": "a", "to_step_id": "b", "relation": "leads_to"}],
                "limitations": [],
            }
        elif request.task_type == "reasoning_review":
            task = json.loads(
                next(s["text"] for s in request.context_spans if s["span_id"] == "task-data")
            )
            output = {
                "claims": [
                    {
                        "text": task["summary"],
                        "decision": "supported",
                        "evidence_span_ids": [request.context_spans[0]["span_id"]],
                        "reason": "引用で支持",
                    }
                ]
            }
        elif request.task_type == "knowledge_detail":
            output = {
                "mechanism": "詳しい説明",
                "transition": "推論ステップ間のつながり",
                "limitations": None,
                "evidence": EVIDENCE,
            }
        elif request.task_type == "knowledge_detail_review":
            output = {"decision": "supported", "reason": "元の根拠が仕組みとつながりを支持する"}
        else:
            output = {
                "items": [
                    {
                        "id": "k",
                        "concept": "正規化",
                        "from_step_id": "a",
                        "to_step_id": "b",
                        "necessity_score": 3,
                        "why_needed": "方法を理解するため",
                        "short_explanation": "文脈に対応する説明",
                        "evidence": EVIDENCE,
                    }
                ]
            }
        if request.generation_settings.get("evidence_binding") == "source-span-v2":

            def pointers(value: Any) -> Any:
                if isinstance(value, list):
                    return [pointers(item) for item in value]
                if isinstance(value, dict):
                    return {
                        k: (
                            [{"span_id": request.context_spans[0]["span_id"]} for e in v]
                            if k in {
                                "evidence", "reader_explanation_evidence", "problem_evidence",
                                "identifying_evidence",
                            }
                            else pointers(v)
                        )
                        for k, v in value.items()
                    }
                return value

            output = pointers(output)
            if request.generation_settings.get("edge_binding") == "flow-edge-v1":
                for item in output.get("items", []):
                    item.pop("from_step_id")
                    item.pop("to_step_id")
                    item["edge_id"] = "E0"
        return ProviderResponse(output=output, request_id="synthetic", returned_model=request.model)


def prepare(engine):
    with Session(engine) as session:
        session.add(
            Paper(
                id=TARGET,
                title="Target",
                authors=["Test"],
                published_at="2017-06-12",
                external_ids={"arxiv": "1706.03762"},
            )
        )
        session.flush()
        session.add(
            PaperRevision(
                id="revision",
                paper_id=TARGET,
                arxiv_version="v7",
                content_hash="synthetic",
                parser_version="test",
                page_count=2,
            )
        )
        session.flush()
        session.add(
            PaperSection(
                id="section",
                revision_id="revision",
                paper_id=TARGET,
                section_index=0,
                title="Introduction",
                text=QUOTE,
                page_start=1,
                page_end=1,
            )
        )
        session.add(
            Reference(
                id="reference",
                revision_id="revision",
                paper_id=TARGET,
                ref_order=1,
                raw_text="A Author. Normalization method. arXiv:1601.00001, 2016.",
                citation_key="[1]",
            )
        )
        job = AnalysisJob(
            paper_id=TARGET,
            arxiv_revision="1706.03762v7",
            fingerprint="test-full",
            pipeline_mode="full",
        )
        session.add(job)
        session.commit()
        return job.id


@pytest.mark.parametrize("http_status", [200, 429])
def test_full_worker_success_and_s2_failure_preserve_independent_features(
    engine, settings, monkeypatch, http_status
):
    settings = settings.model_copy(
        update={"pipeline_mode": "full", "specter2_verified": True, "stage1_audited": True}
    )
    monkeypatch.setattr(full_analysis, "ROOT", settings.database_path.parent)
    # Acquire the existing repaired domain inventory using realistic metadata,
    # then explicit synthetic S2 vectors. These flags do not certify live data.
    calls = []
    metadata = {
        "paperId": CANDIDATE,
        "title": "Normalization method",
        "authors": [{"name": "A Author"}],
        "year": 2016,
        "publicationDate": "2016-01-01",
        "externalIds": {"ArXiv": "1601.00001"},
    }

    def handler(request):
        calls.append(request)
        if http_status == 429:
            return httpx.Response(429, json={})
        if "embedding.specter_v2" in request.url.params.get("fields", ""):
            return httpx.Response(
                200,
                json=[
                    {
                        "paperId": "a" * 40,
                        "title": "Target",
                        "embedding": {"model": "specter_v2", "vector": [1.0, 0.0]},
                    },
                    {**metadata, "embedding": {"model": "specter_v2", "vector": [0.0, 1.0]}},
                ],
            )
        if request.method == "POST":
            return httpx.Response(200, json=[metadata])
        return httpx.Response(200, json={"data": [{"citedPaper": metadata, "isInfluential": True}]})

    def client(**kwargs):
        return SemanticScholarClient(
            transport=httpx.MockTransport(handler),
            api_key="dummy",
            rate_limiter=RateLimiter(0),
            **kwargs,
        )

    monkeypatch.setattr(full_analysis, "SemanticScholarClient", client)
    jid = prepare(engine)
    provider = ScriptedProvider()
    worker = Worker(
        engine, settings, gateway=LLMGateway(engine, settings, provider), pipeline_mode="full"
    )
    assert worker.run_once() == jid
    with Session(engine) as session:
        job = session.get(AnalysisJob, jid)
        assert job is not None and job.result
        assert job.status == (JobStatus.COMPLETED if http_status == 200 else JobStatus.PARTIAL)
        assert job.feature_status["reasoning"] == job.feature_status["knowledge"] == "completed"
        assert job.result["paper"]["retrieval_source"] == "parsed_cache"
        assert len(job.result["reasoning"]["steps"]) == 2
        if http_status == 200:
            audit = job.result["candidate_ranking"]["citation_context_audit"]
            assert audit["contexts_completed"] == 1
            assert audit["contexts"][0]["source_ids"] == ["section:p1:c0"]
            assert provider.calls["citation_classification"] == 1
        assert job.result["knowledge"]["items"][0]["necessity_score"] == 3
        if http_status == 200:
            assert job.feature_status["lineage"] == "completed"
            assert job.result["lineage"]["edges"][0]["status"] == "supported"
            assert (
                job.result["lineage"]["final_ranking"][0]["score"]["adjustment_policy_version"]
                == "identity-v1"
            )
        else:
            assert job.feature_status["lineage"] == "blocked"
            assert provider.calls["lineage"] == 0
            assert len(calls) == 1  # stop S2 work immediately after the first 429
        stages = list(session.scalars(select(StageExecution).where(StageExecution.job_id == jid)))
        assert len(stages) == 8 and all(s.checkpoint is not None for s in stages)
    with TestClient(create_app(settings, engine)) as api:
        assert api.get("/health").json()["pipeline_version"] == "full-v35"
        assert api.get("/health").json()["stage1_selection_policy_version"] == "lower-bound-v1"
        assert api.get("/health").json()["models"]["contribution_extraction"] == "gpt-6.1-sol"
        assert api.get("/health").json()["models"]["contribution_review"] == "gpt-6.1-sol"
        assert api.get("/health").json()["models"]["citation_classification"] == "gpt-6.1-sol"
        assert api.get("/health").json()["models"]["short_knowledge"] == "gpt-6.1-sol"
        assert api.get("/health").json()["models"]["knowledge_detail"] == "gpt-6.1-terra"
        assert api.get(f"/analyses/{jid}").json()["pipeline"] == "full"
        assert api.get(f"/analyses/{jid}/reasoning").status_code == 200
        assert api.get(f"/analyses/{jid}/lineage").status_code == 200


def test_detail_job_is_durable_deduplicated_and_separately_costed(engine, settings, monkeypatch):
    test_full_worker_success_and_s2_failure_preserve_independent_features(
        engine, settings, monkeypatch, 429
    )
    with Session(engine) as session:
        parent = session.scalar(select(AnalysisJob).where(AnalysisJob.fingerprint == "test-full"))
        assert parent is not None
        parent_id = parent.id
    config = settings.model_copy(update={"pipeline_mode": "full"})
    monkeypatch.setenv("PAPER_REVIEW_ACCESS_TOKEN", "dummy-invite")
    headers = {"Authorization": "Bearer dummy-invite"}
    path = f"/analyses/{parent_id}/knowledge/k/detail"
    with TestClient(create_app(config, engine)) as api:
        assert api.post(path).status_code == 401
        assert api.get(f"/analyses/{parent_id}").status_code == 401
        accepted = api.post(path, headers=headers)
        assert accepted.status_code == 202
        detail_id = accepted.json()["job_id"]
        assert api.post(path, headers=headers).json()["job_id"] == detail_id
        assert api.post(path.replace("/k/", "/unknown/"), headers=headers).status_code == 404
        provider = ScriptedProvider()
        worker = Worker(
            engine, config, gateway=LLMGateway(engine, config, provider), pipeline_mode="full"
        )
        assert worker.run_once() == detail_id
        data = api.get(f"/analyses/{detail_id}", headers=headers).json()
        assert data["status"] == "completed"
        assert "詳しい説明" in data["result"]["detail"]["explanation"]
        assert "推論ステップ間のつながり" in data["result"]["detail"]["explanation"]
        assert api.post(path, headers=headers).json()["job_id"] == detail_id
        assert provider.calls == {"knowledge_detail": 1, "knowledge_detail_review": 1}
    from paper_review.infrastructure.database.models import LLMExecution

    with Session(engine) as session:
        attempts = list(
            session.scalars(select(LLMExecution).where(LLMExecution.job_id == detail_id))
        )
        assert len(attempts) == 2 and all(a.cost_category == "jit_detail" for a in attempts)
        assert {a.metadata_json["requested_model"] for a in attempts} == {
            "gpt-6.1-terra",
            "gpt-6.1-sol",
        }
        assert attempts[0].metadata_json["source_ids"] == ["section:p1:c0"]
        parent = session.get(AnalysisJob, parent_id)
        assert parent is not None and parent.result and parent.result["reasoning"]


@pytest.mark.parametrize("decision", ["unsupported", "uncertain", "wrong_scope"])
def test_reasoning_rejection_preserves_lineage_and_never_generates_knowledge(
    engine, settings, monkeypatch, decision
):
    import json

    from paper_review.infrastructure.database.models import LLMExecution

    test_full_worker_success_and_s2_failure_preserve_independent_features(
        engine, settings, monkeypatch, 200
    )
    config = settings.model_copy(
        update={
            "pipeline_mode": "full",
            "specter2_verified": True,
            "stage1_audited": True,
        }
    )
    with Session(engine) as session:
        job = AnalysisJob(
            paper_id=TARGET,
            arxiv_revision="1706.03762v7",
            fingerprint=f"rejected-{decision}",
            pipeline_mode="full",
        )
        session.add(job)
        session.commit()
        jid = job.id

    class RejectingProvider(ScriptedProvider):
        async def generate(self, request):
            if request.task_type != "reasoning_review":
                return await super().generate(request)
            self.calls[request.task_type] += 1
            # No neighboring step or source expansion is available to the judge.
            assert request.context_spans == (
                {"span_id": "section:p1:c0", "text": QUOTE},
                {
                    "span_id": "task-data",
                    "text": json.dumps(
                        {
                            "kind": "problem",
                            "summary": "問題",
                        },
                        ensure_ascii=False,
                    ),
                },
            )
            return ProviderResponse(
                output={
                    "claims": [
                        {
                            "text": "問題",
                            "decision": decision,
                            "evidence_span_ids": [],
                            "reason": "選択した引用では主張の範囲を確認できない",
                        }
                    ]
                },
                request_id="rejected",
                returned_model=request.model,
            )

    provider = RejectingProvider()
    worker = Worker(
        engine, config, gateway=LLMGateway(engine, config, provider), pipeline_mode="full"
    )
    assert worker.run_once() == jid
    assert provider.calls["reasoning"] == provider.calls["reasoning_review"] == 1
    assert provider.calls["short_knowledge"] == 0
    with Session(engine) as session:
        saved = session.get(AnalysisJob, jid)
        assert saved is not None and saved.result is not None
        assert saved.status == "partial_completed"
        assert saved.feature_status == {
            "lineage": "completed",
            "reasoning": "failed",
            "knowledge": "failed",
        }
        assert "reasoning" not in saved.result and "knowledge" not in saved.result
        assert "reasoning_evidence_not_supported" in saved.result["missing_reasons"]
        audit = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == jid,
                StageExecution.name == "reasoning_evidence_review_0_summary",
            )
        )
        assert audit is not None and audit.checkpoint is not None
        assert audit.checkpoint["supported"] is False
        attempts = list(
            session.scalars(
                select(LLMExecution).where(
                    LLMExecution.job_id == jid,
                    LLMExecution.logical_call_id == "reasoning-evidence-review-0-summary-v1",
                )
            )
        )
        assert len(attempts) == 1
        assert attempts[0].cost_category == "initial_analysis"
        assert attempts[0].metadata_json["requested_model"] == "gpt-6.1-sol"


@pytest.mark.parametrize("part", [None, "summary", "explanation"])
def test_reasoning_review_checkpoint_reuses_the_same_assessment(engine, settings, part):
    import asyncio

    from paper_review.application.reasoning_review import review_reasoning_step
    from paper_review.domain.understanding import ReasoningStep

    jid = prepare(engine)
    provider = ScriptedProvider()
    worker = Worker(
        engine, settings, gateway=LLMGateway(engine, settings, provider), pipeline_mode="full"
    )
    assert worker.claim() == jid
    step = ReasoningStep.model_validate(
        {
            "id": "a",
            "kind": "problem",
            "summary": "問題",
            "evidence": EVIDENCE,
        }
    )
    first = asyncio.run(review_reasoning_step(worker, jid, step, 0, part=part))
    again = asyncio.run(review_reasoning_step(worker, jid, step, 0, part=part))
    assert first == again and first.supported
    assert provider.calls == {"reasoning_review": 1}
    with pytest.raises(ValueError, match="input_changed"):
        asyncio.run(
            review_reasoning_step(
                worker, jid, step.model_copy(update={"summary": "別の主張"}), 0, part=part
            )
        )
    assert provider.calls == {"reasoning_review": 1}
    other = "explanation" if part != "explanation" else "summary"
    asyncio.run(review_reasoning_step(worker, jid, step, 0, part=other))
    asyncio.run(review_reasoning_step(worker, jid, step, 0, part=other))
    assert provider.calls == {"reasoning_review": 2}  # no cross-part cache borrowing


@pytest.mark.parametrize("accepted", [True, False])
def test_reader_explanation_is_reviewed_before_publication_or_knowledge(
    engine, settings, monkeypatch, accepted
):
    import json

    test_full_worker_success_and_s2_failure_preserve_independent_features(
        engine, settings, monkeypatch, 200
    )
    config = settings.model_copy(
        update={
            "pipeline_mode": "full",
            "specter2_verified": True,
            "stage1_audited": True,
        }
    )
    with Session(engine) as session:
        session.add(
            PaperSection(
                id="parallel-section",
                revision_id="revision",
                paper_id=TARGET,
                section_index=1,
                title="Attention",
                text="Queries are processed in parallel.",
                page_start=2,
                page_end=2,
            )
        )
        job = AnalysisJob(
            paper_id=TARGET,
            arxiv_revision="1706.03762v7",
            fingerprint=f"explanation-{accepted}",
            pipeline_mode="full",
        )
        session.add(job)
        session.commit()
        jid = job.id
    factual = "注意は並列計算される。"
    explanation = "注意の並列計算と正規化は別の操作として整理できる。"
    explanation_context = "対象は注意の並列計算と正規化である。"
    displayed_explanation = explanation_context + "\n" + explanation
    reviewed_text = []

    class Provider(ScriptedProvider):
        async def generate(self, request):
            if request.task_type == "reasoning_review":
                task = json.loads(
                    next(s["text"] for s in request.context_spans if s["span_id"] == "task-data")
                )
                reviewed_text.append(task["summary"])
                if "-explanation-" in request.logical_call_id:
                    ids = [
                        s["span_id"] for s in request.context_spans if s["span_id"] != "task-data"
                    ]
                    assert ids == (
                        ["parallel-section:p2:c0", "section:p1:c0"]
                        if accepted
                        else ["section:p1:c0"]
                    )
                    assert task["summary"] == displayed_explanation
                elif task["summary"] == factual:
                    assert request.context_spans[0]["text"] == "Queries are processed in parallel."
                if not accepted and "-explanation-" in request.logical_call_id:
                    self.calls[request.task_type] += 1
                    return ProviderResponse(
                        output={
                            "claims": [
                                {
                                    "text": task["summary"],
                                    "decision": "unsupported",
                                    "evidence_span_ids": [],
                                    "reason": "補足も選択した根拠で支える必要がある",
                                }
                            ]
                        },
                        request_id="mock-rejection",
                        returned_model=request.model,
                    )
            response = await super().generate(request)
            if request.task_type == "reasoning":
                parallel = next(
                    s["span_id"]
                    for s in request.context_spans
                    if s.get("section_id") == "parallel-section"
                )
                original = next(
                    s["span_id"] for s in request.context_spans if s.get("section_id") == "section"
                )
                step = response.output["steps"][0]
                step["summary"] = factual
                step["evidence"] = [{"span_id": parallel}]
                step["reader_explanation"] = explanation
                step["reader_explanation_context"] = explanation_context
                step["reader_explanation_evidence"] = (
                    [{"span_id": parallel}, {"span_id": original}]
                    if accepted
                    else [{"span_id": original}]
                )
            if request.task_type == "short_knowledge":
                task = json.loads(
                    next(s["text"] for s in request.context_spans if s["span_id"] == "task-data")
                )
                assert explanation in task["flow"]["steps"][0]["summary"]
            return response

    provider = Provider()
    worker = Worker(
        engine, config, gateway=LLMGateway(engine, config, provider), pipeline_mode="full"
    )
    assert worker.run_once() == jid
    assert reviewed_text[:2] == [factual, displayed_explanation]
    assert provider.calls["reasoning"] == 1
    with Session(engine) as session:
        job = session.get(AnalysisJob, jid)
        assert job is not None and job.result is not None
        if accepted:
            assert job.feature_status["reasoning"] == job.feature_status["knowledge"] == "completed"
            assert (
                job.result["reasoning"]["steps"][0]["summary"]
                == f"{factual}\n\n補足（引用に基づく解釈）\n{displayed_explanation}"
            )
            assert [e["span_id"] for e in job.result["reasoning"]["steps"][0]["evidence"]] == [
                "parallel-section:p2:c0",
                "section:p1:c0",
            ]
            assert provider.calls["short_knowledge"] == 1
        else:
            assert job.feature_status["lineage"] == "completed"
            assert "reasoning" not in job.result
            assert provider.calls["reasoning_review"] == 2
            assert provider.calls["short_knowledge"] == 0


def test_semantic_detail_rejection_is_terminal_and_never_published(engine, settings, monkeypatch):
    test_full_worker_success_and_s2_failure_preserve_independent_features(
        engine, settings, monkeypatch, 429
    )
    with Session(engine) as session:
        parent = session.scalar(select(AnalysisJob).where(AnalysisJob.fingerprint == "test-full"))
        assert parent is not None
        pid = parent.id
    config = settings.model_copy(update={"pipeline_mode": "full"})

    class RejectingProvider(ScriptedProvider):
        async def generate(self, request):
            if request.task_type == "knowledge_detail_review":
                self.calls[request.task_type] += 1
                return ProviderResponse(
                    output={"decision": "non_answer", "reason": "前置きのみ"},
                    request_id="rejected",
                    returned_model=request.model,
                )
            return await super().generate(request)

    provider = RejectingProvider()
    with TestClient(create_app(config, engine)) as api:
        jid = api.post(f"/analyses/{pid}/knowledge/k/detail").json()["job_id"]
        worker = Worker(
            engine, config, gateway=LLMGateway(engine, config, provider), pipeline_mode="full"
        )
        assert worker.run_once() == jid
        data = api.get(f"/analyses/{jid}").json()
        assert data["status"] == "failed" and data["result"] is None
        assert data["error_code"] == "DetailQualityFailure"
    assert provider.calls == {"knowledge_detail": 1, "knowledge_detail_review": 1}
    with Session(engine) as session:
        review = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == jid,
                StageExecution.name == "knowledge_detail_quality_review",
            )
        )
        assert review is not None and review.checkpoint is not None
        assert review.checkpoint["decision"] == "non_answer"
        parent = session.get(AnalysisJob, pid)
        assert parent is not None and parent.result and parent.result["reasoning"]


def test_long_flow_knowledge_omits_duplicate_quotes_and_fits_real_guard(
    engine, settings, monkeypatch
):
    import json

    from paper_review.llm.token_budget import estimate_input_tokens

    class LongFlowProvider(ScriptedProvider):
        def __init__(self):
            super().__init__()
            self.requests = []

        async def generate(self, request):
            self.requests.append(request)
            response = await super().generate(request)
            if request.task_type != "reasoning":
                return response
            ids = list("abcdefgh")
            return ProviderResponse(
                output={
                    "steps": [
                        {
                            "id": i,
                            "kind": "approach",
                            "reader_explanation": None,
                            "reader_explanation_evidence": [],
                            "summary": "手法の制約と次の段階の理解に必要な論理関係を説明します。"
                            * 3,
                            # Preserve a genuinely long flow after the public union
                            # deduplicates source IDs. Every step still reuses these
                            # quotes, exercising cross-step context deduplication.
                            "evidence": [
                                {"span_id": s["span_id"]} for s in request.context_spans[:2]
                            ],
                        }
                        for i in ids
                    ],
                    "edges": [
                        {"from_step_id": a, "to_step_id": b, "relation": "leads_to"}
                        for a, b in zip(ids, ids[1:], strict=False)
                    ],
                    "limitations": [],
                },
                request_id="synthetic-long-flow",
                returned_model=request.model,
            )

    config = settings.model_copy(update={"pipeline_mode": "full", "provider": "openai"})
    monkeypatch.setattr(full_analysis, "ROOT", settings.database_path.parent)
    monkeypatch.setattr(
        full_analysis,
        "SemanticScholarClient",
        lambda **kwargs: SemanticScholarClient(
            api_key="dummy",
            rate_limiter=RateLimiter(0),
            transport=httpx.MockTransport(lambda _: httpx.Response(429, json={})),
            **kwargs,
        ),
    )
    jid = prepare(engine)
    with Session(engine) as session:
        section = session.get(PaperSection, "section")
        assert section is not None
        section.text = QUOTE * 20
        session.commit()
    provider = LongFlowProvider()
    worker = Worker(
        engine, config, gateway=LLMGateway(engine, config, provider), pipeline_mode="full"
    )
    assert worker.run_once() == jid
    with Session(engine) as session:
        job = session.get(AnalysisJob, jid)
        assert job is not None and job.result is not None
        assert job.feature_status["knowledge"] == "completed"
        assert len(json.dumps(job.result["reasoning"], ensure_ascii=False).encode()) > 8000
    request = next(r for r in provider.requests if r.task_type == "short_knowledge")
    assert (
        estimate_input_tokens(
            request.output_schema,
            request.context_spans,
            request.generation_settings["instructions"],
        )
        <= request.max_input_tokens
    )
    task_data = json.loads(
        next(s["text"] for s in request.context_spans if s["span_id"] == "task-data")
    )
    assert all("evidence" not in step for step in task_data["flow"]["steps"])
    assert len(request.context_spans) > 1  # real source context remains, not just generated steps
