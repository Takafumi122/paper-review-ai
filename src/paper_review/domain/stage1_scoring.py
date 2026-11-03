"""Stage 1 Initial Score and Candidate Ranking for Research Lineage.

Adheres strictly to docs/scoring-lineage.md and docs/research-lineage.md:
- InitialScore = 0.40 * SemanticSimilarity + 0.35 * CitationRelation + 0.25 * CitationImportance
- Missing value policy: missing != 0 (coverage-weighted aggregation, score=None if coverage=0)
- Normalization: 0.0 <= component <= 1.0
- Deterministic selection: lower bound desc -> coverage desc -> paper_id asc
"""

from __future__ import annotations

import math
import re
from typing import Final, Literal

from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    MetricValue,
    Paper,
    Stage1RankedCandidate,
    Stage1RankingResult,
    Stage1Score,
)

# Canonical weights from docs/scoring-lineage.md Section 1
DEFAULT_STAGE1_WEIGHTS: Final[dict[str, float]] = {
    "semantic_similarity": 0.40,
    "citation_relation": 0.35,
    "citation_importance": 0.25,
}

SCORING_VERSION: Final[str] = "2"
DEFAULT_TOP_K: Final[int] = 10
SelectionPolicy = Literal["observed-renormalized-v1", "lower-bound-v1"]
DEFAULT_SELECTION_POLICY: Final[SelectionPolicy] = "lower-bound-v1"
LEGACY_SELECTION_POLICY: Final[SelectionPolicy] = "observed-renormalized-v1"


# ---------------------------------------------------------------------------
# 1. Semantic Similarity
# ---------------------------------------------------------------------------


def calculate_specter2_similarity(
    target_vec: list[float] | None,
    candidate_vec: list[float] | None,
) -> tuple[float | None, str | None]:
    """Calculate SPECTER2 cosine similarity per docs/scoring-lineage.md Section 3.

    cos = dot(target, candidate) / (norm(target) * norm(candidate))
    SemanticSimilarity = (clip(cos, -1, 1) + 1) / 2
    Missing/zero-norm/dimension-mismatch returns (None, reason).
    """
    if target_vec is None or candidate_vec is None:
        return None, "embedding_unavailable"

    if len(target_vec) == 0 or len(candidate_vec) == 0:
        return None, "embedding_empty"

    if len(target_vec) != len(candidate_vec):
        return None, "embedding_dimension_mismatch"

    if not all(math.isfinite(x) for x in target_vec + candidate_vec):
        return None, "embedding_non_finite"

    norm_t_sq = sum(x * x for x in target_vec)
    norm_c_sq = sum(x * x for x in candidate_vec)

    if norm_t_sq <= 0.0 or norm_c_sq <= 0.0:
        return None, "embedding_zero_norm"

    dot = sum(a * b for a, b in zip(target_vec, candidate_vec, strict=True))
    cos = dot / (math.sqrt(norm_t_sq) * math.sqrt(norm_c_sq))

    if not math.isfinite(cos):
        return None, "embedding_non_finite"

    # Clip only for floating-point rounding errors per canonical spec
    cos_clipped = max(-1.0, min(1.0, cos))
    similarity = (cos_clipped + 1.0) / 2.0
    return similarity, None


def _tokenize(text: str) -> list[str]:
    """Simple alphanumeric tokenizer for deterministic lexical baseline."""
    return re.findall(r"\b[a-z0-9]+\b", text.lower())


def calculate_lexical_similarity(
    target_text: str,
    candidate_text: str,
) -> tuple[float | None, str | None]:
    """Deterministic token frequency cosine similarity baseline on title + abstract.

    Returns (score, None) in [0.0, 1.0], or (None, missing_reason) if text is empty.
    """
    t_tokens = _tokenize(target_text)
    c_tokens = _tokenize(candidate_text)

    if not t_tokens or not c_tokens:
        return None, "insufficient_text"

    t_counts: dict[str, int] = {}
    for tok in t_tokens:
        t_counts[tok] = t_counts.get(tok, 0) + 1

    c_counts: dict[str, int] = {}
    for tok in c_tokens:
        c_counts[tok] = c_counts.get(tok, 0) + 1

    common = set(t_counts.keys()) & set(c_counts.keys())
    if not common:
        return 0.0, None

    dot = sum(t_counts[tok] * c_counts[tok] for tok in common)
    norm_t = math.sqrt(sum(v * v for v in t_counts.values()))
    norm_c = math.sqrt(sum(v * v for v in c_counts.values()))

    if norm_t == 0.0 or norm_c == 0.0:
        return None, "insufficient_text"

    cos = dot / (norm_t * norm_c)
    return max(0.0, min(1.0, cos)), None


def calculate_semantic_similarity(
    target_paper: Paper,
    candidate: CandidatePaper,
    *,
    strategy: str = "specter2_canonical",
) -> MetricValue:
    """Calculate SemanticSimilarity between Target and Candidate.

    Strategies:
    - 'specter2_canonical': strict docs/scoring-lineage.md adherence. Uses SPECTER2
      embeddings if present on target and candidate; returns None if unavailable.
    - 'lexical': deterministic token cosine similarity on title + abstract (fallback: title).
    - 'hybrid': uses SPECTER2 vectors if present, falls back to lexical baseline.
    """
    target_text = f"{target_paper.title or ''} {target_paper.abstract or ''}".strip()
    candidate_text = f"{candidate.title} {candidate.abstract or ''}".strip()

    if strategy in ("specter2_canonical", "hybrid"):
        score, reason = calculate_specter2_similarity(
            target_paper.embedding,
            candidate.embedding,
        )
        if score is not None:
            if not target_paper.embedding_model or not candidate.embedding_model:
                score, reason = None, "embedding_model_unavailable"
            elif target_paper.embedding_model != candidate.embedding_model:
                score, reason = None, "embedding_model_mismatch"
        if score is not None or strategy == "specter2_canonical":
            return MetricValue(
                value=score,
                missing_reason=reason,
                source_ids=[target_paper.id, candidate.paper_id],
                normalization_version=SCORING_VERSION,
                calculation_source="specter2" if score is not None else "unavailable",
            )

    if strategy in ("lexical", "hybrid"):
        score, reason = calculate_lexical_similarity(target_text, candidate_text)
        return MetricValue(
            value=score,
            missing_reason=reason,
            source_ids=[target_paper.id, candidate.paper_id],
            normalization_version=SCORING_VERSION,
            calculation_source=(
                ("lexical_fallback" if strategy == "hybrid" else "lexical")
                if score is not None
                else "unavailable"
            ),
        )

    raise ValueError(f"Unknown semantic similarity strategy: {strategy!r}")


# ---------------------------------------------------------------------------
# 2. Citation Relation
# ---------------------------------------------------------------------------


def calculate_citation_relation(
    candidate: CandidatePaper,
    *,
    target_references_fully_resolved: bool = True,
) -> MetricValue:
    """Calculate CitationRelation per docs/scoring-lineage.md Section 3.

    - Directly cited in Target References: 1.00
    - Confirmed non-cited (Target references fully resolved): 0.00
    - Unresolved/unverified citation status: None
    """
    if candidate.is_target_reference:
        return MetricValue(
            value=1.00,
            missing_reason=None,
            source_ids=[candidate.paper_id],
            normalization_version=SCORING_VERSION,
        )

    if target_references_fully_resolved:
        return MetricValue(
            value=0.00,
            missing_reason=None,
            source_ids=[candidate.paper_id],
            normalization_version=SCORING_VERSION,
        )

    return MetricValue(
        value=None,
        missing_reason="reference_relation_unverified",
        source_ids=[candidate.paper_id],
        normalization_version=SCORING_VERSION,
    )


# ---------------------------------------------------------------------------
# 3. Citation Importance
# ---------------------------------------------------------------------------

RUBRIC_WEIGHTS: Final[dict[str, float]] = {
    "core_foundation": 1.00,
    "motivating_prior_work": 0.75,
    "comparison_baseline": 0.50,
    "general_background": 0.25,
    "confirmed_non_cited": 0.00,
}


def calculate_citation_importance(
    candidate: CandidatePaper,
    *,
    mode: str = "canonical",
    target_references_fully_resolved: bool = True,
    citation_context_classification: str | None = None,
) -> MetricValue:
    """Calculate CitationImportance per docs/scoring-lineage.md Section 3.

    Modes:
    - 'canonical':
      * Confirmed non-cited: 0.00
      * Explicit rubric classification: 1.00, 0.75, 0.50, 0.25, 0.00
      * is_influential_citation == True proxy: 0.75
      * Otherwise (is_influential is False or unclassified): None
        ("isInfluential=false is not evidence of 0, alone it is null")
    - 'bibliometric': log scaling of candidate citation count (outlier-resistant heuristic)
    - 'hybrid': canonical context/proxy first, bibliometric fallback if null
    """
    if mode in ("canonical", "hybrid"):
        # 1. Confirmed non-cited
        if not candidate.is_target_reference and target_references_fully_resolved:
            return MetricValue(
                value=0.00,
                missing_reason=None,
                source_ids=[candidate.paper_id],
                normalization_version=SCORING_VERSION,
            )

        # 2. Rubric classification if provided
        if citation_context_classification:
            val = RUBRIC_WEIGHTS.get(citation_context_classification)
            if val is not None:
                return MetricValue(
                    value=val,
                    missing_reason=None,
                    source_ids=[candidate.paper_id],
                    normalization_version=SCORING_VERSION,
                )

        # 3. isInfluential proxy (docs/scoring-lineage.md line 78)
        if candidate.is_influential_citation is True:
            return MetricValue(
                value=0.75,
                missing_reason=None,
                source_ids=[candidate.paper_id],
                normalization_version=SCORING_VERSION,
            )

        # Canonical mode: unclassified context without influential flag is null
        if mode == "canonical":
            return MetricValue(
                value=None,
                missing_reason="context_unclassified_nano_unavailable",
                source_ids=[candidate.paper_id],
                normalization_version=SCORING_VERSION,
            )

    # Bibliometric or hybrid fallback (Prompt Section 7: log scaling)
    if candidate.citation_count is not None and candidate.citation_count >= 0:
        # Scale logarithmically: 10,000 citations maps to ~1.0
        max_log = math.log1p(10000.0)
        scaled = min(1.0, math.log1p(float(candidate.citation_count)) / max_log)
        return MetricValue(
            value=scaled,
            missing_reason=None,
            source_ids=[candidate.paper_id],
            normalization_version=SCORING_VERSION,
        )

    return MetricValue(
        value=None,
        missing_reason="citation_count_unavailable",
        source_ids=[candidate.paper_id],
        normalization_version=SCORING_VERSION,
    )


# ---------------------------------------------------------------------------
# 4. Aggregation and InitialScore
# ---------------------------------------------------------------------------


def calculate_initial_score(
    semantic_metric: MetricValue,
    citation_relation_metric: MetricValue,
    citation_importance_metric: MetricValue,
    *,
    weights: dict[str, float] = DEFAULT_STAGE1_WEIGHTS,
    scoring_version: str = SCORING_VERSION,
) -> Stage1Score:
    """Aggregate component metrics into InitialScore per docs/scoring-lineage.md Section 4.

    O = observed metric indices
    coverage = sum(w[i] for i in O)
    score = sum(w[i] * value[i] for i in O) / coverage   # coverage > 0
    score = None                                         # coverage == 0
    """
    components = [
        ("semantic_similarity", semantic_metric),
        ("citation_relation", citation_relation_metric),
        ("citation_importance", citation_importance_metric),
    ]

    weighted_sum = 0.0
    coverage = 0.0
    missing_components: list[str] = []
    missing_reasons: dict[str, str] = {}
    component_details: dict[str, MetricValue] = {}

    for name, metric in components:
        component_details[name] = metric
        weight = weights.get(name, 0.0)
        if metric.value is not None:
            weighted_sum += weight * metric.value
            coverage += weight
        else:
            missing_components.append(name)
            if metric.missing_reason:
                missing_reasons[name] = metric.missing_reason

    if coverage > 0.0:
        initial_score = weighted_sum / coverage
        status = "complete" if abs(coverage - 1.0) < 1e-6 else "partial"
    else:
        initial_score = None
        status = "unavailable"

    total_weight = sum(weights.get(name, 0.0) for name, _ in components)
    lower = weighted_sum / total_weight if total_weight > 0 else None
    upper = (weighted_sum + total_weight - coverage) / total_weight if total_weight > 0 else None
    # Preserve the exact point value for complete observations; no rounding or
    # additional tie-break is allowed for mathematically identical bounds.
    if not missing_components and lower is not None:
        upper = lower

    return Stage1Score(
        semantic_similarity=semantic_metric.value,
        citation_relation=citation_relation_metric.value,
        citation_importance=citation_importance_metric.value,
        initial_score=initial_score,
        selection_lower_bound=lower,
        selection_upper_bound=upper,
        coverage=coverage,
        status=status,
        missing_components=missing_components,
        missing_reasons=missing_reasons,
        component_details=component_details,
        scoring_version=scoring_version,
    )


def score_stage1_candidate(
    target_paper: Paper,
    candidate: CandidatePaper,
    *,
    weights: dict[str, float] = DEFAULT_STAGE1_WEIGHTS,
    semantic_strategy: str = "specter2_canonical",
    citation_importance_mode: str = "canonical",
    target_references_fully_resolved: bool = True,
    citation_context_classification: str | None = None,
) -> Stage1Score:
    """Compute all Stage 1 components and aggregate score for a single candidate."""
    sem_metric = calculate_semantic_similarity(
        target_paper,
        candidate,
        strategy=semantic_strategy,
    )
    rel_metric = calculate_citation_relation(
        candidate,
        target_references_fully_resolved=target_references_fully_resolved,
    )
    imp_metric = calculate_citation_importance(
        candidate,
        mode=citation_importance_mode,
        target_references_fully_resolved=target_references_fully_resolved,
        citation_context_classification=citation_context_classification,
    )

    return calculate_initial_score(
        sem_metric,
        rel_metric,
        imp_metric,
        weights=weights,
    )


# ---------------------------------------------------------------------------
# 5. Deterministic Ranking
# ---------------------------------------------------------------------------


def rank_stage1_candidates(
    target_paper: Paper,
    candidate_set: CandidateSet,
    *,
    weights: dict[str, float] = DEFAULT_STAGE1_WEIGHTS,
    semantic_strategy: str = "specter2_canonical",
    citation_importance_mode: str = "canonical",
    target_references_fully_resolved: bool = True,
    citation_context_classifications: dict[str, str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    selection_policy: SelectionPolicy = DEFAULT_SELECTION_POLICY,
) -> Stage1RankingResult:
    """Rank eligible candidates deterministically per docs/scoring-lineage.md Section 4.

    Deterministic tie-breaking:
    1. selection_lower_bound descending (unrounded float)
    2. coverage descending
    3. canonical paper_id ascending (lexicographical)

    Candidates with initial_score == None are placed into unranked_candidates.
    Excluded and later_work candidates are separated into excluded_candidates.
    Uncertain candidates (e.g. contemporary_work) are separated into uncertain_candidates.
    """
    if selection_policy not in {DEFAULT_SELECTION_POLICY, LEGACY_SELECTION_POLICY}:
        raise ValueError("unknown_stage1_selection_policy")
    excluded_candidates: list[CandidatePaper] = []
    uncertain_candidates: list[CandidatePaper] = []
    eligible_to_score: list[CandidatePaper] = []

    for cand in candidate_set.candidates:
        if cand.eligibility_status == "excluded" or cand.temporal_status == "later_work":
            excluded_candidates.append(cand)
        elif cand.eligibility_status == "uncertain":
            uncertain_candidates.append(cand)
        elif cand.eligibility_status == "eligible":
            eligible_to_score.append(cand)
        else:
            # Unknown eligibility status: treat as excluded
            excluded_candidates.append(cand)

    scored_eligible: list[tuple[CandidatePaper, Stage1Score]] = []
    unranked_candidates: list[Stage1RankedCandidate] = []

    for cand in eligible_to_score:
        classification = None
        if citation_context_classifications:
            classification = citation_context_classifications.get(cand.paper_id)

        score = score_stage1_candidate(
            target_paper,
            cand,
            weights=weights,
            semantic_strategy=semantic_strategy,
            citation_importance_mode=citation_importance_mode,
            target_references_fully_resolved=target_references_fully_resolved,
            citation_context_classification=classification,
        )

        if score.initial_score is None:
            # Score is null: cannot be ranked (docs/scoring-lineage.md line 128)
            unranked_candidates.append(Stage1RankedCandidate(candidate=cand, score=score, rank=0))
        else:
            scored_eligible.append((cand, score))

    # Sort deterministically:
    # 1. selected value desc; legacy is explicit for historical frozen replays only
    # 2. coverage desc
    # 3. paper_id asc
    def sort_key(item: tuple[CandidatePaper, Stage1Score]) -> tuple[float, float, str]:
        c, s = item
        value = (s.selection_lower_bound if selection_policy == DEFAULT_SELECTION_POLICY
                 else s.initial_score)
        if value is None:
            raise ValueError("stage1_selection_bound_missing")
        cov = s.coverage
        pid = c.paper_id or ""
        return (-value, -cov, pid)

    scored_eligible.sort(key=sort_key)

    ranked_candidates: list[Stage1RankedCandidate] = []
    for rank_idx, (cand, score) in enumerate(scored_eligible, start=1):
        ranked_candidates.append(Stage1RankedCandidate(candidate=cand, score=score, rank=rank_idx))

    return Stage1RankingResult(
        target_paper_id=target_paper.id,
        target_title=target_paper.title,
        scoring_version=SCORING_VERSION,
        selection_policy_version=selection_policy,
        weights=dict(weights),
        ranked_candidates=ranked_candidates,
        unranked_candidates=unranked_candidates,
        excluded_candidates=excluded_candidates,
        uncertain_candidates=uncertain_candidates,
        total_candidates_evaluated=candidate_set.total_count,
        shortlist_limit=top_k,
    )
