import hashlib
import time
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from paper_review.config import Settings
from paper_review.domain.jobs import TERMINAL, arxiv_identity
from paper_review.infrastructure.database.models import AnalysisJob, LLMExecution, Paper
from paper_review.infrastructure.database.session import make_engine, transaction


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
    app = FastAPI(title="Paper Understanding — Phase 0 fake")

    @app.get("/health")
    def health() -> dict[str, str]:
        with db.connect() as connection:
            connection.exec_driver_sql("SELECT 1")
        return {"status": "ok", "pipeline": "fake"}

    @app.post("/analyses", status_code=202, response_model=AcceptedAnalysis)
    def submit(body: AnalysisInput) -> AcceptedAnalysis:
        paper_id, revision = arxiv_identity(body.arxiv_url)
        fingerprint = hashlib.sha256(f"{revision}|fake-pipeline-v1".encode()).hexdigest()
        with transaction(db) as session:
            existing = session.scalar(
                select(AnalysisJob).where(
                    AnalysisJob.fingerprint == fingerprint, AnalysisJob.status.not_in(TERMINAL)
                )
            )
            if existing is not None:
                return AcceptedAnalysis(job_id=existing.id, paper_id=paper_id)
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
            job = AnalysisJob(paper_id=paper_id, arxiv_revision=revision, fingerprint=fingerprint)
            session.add(job)
            session.flush()
            return AcceptedAnalysis(job_id=job.id, paper_id=paper_id)

    @app.get("/analyses/{job_id}")
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
            return {
                "job_id": job.id,
                "paper_id": job.paper_id,
                "status": job.status,
                "current_stage": job.current_stage,
                "feature_status": job.feature_status,
                "result": job.result,
                "error_code": job.error_code,
                "pipeline": "fake",
                "missing_reasons": ["phase0_fake_no_paper_retrieval"],
                "cost": {
                    "currency": "USD",
                    "simulated": True,
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

    return app
