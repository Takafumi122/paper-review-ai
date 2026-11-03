"""Unit tests for Stage 1 Initial Score and Candidate Ranking.

Validates:
- Canonical formula: InitialScore = 0.40 * S + 0.35 * C + 0.25 * I
- Coverage-weighted aggregation for missing values (missing != 0)
- Normalization of all components into [0.0, 1.0]
- SemanticSimilarity (SPECTER2 cosine similarity, clipping, lexical baseline)
- CitationRelation (direct reference, confirmed non-reference, unresolved)
- CitationImportance (rubric values, isInfluential proxy, bibliometric log scaling)
- Deterministic ranking and tie-breaking
- Edge cases (empty candidate set, all uncertain, all missing, duplicate IDs)
"""

from __future__ import annotations

import pytest

from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    MetricValue,
    Paper,
    Stage1Score,
)
from paper_review.domain.stage1_scoring import (
    calculate_citation_importance,
    calculate_citation_relation,
    calculate_initial_score,
    calculate_lexical_similarity,
    calculate_semantic_similarity,
    calculate_specter2_similarity,
    rank_stage1_candidates,
)

# ===========================================================================
# 1. Semantic Similarity Tests
# ===========================================================================


def test_specter2_similarity_identical_vectors():
    """Identical vectors should yield cosine = 1.0, normalized similarity = 1.0."""
    v = [0.2, 0.5, -0.3, 0.8]
    score, reason = calculate_specter2_similarity(v, v)
    assert score is not None
    assert pytest.approx(score, abs=1e-6) == 1.0
    assert reason is None


def test_specter2_similarity_orthogonal_vectors():
    """Orthogonal vectors should yield cosine = 0.0, normalized similarity = 0.5."""
    v1 = [1.0, 0.0, 0.0]
    v2 = [0.0, 1.0, 0.0]
    score, reason = calculate_specter2_similarity(v1, v2)
    assert score is not None
    assert pytest.approx(score, abs=1e-6) == 0.5
    assert reason is None


def test_specter2_similarity_opposite_vectors():
    """Opposite vectors should yield cosine = -1.0, normalized similarity = 0.0."""
    v1 = [1.0, 2.0, 3.0]
    v2 = [-1.0, -2.0, -3.0]
    score, reason = calculate_specter2_similarity(v1, v2)
    assert score is not None
    assert pytest.approx(score, abs=1e-6) == 0.0
    assert reason is None


def test_specter2_similarity_missing_or_empty():
    """Missing or empty vectors must return None with explicit missing reasons."""
    s1, r1 = calculate_specter2_similarity(None, [1.0, 2.0])
    assert s1 is None
    assert r1 == "embedding_unavailable"

    s2, r2 = calculate_specter2_similarity([1.0, 2.0], None)
    assert s2 is None
    assert r2 == "embedding_unavailable"

    s3, r3 = calculate_specter2_similarity([], [])
    assert s3 is None
    assert r3 == "embedding_empty"


def test_specter2_similarity_dimension_mismatch_and_zero_norm():
    """Dimension mismatch and zero-norm vectors must return None."""
    s1, r1 = calculate_specter2_similarity([1.0, 2.0], [1.0, 2.0, 3.0])
    assert s1 is None
    assert r1 == "embedding_dimension_mismatch"

    s2, r2 = calculate_specter2_similarity([0.0, 0.0], [1.0, 2.0])
    assert s2 is None
    assert r2 == "embedding_zero_norm"


def test_lexical_similarity_identical_and_different():
    """Lexical token cosine similarity on text."""
    t1 = "Attention Is All You Need sequence transduction models"
    score_ident, _ = calculate_lexical_similarity(t1, t1)
    assert score_ident is not None
    assert pytest.approx(score_ident, abs=1e-6) == 1.0

    t2 = "Completely unrelated culinary recipe for chocolate cake"
    score_diff, _ = calculate_lexical_similarity(t1, t2)
    assert score_diff == 0.0

    # Partial overlap
    t3 = "Neural sequence models with recurrence"
    score_part, _ = calculate_lexical_similarity(t1, t3)
    assert score_part is not None
    assert 0.0 < score_part < 1.0


def test_lexical_similarity_empty_text():
    """Empty text must yield None with insufficient_text."""
    score, reason = calculate_lexical_similarity("", "some valid text")
    assert score is None
    assert reason == "insufficient_text"


def test_calculate_semantic_similarity_strategies():
    """Verify behavior across canonical, lexical, and hybrid strategies."""
    target = Paper(
        id="target",
        title="Neural Machine Translation",
        abstract="We propose an attention mechanism.",
        embedding=[1.0, 0.0],
        embedding_model="specter2@test",
    )
    cand_with_vec = CandidatePaper(
        paper_id="cand_1",
        title="Layer Normalization",
        abstract="Normalizing activations.",
        embedding=[1.0, 0.0],
        embedding_model="specter2@test",
    )
    cand_no_vec = CandidatePaper(
        paper_id="cand_2",
        title="Attention Mechanism for Neural Translation",
        abstract="Learning to align and translate.",
        embedding=None,
    )

    # 1. Canonical strategy: requires vectors
    m1 = calculate_semantic_similarity(target, cand_with_vec, strategy="specter2_canonical")
    assert m1.value is not None
    assert pytest.approx(m1.value, abs=1e-6) == 1.0

    m2 = calculate_semantic_similarity(target, cand_no_vec, strategy="specter2_canonical")
    assert m2.value is None
    assert m2.missing_reason == "embedding_unavailable"

    # 2. Lexical strategy: uses text
    m3 = calculate_semantic_similarity(target, cand_no_vec, strategy="lexical")
    assert m3.value is not None
    assert m3.value > 0.0

    # 3. Hybrid strategy: vector if present, fallback to lexical
    m4 = calculate_semantic_similarity(target, cand_no_vec, strategy="hybrid")
    assert m4.value is not None
    assert m4.value > 0.0


# ===========================================================================
# 2. Citation Relation Tests
# ===========================================================================


def test_citation_relation_direct_reference():
    """Direct reference in Target references yields 1.00."""
    cand = CandidatePaper(paper_id="p1", title="Title 1", is_target_reference=True)
    m = calculate_citation_relation(cand)
    assert m.value == 1.00
    assert m.missing_reason is None


def test_citation_relation_confirmed_non_reference():
    """Confirmed non-reference when target references fully resolved yields 0.00."""
    cand = CandidatePaper(paper_id="p2", title="Title 2", is_target_reference=False)
    m = calculate_citation_relation(cand, target_references_fully_resolved=True)
    assert m.value == 0.00
    assert m.missing_reason is None


def test_citation_relation_unverified_non_reference():
    """Unverified citation relation when references not fully resolved yields None."""
    cand = CandidatePaper(paper_id="p3", title="Title 3", is_target_reference=False)
    m = calculate_citation_relation(cand, target_references_fully_resolved=False)
    assert m.value is None
    assert m.missing_reason == "reference_relation_unverified"


# ===========================================================================
# 3. Citation Importance Tests
# ===========================================================================


def test_citation_importance_canonical_rubric():
    """Verify canonical rubric values from docs/scoring-lineage.md Section 3."""
    cand = CandidatePaper(paper_id="p1", title="T1", is_target_reference=True)

    m1 = calculate_citation_importance(
        cand,
        citation_context_classification="core_foundation",
    )
    assert m1.value == 1.00

    m2 = calculate_citation_importance(
        cand,
        citation_context_classification="motivating_prior_work",
    )
    assert m2.value == 0.75

    m3 = calculate_citation_importance(
        cand,
        citation_context_classification="comparison_baseline",
    )
    assert m3.value == 0.50

    m4 = calculate_citation_importance(
        cand,
        citation_context_classification="general_background",
    )
    assert m4.value == 0.25


def test_citation_importance_canonical_non_cited():
    """Confirmed non-cited candidate has citation importance 0.00."""
    cand = CandidatePaper(paper_id="p2", title="T2", is_target_reference=False)
    m = calculate_citation_importance(cand, target_references_fully_resolved=True)
    assert m.value == 0.00


def test_citation_importance_is_influential_proxy():
    """isInfluential=True acts as 0.75 proxy when context unclassified."""
    cand_inf = CandidatePaper(
        paper_id="p3",
        title="T3",
        is_target_reference=True,
        is_influential_citation=True,
    )
    m = calculate_citation_importance(cand_inf)
    assert m.value == 0.75
    assert m.missing_reason is None


def test_citation_importance_is_influential_false_is_null():
    """isInfluential=False is NOT evidence of 0, alone it must be None."""
    cand_non_inf = CandidatePaper(
        paper_id="p4",
        title="T4",
        is_target_reference=True,
        is_influential_citation=False,
    )
    m = calculate_citation_importance(cand_non_inf)
    assert m.value is None
    assert m.missing_reason == "context_unclassified_nano_unavailable"


def test_citation_importance_bibliometric_log_scaling():
    """Bibliometric mode scales logarithmically without outlier distortion."""
    c0 = CandidatePaper(paper_id="c0", title="Zero", citation_count=0)
    m0 = calculate_citation_importance(c0, mode="bibliometric")
    assert m0.value == 0.0

    c1000 = CandidatePaper(paper_id="c1k", title="1k", citation_count=1000)
    m1000 = calculate_citation_importance(c1000, mode="bibliometric")
    assert m1000.value is not None
    assert 0.7 < m1000.value < 0.8

    c10000 = CandidatePaper(paper_id="c10k", title="10k", citation_count=10000)
    m10000 = calculate_citation_importance(c10000, mode="bibliometric")
    assert m10000.value is not None
    assert pytest.approx(m10000.value, abs=1e-3) == 1.0

    # Missing citation count
    c_none = CandidatePaper(paper_id="cnone", title="None", citation_count=None)
    m_none = calculate_citation_importance(c_none, mode="bibliometric")
    assert m_none.value is None
    assert m_none.missing_reason == "citation_count_unavailable"


# ===========================================================================
# 4. Aggregation and Formula Tests
# ===========================================================================


def test_initial_score_complete_canonical_example():
    """Verify exact formula from docs/scoring-lineage.md line 139: S=0.8, C=1.0, I=0.5 -> 0.795."""
    s_metric = MetricValue(value=0.8)
    c_metric = MetricValue(value=1.0)
    i_metric = MetricValue(value=0.5)

    res = calculate_initial_score(s_metric, c_metric, i_metric)
    # 0.40 * 0.8 + 0.35 * 1.0 + 0.25 * 0.5 = 0.32 + 0.35 + 0.125 = 0.795
    assert res.initial_score is not None
    assert pytest.approx(res.initial_score, abs=1e-6) == 0.795
    assert pytest.approx(res.coverage, abs=1e-6) == 1.0
    assert res.status == "complete"
    assert res.missing_components == []


def test_initial_score_partial_canonical_example():
    """Verify exact partial formula from docs/scoring-lineage.md line 138:

    S=0.8, C=1.0, I=null -> (0.32 + 0.35)/0.75 = 0.893333..., coverage=0.75.
    """
    s_metric = MetricValue(value=0.8)
    c_metric = MetricValue(value=1.0)
    i_metric = MetricValue(value=None, missing_reason="context_unclassified")

    res = calculate_initial_score(s_metric, c_metric, i_metric)
    assert res.initial_score is not None
    expected = (0.40 * 0.8 + 0.35 * 1.0) / 0.75
    assert pytest.approx(res.initial_score, abs=1e-6) == expected
    assert pytest.approx(res.coverage, abs=1e-6) == 0.75
    assert res.status == "partial"
    assert res.missing_components == ["citation_importance"]
    assert res.missing_reasons["citation_importance"] == "context_unclassified"


def test_initial_score_all_missing():
    """Verify all missing returns initial_score = None, coverage = 0.0, status = unavailable."""
    s_metric = MetricValue(value=None, missing_reason="embedding_unavailable")
    c_metric = MetricValue(value=None, missing_reason="reference_unverified")
    i_metric = MetricValue(value=None, missing_reason="context_unclassified")

    res = calculate_initial_score(s_metric, c_metric, i_metric)
    assert res.initial_score is None
    assert res.coverage == 0.0
    assert res.status == "unavailable"
    assert len(res.missing_components) == 3


def test_missing_value_is_never_converted_to_zero():
    """Crucial contract: missing != 0.

    If missing were converted to 0, S=0.8, C=1.0, I=0 would yield:
    0.40*0.8 + 0.35*1.0 + 0 = 0.67.
    With proper coverage weighting, it yields 0.67 / 0.75 = 0.893333...
    """
    s_metric = MetricValue(value=0.8)
    c_metric = MetricValue(value=1.0)
    i_metric_missing = MetricValue(value=None)
    i_metric_zero = MetricValue(value=0.0)

    score_missing = calculate_initial_score(s_metric, c_metric, i_metric_missing)
    score_zero = calculate_initial_score(s_metric, c_metric, i_metric_zero)

    assert score_missing.initial_score != score_zero.initial_score
    assert score_missing.initial_score is not None
    assert score_zero.initial_score is not None
    assert score_missing.initial_score > score_zero.initial_score


# ===========================================================================
# 5. Deterministic Ranking Tests
# ===========================================================================


def test_deterministic_ranking_order_and_tie_breaking():
    """Verify descending rank and tie-breaking:

    1. selection_lower_bound desc
    2. coverage desc
    3. paper_id asc
    """
    target = Paper(
        id="target",
        title="Target Paper",
        embedding=[1.0, 0.0],
        embedding_model="specter2@test",
    )

    # c1: complete score = 0.40*0.8 + 0.35*1.0 + 0.25*0.5 = 0.795, cov=1.0
    c1 = CandidatePaper(
        paper_id="arxiv:1409.0473",
        title="Bahdanau Attention",
        is_target_reference=True,
        is_influential_citation=False,
        embedding=[1.0, 0.0],
        embedding_model="specter2@test",
    )
    # c2: complete score = 0.40*0.8 + 0.35*1.0 + 0.25*0.75 = 0.8575, cov=1.0
    c2 = CandidatePaper(
        paper_id="arxiv:1607.06450",
        title="Layer Normalization",
        is_target_reference=True,
        is_influential_citation=True,  # 0.75 proxy
        embedding=[1.0, 0.0],
        embedding_model="specter2@test",
    )
    # c3: complete score = 0.40*0.5 + 0.35*1.0 + 0.25*0.0 = 0.55, cov=1.0
    c3 = CandidatePaper(
        paper_id="arxiv:1705.03122",
        title="ConvS2S",
        is_target_reference=True,
        is_influential_citation=False,
        embedding=[0.0, 1.0],  # orthogonal -> 0.5
        embedding_model="specter2@test",
    )

    cset = CandidateSet(
        target_paper_id="target",
        candidates=[c1, c2, c3],
    )

    classifications = {
        "arxiv:1409.0473": "comparison_baseline",  # 0.50
        "arxiv:1705.03122": "confirmed_non_cited",  # 0.00
    }

    result = rank_stage1_candidates(
        target,
        cset,
        citation_context_classifications=classifications,
    )

    assert len(result.ranked_candidates) == 3
    # c2 (0.8575) -> rank 1
    # c1 (0.7950) -> rank 2
    # c3 (0.5500) -> rank 3
    assert result.ranked_candidates[0].candidate.paper_id == "arxiv:1607.06450"
    assert result.ranked_candidates[0].rank == 1

    assert result.ranked_candidates[1].candidate.paper_id == "arxiv:1409.0473"
    assert result.ranked_candidates[1].rank == 2

    assert result.ranked_candidates[2].candidate.paper_id == "arxiv:1705.03122"
    assert result.ranked_candidates[2].rank == 3


def test_ranking_tie_breaking_paper_id_lexicographical():
    """Identical scores and coverage must break ties by paper_id lexicographically."""
    target = Paper(id="target", title="Target Paper")

    # Both cA and cB have identical partial scores (rel=1.0, sem=None, imp=None)
    cA = CandidatePaper(paper_id="arxiv:1000.0001", title="Paper A", is_target_reference=True)
    cB = CandidatePaper(paper_id="arxiv:1000.0002", title="Paper B", is_target_reference=True)

    # Supply in reverse order to verify sort
    cset = CandidateSet(target_paper_id="target", candidates=[cB, cA])

    result = rank_stage1_candidates(target, cset)
    assert len(result.ranked_candidates) == 2
    assert result.ranked_candidates[0].candidate.paper_id == "arxiv:1000.0001"
    assert result.ranked_candidates[0].rank == 1
    assert result.ranked_candidates[1].candidate.paper_id == "arxiv:1000.0002"
    assert result.ranked_candidates[1].rank == 2


def test_equal_score_and_coverage_use_id_without_extra_relation_tie_break(monkeypatch):
    def tied_score(target, candidate, **kwargs):
        return Stage1Score(initial_score=0.5, selection_lower_bound=0.5,
                           selection_upper_bound=0.5, coverage=1, citation_relation=(
            1 if candidate.paper_id == "z" else 0
        ))

    monkeypatch.setattr("paper_review.domain.stage1_scoring.score_stage1_candidate", tied_score)
    result = rank_stage1_candidates(Paper(id="target", title="Target"), CandidateSet(
        target_paper_id="target", candidates=[
            CandidatePaper(paper_id="z", title="Higher relation"),
            CandidatePaper(paper_id="a", title="Canonical first ID"),
        ],
    ))
    assert [r.candidate.paper_id for r in result.ranked_candidates] == ["a", "z"]
    assert result.scoring_version == "2"


def test_ranking_filters_excluded_and_later_work():
    """Excluded candidates and later_work must not be included in ranked list."""
    target = Paper(id="target", title="Target Paper")

    c_valid = CandidatePaper(
        paper_id="valid",
        title="Valid Predecessor",
        is_target_reference=True,
        eligibility_status="eligible",
        temporal_status="eligible",
    )
    c_survey = CandidatePaper(
        paper_id="survey",
        title="Survey of Attention",
        is_target_reference=True,
        eligibility_status="excluded",
        exclusion_reason="survey",
    )
    c_later = CandidatePaper(
        paper_id="later",
        title="Later Work (2025)",
        is_target_reference=True,
        eligibility_status="eligible",
        temporal_status="later_work",
    )
    c_uncertain = CandidatePaper(
        paper_id="uncertain",
        title="Contemporary Work",
        is_target_reference=True,
        eligibility_status="uncertain",
        temporal_status="contemporary_work",
    )

    cset = CandidateSet(
        target_paper_id="target",
        candidates=[c_valid, c_survey, c_later, c_uncertain],
    )

    result = rank_stage1_candidates(target, cset)
    assert len(result.ranked_candidates) == 1
    assert result.ranked_candidates[0].candidate.paper_id == "valid"
    assert len(result.excluded_candidates) == 2  # survey + later
    assert len(result.uncertain_candidates) == 1  # contemporary


def test_unranked_candidates_when_all_components_missing():
    """Candidates with initial_score == None must be placed into unranked_candidates."""
    target = Paper(id="target", title="Target Paper")

    # When target references are not fully resolved and candidate not cited, rel=None
    # Vector is missing -> sem=None
    # imp=None
    c_missing = CandidatePaper(
        paper_id="missing_all",
        title="Completely Missing",
        is_target_reference=False,
    )
    cset = CandidateSet(target_paper_id="target", candidates=[c_missing])

    result = rank_stage1_candidates(
        target,
        cset,
        target_references_fully_resolved=False,
    )
    assert len(result.ranked_candidates) == 0
    assert len(result.unranked_candidates) == 1
    assert result.unranked_candidates[0].candidate.paper_id == "missing_all"
    assert result.unranked_candidates[0].score.initial_score is None


def test_top_k_shortlist():
    """Verify top_k method returns at most K candidates."""
    target = Paper(id="target", title="Target Paper")
    candidates = [
        CandidatePaper(
            paper_id=f"cand_{i}",
            title=f"Paper {i}",
            is_target_reference=True,
            citation_count=i * 100,
        )
        for i in range(15)
    ]
    cset = CandidateSet(target_paper_id="target", candidates=candidates)

    result = rank_stage1_candidates(target, cset, citation_importance_mode="bibliometric")
    assert len(result.ranked_candidates) == 15
    top_5 = result.top_k(5)
    assert len(top_5) == 5
    assert top_5[0].rank == 1
    assert top_5[4].rank == 5

    top_10 = result.top_k(10)
    assert len(top_10) == 10
