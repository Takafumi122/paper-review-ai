"""Unit tests for domain models (Paper, PaperRevision, PaperSection, Reference, ExternalIds)."""

from paper_review.domain.models import (
    ExternalIds,
    Paper,
    PaperRevision,
    PaperSection,
    Reference,
)


def test_external_ids_defaults_and_serialization():
    ids = ExternalIds(arxiv="1706.03762", doi="10.1234/test")
    assert ids.arxiv == "1706.03762"
    assert ids.doi == "10.1234/test"
    assert ids.semantic_scholar is None
    assert ids.openalex is None  # MVP does not use OpenAlex, but schema supports it

    dumped = ids.to_dict()
    assert dumped == {
        "arxiv": "1706.03762",
        "doi": "10.1234/test",
        "semantic_scholar": None,
        "openalex": None,
    }

    reloaded = ExternalIds.from_dict(dumped)
    assert reloaded == ids


def test_paper_domain_entity():
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
        external_ids=ExternalIds(arxiv="1706.03762"),
    )
    assert paper.id == "arxiv:1706.03762"
    assert len(paper.authors) == 2
    assert paper.primary_category == "cs.CL"
    assert paper.external_ids.arxiv == "1706.03762"
    assert paper.created_at > 0


def test_paper_revision_domain_entity():
    rev = PaperRevision(
        id="rev-1",
        paper_id="arxiv:1706.03762",
        arxiv_version="v7",
        content_hash="a" * 64,
        pdf_source_url="https://arxiv.org/pdf/1706.03762v7.pdf",
        parser_version="pdfplumber-v1",
        page_count=15,
    )
    assert rev.paper_id == "arxiv:1706.03762"
    assert rev.arxiv_version == "v7"
    assert rev.page_count == 15


def test_paper_section_repr_does_not_leak_body():
    """Verify security rule: paper body text must never leak into repr/logs."""
    private_body = "SECRET_PAPER_BODY_CONTENT_1234567890"
    section = PaperSection(
        id="sec-1",
        revision_id="rev-1",
        paper_id="arxiv:1706.03762",
        section_index=0,
        title="1 Introduction",
        text=private_body,
        page_start=1,
        page_end=2,
    )
    assert section.text == private_body
    # repr and str MUST NOT include private_body
    assert private_body not in repr(section)
    assert private_body not in str(section)
    assert "1 Introduction" in repr(section)


def test_reference_domain_entity():
    ref = Reference(
        id="ref-1",
        revision_id="rev-1",
        paper_id="arxiv:1706.03762",
        ref_order=0,
        raw_text=(
            "Bahdanau, Dzmitry, Kyunghyun Cho, and Yoshua Bengio. Neural machine translation..."
        ),
        citation_key="[1]",
        matched_paper_id="arxiv:1409.0473",
        matched_arxiv_id="1409.0473",
        resolution_status="resolved",
    )
    assert ref.ref_order == 0
    assert ref.resolution_status == "resolved"
    assert "ref_order=0" in repr(ref)
