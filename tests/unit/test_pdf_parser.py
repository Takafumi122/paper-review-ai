"""Unit tests for PDF Parser Adapter without network dependencies."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from paper_review.infrastructure.parsing.pdf_parser import (
    PDFParserAdapter,
    parse_pdf,
)


def create_minimal_pdf(pages_text: list[list[str]]) -> bytes:
    """Generate minimal valid PDF bytes with Helvetica font for offline testing."""
    objects: list[str] = []

    # 1: Catalog, 2: Pages, 3: Font
    font_id = 3
    page_ids: list[int] = []
    content_ids: list[int] = []

    # Placeholder for catalog, pages, font
    objects.append("")  # 1
    objects.append("")  # 2
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")  # 3

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


def test_multi_page_pdf_and_provenance():
    """Verify multi-page PDF parsing, section extraction, and exact page provenance."""
    pages = [
        [
            "Attention Is All You Need",
            "Ashish Vaswani, Noam Shazeer",
            "Abstract",
            "The dominant sequence transduction models are based on recurrent neural networks.",
            "In this work we propose the Transformer architecture.",
            "1 Introduction",
            "Recurrent neural networks have been firmly established as state of the art.",
        ],
        [
            "Efforts have since continued to push boundaries of recurrent models.",
            "To the best of our knowledge, the Transformer is the first transduction model.",
            "2 Background",
            "The goal of reducing sequential computation also forms the foundation of Extended.",
        ],
        [
            "3 Conclusion",
            "In this work, we presented the Transformer, the first sequence transduction model.",
            "References",
            "[1] Jimmy Lei Ba. Layer normalization. 2016.",
            "[2] Dzmitry Bahdanau. Neural machine translation. 2014.",
            "[3] Ashish Vaswani. Attention is all you need. 2017.",
        ],
    ]
    pdf_bytes = create_minimal_pdf(pages)
    doc = parse_pdf(pdf_bytes)

    assert doc.page_count == 3
    assert doc.extraction_quality == "good"
    assert len(doc.warnings) == 0

    # Verify sections
    titles = [s.title for s in doc.sections]
    assert "Abstract" in titles
    assert "1 Introduction" in titles
    assert "2 Background" in titles
    assert "3 Conclusion" in titles
    assert "References" in titles

    # Verify Abstract
    abs_sec = next(s for s in doc.sections if s.title == "Abstract")
    assert abs_sec.page_start == 1
    assert abs_sec.page_end == 1
    assert "Transformer architecture" in abs_sec.text
    assert len(abs_sec.source_spans) == 1
    assert abs_sec.source_spans[0].page == 1

    # Verify multi-page Introduction section spanning page 1 and page 2
    intro = next(s for s in doc.sections if s.title == "1 Introduction")
    assert intro.page_start == 1
    assert intro.page_end == 2
    assert len(intro.source_spans) == 2

    # Verify exact provenance slice mapping
    span1 = intro.source_spans[0]
    span2 = intro.source_spans[1]
    assert span1.page == 1
    assert span2.page == 2
    slice1 = intro.text[span1.start_offset : span1.end_offset]
    slice2 = intro.text[span2.start_offset : span2.end_offset]
    assert "Recurrent neural networks" in slice1
    assert "Efforts have since continued" in slice2

    # Verify domain entity conversions
    paper_sections = doc.to_paper_sections("rev_123", "paper_123")
    assert len(paper_sections) == len(doc.sections)
    p_sec = paper_sections[1]
    assert p_sec.revision_id == "rev_123"
    assert p_sec.paper_id == "paper_123"
    assert p_sec.span_info is not None
    assert "source_spans" in p_sec.span_info


def test_numbered_headings():
    """Verify detection of various numbered headings and sub-headings."""
    pages = [
        [
            "Abstract",
            "This paper studies model architectures.",
            "1 Introduction",
            "Overview of the problem.",
            "2 Methodology",
            "Overall method description.",
            "2.1 Model Architecture",
            "Detailed neural network layout.",
            "2.1.1 Attention Mechanism",
            "Scaled dot-product attention equation.",
            "3 Experiments",
            "Experimental results on standard benchmarks.",
            "4 Conclusion",
            "Final concluding remarks.",
        ]
    ]
    doc = parse_pdf(create_minimal_pdf(pages))
    titles = [s.title for s in doc.sections if s.title]

    assert "1 Introduction" in titles
    assert "2 Methodology" in titles
    assert "2.1 Model Architecture" in titles
    assert "2.1.1 Attention Mechanism" in titles
    assert "3 Experiments" in titles
    assert "4 Conclusion" in titles


def test_numbered_references_detection():
    """Verify numbered references ([1], [2], etc.) are cleanly parsed."""
    pages = [
        [
            "References",
            "[1] Vaswani, A., et al. Attention Is All You Need. NeurIPS 2017.",
            "[2] Brown, T., et al. Language Models are Few-Shot Learners. NeurIPS 2020.",
            "[3] Hu, E. J., et al. LoRA: Low-Rank Adaptation. ICLR 2022.",
        ]
    ]
    doc = parse_pdf(create_minimal_pdf(pages))

    assert len(doc.references) == 3
    ref1 = doc.references[0]
    assert ref1.ref_order == 1
    assert ref1.citation_key == "[1]"
    assert "Attention Is All You Need" in ref1.raw_text
    assert ref1.page == 1

    ref2 = doc.references[1]
    assert ref2.ref_order == 2
    assert ref2.citation_key == "[2]"
    assert "Language Models are Few-Shot Learners" in ref2.raw_text

    # Test conversion to Reference domain model
    domain_refs = doc.to_references("rev_1", "paper_1")
    assert len(domain_refs) == 3
    assert domain_refs[0].ref_order == 1
    assert domain_refs[0].citation_key == "[1]"
    assert domain_refs[0].resolution_status == "unresolved"


def test_author_year_references_detection():
    """Verify unnumbered author-year format bibliography items are parsed."""
    pages = [
        [
            "References",
            "Brown, T., Mann, B., Ryder, N., Subbiah, M., and Amodei, D. (2020).",
            "Language models are few-shot learners. In NeurIPS.",
            "Devlin, J., Chang, M. W., Lee, K., and Toutanova, K. (2018).",
            "BERT: Pre-training of deep bidirectional transformers. In NAACL.",
            "Vaswani, A., Shazeer, N., Parmar, N., and Kaiser, L. (2017).",
            "Attention is all you need. In NeurIPS.",
        ]
    ]
    doc = parse_pdf(create_minimal_pdf(pages))

    assert len(doc.references) == 3
    assert doc.references[0].ref_order == 1
    assert "Brown" in doc.references[0].raw_text
    assert doc.references[1].ref_order == 2
    assert "Devlin" in doc.references[1].raw_text
    assert doc.references[2].ref_order == 3
    assert "Vaswani" in doc.references[2].raw_text


def test_appendix_detection():
    """Verify Appendix sections are extracted and flagged with is_appendix=True."""
    pages = [
        [
            "References",
            "[1] A. Vaswani. Attention Is All You Need. 2017.",
            "[2] T. Brown. Language Models. 2020.",
            "[3] E. Hu. LoRA. 2022.",
            "A Proof of Theorem 1",
            "Here we present the full mathematical proof of Theorem 1.",
            "B Additional Hyperparameters",
            "All learning rates and weight decay parameters are detailed below.",
        ]
    ]
    doc = parse_pdf(create_minimal_pdf(pages))

    appendix_secs = [s for s in doc.sections if s.is_appendix]
    assert len(appendix_secs) == 2
    assert appendix_secs[0].title is not None and "A Proof of Theorem 1" in appendix_secs[0].title
    assert (
        appendix_secs[1].title is not None
        and "B Additional Hyperparameters" in appendix_secs[1].title
    )

    # Regular section before is not appendix
    ref_sec = next(s for s in doc.sections if s.title == "References")
    assert ref_sec.is_appendix is False


def test_blank_pdf():
    """Verify blank PDF handling produces unsupported status and warning."""
    pages = [
        [],  # Page 1 empty
        [],  # Page 2 empty
    ]
    doc = parse_pdf(create_minimal_pdf(pages))

    assert doc.page_count == 2
    assert doc.extraction_quality == "unsupported"
    assert "scanned_or_image_only_pdf" in doc.warnings


def test_image_only_or_text_poor_pdf():
    """Verify very short/text-poor PDF produces appropriate warnings."""
    pages = [["Single line."]]
    doc = parse_pdf(create_minimal_pdf(pages))

    assert doc.page_count == 1
    assert doc.extraction_quality == "unsupported"
    assert "scanned_or_image_only_pdf" in doc.warnings


def test_malformed_pdf():
    """Verify corrupt/malformed PDF bytes return unsupported status gracefully."""
    doc = parse_pdf(b"NOT_A_VALID_PDF_HEADER_CORRUPTED_BYTES")

    assert doc.page_count == 0
    assert doc.extraction_quality == "unsupported"
    assert any("malformed_pdf" in w for w in doc.warnings)


def test_file_not_found():
    """Verify FileNotFoundError is raised when target path does not exist."""
    with pytest.raises(FileNotFoundError, match="PDF file not found"):
        parse_pdf("/non/existent/path/to/paper.pdf")


def test_temporary_pdf_not_deleted_by_parser():
    """CRITICAL: Parser must NOT delete the input file (retrieval owns lifecycle)."""
    pdf_bytes = create_minimal_pdf([["Abstract", "Hello world."]])

    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        f.flush()
        temp_path = Path(f.name)

    try:
        assert temp_path.exists()

        # Parse using path
        doc = parse_pdf(temp_path)
        assert doc.page_count == 1

        # The temporary PDF MUST STILL EXIST after parsing
        assert temp_path.exists(), "Parser improperly deleted the input temporary PDF!"
    finally:
        if temp_path.exists():
            temp_path.unlink()


def test_table_dependency_warning():
    """Verify sections heavily referencing tables with sparse text trigger warning."""
    pages = [
        [
            "Abstract",
            "This is a valid abstract with enough text content to pass quality checks.",
            "1 Introduction",
            "This is the introduction section describing the background and motivations.",
            "6 Results",
            "See Table 1 for detailed results.",
            "References",
            "[1] Author. Title. 2020.",
            "[2] Author. Title. 2021.",
            "[3] Author. Title. 2022.",
        ]
    ]
    doc = parse_pdf(create_minimal_pdf(pages))

    results_sec = next(s for s in doc.sections if s.title == "6 Results")
    assert results_sec.extraction_quality == "partial"
    assert any("table_or_figure_dependency" in w for w in doc.warnings)


def test_to_paper_revision_conversion():
    """Verify ParsedDocument correctly initializes PaperRevision domain entity."""
    pages = [["Abstract", "Sample abstract text."]]
    doc = parse_pdf(create_minimal_pdf(pages))

    rev = doc.to_paper_revision(
        revision_id="arxiv:1706.03762:v7",
        paper_id="arxiv:1706.03762",
        arxiv_version="v7",
        content_hash="sha256_dummy",
        pdf_source_url="https://arxiv.org/pdf/1706.03762v7.pdf",
    )

    assert rev.id == "arxiv:1706.03762:v7"
    assert rev.paper_id == "arxiv:1706.03762"
    assert rev.arxiv_version == "v7"
    assert rev.content_hash == "sha256_dummy"
    assert rev.pdf_source_url == "https://arxiv.org/pdf/1706.03762v7.pdf"
    assert rev.page_count == 1
    assert rev.parser_version == "pdfplumber-0.2.0"


def test_repr_never_leaks_raw_text():
    """Security/Observability: Ensure __repr__ and __str__ never output raw text."""
    pages = [
        [
            "Abstract",
            "TOP_SECRET_CONFIDENTIAL_TEXT_ABSTRACT",
            "References",
            "[1] TOP_SECRET_CONFIDENTIAL_REFERENCE_TEXT",
        ]
    ]
    doc = parse_pdf(create_minimal_pdf(pages))

    doc_repr = repr(doc)
    assert "TOP_SECRET" not in doc_repr

    sec = doc.sections[0]
    sec_repr = repr(sec)
    assert "TOP_SECRET" not in sec_repr
    assert str(sec) == sec_repr

    if doc.references:
        ref = doc.references[0]
        ref_repr = repr(ref)
        assert "TOP_SECRET" not in ref_repr
        assert str(ref) == ref_repr


def test_adapter_instance_parse():
    """Verify PDFParserAdapter instance parses correctly."""
    adapter = PDFParserAdapter(parser_version="custom-0.2.0")
    assert adapter.parser_version == "custom-0.2.0"
    doc = adapter.parse(create_minimal_pdf([["Abstract", "Content"]]))
    assert doc.parser_version == "custom-0.2.0"
    assert doc.page_count == 1
