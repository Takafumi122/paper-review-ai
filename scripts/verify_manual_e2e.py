"""Manual real-network E2E verification script for Phase 1.

Performs a live pipeline run against arXiv for Attention Is All You Need (1706.03762):
  AnalysisJob -> Worker claim -> arXiv metadata retrieval -> Paper persistence
  -> temporary PDF download -> PDF parsing -> PaperRevision / PaperSection / Reference persistence
  -> temporary PDF cleanup -> AnalysisJob COMPLETED

Outputs concise structured verification metrics without dumping full paper text or references.
"""

from __future__ import annotations

import glob
import os
import sys
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from paper_review.api.app import create_app
from paper_review.config import Settings
from paper_review.domain.jobs import JobStatus
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
    StageExecution,
)
from paper_review.infrastructure.database.session import make_engine
from paper_review.worker import Worker


def run_verification() -> int:
    settings = Settings.from_environment()
    engine = make_engine(settings)

    print("=" * 70)
    print("PHASE 1 MANUAL REAL-ARXIV E2E VERIFICATION")
    print("Target: Attention Is All You Need (arXiv: 1706.03762)")
    print(f"Database: {settings.database_path}")
    print("=" * 70)

    # 1. Track temporary directory before execution
    temp_dir = tempfile.gettempdir()
    before_temp_files = set(glob.glob(os.path.join(temp_dir, "arxiv_*"))) | set(
        glob.glob(os.path.join(temp_dir, "paper_review_*"))
    )

    # 2. Submit AnalysisJob via FastAPI app
    app = create_app(settings, engine)
    with TestClient(app) as client:
        submit_res = client.post(
            "/analyses",
            json={"arxiv_url": "https://arxiv.org/abs/1706.03762"},
        )
        if submit_res.status_code != 202:
            print(f"[FAIL] Job submission failed: {submit_res.status_code} {submit_res.text}")
            return 1
        job_id = submit_res.json()["job_id"]
        paper_id = submit_res.json()["paper_id"]

    print("\n[1] Job Created:")
    print(f"    - Job ID: {job_id}")
    print(f"    - Paper ID: {paper_id}")

    # 3. Instantiate Phase 1 Worker and execute single job
    print("\n[2] Executing Phase 1 Worker (Real arXiv Network Mode)...")
    worker = Worker(engine, settings, pipeline_mode="phase1")
    claimed_id = worker.run_once()
    if claimed_id != job_id:
        print(f"[FAIL] Worker claimed unexpected job: {claimed_id} != {job_id}")
        return 1

    # 4. Verify temporary files cleanup
    after_temp_files = set(glob.glob(os.path.join(temp_dir, "arxiv_*"))) | set(
        glob.glob(os.path.join(temp_dir, "paper_review_*"))
    )
    leaked_files = after_temp_files - before_temp_files

    # 5. Verify DB Persistence & Job State
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        if job is None:
            print(f"[FAIL] Job {job_id} not found in DB")
            return 1

        stage = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == job_id, StageExecution.name == "parsing"
            )
        )

        paper = session.get(Paper, paper_id)
        if paper is None:
            print(f"[FAIL] Paper {paper_id} not found in DB")
            return 1

        revisions = list(
            session.scalars(
                select(PaperRevision)
                .where(PaperRevision.paper_id == paper_id)
                .order_by(PaperRevision.created_at.desc())
            )
        )
        if not revisions:
            print(f"[FAIL] No PaperRevision found for paper {paper_id}")
            return 1
        revision = revisions[0]

        sections = list(
            session.scalars(
                select(PaperSection)
                .where(PaperSection.revision_id == revision.id)
                .order_by(PaperSection.section_index)
            )
        )

        references = list(
            session.scalars(
                select(Reference)
                .where(Reference.revision_id == revision.id)
                .order_by(Reference.ref_order)
            )
        )

        total_papers = session.scalar(select(func.count(Paper.id)))
        total_revs = session.scalar(select(func.count(PaperRevision.id)))
        total_secs = session.scalar(select(func.count(PaperSection.id)))
        total_refs = session.scalar(select(func.count(Reference.id)))
        total_jobs = session.scalar(select(func.count(AnalysisJob.id)))

    print("\n[3] Execution & State Results:")
    print(f"    - Final Job Status: {job.status}")
    print(f"    - Current Stage: {job.current_stage}")
    print(f"    - Stage Status: {stage.status if stage else 'N/A'}")
    print(f"    - Error Code: {job.error_code}")
    print(f"    - Lease Released: {job.owner is None and job.lease_until is None}")

    author_sample = paper.authors[0] if paper.authors else ""
    print("\n[4] Paper & Metadata Verification:")
    print(f"    - Paper ID: {paper.id}")
    print(f"    - Title: {paper.title}")
    print(f"    - Authors: {len(paper.authors or [])} authors (e.g. {author_sample}...)")
    print(f"    - Primary Category: {paper.primary_category}")
    print(f"    - Categories: {paper.categories}")
    print(f"    - Published At: {paper.published_at} ({paper.published_date_source})")
    print(f"    - External IDs: {paper.external_ids}")

    print("\n[5] PaperRevision Verification:")
    print(f"    - Revision ID: {revision.id}")
    print(f"    - Resolved Version: {revision.arxiv_version}")
    print(f"    - Page Count: {revision.page_count}")
    print(f"    - Parser Version: {revision.parser_version}")
    print(f"    - PDF Source URL: {revision.pdf_source_url}")
    print(f"    - Content Hash (SHA-256): {revision.content_hash}")
    print(f"    - Content Hash Exists: {bool(revision.content_hash)}")

    print("\n[6] Document Structure & Provenance Verification:")
    print(f"    - Total Sections: {len(sections)}")
    print(f"    - Total References: {len(references)}")
    print("    - Section Headings (sample first 8):")
    for s in sections[:8]:
        info = f"pages {s.page_start}-{s.page_end}, {len(s.text)} chars"
        print(f"        [{s.section_index}] {s.title!r} ({info})")
    appendix_secs = [s for s in sections if s.span_info and s.span_info.get("is_appendix")]
    print(f"    - Appendix Sections Count: {len(appendix_secs)}")

    print("\n[7] Database Persistence & Foreign Key Integrity:")
    print(f"    - Table papers count: {total_papers}")
    print(f"    - Table paper_revisions count: {total_revs}")
    print(f"    - Table paper_sections count: {total_secs}")
    print(f"    - Table paper_references count: {total_refs}")
    print(f"    - Table analysis_jobs count: {total_jobs}")

    fk_errors = 0
    for s in sections:
        if s.revision_id != revision.id or s.paper_id != paper_id:
            fk_errors += 1
    for r in references:
        if r.revision_id != revision.id or r.paper_id != paper_id:
            fk_errors += 1
    print(f"    - Foreign Key Integrity Errors: {fk_errors}")

    print("\n[8] Resource Cleanup Verification:")
    print(f"    - Leaked Temporary Files: {len(leaked_files)}")
    if leaked_files:
        print(f"      Leaked: {leaked_files}")
    else:
        print("    - Temporary PDF cleanup verified: 0 leaked files")

    # Assertions
    success = (
        job.status == JobStatus.COMPLETED
        and revision.arxiv_version == "v7"
        and (revision.page_count or 0) >= 12
        and len(sections) >= 5
        and len(references) >= 20
        and bool(revision.content_hash)
        and fk_errors == 0
        and len(leaked_files) == 0
    )

    print("\n" + "=" * 70)
    if success:
        print("PHASE 1 MANUAL REAL-ARXIV E2E VERIFICATION: ALL CHECKS PASSED")
    else:
        print("PHASE 1 MANUAL REAL-ARXIV E2E VERIFICATION: FAILED")
    print("=" * 70)

    return 0 if success else 1


if __name__ == "__main__":
    sys.exit(run_verification())
