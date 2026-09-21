import time
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def identifier() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON)


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"))
    arxiv_revision: Mapped[str] = mapped_column(String)
    fingerprint: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="queued")
    current_stage: Mapped[str | None] = mapped_column(String)
    feature_status: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=lambda: {
            "lineage": "not_implemented",
            "reasoning": "not_implemented",
            "knowledge": "not_implemented",
        },
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String)
    owner: Mapped[str | None] = mapped_column(String)
    lease_until: Mapped[float | None] = mapped_column(Float)
    heartbeat_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)
    started_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        Index(
            "uq_active_fingerprint",
            "fingerprint",
            unique=True,
            sqlite_where=text("status NOT IN ('completed', 'failed', 'partial_completed')"),
        ),
    )


class StageExecution(Base):
    __tablename__ = "stage_executions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    job_id: Mapped[str] = mapped_column(ForeignKey("analysis_jobs.id"))
    name: Mapped[str] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, default="running")
    input_hash: Mapped[str] = mapped_column(String)
    checkpoint: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float)
    error_code: Mapped[str | None] = mapped_column(String)
    __table_args__ = (UniqueConstraint("job_id", "name"),)


class LLMExecution(Base):
    __tablename__ = "llm_executions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    job_id: Mapped[str] = mapped_column(ForeignKey("analysis_jobs.id"))
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"))
    stage_id: Mapped[str] = mapped_column(ForeignKey("stage_executions.id"))
    logical_call_id: Mapped[str] = mapped_column(String)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String, default="running")
    cost_status: Mapped[str] = mapped_column(String, default="reserved")
    reserved_usd: Mapped[float] = mapped_column(Float)
    estimated_cost_usd: Mapped[float | None] = mapped_column(Float)
    cost_category: Mapped[str] = mapped_column(String, default="initial_analysis")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String)
    started_at: Mapped[float] = mapped_column(Float, default=time.time)
    finished_at: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (UniqueConstraint("job_id", "logical_call_id", "attempt"),)
