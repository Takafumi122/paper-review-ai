"""Offline bibliography -> exact S2 metadata -> deduplication -> Stage 1."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from paper_review.domain.candidate_inventory import resolve_candidate_inventory
from paper_review.domain.models import ExternalIds, Paper, SourceSpan
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.infrastructure.parsing.pdf_parser import extract_references


def test_segment_resolve_deduplicate_and_rank_with_reference_edge_provenance():
    text = (
        "Alice Author, Bob Builder, and Carol Contributor.\n"
        "Damagedtitle. arXiv:2001.00001, 2020.\n"
        "Alice Author. An alternative spelling. doi:10.1234/alpha, 2020.\n"
        "Betty Writer. Canonical Beta Method. 2020.\n"
        "Unknown Author. An unresolved method. 2019."
    )
    parsed = extract_references(
        text,
        [SourceSpan(page=1, start_offset=0, end_offset=len(text))],
        line_indents=[72, 82, 72, 72, 72],
    )
    refs = [p.to_reference("v1", "target") for p in parsed]
    alpha = dict(
        paperId="a" * 40,
        title="Canonical Alpha Method",
        year=2020,
        authors=[{"name": "Alice Author"}],
        abstract="A general method.",
        externalIds={"ArXiv": "2001.00001", "DOI": "10.1234/alpha"},
    )
    beta = dict(
        paperId="b" * 40,
        title="Canonical Beta Method",
        year=2020,
        authors=[{"name": "Betty Writer"}],
        abstract="Another general method.",
        externalIds={},
    )
    calls = []

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            assert json.loads(request.content)["ids"] == ["ARXIV:2001.00001", "DOI:10.1234/alpha"]
            return httpx.Response(200, json=[alpha, alpha])
        if request.url.path.endswith("/references"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "citedPaper": alpha,
                            "isInfluential": True,
                            "contexts": ["Uses the method."],
                        },
                        {"citedPaper": beta, "isInfluential": False},
                    ]
                },
            )
        return httpx.Response(200, json={"total": 0, "data": []})

    target = Paper(
        id="target",
        title="Target",
        published_at="2022-06-01",
        external_ids=ExternalIds(arxiv="2206.00001"),
    )

    async def resolve():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            async with SemanticScholarClient(client=http, rate_limiter=RateLimiter(0)) as client:
                return await resolve_candidate_inventory(target, refs, client)

    inventory = asyncio.run(resolve())
    assert len(refs) == 4
    assert inventory.total_count == 3
    assert [r.status for r in inventory.reference_resolutions] == [
        "resolved",
        "resolved",
        "resolved",
        "unresolved",
    ]
    c = inventory.candidates[0]
    assert c.title == "Canonical Alpha Method"
    assert [r.ref_order for r in c.source_references] == [1, 2]
    assert c.citation_contexts == ["Uses the method."]
    assert c.is_influential_citation is True
    assert calls[0][0] == "POST"
    assert len(calls) == 3
    ranking = rank_stage1_candidates(target, inventory, target_references_fully_resolved=False)
    assert len(ranking.ranked_candidates) == 2
    assert len(ranking.uncertain_candidates) == 1
    assert ranking.selection_policy_version == "lower-bound-v1"
    assert ranking.ranked_candidates[0].candidate.paper_id == "a" * 40
    assert ranking.ranked_candidates[0].score.selection_lower_bound == pytest.approx(0.5375)
    assert ranking.ranked_candidates[0].score.citation_importance == 0.75
    assert ranking.ranked_candidates[1].candidate.paper_id == "b" * 40
    assert ranking.ranked_candidates[1].score.initial_score == 1.0
    assert ranking.ranked_candidates[1].score.selection_lower_bound == 0.35
    assert all(r.score.semantic_similarity is None for r in ranking.ranked_candidates)
