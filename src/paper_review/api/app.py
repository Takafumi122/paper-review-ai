import hashlib
import os
import secrets
import time
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from paper_review.config import LINEAGE_VALIDATION_VERSION, ROOT, Settings
from paper_review.domain.jobs import TERMINAL, arxiv_identity
from paper_review.domain.stage1_scoring import DEFAULT_SELECTION_POLICY
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    LLMExecution,
    Paper,
    StageExecution,
)
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.provider import ROUTING


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arxiv_url: str

    @field_validator("arxiv_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        arxiv_identity(value)
        return value


class AcceptedAnalysis(BaseModel):
    job_id: str
    paper_id: str


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    config = settings or Settings.from_environment()
    db = engine if engine is not None else make_engine(config)
    app = FastAPI(title="Paper Review AI")
    bearer = HTTPBearer(auto_error=False)
    access_token = os.environ.get("PAPER_REVIEW_ACCESS_TOKEN")

    def authorize(
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    ) -> None:
        if access_token and (
            credentials is None or not secrets.compare_digest(credentials.credentials, access_token)
        ):
            raise HTTPException(401, "アクセスコードを確認してください")

    protected = [Depends(authorize)]

    @app.get("/health")
    def health() -> dict[str, Any]:
        with db.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        if config.pipeline_mode == "phase0_fake":
            return {"status": "ok", "pipeline": "fake"}
        return {
            "status": "ok",
            "pipeline": config.pipeline_mode,
            "version": "0.2",
            "pipeline_version": "full-v44" if config.pipeline_mode == "full" else "phase1-v1",
            "llm_diagnostics_version": "attempt-timing-v1",
            "s2_request_gate_version": "shared-file-v1",
            "stage1_selection_policy_version": DEFAULT_SELECTION_POLICY,
            "lineage_validation_version": LINEAGE_VALIDATION_VERSION,
            "reading_only": config.reading_only,
            "candidate_retrieval": {
                "enabled": config.pipeline_mode == "full" and not config.reading_only,
                "backend": config.academic_backend,
            },
            "academic_backend": config.academic_backend,
            "retrieval_configuration": config.retrieval_configuration(),
            "openalex_local_readiness": {
                "verified": config.openalex_local_verified,
                "stage1_audited": config.openalex_local_audited,
            },
            "limits": {"initial_analysis_budget_usd": config.initial_budget_usd},
            "lineage_readiness": {
                "specter2_verified": config.specter2_verified,
                "stage1_audited": config.stage1_audited,
            },
            **(
                {
                    "models": {
                        task: ROUTING[task].model
                        for task in (
                            "reasoning",
                            "citation_classification",
                            "contribution_extraction",
                            "contribution_review",
                            "contribution_relevance",
                            "lineage",
                            "lineage_edge_review",
                            "lineage_merge_review",
                            "reasoning_review",
                            "short_knowledge",
                            "knowledge_detail",
                            "knowledge_detail_review",
                        )
                    }
                }
                if config.pipeline_mode == "full"
                else {}
            ),
        }

    @app.post("/analyses", status_code=202, response_model=AcceptedAnalysis, dependencies=protected)
    def submit(body: AnalysisInput) -> AcceptedAnalysis:
        paper_id, revision = arxiv_identity(body.arxiv_url)
        fingerprint = config.analysis_fingerprint(revision)
        with transaction(db) as session:
            existing = session.scalar(
                select(AnalysisJob).where(
                    AnalysisJob.fingerprint == fingerprint, AnalysisJob.status.not_in(TERMINAL)
                )
            )
            if existing is not None:
                return AcceptedAnalysis(job_id=existing.id, paper_id=paper_id)
            active_count = (
                session.scalar(
                    select(func.count(AnalysisJob.id)).where(AnalysisJob.status.not_in(TERMINAL))
                )
                or 0
            )
            today_count = (
                session.scalar(
                    select(func.count(AnalysisJob.id)).where(
                        AnalysisJob.created_at >= time.time() - 86400
                    )
                )
                or 0
            )
            if active_count >= config.max_queued_jobs or today_count >= config.max_daily_jobs:
                raise HTTPException(429, "現在の受付上限に達しています。時間をおいてお試しください")
            if config.pipeline_mode == "full" and config.provider != "openai":
                raise HTTPException(503, "実LLMの設定が必要です")
            if session.get(Paper, paper_id) is None:
                session.add(
                    Paper(
                        id=paper_id,
                        external_ids={
                            "arxiv": paper_id.removeprefix("arxiv:"),
                            "doi": None,
                            "semantic_scholar": None,
                            "openalex": None,
                        },
                    )
                )
                session.flush()
            job = AnalysisJob(
                paper_id=paper_id,
                arxiv_revision=revision,
                fingerprint=fingerprint,
                pipeline_mode=config.pipeline_mode,
            )
            session.add(job)
            session.flush()
            if config.pipeline_mode == "full":
                session.add(StageExecution(
                    job_id=job.id, name="retrieval_configuration", input_hash=fingerprint,
                    status="completed", checkpoint=config.retrieval_configuration(),
                ))
            return AcceptedAnalysis(job_id=job.id, paper_id=paper_id)

    @app.get("/analyses/{job_id}", dependencies=protected)
    def get_analysis(job_id: str) -> dict[str, Any]:
        with Session(db) as session:
            job = session.get(AnalysisJob, job_id)
            if job is None:
                raise HTTPException(404, "解析Jobが見つかりません")
            executions = list(
                session.scalars(select(LLMExecution).where(LLMExecution.job_id == job_id))
            )
            paper_executions = list(
                session.scalars(select(LLMExecution).where(LLMExecution.paper_id == job.paper_id))
            )
            end = job.finished_at or time.time()
            result = job.result
            features = job.feature_status
            lineage = (result or {}).get("lineage")
            if isinstance(lineage, dict):
                # Keep archived proposals and verdicts in the DB for audit.
                public_lineage = dict(lineage)
                public_lineage.pop("unverified_proposal", None)
                edge_review = public_lineage.pop("edge_review", None)
                if lineage.get("edges") and (
                    lineage.get("validation_version") != LINEAGE_VALIDATION_VERSION
                    or not isinstance(edge_review, dict)
                    or edge_review.get("supported") is not True
                ):
                    public_lineage.update(
                        status="insufficient_evidence", nodes=[], edges=[], merge_reason=None,
                        merge_support=None, merge_review=None,
                        missing_reason="先行研究固有の貢献を確認する審査が必要です。",
                    )
                    features = {**features, "lineage": "insufficient_evidence"}
                result = {**(result or {}), "lineage": public_lineage, "features": features}
            return {
                "job_id": job.id,
                "paper_id": job.paper_id,
                "status": job.status,
                "current_stage": job.current_stage,
                "feature_status": features,
                "result": result,
                "error_code": job.error_code,
                "pipeline": "fake" if job.pipeline_mode == "phase0_fake" else job.pipeline_mode,
                "missing_reasons": ["phase0_fake_no_paper_retrieval"]
                if job.pipeline_mode == "phase0_fake"
                else (job.result or {}).get("missing_reasons", []),
                "cost": {
                    "currency": "USD",
                    "simulated": job.pipeline_mode == "phase0_fake",
                    "estimated_usd": sum(e.estimated_cost_usd or 0 for e in executions),
                    "unsettled_reserved_usd": sum(
                        e.reserved_usd
                        for e in executions
                        if e.cost_status in {"reserved", "unknown"}
                    ),
                    "has_unknown_cost": any(e.cost_status == "unknown" for e in executions),
                    "paper_cumulative_estimated_usd": sum(
                        e.estimated_cost_usd or 0 for e in paper_executions
                    ),
                    "paper_has_unknown_cost": any(
                        e.cost_status == "unknown" for e in paper_executions
                    ),
                },
                "timing": {
                    "created_at": job.created_at,
                    "started_at": job.started_at,
                    "finished_at": job.finished_at,
                    "queue_wait_seconds": (job.started_at or end) - job.created_at,
                    "worker_seconds": end - job.started_at if job.started_at else None,
                    "total_seconds": end - job.created_at,
                },
            }

    @app.get("/analyses/{job_id}/lineage", dependencies=protected)
    def get_lineage(job_id: str) -> dict:
        result = get_analysis(job_id)["result"] or {}
        if "lineage" not in result:
            raise HTTPException(409, "研究系列の結果はまだ利用できません")
        return {**result["lineage"], "sources": result.get("sources", [])}

    @app.get("/analyses/{job_id}/reasoning", dependencies=protected)
    def get_reasoning(job_id: str) -> dict:
        result = get_analysis(job_id)["result"] or {}
        if "reasoning" not in result:
            raise HTTPException(409, "論理構造の結果はまだ利用できません")
        return {
            "reasoning": result["reasoning"],
            "knowledge": result.get("knowledge", {}),
            "sources": result.get("sources", []),
        }

    @app.post(
        "/analyses/{job_id}/knowledge/{item_id}/detail",
        status_code=202,
        response_model=AcceptedAnalysis,
        dependencies=protected,
    )
    def request_knowledge_detail(job_id: str, item_id: str) -> AcceptedAnalysis:
        with transaction(db) as session:
            parent = session.get(AnalysisJob, job_id)
            if parent is None:
                raise HTTPException(404, "解析Jobが見つかりません")
            items = (parent.result or {}).get("knowledge", {}).get("items", [])
            if not any(item.get("id") == item_id for item in items):
                raise HTTPException(404, "前提知識が見つかりません")
            fingerprint = hashlib.sha256(f"{job_id}|{item_id}|detail-v13".encode()).hexdigest()
            existing = session.scalar(
                select(AnalysisJob).where(
                    AnalysisJob.fingerprint == fingerprint, AnalysisJob.status != "failed"
                )
            )
            if existing is not None:
                return AcceptedAnalysis(job_id=existing.id, paper_id=parent.paper_id)
            count = (
                session.scalar(
                    select(func.count(AnalysisJob.id)).where(
                        AnalysisJob.created_at >= time.time() - 86400
                    )
                )
                or 0
            )
            if count >= config.max_daily_jobs:
                raise HTTPException(429, "本日の受付上限に達しています")
            active = (
                session.scalar(
                    select(func.count(AnalysisJob.id)).where(
                        AnalysisJob.status.not_in([s.value for s in TERMINAL])
                    )
                )
                or 0
            )
            if active >= config.max_queued_jobs:
                raise HTTPException(429, "受付が混み合っています")
            job = AnalysisJob(
                paper_id=parent.paper_id,
                arxiv_revision=parent.arxiv_revision,
                pipeline_mode="knowledge_detail",
                fingerprint=fingerprint,
            )
            session.add(job)
            session.flush()
            session.add(
                StageExecution(
                    job_id=job.id,
                    name="knowledge_detail_input",
                    input_hash=fingerprint,
                    status="completed",
                    checkpoint={"source_job_id": job_id, "item_id": item_id},
                )
            )
            return AcceptedAnalysis(job_id=job.id, paper_id=parent.paper_id)

    if os.environ.get("PAPER_REVIEW_ENABLE_PROVIDER_CHECKS") == "1":

        @app.post("/verification/specter2", dependencies=protected)
        def verify_provider(request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1",
                "::1",
                "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            from paper_review.application.provider_verification import verify_once

            return verify_once()

        @app.post("/verification/stage1", dependencies=protected)
        def verify_stage1(request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1",
                "::1",
                "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            from paper_review.application.stage1_verification import verify_stage1_once

            return verify_stage1_once()

        @app.post("/verification/knowledge", dependencies=protected)
        def verify_knowledge(request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1",
                "::1",
                "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            from paper_review.application.knowledge_quality_evaluation import evaluate

            result = evaluate()
            return {
                "status": result["status"],
                "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }

        @app.post("/verification/citation-contexts", dependencies=protected)
        def verify_citation_contexts(request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1", "::1", "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            from paper_review.application.citation_quality_evaluation import evaluate

            result = evaluate()
            return {
                "status": result["status"], "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }

        @app.post("/verification/citation-contexts/{phase}", dependencies=protected)
        def verify_citation_controls(phase: str, request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1", "::1", "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            if phase not in {"sol", "nano"}:
                raise HTTPException(422, "unknown_evaluation_phase")
            from paper_review.application.citation_quality_evaluation import evaluate

            result = evaluate(phase)
            return {
                "status": result["status"], "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }

        @app.post("/verification/citation-context-recovery/{phase}", dependencies=protected)
        def verify_citation_recovery(phase: str, request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1", "::1", "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            if phase not in {"sol", "nano"}:
                raise HTTPException(422, "unknown_evaluation_phase")
            from paper_review.application.citation_quality_recovery import evaluate

            result = evaluate(phase)
            return {
                "status": result["status"], "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }

        @app.post("/verification/citation-sentences", dependencies=protected)
        def verify_citation_sentences(
            request: Request, budget_cents: int = 10, scope: str = "controls",
        ) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1", "::1", "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            if scope not in {"controls", "expanded"}:
                raise HTTPException(422, "unknown_evaluation_scope")
            from paper_review.application.citation_quality_recovery import (
                evaluate_expanded,
                evaluate_sentences,
            )

            evaluator = evaluate_expanded if scope == "expanded" else evaluate_sentences
            result = evaluator(budget_cents)
            return {
                "status": result["status"], "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }

        @app.post("/verification/roadmap/{phase}", dependencies=protected)
        def verify_roadmap(request: Request, phase: str, budget_cents: int = 15) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1", "::1", "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            from paper_review.application.roadmap_verification import run_operator

            result = run_operator(phase, budget_cents)
            return {
                "status": result["status"], "reason": result.get("reason"),
                "http_status": result.get("http_status"),
                "cases_finished": result.get("cases_finished", 0),
            }

        @app.post("/verification/models/{phase}", dependencies=protected)
        def verify_models(phase: str, request: Request) -> dict:
            if request.client is None or request.client.host not in {
                "127.0.0.1",
                "::1",
                "testclient",
            }:
                raise HTTPException(403, "local_operator_only")
            if phase not in {"sol", "luna", "terra"}:
                raise HTTPException(422, "unknown_evaluation_phase")
            from paper_review.application.model_evaluation import evaluate

            result = evaluate(phase)
            return {
                "status": result["status"],
                "reason": result.get("reason"),
                "cases_finished": len(result.get("cases", [])),
            }

    dist = ROOT / "frontend/dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
    return app
