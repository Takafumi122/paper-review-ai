"""Integration tests for parsing DB schema (Paper, PaperRevision, PaperSection, Reference)."""

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from paper_review.infrastructure.database.models import (
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
)


def test_parsing_entities_persistence(engine):
    with Session(engine) as session:
        paper = Paper(
            id="arxiv:1706.03762",
            title="Attention Is All You Need",
            authors=["Ashish Vaswani", "Noam Shazeer"],
            abstract="The dominant sequence transduction models...",
            primary_category="cs.CL",
            categories=["cs.CL", "cs.LG"],
            published_at="2017-06-12",
            published_date_precision="day",
            published_date_source="arxiv_first_submitted",
            external_ids={
                "arxiv": "1706.03762",
                "doi": "10.48550/arXiv.1706.03762",
                "semantic_scholar": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
                "openalex": None,
            },
        )
        session.add(paper)
        session.flush()

        rev = PaperRevision(
            paper_id=paper.id,
            arxiv_version="v7",
            content_hash="sha256_mock_hash_value",
            pdf_source_url="https://arxiv.org/pdf/1706.03762v7.pdf",
            parser_version="pdfplumber-v1",
            page_count=15,
        )
        session.add(rev)
        session.flush()

        sec0 = PaperSection(
            revision_id=rev.id,
            paper_id=paper.id,
            section_index=0,
            title="Abstract",
            text="The dominant sequence transduction models are based on complex recurrent...",
            page_start=1,
            page_end=1,
        )
        sec1 = PaperSection(
            revision_id=rev.id,
            paper_id=paper.id,
            section_index=1,
            title="1 Introduction",
            text="Recurrent neural networks, long short-term memory and gated recurrent...",
            page_start=1,
            page_end=2,
            span_info={"char_offset_start": 500, "char_offset_end": 2300},
            extraction_quality={"char_count": 1800, "warnings": []},
        )
        session.add_all([sec0, sec1])

        ref0 = Reference(
            revision_id=rev.id,
            paper_id=paper.id,
            ref_order=0,
            raw_text=(
                "Dzmitry Bahdanau, Kyunghyun Cho, and Yoshua Bengio. Neural machine translation..."
            ),
            citation_key="[1]",
            matched_paper_id=None,
            matched_arxiv_id="1409.0473",
            resolution_status="unresolved",
        )
        session.add(ref0)
        session.commit()

    with Session(engine) as session:
        loaded_paper = session.get(Paper, "arxiv:1706.03762")
        assert loaded_paper is not None
        assert loaded_paper.title == "Attention Is All You Need"
        assert loaded_paper.authors == ["Ashish Vaswani", "Noam Shazeer"]
        assert len(loaded_paper.revisions) == 1

        loaded_rev = loaded_paper.revisions[0]
        assert loaded_rev.arxiv_version == "v7"
        assert loaded_rev.page_count == 15
        assert len(loaded_rev.sections) == 2
        assert len(loaded_rev.references) == 1

        # Verify parsed PaperSection text IS stored in DB
        assert loaded_rev.sections[0].title == "Abstract"
        assert "dominant sequence" in loaded_rev.sections[0].text
        assert loaded_rev.sections[1].span_info == {
            "char_offset_start": 500,
            "char_offset_end": 2300,
        }

        # Verify safe repr (no body text)
        assert loaded_rev.sections[0].text not in repr(loaded_rev.sections[0])


def test_unique_constraints(engine):
    with Session(engine) as session:
        paper = Paper(id="arxiv:2106.09685", external_ids={"arxiv": "2106.09685"})
        session.add(paper)
        rev = PaperRevision(paper_id=paper.id, arxiv_version="v1")
        session.add(rev)
        session.commit()

    # Duplicate revision version for same paper must fail
    with pytest.raises(IntegrityError):
        with Session(engine) as session:
            session.add(PaperRevision(paper_id="arxiv:2106.09685", arxiv_version="v1"))
            session.commit()

    # Duplicate section_index for same revision must fail
    with Session(engine) as session:
        rev = session.scalar(
            select(PaperRevision).where(PaperRevision.paper_id == "arxiv:2106.09685")
        )
        assert rev is not None
        session.add(
            PaperSection(
                revision_id=rev.id,
                paper_id="arxiv:2106.09685",
                section_index=0,
                text="first",
            )
        )
        session.commit()

    with pytest.raises(IntegrityError):
        with Session(engine) as session:
            rev = session.scalar(
                select(PaperRevision).where(PaperRevision.paper_id == "arxiv:2106.09685")
            )
            assert rev is not None
            session.add(
                PaperSection(
                    revision_id=rev.id,
                    paper_id="arxiv:2106.09685",
                    section_index=0,
                    text="duplicate",
                )
            )
            session.commit()


def test_cascade_delete_paper(engine):
    with Session(engine) as session:
        paper = Paper(id="arxiv:2210.03629", external_ids={"arxiv": "2210.03629"})
        session.add(paper)
        rev = PaperRevision(paper_id=paper.id, arxiv_version="v2")
        session.add(rev)
        session.flush()
        session.add(
            PaperSection(revision_id=rev.id, paper_id=paper.id, section_index=0, text="intro")
        )
        session.add(Reference(revision_id=rev.id, paper_id=paper.id, ref_order=0, raw_text="ref 1"))
        session.commit()

    with Session(engine) as session:
        paper = session.get(Paper, "arxiv:2210.03629")
        assert paper is not None
        session.delete(paper)
        session.commit()

    with Session(engine) as session:
        assert (
            session.scalar(
                select(PaperRevision).where(PaperRevision.paper_id == "arxiv:2210.03629")
            )
            is None
        )
        assert (
            session.scalar(select(PaperSection).where(PaperSection.paper_id == "arxiv:2210.03629"))
            is None
        )
        assert (
            session.scalar(select(Reference).where(Reference.paper_id == "arxiv:2210.03629"))
            is None
        )


def test_section_text_not_leaked_in_logging(engine, caplog):
    """Verify application log never contains paper body text."""
    import logging

    caplog.set_level(logging.INFO, logger="paper_review")
    private_text = "ULTRA_SENSITIVE_PAPER_CONTENT_NOT_TO_LOG"

    with Session(engine) as session:
        paper = Paper(id="arxiv:2305.18290", external_ids={"arxiv": "2305.18290"})
        session.add(paper)
        rev = PaperRevision(paper_id=paper.id, arxiv_version="v2")
        session.add(rev)
        session.flush()
        sec = PaperSection(
            revision_id=rev.id,
            paper_id=paper.id,
            section_index=0,
            title="1 Introduction",
            text=private_text,
        )
        session.add(sec)
        session.commit()

        # Log section object and ensure private text is absent
        logging.getLogger("paper_review").info("Processing section: %s", repr(sec))

    assert private_text not in caplog.text
    assert "1 Introduction" in caplog.text
