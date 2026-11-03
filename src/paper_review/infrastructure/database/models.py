import time
from typing import Any
from uuid import uuid4

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def identifier() -> str:
    return str(uuid4())


class Base(DeclarativeBase):
    pass


class Paper(Base):
    __tablename__ = "papers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    authors: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)
    abstract: Mapped[str | None] = mapped_column(String, nullable=True)
    primary_category: Mapped[str | None] = mapped_column(String, nullable=True)
    categories: Mapped[list[str] | None] = mapped_column(JSON, default=list, nullable=True)
    published_at: Mapped[str | None] = mapped_column(String, nullable=True)
    published_date_precision: Mapped[str | None] = mapped_column(String, nullable=True)
    published_date_source: Mapped[str | None] = mapped_column(String, nullable=True)
    external_ids: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[float | None] = mapped_column(Float, default=time.time, nullable=True)
    updated_at: Mapped[float | None] = mapped_column(Float, default=time.time, nullable=True)

    revisions: Mapped[list["PaperRevision"]] = relationship(
        back_populates="paper", cascade="all, delete-orphan"
    )


class PaperRevision(Base):
    __tablename__ = "paper_revisions"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    arxiv_version: Mapped[str] = mapped_column(String, nullable=False)
    content_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    pdf_source_url: Mapped[str | None] = mapped_column(String, nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String, nullable=True)
    page_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    paper: Mapped["Paper"] = relationship(back_populates="revisions")
    sections: Mapped[list["PaperSection"]] = relationship(
        back_populates="revision",
        cascade="all, delete-orphan",
        order_by="PaperSection.section_index",
    )
    references: Mapped[list["Reference"]] = relationship(
        back_populates="revision", cascade="all, delete-orphan", order_by="Reference.ref_order"
    )

    __table_args__ = (
        UniqueConstraint("paper_id", "arxiv_version", name="uq_paper_revision"),
        Index("ix_paper_revisions_paper_id", "paper_id"),
    )


class PaperSection(Base):
    __tablename__ = "paper_sections"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("paper_revisions.id", ondelete="CASCADE"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    section_index: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str | None] = mapped_column(String, nullable=True)
    text: Mapped[str] = mapped_column(String, nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    span_info: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    extraction_quality: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    revision: Mapped["PaperRevision"] = relationship(back_populates="sections")

    __table_args__ = (
        UniqueConstraint("revision_id", "section_index", name="uq_revision_section_index"),
        Index("ix_paper_sections_revision_id", "revision_id"),
        Index("ix_paper_sections_paper_id", "paper_id"),
    )

    def __repr__(self) -> str:
        # Deliberately NEVER output section text in repr to prevent log leaks.
        return (
            f"<PaperSection id={self.id!r} paper_id={self.paper_id!r} "
            f"section_index={self.section_index} title={self.title!r} "
            f"pages={self.page_start}-{self.page_end}>"
        )


class Reference(Base):
    __tablename__ = "paper_references"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    revision_id: Mapped[str] = mapped_column(
        ForeignKey("paper_revisions.id", ondelete="CASCADE"), nullable=False
    )
    paper_id: Mapped[str] = mapped_column(
        ForeignKey("papers.id", ondelete="CASCADE"), nullable=False
    )
    ref_order: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_text: Mapped[str] = mapped_column(String, nullable=False)
    citation_key: Mapped[str | None] = mapped_column(String, nullable=True)
    matched_paper_id: Mapped[str | None] = mapped_column(
        ForeignKey("papers.id", ondelete="SET NULL"), nullable=True
    )
    matched_arxiv_id: Mapped[str | None] = mapped_column(String, nullable=True)
    matched_doi: Mapped[str | None] = mapped_column(String, nullable=True)
    resolution_status: Mapped[str] = mapped_column(String, default="unresolved")
    created_at: Mapped[float] = mapped_column(Float, default=time.time)

    revision: Mapped["PaperRevision"] = relationship(back_populates="references")

    __table_args__ = (
        UniqueConstraint("revision_id", "ref_order", name="uq_revision_ref_order"),
        Index("ix_paper_references_revision_id", "revision_id"),
        Index("ix_paper_references_paper_id", "paper_id"),
        Index("ix_paper_references_matched_paper_id", "matched_paper_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Reference id={self.id!r} paper_id={self.paper_id!r} "
            f"ref_order={self.ref_order} status={self.resolution_status!r}>"
        )


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True, default=identifier)
    paper_id: Mapped[str] = mapped_column(ForeignKey("papers.id"))
    arxiv_revision: Mapped[str] = mapped_column(String)
    pipeline_mode: Mapped[str] = mapped_column(
        String, default="phase0_fake", server_default="phase0_fake"
    )
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
