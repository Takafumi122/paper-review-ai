"""PDF parser adapter using pdfplumber to extract structured documents.

Converts raw PDF files into domain ParsedDocument objects, with section detection,
page-level provenance tracking, bibliography extraction, and quality metrics.

The parser adapter NEVER deletes the input file. PDF lifecycle is owned exclusively
by the retrieval layer.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any, BinaryIO, cast

import pdfplumber
from pdfminer.pdfparser import PDFSyntaxError

from paper_review.domain.models import (
    ParsedDocument,
    ParsedReference,
    ParsedSection,
    SourceSpan,
)
from paper_review.domain.section_titles import is_bibliography_heading

logger = logging.getLogger("paper_review")

DEFAULT_PARSER_VERSION = "pdfplumber-0.2.2"

STANDARD_HEADINGS: frozenset[str] = frozenset(
    {
        "abstract",
        "introduction",
        "related work",
        "related works",
        "background",
        "preliminaries",
        "methodology",
        "methods",
        "approach",
        "model architecture",
        "proposed method",
        "system architecture",
        "experiments",
        "experimental setup",
        "evaluation",
        "results",
        "empirical results",
        "findings",
        "discussion",
        "analysis",
        "conclusion",
        "conclusions",
        "concluding remarks",
        "future work",
        "acknowledgements",
        "acknowledgments",
        "references",
        "bibliography",
        "appendix",
        "appendices",
        "supplementary material",
        "supplementary materials",
        "reproducibility statement",
        "ethics statement",
        "limitations",
        "broader impact",
    }
)


class PDFParserError(Exception):
    """Raised when PDF parsing fails critically."""


def is_heading_candidate(
    line: str,
    *,
    in_appendix_mode: bool = False,
) -> tuple[bool, str | None, str | None, bool]:
    """Determine if a text line is a section heading candidate.

    Returns:
        tuple: (is_heading, normalized_title, remaining_inline_text, is_appendix)
    """
    s = line.strip()
    if not s or len(s) > 120:
        return False, None, None, False

    # Reject table / figure continuation headers
    if re.search(r"continued\s+from\s+previous\s+page", s, re.I):
        return False, None, None, False

    # 1. Abstract
    m_abs = re.match(r"^(abstract)\b(?:\s*[-:—.]\s*(.*))?$", s, re.I)
    if m_abs:
        inline = m_abs.group(2).strip() if m_abs.group(2) else None
        return True, "Abstract", inline, False

    # 2. References / Bibliography
    if re.match(r"^(references|bibliography)\b", s, re.I):
        return True, "References", None, False

    # 3. Explicit Appendix keyword (e.g. "Appendix", "Appendix A", "Appendices")
    m_app_word = re.match(r"^(appendix|appendices)(?:\s+([A-Z0-9].*))?$", s, re.I)
    if m_app_word:
        return True, s, None, True

    # 4. Numbered headings: e.g. "1 Introduction", "2.1 Model Architecture", "3.2.1 Training"
    # Title must start with an uppercase letter to filter out math or data rows (e.g. "1 512 512").
    m_num = re.match(
        r"^(\d+(?:\.\d+)*)\.?\s+([A-Z][A-Za-z0-9\-_\s\x27\x22’?:+&(),/]+)$",
        s,
    )
    if m_num:
        num_str = m_num.group(1)
        title_part = m_num.group(2).strip()
        words = title_part.split()

        # Filter out standalone high numbers without dots (e.g. "100. The gap between...")
        if "." not in num_str and int(num_str) > 30:
            return False, None, None, False

        # Filter out lines that are clearly running sentences ending in commas, hyphens, etc.
        if not s.endswith(("-", ",", "...")) and len(title_part) >= 2:
            # Reject long sentence-like lines ending in periods
            if not (s.endswith(".") and len(words) > 7):
                # Reject lines consisting purely of single-character tokens (math tables)
                if not all(len(w) == 1 for w in words):
                    return True, s, None, False

    # 5. Letter-numbered Appendix (e.g. "A Additional Results", "B Hyperparameters")
    m_app = re.match(
        r"^([A-Z](?:\.\d+)*)\.?\s+([A-Z][A-Za-z0-9\-_\s\x27\x22’?:+&(),/]+)$",
        s,
    )
    if m_app:
        letter = m_app.group(1)
        title_part = m_app.group(2).strip()
        words = title_part.split()
        if (
            len(words) >= 1
            and not any(len(w) == 1 for w in words[:2])
            and not s.endswith(("-", ","))
        ):
            if (
                in_appendix_mode
                or letter.startswith("A")
                or any(
                    k in title_part.lower()
                    for k in [
                        "proof",
                        "result",
                        "detail",
                        "dataset",
                        "param",
                        "experiment",
                        "prompt",
                        "trajectory",
                        "analysis",
                    ]
                )
            ):
                return True, s, None, True

    # 6. Standard unnumbered headings (exact match or normalized title)
    norm = s.lower().rstrip(".:")
    if norm in STANDARD_HEADINGS and not s.islower():
        is_app = norm in {
            "appendix",
            "appendices",
            "supplementary material",
            "supplementary materials",
        }
        return True, s, None, is_app

    # 7. Appendix titles in appendix mode (e.g. "Attention Visualizations")
    if in_appendix_mode and len(s) < 60 and not s.endswith((".", "-", ",")):
        if any(w in s.lower() for w in ["visualization", "visualizations"]):
            return True, s, None, True

    return False, None, None, False


def extract_references(
    ref_text: str,
    source_spans: list[SourceSpan],
    *,
    line_indents: list[float] | None = None,
) -> list[ParsedReference]:
    """Parse bibliography entries from extracted References section text.

    Supports bracketed numeric/alphanumeric labels and unnumbered author-year styles.
    Maps each entry to its originating page using character offset lookup in source_spans.
    """
    # Preserve offsets before trimming, including repeated lines and page separators.
    lines: list[tuple[str, int, float | None]] = []
    offset = 0
    for index, raw in enumerate(ref_text.splitlines(keepends=True)):
        text = raw.strip()
        indent = line_indents[index] if line_indents is not None else None
        if text and not text.isdigit():  # running page numbers are not references
            lines.append((text, offset + len(raw) - len(raw.lstrip()), indent))
        offset += len(raw)
    if not lines:
        return []

    label_pattern = r"^(\[[A-Za-z0-9][A-Za-z0-9+*†‡-]{0,31}\])\s*(.*)"
    labelled = any(re.match(label_pattern, line) for line, _, _ in lines)
    indents = [x for _, _, x in lines if x is not None]
    left = min(indents) if indents else 0.0
    hanging = bool(indents and any(5 <= x - left <= 24 for x in indents))
    entries: list[ParsedReference] = []
    current: list[str] = []
    current_offset = 0
    current_key: str | None = None

    def commit() -> None:
        if not current:
            return
        page = next(
            (
                span.page
                for span in source_spans
                if span.start_offset <= current_offset < span.end_offset
            ),
            None,
        )
        entries.append(
            ParsedReference(
                ref_order=len(entries) + 1,
                raw_text=" ".join(current),
                citation_key=current_key,
                page=page,
            )
        )

    for line, position, indent in lines:
        number = re.match(label_pattern, line)
        if labelled:
            is_start = number is not None
        elif hanging and indent is not None:
            # Hanging bibliography: author starts at left, *all* continuation
            # lines (including long author lists and URLs) are indented.
            is_start = indent <= left + 2 and bool(re.match(r"^[A-ZÀ-ÖØ-Þ]", line))
        else:
            # Without layout require both a completed previous bibliography item
            # and an author-like next line. Never split just because it is long.
            previous = " ".join(current)
            complete = bool(re.search(r"(?:19|20)\d{2}[a-z]?[.)]*(?:\s|$)", previous))
            author = bool(re.match(r"^[A-Z][\w’'-]+(?:,\s*[A-Z]\.| [A-Z][\w’'.-]+)", line))
            continuation = bool(
                re.match(r"^(?:In |URL|https?://|arXiv|doi:|Proceedings|pp\.|pages |and )", line)
            )
            is_start = (
                complete and author and not continuation and not previous.endswith((",", "-"))
            )
        if is_start and current:
            commit()
            current = []
        if not current:
            current_offset = position
            current_key = number.group(1) if number else None
        current.append(number.group(2) if number else line)
    commit()

    return entries


class PDFParserAdapter:
    """Adapter for parsing academic PDFs using pdfplumber."""

    def __init__(self, parser_version: str = DEFAULT_PARSER_VERSION) -> None:
        self.parser_version = parser_version

    def parse(self, pdf_input: Path | str | bytes | BinaryIO) -> ParsedDocument:
        """Parse a PDF document from a file path, bytes, or binary stream.

        IMPORTANT:
        This adapter NEVER deletes the input file. PDF lifecycle is owned
        exclusively by the caller (Retrieval layer).
        """
        warnings: list[str] = []

        try:
            if isinstance(pdf_input, bytes):
                stream = io.BytesIO(pdf_input)
                pdf = pdfplumber.open(stream)
            elif isinstance(pdf_input, (str, Path)):
                path = Path(pdf_input)
                if not path.exists():
                    raise FileNotFoundError(f"PDF file not found: {path}")
                pdf = pdfplumber.open(path)
            else:
                pdf = pdfplumber.open(cast(Any, pdf_input))
        except (PDFSyntaxError, Exception) as exc:
            if isinstance(exc, FileNotFoundError):
                raise
            logger.warning("PDF syntax/parse failure: %s", exc)
            return ParsedDocument(
                page_count=0,
                sections=[],
                references=[],
                warnings=[f"malformed_pdf: {exc}"],
                parser_version=self.parser_version,
                extraction_quality="unsupported",
            )

        try:
            page_count = len(pdf.pages)
            if page_count == 0:
                return ParsedDocument(
                    page_count=0,
                    sections=[],
                    references=[],
                    warnings=["empty_document"],
                    parser_version=self.parser_version,
                    extraction_quality="unsupported",
                )

            # Step 1: Extract text page-by-page
            page_texts: list[tuple[int, str]] = []
            line_layout: dict[tuple[int, str], tuple[float, float]] = {}
            total_chars = 0
            text_poor_pages: list[int] = []

            for p_idx, page in enumerate(pdf.pages):
                p_num = p_idx + 1
                try:
                    layout_lines = page.extract_text_lines(x_tolerance=1)
                    txt = "\n".join(line["text"] for line in layout_lines)
                    for line in layout_lines:
                        line_layout[(p_num, line["text"].strip())] = (
                            float(line["x0"]),
                            max(float(char["size"]) for char in line["chars"]),
                        )
                except Exception as exc:
                    logger.debug("Page %d extract_text error: %s", p_num, exc)
                    txt = ""

                char_count = len(txt.strip())
                total_chars += char_count
                if char_count < 50:
                    text_poor_pages.append(p_num)
                page_texts.append((p_num, txt))

            # Image-only / Scanned PDF check
            if total_chars < 100:
                return ParsedDocument(
                    page_count=page_count,
                    sections=[],
                    references=[],
                    warnings=["scanned_or_image_only_pdf"],
                    parser_version=self.parser_version,
                    extraction_quality="unsupported",
                )

            if text_poor_pages:
                warnings.append(f"text_poor_pages: {text_poor_pages}")

            # Step 2: Line-by-line scanning & Section chunking
            sections: list[ParsedSection] = []
            current_title: str | None = None
            current_lines_by_page: list[tuple[int, list[str]]] = []
            current_is_appendix = False
            current_heading_page: int = 1
            in_appendix_mode = False
            section_counter = 0
            bibliography_font: float | None = None

            def commit_section() -> None:
                nonlocal section_counter, current_title, current_lines_by_page
                nonlocal current_is_appendix, current_heading_page
                if not current_lines_by_page and not current_title:
                    return

                full_text_chunks: list[str] = []
                source_spans: list[SourceSpan] = []
                curr_offset = 0

                page_start = (
                    current_lines_by_page[0][0] if current_lines_by_page else current_heading_page
                )
                page_end = (
                    current_lines_by_page[-1][0] if current_lines_by_page else current_heading_page
                )

                for p_num, lines in current_lines_by_page:
                    chunk = "\n".join(lines).strip()
                    if not chunk:
                        continue
                    if full_text_chunks:
                        curr_offset += 1  # newline separator
                    chunk_len = len(chunk)
                    source_spans.append(
                        SourceSpan(
                            page=p_num,
                            start_offset=curr_offset,
                            end_offset=curr_offset + chunk_len,
                        )
                    )
                    full_text_chunks.append(chunk)
                    curr_offset += chunk_len

                sec_text = "\n".join(full_text_chunks).strip()

                if current_title or sec_text:
                    q = "good" if len(sec_text) > 40 else "partial"

                    # Check for Table / Figure dependency if text is sparse
                    if q == "partial" and current_title:
                        if re.search(r"\b(table|figure|fig\.)\b", sec_text, re.I):
                            warnings.append(
                                f"table_or_figure_dependency: section '{current_title}' "
                                "is text-sparse"
                            )

                    sections.append(
                        ParsedSection(
                            section_index=section_counter,
                            title=current_title,
                            text=sec_text,
                            page_start=page_start,
                            page_end=page_end,
                            source_spans=source_spans,
                            extraction_quality=q,
                            is_appendix=current_is_appendix,
                        )
                    )
                    section_counter += 1

                current_lines_by_page = []

            for p_num, p_text in page_texts:
                lines = p_text.splitlines()
                cur_page_lines: list[str] = []

                for line in lines:
                    sline = line.strip()
                    if not sline:
                        continue

                    is_h, h_title, inline_body, is_app = is_heading_candidate(
                        sline,
                        in_appendix_mode=in_appendix_mode,
                    )
                    if current_title == "References":
                        font = line_layout.get((p_num, sline), (0.0, 0.0))[1]
                        if sline.lower() in {"references", "bibliography"}:
                            continue  # repeated running heading
                        if bibliography_font is None and not sline.isdigit():
                            bibliography_font = font
                        if is_h and not (
                            is_app
                            and (
                                font > (bibliography_font or font) + 0.5
                                or sline.isupper()
                                or sline.lower().startswith("appendix")
                                or re.match(
                                    r"^[A-Z] (Proof|Additional|Experimental|Hyperparameters)\b",
                                    sline,
                                )
                            )
                        ):
                            is_h = False  # initials/titles inside a bibliography are not headings
                    if is_h:
                        if cur_page_lines:
                            current_lines_by_page.append((p_num, cur_page_lines))
                            cur_page_lines = []
                        commit_section()

                        current_title = h_title
                        current_is_appendix = is_app
                        current_heading_page = p_num

                        if is_bibliography_heading(h_title):
                            in_appendix_mode = True
                        if is_app:
                            in_appendix_mode = True

                        if inline_body:
                            cur_page_lines.append(inline_body)
                    else:
                        cur_page_lines.append(sline)

                if cur_page_lines:
                    current_lines_by_page.append((p_num, cur_page_lines))

            commit_section()

            # Step 3: Extract References
            references: list[ParsedReference] = []
            ref_sec = next(
                (
                    s
                    for s in sections
                    if s.title and any(k in s.title.lower() for k in ["references", "bibliography"])
                ),
                None,
            )
            if ref_sec:
                indents = [
                    line_layout.get((span.page, line.strip()), (0.0, 0.0))[0]
                    for span in ref_sec.source_spans
                    for line in ref_sec.text[span.start_offset : span.end_offset].splitlines()
                ]
                references = extract_references(
                    ref_sec.text,
                    ref_sec.source_spans,
                    line_indents=indents,
                )
                if not references:
                    warnings.append("references_extraction_empty")
            else:
                warnings.append("missing_references")

            # Step 4: Quality Assessment
            has_abstract = any(s.title and "abstract" in s.title.lower() for s in sections)
            if not has_abstract:
                warnings.append("missing_abstract")

            if not sections:
                overall_quality = "low_quality"
            elif "missing_abstract" in warnings and "missing_references" in warnings:
                overall_quality = "partial"
            elif total_chars / page_count < 150:
                overall_quality = "low_quality"
            else:
                overall_quality = "good"

            return ParsedDocument(
                page_count=page_count,
                sections=sections,
                references=references,
                warnings=warnings,
                parser_version=self.parser_version,
                extraction_quality=overall_quality,
            )

        finally:
            pdf.close()


def parse_pdf(
    pdf_input: Path | str | bytes | BinaryIO,
    parser_version: str = DEFAULT_PARSER_VERSION,
) -> ParsedDocument:
    """Functional convenience wrapper for PDFParserAdapter.

    IMPORTANT: Does NOT delete the input file or alter its lifecycle.
    """
    adapter = PDFParserAdapter(parser_version=parser_version)
    return adapter.parse(pdf_input)
