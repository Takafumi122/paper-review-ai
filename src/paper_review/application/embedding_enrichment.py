"""Populate existing domain vectors from one bounded S2 batch, without changing scores."""

from __future__ import annotations

import re
import time

from paper_review.domain.models import CandidatePaper, CandidateSet, Paper
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SPECTER2_FIELD,
    SPECTER2_MODEL,
    S2Paper,
    SemanticScholarClient,
    SemanticScholarError,
)


def _s2_id(value: str | None) -> str | None:
    return value.lower() if value and re.fullmatch(r"[a-fA-F0-9]{40}", value) else None


async def enrich_specter2_embeddings(
    target: Paper, inventory: CandidateSet, client: SemanticScholarClient
) -> tuple[Paper, CandidateSet]:
    """Copy and enrich target plus resolved candidates; preserve inventory on failure.

    An arXiv/DOI target alias can resolve to a canonical S2 ID in this same batch.
    Candidate identities come only from the already resolved inventory. No title
    searches, local inference, lexical fallback, or per-candidate requests occur.
    """
    target = target.model_copy(deep=True)
    inventory = inventory.model_copy(deep=True)
    target_key = client.resolve_target_id(target.external_ids) or inventory.target_s2_id
    groups: dict[str, list[Paper | CandidatePaper]] = {}
    attempted_at = str(time.time())

    def unavailable(obj: Paper | CandidatePaper, reason: str, key: str | None = None) -> None:
        obj.embedding = None
        obj.embedding_model = None
        obj.embedding_provenance = {
            "source": f"{client.api_base_url}/paper/batch",
            "requested_field": SPECTER2_FIELD,
            "requested_model": SPECTER2_MODEL,
            "attempted_at": attempted_at,
            "status": "unavailable",
            "missing_reason": reason,
        }
        if key:
            obj.embedding_provenance["lookup_id"] = key

    unavailable(target, "target_identity_unavailable", target_key)
    if target_key:
        groups.setdefault(target_key, []).append(target)
    for candidate in inventory.candidates:
        key = _s2_id(candidate.external_ids.semantic_scholar) or _s2_id(candidate.paper_id)
        if candidate.resolution_status not in {"resolved", "legacy"}:
            key = None
        if key and _s2_id(candidate.paper_id) and key != _s2_id(candidate.paper_id):
            key = None
        unavailable(candidate, "candidate_s2_identity_unavailable", key)
        if key:
            groups.setdefault(key, []).append(candidate)
    if not groups:
        return target, inventory
    if len(groups) > 500:
        for key, objects in groups.items():
            for obj in objects:
                unavailable(obj, "embedding_batch_limit_exceeded", key)
        return target, inventory
    try:
        papers = await client.get_specter2_papers(list(groups))
    except SemanticScholarError as exc:
        reason = f"embedding_lookup:{type(exc).__name__}"
        status = getattr(exc, "status_code", None)
        if status is not None:
            reason += f":{status}"
        inventory.retrieval_warnings.append(reason)
        for key, objects in groups.items():
            for obj in objects:
                unavailable(obj, reason, key)
        return target, inventory

    def identity_matches(key: str, paper: S2Paper) -> bool:
        if not _s2_id(paper.paper_id):
            return False
        known = _s2_id(key)
        return known is None or known == paper.paper_id.lower()

    target_paper = papers.get(target_key) if target_key else None
    if target_paper and target_key and identity_matches(target_key, target_paper):
        target.external_ids = target.external_ids.model_copy(
            update={"semantic_scholar": target_paper.paper_id}
        )
        inventory.target_s2_id = target_paper.paper_id
    else:
        target_paper = None
    # The target anchors dimension when valid. Without it, accept only a single
    # consistent candidate dimension; no guessed dimension or majority vote.
    dimensions = {
        len(p.embedding)
        for key, p in papers.items()
        if p is not None and p.embedding is not None and identity_matches(key, p)
    }
    dimension = (
        len(target_paper.embedding)
        if target_paper and target_paper.embedding is not None
        else next(iter(dimensions))
        if len(dimensions) == 1
        else None
    )
    for key, objects in groups.items():
        paper = papers.get(key)
        reason = (
            "embedding_paper_not_found"
            if paper is None
            else "embedding_identity_mismatch"
            if not identity_matches(key, paper)
            else paper.embedding_missing_reason
        )
        if reason is None and paper is not None and paper.embedding is not None:
            if dimension is None or len(paper.embedding) != dimension:
                reason = "embedding_dimension_mismatch"
        for obj in objects:
            unavailable(obj, reason or "embedding_missing", key)
            if paper is None:
                continue
            provenance = obj.embedding_provenance
            provenance["paper_id"] = paper.paper_id
            if paper.embedding_response_model:
                provenance["response_model"] = paper.embedding_response_model
            if reason is None and paper.embedding is not None:
                obj.embedding = list(paper.embedding)
                obj.embedding_model = paper.embedding_model
                provenance.update(status="available", dimension=str(len(paper.embedding)))
                provenance.pop("missing_reason")
    return target, inventory
