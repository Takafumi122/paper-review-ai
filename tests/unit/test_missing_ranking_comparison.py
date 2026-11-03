"""Decision-policy sensitivity, unknown bounds and exact non-mutation of metrics."""

import copy
import importlib.util

import pytest

from paper_review.config import ROOT

spec = importlib.util.spec_from_file_location(
    "missing_comparison", ROOT / "scripts/compare_missing_rankings.py",
)
assert spec and spec.loader
comparison = importlib.util.module_from_spec(spec)
spec.loader.exec_module(comparison)
WEIGHTS = {"semantic_similarity": 0.4, "citation_relation": 0.35, "citation_importance": 0.25}


def row(cid, importance, similarity=0.8):
    observed = 0.4 * similarity + 0.35
    coverage = 0.75
    if importance is not None:
        observed += 0.25 * importance
        coverage = 1
    return {"id": cid, "title": cid, "score": {
        "semantic_similarity": similarity, "citation_relation": 1,
        "citation_importance": importance, "initial_score": observed / coverage,
        "coverage": coverage, "missing_components": ["citation_importance"]
        if importance is None else [],
    }}


def test_unknown_metric_stays_unknown_with_admissible_total_interval():
    value = row("missing", None)["score"]
    lower, upper = comparison.bounds(value, WEIGHTS)
    assert lower == pytest.approx(0.67)
    assert upper == pytest.approx(0.92)
    assert value["citation_importance"] is None
    assert comparison.bounds(row("zero", 0)["score"], WEIGHTS) == pytest.approx((0.67, 0.67))


def test_current_policy_can_reward_losing_evidence_alternatives_do_not():
    complete = row("candidate", 0.25)
    missing = row("candidate", None)
    competitor = row("other", 0.5, similarity=0.75)
    assert complete["score"]["initial_score"] < competitor["score"]["initial_score"]
    assert missing["score"]["initial_score"] > competitor["score"]["initial_score"]
    before = comparison.compare([complete, competitor], WEIGHTS)
    after = comparison.compare([missing, competitor], WEIGHTS)
    assert before["policies"]["current"]["ranked"][0]["id"] == "other"
    assert after["policies"]["current"]["ranked"][0]["id"] == "candidate"
    for policy in ["coverage_first", "lower_bound", "complete_only"]:
        assert after["policies"][policy]["ranked"][0]["id"] == "other"
    assert len(after["policies"]["complete_only"]["withheld"]) == 1


def test_complete_case_can_underfill_without_promoting_unknowns():
    source = [row("a", None), row("b", 0.25)]
    frozen = copy.deepcopy(source)
    result = comparison.compare(source, WEIGHTS, top_k=10)
    assert source == frozen
    assert result["policies"]["complete_only"]["top_k_count"] == 1
    assert result["policies"]["complete_only"]["withheld"][0]["id"] == "a"
    for policy in comparison.POLICIES:
        all_rows = result["policies"][policy]["ranked"] + result["policies"][policy]["withheld"]
        assert next(r for r in all_rows if r["id"] == "a")["score"]["citation_importance"] is None


def test_ties_are_deterministic_and_complete_scores_agree_across_policies():
    result = comparison.compare([row("b", 0.25), row("a", 0.25)], WEIGHTS)
    for policy in comparison.POLICIES:
        assert [r["id"] for r in result["policies"][policy]["ranked"]] == ["a", "b"]


def test_coverage_priority_and_lower_bound_have_different_tradeoffs():
    result = comparison.compare([row("strong_partial", None, 1), row("weak_complete", 0, 0)],
                                WEIGHTS)
    assert result["policies"]["coverage_first"]["ranked"][0]["id"] == "weak_complete"
    assert result["policies"]["lower_bound"]["ranked"][0]["id"] == "strong_partial"


def test_all_unknown_metrics_remain_unranked_even_with_lower_bound_zero():
    value = row("unknown", None)
    value["score"].update(semantic_similarity=None, citation_relation=None, initial_score=None,
                          coverage=0, missing_components=list(WEIGHTS))
    result = comparison.compare([value], WEIGHTS)
    for policy in comparison.POLICIES:
        assert result["policies"][policy]["ranked_count"] == 0
        withheld = result["policies"][policy]["withheld"][0]
        assert withheld["lower_bound"] == 0 and withheld["upper_bound"] == 1
        assert withheld["score"]["initial_score"] is None


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), float("inf")])
def test_invalid_metric_cannot_enter_comparison(bad):
    with pytest.raises(ValueError, match="invalid_metric"):
        comparison.compare([row("a", bad)], WEIGHTS)


def test_duplicate_identity_and_invalid_weights_are_rejected():
    with pytest.raises(ValueError, match="duplicate_candidate"):
        comparison.compare([row("a", 0.5), row("a", None)], WEIGHTS)
    with pytest.raises(ValueError, match="sum_to_one"):
        comparison.bounds(row("a", 0.5)["score"], {"semantic_similarity": 0.4})


def test_real_frozen_sources_explain_page_gap_without_modification():
    case = next(c for c in comparison.load_cases() if c["aid"] == "2106.09685")
    audit = comparison.source_audit(case, ROOT / ".work/app.db")
    gap = audit["dataset_details_span_gaps"]
    assert len(gap) == 1 and gap[0]["stored_text"] == "\n"
    assert (gap[0]["start"], gap[0]["end"]) == (461, 462)
    assert audit["dataset_list_continuation_present"]
    assert all(not c["non_citation_confirmed"] for c in audit["candidate_checks"])
    assert audit["stored_text_and_sources_modified"] is False
