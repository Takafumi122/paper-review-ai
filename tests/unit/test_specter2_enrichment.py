"""Offline S2 contract and failure regressions; short vectors are test data only."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from paper_review.application.embedding_enrichment import enrich_specter2_embeddings
from paper_review.domain.models import CandidatePaper, CandidateSet, ExternalIds, Paper
from paper_review.domain.stage1_scoring import calculate_semantic_similarity, rank_stage1_candidates
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SPECTER2_FIELDS,
    SPECTER2_MODEL,
    SemanticScholarClient,
    parse_s2_paper_json,
)

T, C, D = "a" * 40, "b" * 40, "c" * 40


def payload(pid=T, vector=None, model="specter_v2"):
    return {
        "paperId": pid,
        "title": "A scientific method",
        "embedding": {"model": model, "vector": vector if vector is not None else [1.0, 2.0]},
    }


def objects():
    return (
        Paper(id="target", title="Target", external_ids=ExternalIds(arxiv="1706.03762")),
        CandidateSet(
            target_paper_id="target",
            candidates=[
                CandidatePaper(
                    paper_id=C,
                    title="Candidate",
                    resolution_status="resolved",
                    external_ids=ExternalIds(semantic_scholar=C),
                ),
                CandidatePaper(
                    paper_id="unresolved:x",
                    title="Unresolved",
                    resolution_status="unresolved",
                    eligibility_status="uncertain",
                ),
            ],
        ),
    )


def run(handler, *, target=None, inventory=None, max_attempts=1, budget=40):
    original_target, original_inventory = objects()

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with SemanticScholarClient(
                client=http,
                rate_limiter=RateLimiter(0),
                max_attempts=max_attempts,
                backoff_factor=0,
                max_http_attempts=budget,
            ) as client:
                result = await enrich_specter2_embeddings(
                    target or original_target, inventory or original_inventory, client
                )
                return *result, client.http_attempts

    return asyncio.run(execute())


def test_explicit_batch_enriches_both_and_canonical_ranking_without_mutating_input():
    target, inventory = objects()
    before = inventory.model_dump()

    def handler(request):
        assert request.method == "POST"
        assert request.url.path.endswith("/paper/batch")
        assert request.url.params["fields"] == SPECTER2_FIELDS
        assert "embedding" not in request.url.params["fields"].split(",")
        assert json.loads(request.content)["ids"] == ["ARXIV:1706.03762", C]
        return httpx.Response(200, json=[payload(), payload(C, [2.0, 1.0])])

    enriched, candidates, calls = run(handler, target=target, inventory=inventory)
    assert calls == 1
    assert target.embedding is None and inventory.model_dump() == before
    assert enriched.external_ids.semantic_scholar == T
    assert candidates.target_s2_id == T
    assert enriched.embedding_model == candidates.candidates[0].embedding_model == SPECTER2_MODEL
    assert enriched.embedding_provenance["requested_field"] == "embedding.specter_v2"
    assert candidates.candidates[0].embedding_provenance["dimension"] == "2"
    assert candidates.candidates[1].embedding is None
    assert candidates.candidates[1].embedding_provenance["missing_reason"] == (
        "candidate_s2_identity_unavailable"
    )
    ranked = rank_stage1_candidates(enriched, candidates)
    assert len(ranked.ranked_candidates) == 1
    score = ranked.ranked_candidates[0].score
    assert score.semantic_similarity == pytest.approx(0.9)
    assert score.component_details["semantic_similarity"].calculation_source == "specter2"


@pytest.mark.parametrize(
    "requested,model,reason",
    [
        (False, "specter_v2", "embedding_not_requested"),
        (False, "specter@v0.1.1", "embedding_not_requested"),
        (True, "specter@v0.1.1", "embedding_model_mismatch"),
        (True, "unknown", "embedding_model_mismatch"),
        (True, None, "embedding_model_mismatch"),
    ],
)
def test_model_and_request_identity_fail_closed(requested, model, reason):
    paper = parse_s2_paper_json(payload(model=model), specter2_requested=requested)
    assert paper.embedding is None and paper.embedding_model is None
    assert paper.embedding_missing_reason == reason


@pytest.mark.parametrize(
    "vector,reason",
    [
        ([], "embedding_empty"),
        ("invalid", "embedding_malformed"),
        (["1", 2], "embedding_non_numeric"),
        ([True, 2], "embedding_non_numeric"),
        ([None, 2], "embedding_non_numeric"),
        ([[1], 2], "embedding_non_numeric"),
        ([float("nan"), 1], "embedding_non_finite"),
        ([float("inf"), 1], "embedding_non_finite"),
        ([-float("inf"), 1], "embedding_non_finite"),
        ([10**1000, 1], "embedding_non_finite"),
        ([0, 0], "embedding_zero_norm"),
    ],
)
def test_vector_validation(vector, reason):
    paper = parse_s2_paper_json(payload(vector=vector), specter2_requested=True)
    assert paper.embedding is None and paper.embedding_model is None
    assert paper.embedding_missing_reason == reason


@pytest.mark.parametrize("raw,reason", [(None, "embedding_missing"), ([], "embedding_malformed")])
def test_missing_embedding(raw, reason):
    data = payload()
    data["embedding"] = raw
    paper = parse_s2_paper_json(data, specter2_requested=True)
    assert paper.embedding is None and paper.embedding_missing_reason == reason


@pytest.mark.parametrize("which", ["target", "candidate"])
def test_partial_success_no_canonical_lexical_fallback(which):
    data = [payload(), payload(C)]
    data[0 if which == "target" else 1]["embedding"] = None
    target, inventory, _ = run(lambda _: httpx.Response(200, json=data))
    metric = calculate_semantic_similarity(target, inventory.candidates[0])
    assert metric.value is None and metric.calculation_source == "unavailable"
    assert len(inventory.candidates) == 2
    if which == "target":
        assert inventory.candidates[0].embedding is not None
    else:
        assert target.embedding is not None


def test_dimension_mismatch_rejects_candidate_not_target():
    target, inventory, _ = run(
        lambda _: httpx.Response(200, json=[payload(), payload(C, [1, 2, 3])])
    )
    assert target.embedding is not None and inventory.candidates[0].embedding is None
    assert inventory.candidates[0].embedding_provenance["missing_reason"] == (
        "embedding_dimension_mismatch"
    )


@pytest.mark.parametrize("status", [404, 429, 500, 503])
def test_http_failure_preserves_inventory(status):
    target, inventory, attempts = run(lambda _: httpx.Response(status, json={}))
    assert attempts == 1
    assert target.embedding is None
    assert len(inventory.candidates) == 2
    assert inventory.candidates[0].embedding is None
    assert str(status) in target.embedding_provenance["missing_reason"]


def test_5xx_retry_uses_existing_policy_and_budget():
    calls = []

    def handler(request):
        calls.append(request)
        return (
            httpx.Response(503)
            if len(calls) < 3
            else httpx.Response(200, json=[payload(), payload(C)])
        )

    target, _, attempts = run(handler, max_attempts=3)
    assert attempts == 3 and target.embedding is not None
    calls.clear()
    target, inventory, attempts = run(handler, max_attempts=3, budget=2)
    assert attempts == 2 and target.embedding is None
    assert len(inventory.candidates) == 2


def test_network_failure_is_missing_after_bounded_retries():
    def handler(request):
        raise httpx.ConnectError("offline test", request=request)

    target, inventory, attempts = run(handler, max_attempts=3)
    assert attempts == 3 and target.embedding is None
    assert len(inventory.candidates) == 2


def test_null_batch_item_preserves_successful_items():
    target, inventory, _ = run(lambda _: httpx.Response(200, json=[payload(), None]))
    assert target.embedding is not None
    assert inventory.candidates[0].embedding is None
    assert inventory.candidates[0].embedding_provenance["missing_reason"] == (
        "embedding_paper_not_found"
    )


def test_no_identities_means_no_http_and_stale_vectors_removed():
    target = Paper(id="target", embedding=[1.0], embedding_model="old")
    inventory = CandidateSet(
        target_paper_id="target",
        candidates=[
            CandidatePaper(paper_id="unknown", title="No identity", embedding=[1.0]),
        ],
    )

    def handler(_):
        pytest.fail("No network expected")

    target, inventory, attempts = run(handler, target=target, inventory=inventory)
    assert attempts == 0 and target.embedding is None and target.embedding_model is None
    assert inventory.candidates[0].embedding is None


def test_returned_identity_mismatch_rejected():
    target, inventory, _ = run(lambda _: httpx.Response(200, json=[payload(), payload(D)]))
    assert target.embedding is not None and inventory.candidates[0].embedding is None
    assert inventory.candidates[0].embedding_provenance["missing_reason"] == (
        "embedding_identity_mismatch"
    )


def test_duplicate_s2_identity_requested_once():
    target, inventory = objects()
    target.external_ids = ExternalIds(semantic_scholar=C)

    def handler(request):
        assert json.loads(request.content)["ids"] == [C]
        return httpx.Response(200, json=[payload(C)])

    enriched, candidates, attempts = run(handler, target=target, inventory=inventory)
    assert attempts == 1
    assert enriched.embedding == candidates.candidates[0].embedding


def test_invalid_batch_shape_is_nonfatal():
    target, inventory, _ = run(lambda _: httpx.Response(200, json={"invalid": True}))
    assert target.embedding is None and len(inventory.candidates) == 2
    assert "SemanticScholarParseError" in target.embedding_provenance["missing_reason"]


def test_malformed_candidate_vector_does_not_discard_other_embeddings():
    data = [payload(), payload(C, ["bad", 1])]
    target, inventory, _ = run(lambda _: httpx.Response(200, json=data))
    assert target.embedding is not None
    assert inventory.candidates[0].embedding is None
    assert inventory.candidates[0].embedding_provenance["missing_reason"] == "embedding_non_numeric"


def test_no_target_identity_still_enriches_candidate_without_similarity():
    target = Paper(id="target", title="Same words")

    def handler(request):
        assert json.loads(request.content)["ids"] == [C]
        return httpx.Response(200, json=[payload(C)])

    enriched, inventory, _ = run(handler, target=target)
    assert enriched.embedding is None
    assert inventory.candidates[0].embedding is not None
    assert calculate_semantic_similarity(enriched, inventory.candidates[0]).value is None


def test_missing_target_and_conflicting_candidate_dimensions_reject_all_vectors():
    target, inventory = objects()
    inventory.candidates.append(
        CandidatePaper(paper_id=D, title="Other candidate", resolution_status="resolved")
    )
    enriched, inventory, _ = run(
        lambda _: httpx.Response(200, json=[None, payload(C), payload(D, [1, 2, 3])]),
        target=target,
        inventory=inventory,
    )
    assert enriched.embedding is None
    assert all(c.embedding is None for c in inventory.candidates)
    assert inventory.candidates[0].embedding_provenance["missing_reason"] == (
        "embedding_dimension_mismatch"
    )


def test_429_stops_without_backoff_retry(monkeypatch):
    delays = []

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(
        "paper_review.infrastructure.academic_api.semantic_scholar.asyncio.sleep", sleep
    )
    _, inventory, attempts = run(
        lambda _: httpx.Response(429, headers={"Retry-After": "2"}), max_attempts=3
    )
    assert attempts == 1 and delays == []
    assert len(inventory.candidates) == 2
