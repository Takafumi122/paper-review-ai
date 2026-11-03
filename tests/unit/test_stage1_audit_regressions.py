"""Regression checks for bugs exposed by the shortlist quality audit."""

import runpy
from pathlib import Path

import pytest

from paper_review.domain.models import CandidatePaper, CandidateSet, Paper, Reference
from paper_review.domain.stage1_scoring import (
    calculate_semantic_similarity,
    calculate_specter2_similarity,
    rank_stage1_candidates,
)

EVALUATOR = runpy.run_path(
    str(Path(__file__).resolve().parents[2] / "scripts/evaluate_stage1_ranking.py")
)


@pytest.mark.parametrize(
    "text,year",
    [
        ("A. Example title. arXiv:1906.05392,2019.", 2019),
        ("A. Example title. arXiv:2012.13255 [cs], December 2020.", 2020),
        ("A. Example title. Journal 15:1929–1958,2014.", 2014),
        ("A. Example title. 2019a.", 2019),
        ("A. Example title. arXiv:2012.13255.", None),
        ("A. Example title. arXiv:2101.00190,January2021.", 2021),
    ],
)
def test_reference_year_is_not_identifier_or_page_range(text, year):
    assert EVALUATOR["extract_reference_metadata"](text, 1)["year"] == year


def test_year_only_reference_does_not_gain_false_temporal_precision():
    target = Paper(id="target", title="Target", published_at="2022-10-06")
    refs = [
        Reference(
            id="r",
            revision_id="v",
            paper_id="target",
            ref_order=1,
            raw_text="Alice Author. Example title. 2022.",
        )
    ]
    candidate = EVALUATOR["build_candidates_from_references"](target, refs).candidates[0]
    assert candidate.publication_date == "2022"
    assert candidate.date_provenance.precision == "year"
    assert candidate.temporal_status == "contemporary_work"
    assert candidate.eligibility_status == "uncertain"


@pytest.mark.parametrize(
    "model,reason",
    [
        (None, "embedding_model_unavailable"),
        ("specter2@other", "embedding_model_mismatch"),
    ],
)
def test_model_pairing_and_fallback_provenance(model, reason):
    target = Paper(id="t", title="Example", embedding=[1.0, 0.0], embedding_model="specter2@test")
    candidate = CandidatePaper(
        paper_id="c", title="Example", embedding=[1.0, 0.0], embedding_model=model
    )
    canonical = calculate_semantic_similarity(target, candidate)
    assert canonical.value is None
    assert canonical.missing_reason == reason
    assert canonical.calculation_source == "unavailable"
    fallback = calculate_semantic_similarity(target, candidate, strategy="hybrid")
    assert fallback.value == 1.0
    assert fallback.calculation_source == "lexical_fallback"


def test_observed_zero_retains_lexical_provenance():
    metric = calculate_semantic_similarity(
        Paper(id="t", title="alpha"), CandidatePaper(paper_id="c", title="beta"), strategy="lexical"
    )
    assert metric.value == 0.0
    assert metric.missing_reason is None
    assert metric.calculation_source == "lexical"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_non_finite_vectors_do_not_become_perfect_similarity(value):
    score, reason = calculate_specter2_similarity([value, 1.0], [1.0, 1.0])
    assert score is None
    assert reason == "embedding_non_finite"


def test_ranker_top_k_argument_limits_shortlist_without_truncating_full_ranking():
    target = Paper(id="t", title="Example")
    candidates = CandidateSet(
        target_paper_id="t",
        candidates=[
            CandidatePaper(paper_id=str(i), title="Example", is_target_reference=True)
            for i in range(12)
        ],
    )
    result = rank_stage1_candidates(target, candidates, top_k=2)
    assert len(result.ranked_candidates) == 12
    assert len(result.top_k()) == 2
    assert len(result.top_k(1)) == 1
    assert len(result.top_k(100)) == 2
    assert result.top_k(0) == []
    with pytest.raises(ValueError):
        result.top_k(-1)
    default = rank_stage1_candidates(target, candidates)
    assert len(default.top_k(100)) == 10
