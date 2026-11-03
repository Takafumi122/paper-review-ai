"""Single-host persistent queue worker for pipeline execution.

Supports Phase 1 paper retrieval and parsing pipeline (arXiv metadata, temporary
PDF download, PDF parsing, DB persistence, and temporary PDF cleanup) while
preserving Phase 0 fake execution support for test compatibility.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import time
from contextlib import suppress
from typing import Any
from uuid import uuid4

from sqlalchemy import Engine, delete, func, or_, select

from paper_review.config import Settings
from paper_review.domain.jobs import TERMINAL, JobStatus, normalize_arxiv_id, transition
from paper_review.infrastructure.academic_api.arxiv import ArXivClient
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
    StageExecution,
)
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.infrastructure.observability import record
from paper_review.infrastructure.parsing.pdf_parser import PDFParserAdapter
from paper_review.llm.gateway import ExecutionFailure, LLMGateway
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.llm.provider import FakeLLMProvider, FakeOutput


class LeaseLost(RuntimeError):
    pass


class Worker:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        gateway: LLMGateway | None = None,
        arxiv_client: ArXivClient | None = None,
        pdf_parser: PDFParserAdapter | None = None,
        pipeline_mode: str | None = None,
    ) -> None:
        self.engine = engine
        self.settings = settings
        self.owner = str(uuid4())
        self.gateway = gateway or LLMGateway(
            engine,
            settings,
            OpenAIProvider() if settings.provider == "openai" else FakeLLMProvider(),
        )
        self.arxiv_client = arxiv_client or ArXivClient()
        self.pdf_parser = pdf_parser or PDFParserAdapter()

        if pipeline_mode is not None:
            self.pipeline_mode = pipeline_mode
        elif settings.pipeline_mode == "full":
            self.pipeline_mode = "full"
        elif gateway is not None and arxiv_client is None and pdf_parser is None:
            self.pipeline_mode = "phase0_fake"
        elif settings.provider == "fake" and arxiv_client is None and pdf_parser is None:
            self.pipeline_mode = settings.pipeline_mode
        else:
            self.pipeline_mode = "phase1"

    def claim(self) -> str | None:
        now = time.time()
        with transaction(self.engine) as session:
            # Single active job across worker processes on this host.
            active = session.scalar(
                select(AnalysisJob.id).where(
                    AnalysisJob.status.not_in(TERMINAL),
                    AnalysisJob.owner.is_not(None),
                    AnalysisJob.lease_until > now,
                )
            )
            if active is not None:
                return None
            job = session.scalar(
                select(AnalysisJob)
                .where(
                    AnalysisJob.status.not_in(TERMINAL),
                    (AnalysisJob.pipeline_mode.in_(["full", "knowledge_detail"]))
                    if self.pipeline_mode == "full"
                    else (AnalysisJob.pipeline_mode.not_in(["full", "knowledge_detail"])),
                    or_(AnalysisJob.owner.is_(None), AnalysisJob.lease_until <= now),
                )
                .order_by(AnalysisJob.created_at, AnalysisJob.id)
                .limit(1)
            )
            if job is None:
                return None
            job.owner = self.owner
            job.lease_until = now + self.settings.lease_seconds
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            if job.status == JobStatus.QUEUED:
                job.status = transition(JobStatus.QUEUED, JobStatus.PARSING)
            if self.pipeline_mode != "full" or job.current_stage is None:
                job.current_stage = "parsing"
            if self.pipeline_mode == "phase1":
                job.pipeline_mode = "phase1"
            stage = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job.id, StageExecution.name == "parsing"
                )
            )
            if stage is None:
                session.add(
                    StageExecution(job_id=job.id, name="parsing", input_hash=job.fingerprint)
                )
            job_id = job.id
        record("job_claimed", job_id=job_id, status="parsing")
        return job_id

    def heartbeat(self, job_id: str) -> None:
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            assert job is not None
            now = time.time()
            job.heartbeat_at = now
            job.lease_until = now + self.settings.lease_seconds

    def assert_owned(self, job: AnalysisJob | None) -> None:
        if (
            job is None
            or job.owner != self.owner
            or job.status in TERMINAL
            or job.lease_until is None
            or job.lease_until <= time.time()
        ):
            raise LeaseLost("worker lease lost")

    async def keep_lease(self, job_id: str) -> None:
        while True:
            await asyncio.sleep(self.settings.lease_seconds / 3)
            self.heartbeat(job_id)

    async def generate(self, job_id: str, stage_id: str) -> FakeOutput:
        heartbeat = asyncio.create_task(self.keep_lease(job_id))
        try:
            result = await self.gateway.execute(
                job_id=job_id,
                stage_id=stage_id,
                logical_call_id="fake-readiness-v1",
                task_type="contribution_extraction",
                output_schema=FakeOutput,
                owner=self.owner,
            )
            if heartbeat.done():
                heartbeat.result()
            return result
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    def execute_phase1_parsing(self, job_id: str, stage_id: str) -> dict[str, Any]:
        """Execute the Phase 1 end-to-end parsing pipeline:
        1. Fetch arXiv metadata
        2. Persist Paper entity
        3. Download temporary PDF
        4. Parse PDF into ParsedDocument
        5. Persist PaperRevision, PaperSection, and Reference entities
        6. Temporary PDF is automatically cleaned up on context exit
        """
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            assert job is not None
            paper_id = job.paper_id
            arxiv_revision = job.arxiv_revision

        self.heartbeat(job_id)

        # Public arXiv section cache is reusable. Preserve the actual cached
        # version/parser/hash and disclose that no fresh retrieval occurred.
        if self.pipeline_mode == "full":
            parsed_request = normalize_arxiv_id(arxiv_revision)
            with transaction(self.engine) as session:
                revisions = list(
                    session.scalars(select(PaperRevision).where(PaperRevision.paper_id == paper_id))
                )
                if parsed_request.version:
                    revisions = [r for r in revisions if r.arxiv_version == parsed_request.version]
                revisions.sort(key=lambda r: int(r.arxiv_version.removeprefix("v")), reverse=True)
                cached = revisions[0] if revisions else None
                paper = session.get(Paper, paper_id)
                if cached and paper and cached.content_hash and paper.title:
                    count = (
                        session.scalar(
                            select(func.count(PaperSection.id)).where(
                                PaperSection.revision_id == cached.id
                            )
                        )
                        or 0
                    )
                    if count:
                        refs = (
                            session.scalar(
                                select(func.count(Reference.id)).where(
                                    Reference.revision_id == cached.id
                                )
                            )
                            or 0
                        )
                        return {
                            "paper_id": paper_id,
                            "revision_id": cached.id,
                            "arxiv_version": cached.arxiv_version,
                            "page_count": cached.page_count,
                            "section_count": count,
                            "reference_count": refs,
                            "content_hash": cached.content_hash,
                            "parser_version": cached.parser_version,
                            "title": paper.title,
                            "extraction_quality": "cached",
                            "warnings": ["cached_version_not_refreshed"],
                            "retrieval_source": "parsed_cache",
                        }

        # 1. Fetch arXiv metadata
        parsed_id = normalize_arxiv_id(arxiv_revision)
        metadata = self.arxiv_client.get_metadata_sync(parsed_id.raw_revision)

        self.heartbeat(job_id)

        # 2. Persist Paper entity
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            paper = session.get(Paper, paper_id)
            if paper is None:
                paper = Paper(id=paper_id)
                session.add(paper)
            paper.title = metadata.title
            paper.authors = metadata.authors
            paper.abstract = metadata.abstract
            paper.primary_category = metadata.primary_category
            paper.categories = metadata.categories
            paper.published_at = (
                metadata.first_submitted_date[:10] if metadata.first_submitted_date else None
            )
            paper.published_date_precision = "day"
            paper.published_date_source = "arxiv_first_submitted"
            paper.external_ids = metadata.to_external_ids().to_dict()
            paper.updated_at = time.time()
            session.flush()

        record("paper_persisted", job_id=job_id, stage_id=stage_id, status="success")
        self.heartbeat(job_id)

        # 3. Download temporary PDF & 4. Parse PDF into ParsedDocument
        # Context manager guarantees cleanup upon exit.
        with self.arxiv_client.download_pdf_temp_sync(parsed_id.raw_revision) as temp_pdf_path:
            self.heartbeat(job_id)
            pdf_bytes = temp_pdf_path.read_bytes()
            content_hash = hashlib.sha256(pdf_bytes).hexdigest()
            parsed_doc = self.pdf_parser.parse(temp_pdf_path)
            self.heartbeat(job_id)

        record(
            "pdf_parsed",
            job_id=job_id,
            stage_id=stage_id,
            status=parsed_doc.extraction_quality,
        )

        # 5. Persist PaperRevision, PaperSection, and Reference entities
        revision_id = f"{paper_id}:{metadata.current_version}"
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)

            revision = session.get(PaperRevision, revision_id)
            if revision is None:
                revision = PaperRevision(
                    id=revision_id,
                    paper_id=paper_id,
                    arxiv_version=metadata.current_version,
                    content_hash=content_hash,
                    pdf_source_url=metadata.pdf_url,
                    parser_version=parsed_doc.parser_version,
                    page_count=parsed_doc.page_count,
                )
                session.add(revision)
            else:
                revision.content_hash = content_hash
                revision.pdf_source_url = metadata.pdf_url
                revision.parser_version = parsed_doc.parser_version
                revision.page_count = parsed_doc.page_count
            session.flush()

            # Clear existing sections and references for idempotent retry
            session.execute(delete(PaperSection).where(PaperSection.revision_id == revision_id))
            session.execute(delete(Reference).where(Reference.revision_id == revision_id))

            for sec in parsed_doc.sections:
                session.add(
                    PaperSection(
                        id=f"{revision_id}:sec:{sec.section_index}",
                        revision_id=revision_id,
                        paper_id=paper_id,
                        section_index=sec.section_index,
                        title=sec.title,
                        text=sec.text,
                        page_start=sec.page_start,
                        page_end=sec.page_end,
                        span_info={
                            "source_spans": [s.model_dump() for s in sec.source_spans],
                            "is_appendix": sec.is_appendix,
                        },
                        extraction_quality={"status": sec.extraction_quality},
                    )
                )

            for ref in parsed_doc.references:
                session.add(
                    Reference(
                        id=f"{revision_id}:ref:{ref.ref_order}",
                        revision_id=revision_id,
                        paper_id=paper_id,
                        ref_order=ref.ref_order,
                        raw_text=ref.raw_text,
                        citation_key=ref.citation_key,
                        resolution_status="unresolved",
                    )
                )

            session.flush()

        record(
            "sections_persisted",
            job_id=job_id,
            stage_id=stage_id,
            status="success",
        )

        return {
            "paper_id": paper_id,
            "revision_id": revision_id,
            "arxiv_version": metadata.current_version,
            "page_count": parsed_doc.page_count,
            "section_count": len(parsed_doc.sections),
            "reference_count": len(parsed_doc.references),
            "extraction_quality": parsed_doc.extraction_quality,
            "warnings": parsed_doc.warnings,
            "content_hash": content_hash,
            "title": metadata.title,
        }

    def process(self, job_id: str) -> None:
        if self.pipeline_mode == "full":
            from paper_review.application.full_analysis import run_full_analysis
            from paper_review.application.knowledge_detail import run_knowledge_detail

            with transaction(self.engine) as session:
                job = session.get(AnalysisJob, job_id)
                detail = job is not None and job.pipeline_mode == "knowledge_detail"
            asyncio.run(
                run_knowledge_detail(self, job_id) if detail else run_full_analysis(self, job_id)
            )
            return
        self.heartbeat(job_id)
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            stage = session.scalar(
                select(StageExecution).where(
                    StageExecution.job_id == job_id, StageExecution.name == "parsing"
                )
            )
            assert stage is not None
            stage_id = stage.id
            checkpoint = stage.checkpoint
        error = None
        if checkpoint is None:
            try:
                if self.pipeline_mode == "phase0_fake":
                    checkpoint = asyncio.run(self.generate(job_id, stage_id)).model_dump()
                else:
                    checkpoint = self.execute_phase1_parsing(job_id, stage_id)
            except (ExecutionFailure, Exception) as exc:
                error = str(exc)
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            self.assert_owned(job)
            assert job is not None
            stage = session.get(StageExecution, stage_id)
            assert stage is not None
            if stage.checkpoint is None:
                stage.checkpoint = checkpoint
                stage.attempt += 1
                stage.status = "failed" if error else "completed"
                stage.error_code = error
                stage.finished_at = time.time()
            if stage.checkpoint is not None:
                job.result = stage.checkpoint
            target = (
                JobStatus.PARTIAL
                if error and job.result
                else JobStatus.FAILED
                if error
                else JobStatus.COMPLETED
            )
            job.status = transition(JobStatus(job.status), target, fake=True)
            job.error_code = error
            job.finished_at = time.time()
            job.owner = None
            job.lease_until = None
            status = job.status
        record("job_finished", job_id=job_id, status=status, error_code=error)

    def run_once(self) -> str | None:
        job_id = self.claim()
        if job_id is not None:
            self.process(job_id)
        return job_id


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the paper review worker")
    parser.add_argument("--once", action="store_true")
    parser.add_argument(
        "--mode",
        choices=["phase0_fake", "phase1", "full"],
        default=os.environ.get("PAPER_REVIEW_PIPELINE_MODE", "phase0_fake"),
    )
    args = parser.parse_args()
    settings = Settings.from_environment()
    engine = make_engine(settings)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    worker = Worker(engine, settings, pipeline_mode=args.mode)
    try:
        if args.once:
            worker.run_once()
        else:
            while True:
                if worker.run_once() is None:
                    time.sleep(settings.poll_seconds)
    except KeyboardInterrupt:
        pass
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
