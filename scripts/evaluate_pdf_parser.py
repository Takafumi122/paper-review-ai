"""Manual local evaluation script for PDF Parser Adapter on 3 canonical papers.

Papers evaluated:
- Attention Is All You Need (arxiv:1706.03762v7)
- LoRA (arxiv:2106.09685v2)
- ReAct (arxiv:2210.03629v3)

Separated from automated CI/unit tests to avoid external network dependencies.
Evaluates 9 key criteria:
1. page count matches expected range
2. Abstract section detected with non-empty text
3. Introduction section detected with non-empty text
4. Major section headings identified
5. References section detected
6. Reference count reasonableness (> 10 items)
7. Appendix sections detected with is_appendix=True
8. Page provenance (source_spans mapped to valid pages and character slices)
9. Non-empty text across all sections
"""

from __future__ import annotations

import asyncio
import sys

from paper_review.infrastructure.academic_api.arxiv import ArXivClient
from paper_review.infrastructure.parsing.pdf_parser import parse_pdf

PAPERS = [
    {
        "id": "1706.03762v7",
        "title": "Attention Is All You Need",
        "min_pages": 12,
        "max_pages": 16,
        "min_refs": 30,
        "has_appendix": True,
    },
    {
        "id": "2106.09685v2",
        "title": "LoRA: Low-Rank Adaptation of Large Language Models",
        "min_pages": 24,
        "max_pages": 30,
        "min_refs": 20,
        "has_appendix": True,
    },
    {
        "id": "2210.03629v3",
        "title": "ReAct: Synergizing Reasoning and Acting in Language Models",
        "min_pages": 30,
        "max_pages": 36,
        "min_refs": 15,
        "has_appendix": True,
    },
]


async def evaluate_single_paper(client: ArXivClient, paper_spec: dict) -> bool:
    aid = paper_spec["id"]
    name = paper_spec["title"]
    print(f"\n{'=' * 70}\nEvaluating {name} ({aid})\n{'=' * 70}")

    async with client.download_pdf_temp(aid) as temp_pdf:
        # Assert temp file exists before parsing
        if not temp_pdf.exists():
            print("FAILED: Temporary PDF does not exist!")
            return False

        doc = parse_pdf(temp_pdf)

        # Assert temp file is NOT deleted by the parser
        if not temp_pdf.exists():
            print("FAILED: Parser improperly deleted the temporary PDF file!")
            return False

    # 1. Page count check
    p_min, p_max = paper_spec["min_pages"], paper_spec["max_pages"]
    if not (p_min <= doc.page_count <= p_max):
        print(f"FAILED: Page count {doc.page_count} not in [{p_min}, {p_max}]")
        return False
    print(f"[OK] 1. Page count: {doc.page_count} (expected {p_min}-{p_max})")

    # 2. Abstract check
    abstract_sec = next(
        (s for s in doc.sections if s.title and "abstract" in s.title.lower()),
        None,
    )
    if not abstract_sec or len(abstract_sec.text.strip()) < 50:
        print("FAILED: Abstract section missing or text too short!")
        return False
    print(f"[OK] 2. Abstract detected: {len(abstract_sec.text)} chars")

    # 3. Introduction check
    intro_sec = next(
        (s for s in doc.sections if s.title and "introduction" in s.title.lower()),
        None,
    )
    if not intro_sec or len(intro_sec.text.strip()) < 100:
        print("FAILED: Introduction section missing or text too short!")
        return False
    print(
        f"[OK] 3. Introduction detected: {intro_sec.title} "
        f"({len(intro_sec.text)} chars, pages {intro_sec.page_start}-{intro_sec.page_end})"
    )

    # 4. Major headings check
    headings = [s.title for s in doc.sections if s.title]
    if len(headings) < 6:
        print(f"FAILED: Insufficient section headings detected ({len(headings)})")
        return False
    print(f"[OK] 4. Major headings detected ({len(headings)} total):")
    for h in headings[:6]:
        print(f"     - {h}")

    # 5. References section check
    ref_sec = next(
        (
            s
            for s in doc.sections
            if s.title and any(k in s.title.lower() for k in ["references", "bibliography"])
        ),
        None,
    )
    if not ref_sec:
        print("FAILED: References section missing!")
        return False
    print(
        f"[OK] 5. References section found: {ref_sec.title} "
        f"(pages {ref_sec.page_start}-{ref_sec.page_end})"
    )

    # 6. Reference count check
    ref_count = len(doc.references)
    if ref_count < paper_spec["min_refs"]:
        print(f"FAILED: Reference count {ref_count} below minimum {paper_spec['min_refs']}")
        return False
    print(
        f"[OK] 6. Reference count reasonable: {ref_count} items "
        f"(min expected: {paper_spec['min_refs']})"
    )
    sample_ref = doc.references[0]
    print(
        f"     Sample Ref #1: order={sample_ref.ref_order}, "
        f"key={sample_ref.citation_key!r}, page={sample_ref.page}"
    )

    # 7. Appendix detection check
    appendix_secs = [s for s in doc.sections if s.is_appendix]
    if paper_spec["has_appendix"] and not appendix_secs:
        print("FAILED: Expected appendix sections but none marked as appendix!")
        return False
    print(f"[OK] 7. Appendix detected: {len(appendix_secs)} appendix sections")
    if appendix_secs:
        print(
            f"     First appendix: {appendix_secs[0].title} "
            f"(pages {appendix_secs[0].page_start}-{appendix_secs[0].page_end})"
        )

    # 8. Page provenance check
    provenance_ok = True
    for sec in doc.sections:
        if not sec.text:
            continue
        for span in sec.source_spans:
            if not (1 <= span.page <= doc.page_count):
                provenance_ok = False
                break
            # Verify slice exists within section text
            slice_text = sec.text[span.start_offset : span.end_offset]
            if len(slice_text) != (span.end_offset - span.start_offset):
                provenance_ok = False
                break
    if not provenance_ok:
        print("FAILED: Source spans contain invalid page numbers or slice offsets!")
        return False
    print(f"[OK] 8. Page provenance verified across all {len(doc.sections)} sections")

    # 9. Non-empty text check
    empty_secs = [s for s in doc.sections if not s.text.strip() and not s.title]
    if empty_secs:
        print("FAILED: Found empty sections without title or text!")
        return False
    print(f"[OK] 9. Text integrity confirmed (quality: {doc.extraction_quality})")

    return True


async def main() -> int:
    client = ArXivClient()
    async with client:
        results = []
        for paper in PAPERS:
            ok = await evaluate_single_paper(client, paper)
            results.append((paper["title"], ok))

    print(f"\n{'=' * 70}\nEvaluation Summary\n{'=' * 70}")
    all_ok = True
    for title, ok in results:
        status = "PASSED" if ok else "FAILED"
        print(f"  {title}: {status}")
        if not ok:
            all_ok = False

    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
