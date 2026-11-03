"""Offline application E2E: repaired inventory, synthetic S2 success/error, audit."""

import asyncio

import httpx
import pytest

from paper_review.application.stage1_audit import EvaluationArtifact, audit_stage1
from paper_review.application.stage1_shortlist import build_stage1_shortlist
from paper_review.domain.candidate_inventory import resolve_candidate_inventory
from paper_review.domain.models import CandidateSet, DateProvenance, ExternalIds, Paper, Reference
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient


@pytest.mark.parametrize("status", [200, 429])
def test_application_from_repaired_inventory_to_shortlist_and_audit(status):
    tid, cid = "a" * 40, "b" * 40
    target = Paper(
        id="target",
        title="Target",
        published_at="2022-01-01",
        external_ids=ExternalIds(semantic_scholar=tid),
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
        "paperId": cid,
        "title": "Canonical Method",
        "year": 2020,
        "authors": [{"name": "An Author"}],
        "externalIds": {"ArXiv": "2001.00001"},
    }
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "e2e-dummy"
        if "embedding.specter_v2" in request.url.params.get("fields", ""):
            if status == 429:
                return httpx.Response(429, json={"message": "rate limited"})
            return httpx.Response(
                200,
                json=[
                    {
                        "paperId": tid,
                        "title": "Target",
                        "embedding": {"model": "specter_v2", "vector": [1.0, 0.0]},
                    },
                    {**metadata, "embedding": {"model": "specter_v2", "vector": [0.0, 1.0]}},
                ],
            )
        if request.method == "POST":
            return httpx.Response(200, json=[metadata])
        return httpx.Response(200, json={"data": [{"citedPaper": metadata}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with SemanticScholarClient(
                client=http, api_key="e2e-dummy", rate_limiter=RateLimiter(0), max_attempts=1
            ) as client:
                inventory = await resolve_candidate_inventory(target, refs, client)
                before = inventory.model_dump()
                analysis = await build_stage1_shortlist(target, inventory, client)
                assert inventory.model_dump() == before and target.embedding is None
                assert analysis.inventory.reference_resolutions == inventory.reference_resolutions
                candidate = analysis.inventory.candidates[0]
                assert candidate.source_references == refs
                assert candidate.metadata_provenance == inventory.candidates[0].metadata_provenance
                assert candidate.resolution_status == "resolved"
                assert candidate.date_provenance == DateProvenance(
                    kind="publication_year",
                    value="2020",
                    precision="year",
                    source_id="semantic_scholar",
                )
                assert candidate.eligibility_status == "eligible"
                return analysis

    analysis = asyncio.run(run())
    assert len(calls) == 3  # two resolution requests, exactly one embedding batch
    ranked = analysis.ranking.top_k()
    assert len(ranked) == 1
    score = ranked[0].score
    assert score.status == "partial" and score.citation_importance is None
    if status == 200:
        assert score.semantic_similarity == 0.5
        assert score.initial_score == pytest.approx((0.4 * 0.5 + 0.35) / 0.75)
        assert score.component_details["semantic_similarity"].calculation_source == "specter2"
    else:
        assert score.semantic_similarity is None
        assert score.initial_score == 1.0 and score.coverage == 0.35
        assert "429" in analysis.target.embedding_provenance["missing_reason"]

    # Reuse the evaluator's sanitized artifact contract; no body/vector serialization.
    def outcome(c):
        return {
            "paper_id": c.paper_id,
            "title": c.title,
            "eligibility": c.eligibility_status,
            "embedding_available": c.embedding is not None,
            "embedding_model": c.embedding_model,
            "dimension": len(c.embedding) if c.embedding else None,
            "embedding_provenance": c.embedding_provenance,
        }

    artifact = EvaluationArtifact.model_validate(
        {
            "target": "Target",
            "statistics": {
                "target_embedding_available": analysis.target.embedding is not None,
                "target_model": analysis.target.embedding_model,
                "target_dimension": len(analysis.target.embedding)
                if analysis.target.embedding
                else None,
                "target_embedding_provenance": analysis.target.embedding_provenance,
                "http_statuses": [status],
            },
            "inventory_sha256": "c" * 64,
            "fixture_sha256": "d" * 64,
            "scoring_version": analysis.ranking.scoring_version,
            "ranking": [
                {**outcome(r.candidate), "rank": r.rank, "score": r.score.model_dump()}
                for r in ranked
            ],
            "probes": [{"probe": "Method", "paper_id": cid}],
            "candidate_embedding_outcomes": [outcome(c) for c in analysis.inventory.candidates],
            "evidence": {"data_origin": "synthetic", "provider_contract_verified": False},
        }
    )
    audit = audit_stage1(artifact, input_sha256="e" * 64)
    assert audit["gate"] == "NOT EVALUABLE"  # synthetic success also cannot certify quality
    assert audit["probes"][0]["present"]
    assert audit["metric_coverage"]["semantic_similarity"]["observed"] == (
        1 if status == 200 else 0
    )


def test_application_rejects_inventory_for_wrong_target_before_request():
    async def run():
        async with SemanticScholarClient(api_key="dummy") as client:
            with pytest.raises(ValueError, match="target_mismatch"):
                await build_stage1_shortlist(
                    Paper(id="a"), CandidateSet(target_paper_id="b"), client
                )
            assert client.http_attempts == 0

    asyncio.run(run())


def test_frozen_repaired_inventory_preserved_and_default_top10_is_bounded():
    import json
    from pathlib import Path

    raw = json.loads(
        (
            Path(__file__).resolve().parents[2]
            / "evaluation/reports/candidate-inventory/1706.03762-result.json"
        ).read_text()
    )
    target = Paper.model_validate(raw["target"])
    inventory = CandidateSet.model_validate(raw["inventory"])
    before = inventory.model_dump()
    calls = []

    def handler(request):
        calls.append(request)
        assert "embedding.specter_v2" in request.url.params["fields"]
        ids = json.loads(request.content)["ids"]
        return httpx.Response(
            200,
            json=[
                {
                    "paperId": "a" * 40 if pid.startswith("ARXIV:") else pid,
                    "title": "Synthetic embedding response",
                    "embedding": {"model": "specter_v2", "vector": [1.0, 1.0]},
                }
                for pid in ids
            ],
        )

    async def run():
        async with SemanticScholarClient(
            transport=httpx.MockTransport(handler),
            api_key="dummy",
            rate_limiter=RateLimiter(0),
            max_attempts=1,
            max_http_attempts=1,
        ) as client:
            return await build_stage1_shortlist(target, inventory, client)

    result = asyncio.run(run())
    assert len(calls) == 1
    assert inventory.model_dump() == before and target.embedding is None
    assert len(result.ranking.top_k()) == 10
    assert len(result.ranking.ranked_candidates) == 34
    assert result.ranking.weights == {
        "semantic_similarity": 0.40,
        "citation_relation": 0.35,
        "citation_importance": 0.25,
    }
    for old, new in zip(inventory.candidates, result.inventory.candidates, strict=True):
        old_data, new_data = old.model_dump(), new.model_dump()
        for field in ("embedding", "embedding_model", "embedding_provenance"):
            old_data.pop(field)
            new_data.pop(field)
        assert old_data == new_data
    assert result.inventory.reference_resolutions == inventory.reference_resolutions
