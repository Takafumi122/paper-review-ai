"""Evaluation script for Stage 1 Initial Score and Candidate Ranking on 3 canonical papers:

1. Attention Is All You Need (arxiv:1706.03762v7)
2. LoRA: Low-Rank Adaptation of Large Language Models (arxiv:2106.09685v2)
3. ReAct: Synergizing Reasoning and Acting in Language Models (arxiv:2210.03629v2)

Evaluates:
- Candidate extraction and normalization from parsed references
- Temporal eligibility and content filtering (surveys, benchmarks, later work)
- Stage 1 component scores (SemanticSimilarity, CitationRelation, CitationImportance)
- InitialScore coverage-weighted aggregation (missing != 0)
- Deterministic ranking and tie-breaking
- Sanity checks on Top-K candidates
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any

from paper_review.domain.candidate_retrieval import (
    evaluate_content_eligibility,
    evaluate_temporal_eligibility,
)
from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    DateProvenance,
    ExternalIds,
    Paper,
    Reference,
)
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.arxiv import ArXivClient
from paper_review.infrastructure.parsing.pdf_parser import parse_pdf

TARGET_SPECS = [
    {
        "id": "arxiv:1706.03762",
        "arxiv_id": "1706.03762",
        "version": "v7",
        "title": "Attention Is All You Need",
        "abstract": (
            "The dominant sequence transduction models are based on complex recurrent or "
            "convolutional neural networks that include an encoder and a decoder. The best "
            "performing models also connect the encoder and decoder through an attention "
            "mechanism. We propose a new simple network architecture, the Transformer, based "
            "solely on attention mechanisms, dispensing with recurrence and convolutions entirely."
        ),
        "published_at": "2017-06-12",
        "published_date_precision": "day",
    },
    {
        "id": "arxiv:2106.09685",
        "arxiv_id": "2106.09685",
        "version": "v2",
        "title": "LoRA: Low-Rank Adaptation of Large Language Models",
        "abstract": (
            "An important paradigm of natural language processing consists of large-scale "
            "pre-training on general domain data and adaptation to particular tasks or domains. "
            "As we pre-train larger models, full fine-tuning becomes less feasible. We propose "
            "Low-Rank Adaptation, or LoRA, which freezes the pre-trained model weights and "
            "injects trainable rank decomposition matrices into each layer of the Transformer "
            "architecture, greatly reducing the number of trainable parameters."
        ),
        "published_at": "2021-06-17",
        "published_date_precision": "day",
    },
    {
        "id": "arxiv:2210.03629",
        "arxiv_id": "2210.03629",
        "version": "v2",
        "title": "ReAct: Synergizing Reasoning and Acting in Language Models",
        "abstract": (
            "While large language models have demonstrated impressive capabilities across tasks "
            "in language understanding and interactive decision making, their abilities for "
            "reasoning (e.g. chain-of-thought prompting) and acting (e.g. action plan generation) "
            "have primarily been studied as separate topics. In this work, we explore the use "
            "of LLMs to generate both reasoning traces and task-specific actions in an interleaved "
            "induce, track, and update action plans as well as handle exceptions, while "
            "actions allow it to interface with external sources."
        ),
        "published_at": "2022-10-06",
        "published_date_precision": "day",
    },
]


def extract_reference_metadata(raw_text: str, ref_order: int) -> dict[str, Any]:
    """Heuristic extraction of title, year, authors, and arXiv ID from reference text."""
    # Find year: (199x|200x|201x|202x)
    # Exclude decimal IDs/page ranges; allow attached bibliography year suffixes.
    year_text = re.sub(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"(?=\d{4})",
        r"\1 ",
        raw_text,
        flags=re.IGNORECASE,
    )
    year_match = re.search(
        r"(?<![\w.:/–-])(19\d\d|20[0-2]\d)(?:[a-z])?(?![\w]|\.\d|[–-]\d)",
        year_text,
    )
    year = int(year_match.group(1)) if year_match else None

    # Find arXiv ID:
    arxiv_match = re.search(r"arXiv(?::|preprint\s+arXiv:|\s+abs/)?(\d{4}\.\d{4,5})", raw_text)
    arxiv_id = arxiv_match.group(1) if arxiv_match else None

    # Extract title heuristic: text between first period and second period/comma/In
    clean = re.sub(r"^\[\d+\]\s*", "", raw_text).strip()
    parts = clean.split(". ")
    if len(parts) >= 2:
        authors_raw = parts[0]
        title_candidate = parts[1]
    else:
        authors_raw = ""
        title_candidate = clean

    title = title_candidate.strip()
    # Clean up trailing citations/URLs
    title = re.sub(
        r"(arXiv:.*|In\s+.*|CoRR.*|URL.*|doi:.*|Proceedings.*|http.*)",
        "",
        title,
        flags=re.IGNORECASE,
    ).strip()
    if len(title) < 5:
        title = clean[:80]

    authors = [a.strip() for a in re.split(r",|\band\b", authors_raw) if a.strip()]

    return {
        "title": title,
        "year": year,
        "authors": authors,
        "arxiv_id": arxiv_id,
        "raw_text": raw_text,
        "ref_order": ref_order,
    }


def load_references_from_db(revision_id: str) -> list[Reference]:
    """Attempt to load parsed references from SQLite DB."""
    try:
        con = sqlite3.connect("paper-review.db")
        cur = con.cursor()
        rows = cur.execute(
            "SELECT id, revision_id, paper_id, ref_order, raw_text, matched_arxiv_id "
            "FROM paper_references WHERE revision_id = ? ORDER BY ref_order",
            (revision_id,),
        ).fetchall()
        con.close()
        return [
            Reference(
                id=r[0],
                revision_id=r[1],
                paper_id=r[2],
                ref_order=r[3],
                raw_text=r[4],
                matched_arxiv_id=r[5],
            )
            for r in rows
        ]
    except Exception:
        return []


async def get_or_parse_references(client: ArXivClient, spec: dict[str, Any]) -> list[Reference]:
    """Retrieve references from DB cache or download and parse PDF."""
    rev_id = f"{spec['id']}:{spec['version']}"
    cached = load_references_from_db(rev_id)
    if cached:
        print(f"Loaded {len(cached)} cached references from DB for {spec['title']}")
        return cached

    aid = f"{spec['arxiv_id']}{spec['version']}"
    print(f"Downloading and parsing {aid} from arXiv...")
    async with client.download_pdf_temp(aid) as tmp_pdf:
        doc = parse_pdf(tmp_pdf)
        refs = [
            Reference(
                id=f"{rev_id}:ref_{r.ref_order}",
                revision_id=rev_id,
                paper_id=spec["id"],
                ref_order=r.ref_order,
                raw_text=r.raw_text,
                matched_arxiv_id=None,
            )
            for r in doc.references
        ]
        print(f"Parsed {len(refs)} references from PDF for {spec['title']}")
        return refs


def build_candidates_from_references(
    target_paper: Paper,
    references: list[Reference],
) -> CandidateSet:
    """Build normalized CandidatePaper objects from parsed references and filter them."""
    candidates: list[CandidatePaper] = []

    for ref in references:
        meta = extract_reference_metadata(ref.raw_text, ref.ref_order)
        cand_id = (
            f"arxiv:{meta['arxiv_id']}"
            if meta["arxiv_id"]
            else f"ref:{target_paper.id}:{ref.ref_order}"
        )

        cand_date_str = str(meta["year"]) if meta["year"] else None
        date_prov = (
            DateProvenance(
                kind="publication_year",
                value=str(meta["year"]),
                precision="year",
                source_id="parsed_reference",
            )
            if meta["year"]
            else None
        )

        cand = CandidatePaper(
            paper_id=cand_id,
            title=meta["title"],
            authors=meta["authors"],
            year=meta["year"],
            publication_date=cand_date_str,
            date_provenance=date_prov,
            external_ids=ExternalIds(arxiv=meta["arxiv_id"]),
            is_target_reference=True,
            target_reference_order=ref.ref_order,
            target_reference_raw_text=ref.raw_text,
            is_influential_citation=None,  # Not yet classified
        )

        # 1. Temporal eligibility
        temp_status, temp_reason, _ = evaluate_temporal_eligibility(
            candidate_date_str=cand.publication_date,
            candidate_year=cand.year,
            target_date_str=target_paper.published_at,
        )
        cand.temporal_status = temp_status
        cand.temporal_uncertainty = temp_reason

        # 2. Content eligibility
        cont_status, cont_reason = evaluate_content_eligibility(
            title=cand.title,
            abstract=cand.abstract,
            temporal_status=cand.temporal_status,
            authors=cand.authors,
        )
        cand.eligibility_status = cont_status
        cand.exclusion_reason = cont_reason

        candidates.append(cand)

    return CandidateSet(
        target_paper_id=target_paper.id,
        candidates=candidates,
    )


async def run_legacy_evaluation():
    client = ArXivClient()

    for spec in TARGET_SPECS:
        target_paper = Paper(
            id=spec["id"],
            title=spec["title"],
            abstract=spec["abstract"],
            published_at=spec["published_at"],
            published_date_precision=spec["published_date_precision"],
            external_ids=ExternalIds(arxiv=spec["arxiv_id"]),
        )

        print("\n" + "=" * 80)
        print(f"Target: {target_paper.title} ({target_paper.id})")
        print(f"Published: {target_paper.published_at}")
        print("=" * 80)

        refs = await get_or_parse_references(client, spec)
        candidate_set = build_candidates_from_references(target_paper, refs)

        print("\nCandidate Set Statistics:")
        print(f"  Total Candidates:     {candidate_set.total_count}")
        print(f"  Eligible Candidates:  {len(candidate_set.eligible_candidates)}")
        print(f"  Excluded Candidates:  {len(candidate_set.excluded_candidates)}")
        print(f"  Uncertain Candidates: {len(candidate_set.uncertain_candidates)}")

        if candidate_set.excluded_candidates:
            print("\nExcluded Candidates Breakdown:")
            for exc in candidate_set.excluded_candidates[:5]:
                print(f"  - [{exc.exclusion_reason}] {exc.title[:60]} ({exc.year})")

        # Explicit lexical diagnostic baseline; this does not use SPECTER2.
        ranking_result = rank_stage1_candidates(
            target_paper,
            candidate_set,
            semantic_strategy="lexical",
            citation_importance_mode="canonical",
            top_k=10,
        )

        print(f"\nRanked Candidates: {len(ranking_result.ranked_candidates)}")
        print(f"Unranked Candidates: {len(ranking_result.unranked_candidates)}")
        print("\nTop-10 Stage 1 Ranked Candidates:")
        print(
            f"{'Rank':<4} | {'InitialScore':<12} | {'SemSim':<8} | {'CitRel':<6} | "
            f"{'CitImp':<6} | {'Cov':<5} | {'Year':<4} | {'Title'}"
        )
        print("-" * 100)

        top_10 = ranking_result.top_k(10)
        for r in top_10:
            score = r.score
            s_val = (
                f"{score.semantic_similarity:.3f}"
                if score.semantic_similarity is not None
                else "null"
            )
            c_val = (
                f"{score.citation_relation:.2f}" if score.citation_relation is not None else "null"
            )
            i_val = (
                f"{score.citation_importance:.2f}"
                if score.citation_importance is not None
                else "null"
            )
            init_val = f"{score.initial_score:.4f}" if score.initial_score is not None else "null"
            cov_val = f"{score.coverage:.2f}"
            yr = str(r.candidate.year or "N/A")
            print(
                f"{r.rank:<4} | {init_val:<12} | {s_val:<8} | {c_val:<6} | {i_val:<6} | "
                f"{cov_val:<5} | {yr:<4} | {r.candidate.title[:50]}"
            )


async def run_evaluation():
    # Reuse verified inventory identities, enrich both sides with SPECTER2, then
    # apply the existing canonical scorer. No repeated title searches.
    from evaluate_specter2 import evaluate

    await evaluate(live=True)


if __name__ == "__main__":
    asyncio.run(run_evaluation())
