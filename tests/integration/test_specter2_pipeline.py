"""Resolved inventory -> SPECTER2 enrichment -> unchanged canonical scorer."""

import asyncio
import json

import httpx
import pytest

from paper_review.application.embedding_enrichment import enrich_specter2_embeddings
from paper_review.domain.candidate_inventory import resolve_candidate_inventory
from paper_review.domain.models import ExternalIds, Paper, Reference
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient


def test_inventory_provenance_and_eligibility_survive_embedding_enrichment():
    target_id, candidate_id = "a" * 40, "b" * 40
    target = Paper(
        id="target",
        title="Target",
        published_at="2022-01-01",
        external_ids=ExternalIds(semantic_scholar=target_id),
    )
    refs = [
        Reference(
            id="r1",
            paper_id="target",
            revision_id="v1",
            ref_order=1,
            raw_text="An Author. Method. arXiv:2001.00001, 2020.",
        )
    ]
    metadata = {
        "paperId": candidate_id,
        "title": "Canonical Method",
        "authors": [{"name": "An Author"}],
        "year": 2020,
        "externalIds": {"ArXiv": "2001.00001"},
    }
    calls = []

    def handler(request):
        calls.append(request)
        if "embedding.specter_v2" in request.url.params.get("fields", ""):
            assert json.loads(request.content)["ids"] == [target_id, candidate_id]
            return httpx.Response(
                200,
                json=[
                    {
                        "paperId": target_id,
                        "title": "Target",
                        "embedding": {"model": "specter_v2", "vector": [1.0, 0.0]},
                    },
                    {**metadata, "embedding": {"model": "specter_v2", "vector": [0.0, 1.0]}},
                ],
            )
        if request.method == "POST":
            return httpx.Response(200, json=[metadata])
        return httpx.Response(200, json={"data": [{"citedPaper": metadata}]})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with SemanticScholarClient(client=http, rate_limiter=RateLimiter(0)) as client:
                original = await resolve_candidate_inventory(target, refs, client)
                enriched_target, enriched = await enrich_specter2_embeddings(
                    target, original, client
                )
                assert enriched.reference_resolutions == original.reference_resolutions
                assert enriched.candidates[0].source_references == refs
                assert enriched.candidates[0].eligibility_status == "eligible"
                assert enriched.candidates[0].metadata_provenance == (
                    original.candidates[0].metadata_provenance
                )
                return rank_stage1_candidates(enriched_target, enriched)

    result = asyncio.run(execute())
    assert len(calls) == 3
    assert result.top_k()[0].score.semantic_similarity == 0.5
    assert result.top_k()[0].score.initial_score == pytest.approx((0.4 * 0.5 + 0.35) / 0.75)
    assert result.top_k()[0].score.citation_importance is None
