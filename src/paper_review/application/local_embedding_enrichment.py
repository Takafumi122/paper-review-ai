"""Replace all vectors using one local SPECTER2 space; never mix with S2 vectors."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

from paper_review.domain.models import CandidatePaper, CandidateSet, Paper
from paper_review.infrastructure.academic_api.local_cache import LocalArtifactCache
from paper_review.infrastructure.local_specter2 import (
    PREPROCESSING,
    bundle_identity,
    load_local_specter2,
    valid_vector,
)


async def enrich_local_embeddings(
    target: Paper, inventory: CandidateSet, manifest: Path | None, cache: LocalArtifactCache,
) -> tuple[Paper, CandidateSet]:
    target = target.model_copy(deep=True)
    inventory = inventory.model_copy(deep=True)
    identity = bundle_identity(manifest)
    pending: list[tuple[Paper | CandidatePaper, str, str, str]] = []
    for paper in [target, *inventory.candidates]:
        paper.embedding = None
        paper.embedding_model = None
        paper.embedding_provenance = {
            "source": "local_specter2", "requested_model": identity,
            "preprocessing_version": PREPROCESSING, "status": "unavailable",
        }
        reason = (
            "local_specter2_unconfigured" if manifest is None or not manifest.is_file() else
            "candidate_identity_unresolved" if isinstance(paper, CandidatePaper)
            and paper.resolution_status != "resolved" else
            "embedding_title_or_abstract_missing" if not paper.title or not paper.abstract else None
        )
        if reason:
            paper.embedding_provenance["missing_reason"] = reason
            continue
        title, abstract = paper.title or "", paper.abstract or ""
        content_hash = hashlib.sha256(json.dumps([title, abstract]).encode()).hexdigest()
        key = f"{identity}:{content_hash}"
        paper.embedding_provenance["input_hash"] = content_hash
        saved = cache.get(key)
        if (saved is not None and saved.get("model") == identity
                and valid_vector(saved.get("vector"))):
            paper.embedding = saved["vector"]
            paper.embedding_model = identity
            paper.embedding_provenance.update(status="available", cache_hit="true", dimension="768")
        else:
            pending.append((paper, title, abstract, key))
    if not pending or manifest is None:
        return target, inventory
    try:
        model = await asyncio.to_thread(load_local_specter2, manifest, identity)
        vectors = await asyncio.to_thread(model.encode, [(t, a) for _, t, a, _ in pending])
        if len(vectors) != len(pending) or not all(valid_vector(v) for v in vectors):
            raise ValueError("invalid_local_specter2_vectors")
        for (paper, _, _, key), vector in zip(pending, vectors, strict=True):
            paper.embedding = vector
            paper.embedding_model = identity
            paper.embedding_provenance.update(
                status="available", cache_hit="false", dimension="768",
            )
            cache.put(key, {"model": identity, "vector": vector})
    except Exception:
        # Never log third-party exceptions: they can contain source text or local paths.
        inventory.retrieval_warnings.append("local_specter2_inference_unavailable")
        for paper, _, _, _ in pending:
            paper.embedding = None
            paper.embedding_model = None
            paper.embedding_provenance["status"] = "unavailable"
            paper.embedding_provenance["missing_reason"] = "local_specter2_inference_unavailable"
    return target, inventory
