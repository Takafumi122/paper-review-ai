"""Compose existing inventory enrichment and canonical Stage 1 ranking."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from paper_review.application.embedding_enrichment import enrich_specter2_embeddings
from paper_review.domain.models import CandidateSet, Paper, Stage1RankingResult
from paper_review.domain.stage1_scoring import (
    DEFAULT_SELECTION_POLICY,
    RUBRIC_WEIGHTS,
    SelectionPolicy,
    rank_stage1_candidates,
)
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient


class Stage1Analysis(BaseModel):
    """Retain enriched inputs alongside scores, including excluded/uncertain candidates."""

    model_config = ConfigDict(extra="forbid")
    target: Paper
    inventory: CandidateSet
    ranking: Stage1RankingResult
    citation_context_audit: dict = Field(default_factory=dict)


def apply_context_classifications(analysis: Stage1Analysis, audit: dict) -> Stage1Analysis:
    """Use maximum observed role per candidate; retain all decisions and unknown contexts."""
    classified: dict[str, list[dict]] = {}
    for context in audit["contexts"]:
        if context["status"] == "completed" and context["classification"] is not None:
            classified.setdefault(context["candidate_id"], []).append(context)
    maximum = {
        pid: max(items, key=lambda c: RUBRIC_WEIGHTS[c["classification"]])["classification"]
        for pid, items in classified.items()
    }
    complete = bool(analysis.inventory.reference_resolutions) and all(
        r.status == "resolved" for r in analysis.inventory.reference_resolutions
    )
    ranking = rank_stage1_candidates(
        analysis.target,
        analysis.inventory,
        target_references_fully_resolved=complete,
        citation_context_classifications=maximum,
        weights=analysis.ranking.weights,
        top_k=analysis.ranking.shortlist_limit,
        selection_policy=analysis.ranking.selection_policy_version,
    )
    for row in [*ranking.ranked_candidates, *ranking.unranked_candidates]:
        pid = row.candidate.paper_id
        if pid in maximum:
            metric = row.score.component_details["citation_importance"]
            source_ids = sorted({
                sid for c in classified[pid] if c["classification"] == maximum[pid]
                for sid in c.get("evidence_source_ids", c["source_ids"])
            })
            row.score.component_details["citation_importance"] = metric.model_copy(update={
                "source_ids": source_ids,
                "calculation_source": audit.get("version", "citation_context_max_v1"),
            })
        elif row.score.citation_importance is None:
            observed = [c for c in audit["contexts"] if c["candidate_id"] == pid]
            reason = (
                "citation_context_not_identified" if not observed else
                "citation_classification_unavailable" if any(
                    c["status"] == "failed" for c in observed
                ) else "citation_context_budget_unprocessed" if any(
                    c["status"] == "unprocessed" for c in observed
                ) else "citation_context_uncertain"
            )
            row.score.missing_reasons["citation_importance"] = reason
            row.score.component_details["citation_importance"] = row.score.component_details[
                "citation_importance"
            ].model_copy(update={"missing_reason": reason})
    return Stage1Analysis(
        target=analysis.target, inventory=analysis.inventory, ranking=ranking,
        citation_context_audit=audit,
    )


async def build_stage1_shortlist(
    target: Paper,
    candidate_inventory: CandidateSet,
    semantic_scholar_client: SemanticScholarClient,
    *, selection_policy: SelectionPolicy = DEFAULT_SELECTION_POLICY,
) -> Stage1Analysis:
    """Enrich a repaired inventory once, then rank with unchanged canonical defaults.

    No retrieval/resolution repeats, persistence, LLM calls, or lexical fallback.
    Embedding failures preserve candidates and produce missing/partial scores.
    The caller owns the provider's request/attempt budget and rate limiter.
    """
    if candidate_inventory.target_paper_id != target.id:
        raise ValueError("candidate_inventory_target_mismatch")
    enriched_target, enriched_inventory = await enrich_specter2_embeddings(
        target, candidate_inventory, semantic_scholar_client
    )
    references_complete = bool(enriched_inventory.reference_resolutions) and all(
        r.status == "resolved" for r in enriched_inventory.reference_resolutions
    )
    ranking = rank_stage1_candidates(
        enriched_target,
        enriched_inventory,
        semantic_strategy="specter2_canonical",
        target_references_fully_resolved=references_complete,
        selection_policy=selection_policy,
    )
    return Stage1Analysis(target=enriched_target, inventory=enriched_inventory, ranking=ranking)
