"""Audit sanitized Stage 1 evaluation artifacts; never infer live data from vectors."""

from __future__ import annotations

from collections import Counter
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from paper_review.domain.models import Stage1Score
from paper_review.infrastructure.academic_api.semantic_scholar import SPECTER2_MODEL


class EmbeddingOutcome(BaseModel):
    model_config = ConfigDict(extra="ignore")
    paper_id: str
    title: str
    eligibility: str
    embedding_available: bool
    embedding_model: str | None = None
    dimension: int | None = Field(default=None, gt=0)
    embedding_provenance: dict[str, str] = Field(default_factory=dict)


class EvaluationRow(EmbeddingOutcome):
    rank: int = Field(ge=0)
    score: Stage1Score


class Probe(BaseModel):
    model_config = ConfigDict(extra="ignore")
    probe: str
    paper_id: str


class EvaluationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_origin: Literal[
        "unknown", "synthetic", "error_fixture", "live_provider", "recorded_provider_success"
    ] = "unknown"
    provider_contract_verified: bool = False


class EvaluationArtifact(BaseModel):
    """Compatible with Task 2.7 JSON, with optional new evidence/unranked fields."""

    model_config = ConfigDict(extra="ignore")
    target: str
    statistics: dict[str, Any] = Field(default_factory=dict)
    inventory_sha256: str
    fixture_sha256: str
    scoring_version: str
    selection_policy_version: Literal["observed-renormalized-v1", "lower-bound-v1"] = (
        "observed-renormalized-v1"
    )
    shortlist_limit: int = Field(default=10, ge=0, le=10)
    ranking: list[EvaluationRow]
    unranked: list[EvaluationRow] = Field(default_factory=list)
    probes: list[Probe]
    candidate_embedding_outcomes: list[EmbeddingOutcome]
    evidence: EvaluationEvidence = Field(default_factory=EvaluationEvidence)


class QualityReview(BaseModel):
    """Separate human review; bound to the exact evaluation artifact's bytes."""

    model_config = ConfigDict(extra="forbid")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    reviewer: str = Field(min_length=1)
    rubric_version: str = Field(min_length=1)
    decision: Literal["pass", "fail", "inconclusive"]
    notes: str = Field(min_length=1)


def _embedding_summary(item: EmbeddingOutcome) -> dict[str, Any]:
    p = item.embedding_provenance
    return {
        "paper_id": item.paper_id,
        "title": item.title,
        "eligibility": item.eligibility,
        "embedding_available": item.embedding_available,
        "model": item.embedding_model,
        "provider_model": p.get("response_model"),
        "dimension": item.dimension,
        "source": p.get("source"),
        "missing_reason": p.get("missing_reason"),
    }


def audit_stage1(
    artifact: EvaluationArtifact, *, input_sha256: str, quality_review: QualityReview | None = None
) -> dict[str, Any]:
    """Report coverage for all eligible candidates, including unavailable scores.

    Origin/contract attestations must come from verified acquisition evidence. They
    are not inferred from the presence of a provider-shaped synthetic test response.
    This gate measures data evaluability, not ranking quality or Stage 2 readiness.
    """
    outcomes = sorted(artifact.candidate_embedding_outcomes, key=lambda c: c.paper_id)
    by_id = {c.paper_id: c for c in outcomes}
    rows = sorted(artifact.ranking, key=lambda r: (r.rank, r.paper_id))
    eligible = [c for c in outcomes if c.eligibility == "eligible"]
    scored = {r.paper_id: r for r in [*rows, *artifact.unranked]}
    eligible_rows = [scored[c.paper_id] for c in eligible if c.paper_id in scored]
    missing_scores = len(eligible) - len(eligible_rows)
    available = [c for c in eligible if c.embedding_available]
    stats = artifact.statistics
    reasons: list[str] = []
    if not stats.get("target_embedding_available"):
        reasons.append("target_embedding_missing")
    if not available:
        reasons.append("zero_candidate_embedding_coverage")
    if artifact.evidence.data_origin not in {"live_provider", "recorded_provider_success"}:
        reasons.append("provider_success_provenance_unverified")
    if not artifact.evidence.provider_contract_verified:
        reasons.append("provider_contract_unverified")
    if stats.get("http_statuses") != [200]:
        reasons.append("successful_provider_response_unverified")
    target_p = stats.get("target_embedding_provenance", {})
    target_dim = stats.get("target_dimension")
    if (
        stats.get("target_model") != SPECTER2_MODEL
        or type(target_dim) is not int
        or target_dim <= 0
        or target_p.get("requested_field") != "embedding.specter_v2"
        or target_p.get("status") != "available"
        or target_p.get("response_model") != "specter_v2"
    ):
        reasons.append("target_embedding_contract_invalid")
    valid_provider_candidates = [
        c
        for c in available
        if c.embedding_model == SPECTER2_MODEL
        and c.dimension == target_dim
        and c.embedding_provenance.get("requested_field") == "embedding.specter_v2"
        and c.embedding_provenance.get("status") == "available"
        and c.embedding_provenance.get("response_model") == target_p.get("response_model")
        and c.embedding_provenance.get("source") == target_p.get("source")
        and c.embedding_provenance.get("source")
        == ("https://api.semanticscholar.org/graph/v1/paper/batch")
    ]
    canonical = [
        r
        for r in eligible_rows
        if r.score.semantic_similarity is not None
        and r.score.component_details.get("semantic_similarity") is not None
        and r.score.component_details["semantic_similarity"].calculation_source == "specter2"
        and r.paper_id in {c.paper_id for c in valid_provider_candidates}
    ]
    if not canonical:
        reasons.append("real_canonical_semantic_similarity_unavailable")
    if len(valid_provider_candidates) != len(available):
        reasons.append("candidate_embedding_contract_invalid")
    if missing_scores:
        reasons.append("eligible_score_records_missing")
    if len(by_id) != len(outcomes):
        reasons.append("duplicate_candidate_identity")
    if len(scored) != len(rows) + len(artifact.unranked):
        reasons.append("duplicate_score_identity")
    if [r.rank for r in rows] != list(range(1, len(rows) + 1)):
        reasons.append("ranking_order_invalid")
    if artifact.selection_policy_version == "lower-bound-v1":
        if any(r.score.selection_lower_bound is None or r.score.selection_upper_bound is None
               or r.score.initial_score is None for r in rows):
            reasons.append("selection_bounds_missing")
        elif rows != sorted(rows, key=lambda r: (
            -(r.score.selection_lower_bound if r.score.selection_lower_bound is not None else -1),
            -r.score.coverage, r.paper_id,
        )):
            reasons.append("selection_order_invalid")
    if any(r.paper_id not in by_id or by_id[r.paper_id].eligibility != "eligible" for r in rows):
        reasons.append("ranking_inventory_mismatch")
    if any(
        len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value)
        for value in (artifact.inventory_sha256, artifact.fixture_sha256, input_sha256)
    ):
        reasons.append("source_hashes_invalid")
    if any(
        r.paper_id in by_id
        and (
            r.embedding_available != by_id[r.paper_id].embedding_available
            or r.embedding_model != by_id[r.paper_id].embedding_model
            or r.dimension != by_id[r.paper_id].dimension
        )
        for r in [*rows, *artifact.unranked]
    ):
        reasons.append("score_embedding_inventory_mismatch")
    gate = "NOT EVALUABLE" if reasons else "READY FOR QUALITY AUDIT"
    if quality_review is not None:
        if quality_review.input_sha256 != input_sha256:
            raise ValueError("quality_review_input_hash_mismatch")
        if not reasons:
            gate = "QUALITY AUDIT RESULT"
    top = rows[: artifact.shortlist_limit]
    rank_by_id = {r.paper_id: r.rank for r in rows}
    probes = []
    for probe in sorted(artifact.probes, key=lambda p: (p.probe, p.paper_id)):
        candidate = by_id.get(probe.paper_id)
        rank = rank_by_id.get(probe.paper_id)
        probes.append(
            {
                "probe": probe.probe,
                "paper_id": probe.paper_id,
                "present": candidate is not None,
                "eligibility": candidate.eligibility if candidate else None,
                "embedding_available": candidate.embedding_available if candidate else False,
                "rank": rank,
                "top10": rank is not None and rank <= artifact.shortlist_limit,
            }
        )
    metrics = {}
    for name in ("semantic_similarity", "citation_relation", "citation_importance"):
        count = sum(getattr(r.score, name) is not None for r in eligible_rows)
        metrics[name] = {"observed": count, "missing": len(eligible) - count}
    score_counts = Counter(r.score.status for r in eligible_rows)
    score_counts["unavailable"] += missing_scores
    return {
        "schema_version": "1",
        "target": artifact.target,
        "gate": gate,
        "gate_reasons": reasons,
        "data_evidence": artifact.evidence.model_dump(),
        "input_sha256": input_sha256,
        "inventory_sha256": artifact.inventory_sha256,
        "fixture_sha256": artifact.fixture_sha256,
        "scoring_version": artifact.scoring_version,
        "selection_policy_version": artifact.selection_policy_version,
        "counts": {
            "candidates": len(outcomes),
            "eligible": len(eligible),
            "ranked": len(rows),
            "top_k": len(top),
            "top_k_limit": artifact.shortlist_limit,
        },
        "embeddings": {
            "target_available": stats.get("target_embedding_available", False),
            "target_model": stats.get("target_model"),
            "target_provider_model": target_p.get("response_model"),
            "target_dimension": target_dim,
            "target_missing_reason": target_p.get("missing_reason"),
            "eligible_available": len(available),
            "eligible_missing": len(eligible) - len(available),
            "coverage_percent": 100 * len(available) / len(eligible) if eligible else None,
            "models": sorted({c.embedding_model for c in available if c.embedding_model}),
            "dimensions": sorted({c.dimension for c in available if c.dimension is not None}),
            "missing_reasons": dict(
                sorted(
                    Counter(
                        c.embedding_provenance.get("missing_reason", "unknown")
                        for c in eligible
                        if not c.embedding_available
                    ).items()
                )
            ),
        },
        "metric_coverage": metrics,
        "score_counts": {
            name: score_counts[name] for name in ("complete", "partial", "unavailable")
        },
        "top10": [
            dict(rank=r.rank, **_embedding_summary(r), score=r.score.model_dump()) for r in top
        ],
        "probes": probes,
        "quality_review": quality_review.model_dump() if quality_review and not reasons else None,
        "quality_review_applied": quality_review is not None and not reasons,
        "stage2_ready": False,
    }


def render_audit_markdown(reports: list[dict[str, Any]]) -> str:
    """Allowlist-only tables: vectors, request headers and paper bodies are excluded."""

    def cell(value: Any) -> str:
        return "—" if value is None else str(value).replace("|", "\\|").replace("\n", " ")

    lines = [
        "# Stage 1 Re-Audit preparation",
        "",
        "Readiness refers to data for a quality audit, not quality acceptance.",
        "Diagnostic probes are not scientific gold labels. Stage 2 remains blocked.",
        "",
    ]
    for report in sorted(reports, key=lambda r: r["target"]):
        lines += [
            f"## {cell(report['target'])}",
            "",
            f"**{report['gate']}**",
            "",
            "Reasons: " + ", ".join(report["gate_reasons"] or ["none"]),
            "",
            "| Measure | Value |",
            "|---|---|",
        ]
        for group in ("counts", "embeddings", "metric_coverage", "score_counts"):
            lines += [f"| {group}.{k} | {cell(v)} |" for k, v in report[group].items()]
        lines += [
            "",
            "### Top 10",
            "",
            "| Rank | Title | InitialScore | Selection lower | Selection upper | "
            "SemanticSimilarity | CitationRelation | "
            "CitationImportance | Coverage | Embedding available | Source/model |",
            "|---:|---|---:|---:|---:|---:|---:|---:|---:|---|---|",
        ]
        for row in report["top10"]:
            s = row["score"]
            values = [
                row["rank"],
                row["title"],
                s["initial_score"],
                s.get("selection_lower_bound"),
                s.get("selection_upper_bound"),
                s["semantic_similarity"],
                s["citation_relation"],
                s["citation_importance"],
                s["coverage"],
                row["embedding_available"],
                f"{row['source']} / {row['model']}",
            ]
            lines.append("| " + " | ".join(map(cell, values)) + " |")
        lines += [
            "",
            "### Diagnostic probes",
            "",
            "| Probe | Present | Eligibility | Embedding available | Rank | Top 10 |",
            "|---|---|---|---|---:|---|",
        ]
        for p in report["probes"]:
            lines.append(
                "| "
                + " | ".join(
                    cell(p[k])
                    for k in (
                        "probe",
                        "present",
                        "eligibility",
                        "embedding_available",
                        "rank",
                        "top10",
                    )
                )
                + " |"
            )
        if report["quality_review"]:
            lines += ["", "Human quality review: " + cell(report["quality_review"])]
        lines += [""]
    return "\n".join(lines)
