"""Approved selection changes ordering, never observed metrics or Stage 2 scoring."""

import pytest

from paper_review.application.stage1_audit import EvaluationArtifact, audit_stage1
from paper_review.domain.models import CandidatePaper, CandidateSet, MetricValue, Paper, Stage1Score
from paper_review.domain.stage1_scoring import calculate_initial_score, rank_stage1_candidates
from paper_review.domain.understanding import Evidence, Relevance, final_score


def pool():
    target = Paper(id="t", title="Target", embedding=[1, 0], embedding_model="unit")
    candidates = CandidateSet(target_paper_id="t", candidates=[
        CandidatePaper(paper_id="a_missing", title="Unknown importance", embedding=[1, 0],
                       embedding_model="unit"),
        CandidatePaper(paper_id="z_known", title="Known foundation", embedding=[0, 1],
                       embedding_model="unit"),
    ])
    return target, candidates


def test_production_default_reverses_missing_advantage_with_legacy_replay_available():
    target, candidates = pool()
    roles = {"z_known": "core_foundation"}
    new = rank_stage1_candidates(target, candidates, citation_context_classifications=roles)
    old = rank_stage1_candidates(target, candidates, citation_context_classifications=roles,
                                 selection_policy="observed-renormalized-v1")
    assert new.selection_policy_version == "lower-bound-v1"
    assert [r.candidate.paper_id for r in new.ranked_candidates] == ["z_known", "a_missing"]
    assert [r.candidate.paper_id for r in old.ranked_candidates] == ["a_missing", "z_known"]
    assert {r.candidate.paper_id: r.score for r in new.ranked_candidates} == {
        r.candidate.paper_id: r.score for r in old.ranked_candidates
    }
    missing = new.ranked_candidates[1].score
    assert missing.initial_score == 1
    assert missing.citation_importance is None
    assert missing.selection_lower_bound == 0.75
    assert missing.selection_upper_bound == 1
    assert missing.missing_reasons and missing.component_details


def test_known_zero_and_unknown_have_distinct_bounds_and_coverage():
    observed = MetricValue(value=0.8, source_ids=["s"])
    relation = MetricValue(value=1)
    missing = calculate_initial_score(
        observed, relation, MetricValue(value=None, missing_reason="x"),
    )
    zero = calculate_initial_score(observed, relation, MetricValue(value=0))
    assert missing.selection_lower_bound == zero.selection_lower_bound == pytest.approx(0.67)
    assert missing.selection_upper_bound == pytest.approx(0.92)
    assert zero.selection_upper_bound == pytest.approx(0.67)
    assert missing.initial_score is not None and zero.initial_score is not None
    assert missing.initial_score > zero.initial_score
    assert missing.coverage == 0.75 and zero.coverage == 1
    assert missing.citation_importance is None and zero.citation_importance == 0


def test_all_missing_bounds_do_not_make_candidate_rankable():
    target = Paper(id="t", title="Target")
    candidates = CandidateSet(target_paper_id="t", candidates=[
        CandidatePaper(paper_id="a", title="Unknown reference", is_target_reference=False),
    ])
    result = rank_stage1_candidates(target, candidates, target_references_fully_resolved=False)
    assert not result.ranked_candidates
    score = result.unranked_candidates[0].score
    assert score.initial_score is None
    assert (score.selection_lower_bound, score.selection_upper_bound) == (0, 1)


def test_initial_score_is_not_an_additional_tie_break(monkeypatch):
    def fixed(target, candidate, **kwargs):
        return Stage1Score(initial_score=0.9 if candidate.paper_id.startswith("z") else 0.8,
                           selection_lower_bound=0.6, selection_upper_bound=0.85, coverage=0.75)

    monkeypatch.setattr("paper_review.domain.stage1_scoring.score_stage1_candidate", fixed)
    target, candidates = pool()
    result = rank_stage1_candidates(target, candidates)
    assert [r.candidate.paper_id for r in result.ranked_candidates] == ["a_missing", "z_known"]


def test_legacy_serialized_score_does_not_gain_invented_bounds():
    old = Stage1Score.model_validate({"initial_score": 0.9, "coverage": 0.75})
    assert old.selection_lower_bound is old.selection_upper_bound is None


def test_unknown_policy_is_rejected():
    target, candidates = pool()
    with pytest.raises(ValueError, match="unknown_stage1_selection_policy"):
        rank_stage1_candidates(target, candidates, selection_policy="unknown")  # type: ignore


def test_stage2_formula_ignores_stage1_selection_bounds():
    score = calculate_initial_score(MetricValue(value=0.8), MetricValue(value=1), MetricValue())
    relation = Relevance(candidate_id="a", score=3, contribution_ids=["c"], reason="制約",
                         evidence=[Evidence(span_id="s", quote="Original evidence.")])
    old = score.model_copy(update={"selection_lower_bound": None, "selection_upper_bound": None})
    assert final_score(score, relation, adjustment_policy="identity-v1") == final_score(
        old, relation, adjustment_policy="identity-v1",
    )


def test_auditor_distinguishes_missing_bounds_and_wrong_selection_order():
    rows = [{"paper_id": pid, "title": pid, "eligibility": "eligible",
             "embedding_available": False, "rank": i + 1,
             "score": {"initial_score": 0.9, "coverage": 1,
                       "selection_lower_bound": lower, "selection_upper_bound": lower}}
            for i, (pid, lower) in enumerate([("a", 0.6), ("z", 0.7)])]
    artifact = EvaluationArtifact.model_validate({
        "target": "Test", "inventory_sha256": "a" * 64, "fixture_sha256": "b" * 64,
        "scoring_version": "2", "selection_policy_version": "lower-bound-v1",
        "ranking": rows, "probes": [], "candidate_embedding_outcomes": [],
    })
    assert "selection_order_invalid" in audit_stage1(
        artifact, input_sha256="c" * 64,
    )["gate_reasons"]
    artifact.ranking[0].score.selection_lower_bound = None
    assert "selection_bounds_missing" in audit_stage1(
        artifact, input_sha256="c" * 64,
    )["gate_reasons"]
    artifact.selection_policy_version = "observed-renormalized-v1"
    assert "selection_bounds_missing" not in audit_stage1(
        artifact, input_sha256="c" * 64,
    )["gate_reasons"]
