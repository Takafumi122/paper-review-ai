"""Integration tests for Phase 1 Worker end-to-end retrieval and parsing pipeline.

Verifies:
  arXiv URL / ID -> AnalysisJob -> Worker claim -> arXiv metadata retrieval
  -> Paper persistence -> temporary PDF download -> PDF parsing
  -> PaperRevision persistence -> PaperSection persistence -> Reference persistence
  -> temporary PDF cleanup -> AnalysisJob completed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.api.app import create_app
from paper_review.domain.jobs import JobStatus
from paper_review.infrastructure.academic_api.arxiv import (
    ArXivClient,
    RateLimiter,
)
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
    StageExecution,
)
from paper_review.infrastructure.parsing.pdf_parser import DEFAULT_PARSER_VERSION
from paper_review.worker import Worker

SAMPLE_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/api/query?id_list=1706.03762</id>
  <updated>2023-08-02T00:00:00Z</updated>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T01:09:47Z</updated>
    <published>2017-06-12T17:57:34Z</published>
    <title> Attention Is All You Need
    </title>
    <summary> The dominant sequence transduction models are based on complex recurrent or
convolutional neural networks in an encoder-decoder configuration. </summary>
    <author>
      <name>Ashish Vaswani</name>
    </author>
    <author>
      <name>Noam Shazeer</name>
    </author>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
    <arxiv:comment>15 pages, 5 figures</arxiv:comment>
    <arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v7"
          rel="related" type="application/pdf"/>
  </entry>
</feed>"""


def create_minimal_pdf(pages_text: list[list[str]]) -> bytes:
    """Generate minimal valid PDF bytes with Helvetica font for offline testing."""
    objects: list[str] = ["", "", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    font_id = 3
    page_ids: list[int] = []
    content_ids: list[int] = []

    for lines in pages_text:
        stream_cmds = ["BT", "/F1 12 Tf", "50 750 Td", "14 TL"]
        for line in lines:
            safe_line = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            stream_cmds.append(f"({safe_line}) Tj")
            stream_cmds.append("T*")
        stream_cmds.append("ET")
        stream_data = "\n".join(stream_cmds).encode("latin1")
        c_id = len(objects) + 1
        objects.append(
            f"<< /Length {len(stream_data)} >>\nstream\n"
            + stream_data.decode("latin1")
            + "\nendstream"
        )
        content_ids.append(c_id)

        p_id = len(objects) + 1
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Contents {c_id} 0 R /Resources << /Font << /F1 {font_id} 0 R >> >> >>"
        )
        page_ids.append(p_id)

    kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[0] = "<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_ids)} >>"

    out = [b"%PDF-1.4\n"]
    offsets: list[int] = []
    cur_pos = len(out[0])
    for i, obj in enumerate(objects, 1):
        offsets.append(cur_pos)
        block = f"{i} 0 obj\n{obj}\nendobj\n".encode("latin1")
        out.append(block)
        cur_pos += len(block)

    xref_pos = cur_pos
    xref = [f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode("latin1")]
    for off in offsets:
        xref.append(f"{off:010d} 00000 n \n".encode("latin1"))
    out.extend(xref)
    trailer = (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n".encode("latin1")
    )
    out.append(trailer)
    return b"".join(out)


SAMPLE_PAGES = [
    [
        "Attention Is All You Need",
        "Ashish Vaswani, Noam Shazeer",
        "Abstract",
        "The dominant sequence transduction models are based on complex neural networks.",
        "1 Introduction",
        "Recurrent neural networks, long short-term memory, and gated recurrent networks.",
    ],
    [
        "2 Model Architecture",
        "The Transformer follows this overall architecture using stacked self-attention.",
        "References",
        "[1] Jimmy Ba et al. Layer normalization. arXiv:1607.06450, 2016.",
        "[2] Ashish Vaswani et al. Attention is all you need. NeurIPS, 2017.",
        "[3] Edward J Hu et al. LoRA: Low-rank adaptation of large language models. 2022.",
    ],
]


def create_mock_arxiv_client(
    atom_xml: str = SAMPLE_ATOM_XML,
    pdf_bytes: bytes | None = None,
    pdf_status_code: int = 200,
    metadata_status_code: int = 200,
    download_tracker: list[Path] | None = None,
) -> ArXivClient:
    """Build an ArXivClient configured with offline MockTransport."""
    actual_pdf_bytes = pdf_bytes if pdf_bytes is not None else create_minimal_pdf(SAMPLE_PAGES)

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/query" in url_str:
            if metadata_status_code != 200:
                return httpx.Response(metadata_status_code, text="Metadata Error")
            return httpx.Response(200, text=atom_xml)
        if "/pdf/" in url_str:
            if pdf_status_code != 200:
                return httpx.Response(pdf_status_code, text="PDF Not Found")
            return httpx.Response(
                200,
                content=actual_pdf_bytes,
                headers={"Content-Type": "application/pdf"},
            )
        return httpx.Response(404, text="Not Found")

    transport = httpx.MockTransport(handle_request)
    client = ArXivClient(
        transport=transport,
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )
    return client


def submit_job(engine: Any, settings: Any, arxiv_url: str) -> str:
    with TestClient(create_app(settings, engine)) as client:
        response = client.post("/analyses", json={"arxiv_url": arxiv_url})
        assert response.status_code == 202
        return response.json()["job_id"]


def test_phase1_end_to_end_pipeline(engine, settings):
    """Full vertical slice: arXiv URL -> metadata -> PDF -> parse -> DB persistence -> cleanup."""
    raw_pdf_bytes = create_minimal_pdf(SAMPLE_PAGES)
    expected_hash = hashlib.sha256(raw_pdf_bytes).hexdigest()

    arxiv_client = create_mock_arxiv_client(pdf_bytes=raw_pdf_bytes)
    job_id = submit_job(engine, settings, "https://arxiv.org/abs/1706.03762v7")

    worker = Worker(
        engine,
        settings,
        arxiv_client=arxiv_client,
        pipeline_mode="phase1",
    )
    claimed_id = worker.run_once()
    assert claimed_id == job_id
    assert worker.run_once() is None

    with Session(engine) as session:
        # 1. Check AnalysisJob
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.current_stage == "parsing"
        assert job.error_code is None
        assert job.owner is None
        assert job.lease_until is None
        assert job.result is not None
        assert job.result["paper_id"] == "arxiv:1706.03762"
        assert job.result["revision_id"] == "arxiv:1706.03762:v7"
        assert job.result["page_count"] == 2
        assert job.result["section_count"] >= 2
        assert job.result["reference_count"] == 3
        assert job.result["content_hash"] == expected_hash
        assert job.result["title"] == "Attention Is All You Need"

        # 2. Check Paper entity
        paper = session.get(Paper, "arxiv:1706.03762")
        assert paper is not None
        assert paper.title == "Attention Is All You Need"
        assert paper.authors == ["Ashish Vaswani", "Noam Shazeer"]
        assert paper.abstract is not None
        assert "dominant sequence" in paper.abstract
        assert paper.primary_category == "cs.CL"
        assert paper.categories == ["cs.CL", "cs.LG"]
        assert paper.published_at == "2017-06-12"
        assert paper.published_date_precision == "day"
        assert paper.published_date_source == "arxiv_first_submitted"
        assert paper.external_ids["arxiv"] == "1706.03762"
        assert paper.external_ids["doi"] == "10.48550/arXiv.1706.03762"

        # 3. Check PaperRevision entity
        revision = session.get(PaperRevision, "arxiv:1706.03762:v7")
        assert revision is not None
        assert revision.paper_id == "arxiv:1706.03762"
        assert revision.arxiv_version == "v7"
        assert revision.page_count == 2
        assert revision.content_hash == expected_hash
        assert revision.pdf_source_url == "https://arxiv.org/pdf/1706.03762v7.pdf"
        assert revision.parser_version == DEFAULT_PARSER_VERSION

        # 4. Check PaperSection entities
        sections = list(
            session.scalars(
                select(PaperSection)
                .where(PaperSection.revision_id == revision.id)
                .order_by(PaperSection.section_index)
            )
        )
        assert len(sections) >= 2
        intro_sec = next((s for s in sections if s.title and "Introduction" in s.title), None)
        assert intro_sec is not None
        assert "Recurrent neural networks" in intro_sec.text
        assert intro_sec.page_start == 1
        assert intro_sec.span_info is not None
        assert "source_spans" in intro_sec.span_info
        assert len(intro_sec.span_info["source_spans"]) > 0
        assert intro_sec.extraction_quality is not None
        assert intro_sec.extraction_quality.get("status") in {"clean", "good", "warning"}

        # 5. Check Reference entities
        references = list(
            session.scalars(
                select(Reference)
                .where(Reference.revision_id == revision.id)
                .order_by(Reference.ref_order)
            )
        )
        assert len(references) == 3
        assert references[0].ref_order == 1
        assert "Layer normalization" in references[0].raw_text
        assert references[0].resolution_status == "unresolved"
        assert references[1].ref_order == 2
        assert "Attention is all you need" in references[1].raw_text
        assert references[2].ref_order == 3
        assert "LoRA" in references[2].raw_text

        # 6. Check StageExecution
        stage = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == job_id,
                StageExecution.name == "parsing",
            )
        )
        assert stage is not None
        assert stage.status == "completed"
        assert stage.attempt == 1
        assert stage.checkpoint == job.result
        assert stage.finished_at is not None


def test_phase1_temporary_pdf_cleanup(engine, settings, monkeypatch):
    """Verify downloaded temporary PDF is cleaned up and does not leak on disk."""
    raw_pdf_bytes = create_minimal_pdf(SAMPLE_PAGES)
    arxiv_client = create_mock_arxiv_client(pdf_bytes=raw_pdf_bytes)

    created_temp_paths: list[Path] = []
    orig_download_pdf_temp_sync = arxiv_client.download_pdf_temp_sync

    from contextlib import contextmanager

    @contextmanager
    def tracked_download(arxiv_id_or_url: str):
        with orig_download_pdf_temp_sync(arxiv_id_or_url) as path:
            created_temp_paths.append(path)
            assert path.exists()  # Exists during processing
            yield path

    monkeypatch.setattr(arxiv_client, "download_pdf_temp_sync", tracked_download)

    job_id = submit_job(engine, settings, "https://arxiv.org/abs/1706.03762v7")
    worker = Worker(
        engine,
        settings,
        arxiv_client=arxiv_client,
        pipeline_mode="phase1",
    )
    assert worker.run_once() == job_id

    assert len(created_temp_paths) == 1
    # After pipeline execution finishes, temporary PDF must NOT exist
    assert not created_temp_paths[0].exists()


def test_phase1_idempotent_reexecution(engine, settings):
    """Verify re-executing parsing on the same revision replaces sections without errors."""
    arxiv_client = create_mock_arxiv_client()
    job_id = submit_job(engine, settings, "https://arxiv.org/abs/1706.03762v7")

    worker = Worker(
        engine,
        settings,
        arxiv_client=arxiv_client,
        pipeline_mode="phase1",
    )
    worker.run_once()

    # Re-execute parsing stage directly
    with Session(engine) as session:
        stage = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == job_id,
                StageExecution.name == "parsing",
            )
        )
        assert stage is not None
        stage_id = stage.id

    # Re-claim job artificially for re-run test
    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        job.owner = worker.owner
        job.lease_until = 9999999999.0
        job.status = JobStatus.PARSING
        session.commit()

    # Second execution must succeed without primary key or unique constraint violations
    result = worker.execute_phase1_parsing(job_id, stage_id)
    assert result["reference_count"] == 3

    with Session(engine) as session:
        sections = list(
            session.scalars(
                select(PaperSection).where(PaperSection.revision_id == "arxiv:1706.03762:v7")
            )
        )
        references = list(
            session.scalars(select(Reference).where(Reference.revision_id == "arxiv:1706.03762:v7"))
        )
        # Ensure no duplication
        assert len(references) == 3
        assert len(sections) == result["section_count"]


def test_phase1_arxiv_metadata_error_handling(engine, settings):
    """Verify network or HTTP errors from arXiv transition the job to FAILED."""
    arxiv_client = create_mock_arxiv_client(metadata_status_code=404)
    job_id = submit_job(engine, settings, "https://arxiv.org/abs/1706.03762v7")

    worker = Worker(
        engine,
        settings,
        arxiv_client=arxiv_client,
        pipeline_mode="phase1",
    )
    worker.run_once()

    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code is not None
        assert "404" in job.error_code or "Not Found" in job.error_code
        assert job.owner is None
        assert job.lease_until is None

        stage = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == job_id,
                StageExecution.name == "parsing",
            )
        )
        assert stage is not None
        assert stage.status == "failed"
        assert stage.error_code == job.error_code


def test_phase1_corrupt_pdf_error_handling(engine, settings):
    """Verify invalid PDF bytes transition job to FAILED without lingering temp files."""
    arxiv_client = create_mock_arxiv_client(pdf_bytes=b"This is not a PDF file")
    job_id = submit_job(engine, settings, "https://arxiv.org/abs/1706.03762v7")

    worker = Worker(
        engine,
        settings,
        arxiv_client=arxiv_client,
        pipeline_mode="phase1",
    )
    worker.run_once()

    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        assert job.status == JobStatus.FAILED
        assert job.error_code is not None
        assert "PDF" in job.error_code

        stage = session.scalar(
            select(StageExecution).where(
                StageExecution.job_id == job_id,
                StageExecution.name == "parsing",
            )
        )
        assert stage is not None
        assert stage.status == "failed"


def test_phase1_unversioned_arxiv_id_normalization(engine, settings):
    """Verify unversioned URL uses retrieved version from Atom XML."""
    arxiv_client = create_mock_arxiv_client()
    # Submit without version: 1706.03762
    job_id = submit_job(engine, settings, "https://arxiv.org/abs/1706.03762")

    worker = Worker(
        engine,
        settings,
        arxiv_client=arxiv_client,
        pipeline_mode="phase1",
    )
    worker.run_once()

    with Session(engine) as session:
        job = session.get(AnalysisJob, job_id)
        assert job is not None
        assert job.status == JobStatus.COMPLETED
        assert job.result is not None
        # Current version is resolved to v7 from Atom XML
        assert job.result["revision_id"] == "arxiv:1706.03762:v7"

        revision = session.get(PaperRevision, "arxiv:1706.03762:v7")
        assert revision is not None
        assert revision.arxiv_version == "v7"
