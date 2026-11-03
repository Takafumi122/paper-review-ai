"""Production builder plus frozen real provider fixtures; no live readiness claim."""

import asyncio
import runpy

from paper_review.config import ROOT
from paper_review.domain.models import Stage1RankingResult


def test_production_default_reproduces_approved_whole_rankings_and_preserves_metrics():
    script = runpy.run_path(str(ROOT / "scripts/verify_stage1_adoption.py"))
    result = asyncio.run(script["verify"]())
    assert result["status"] == "passed" and result["new_provider_calls"] == 0
    assert len(result["cases"]) == 2
    for case in result["cases"]:
        assert case["selection_policy_version"] == "lower-bound-v1"
        assert case["full_order_matches_approved_comparison"]
        assert case["original_scores_metrics_and_provenance_unchanged"]
        assert case["top_k"] == 10 and case["top10_incomplete"] == 0


def test_legacy_ranking_payload_keeps_its_policy_and_original_order():
    old = Stage1RankingResult.model_validate({
        "target_paper_id": "t", "ranked_candidates": [
            {"candidate": {"paper_id": "z", "title": "First"}, "rank": 1,
             "score": {"initial_score": 1, "coverage": 0.75}},
            {"candidate": {"paper_id": "a", "title": "Second"}, "rank": 2,
             "score": {"initial_score": 0.9, "coverage": 1}},
        ],
    })
    assert old.selection_policy_version == "observed-renormalized-v1"
    assert [r.candidate.paper_id for r in old.ranked_candidates] == ["z", "a"]
    assert all(r.score.selection_lower_bound is None for r in old.ranked_candidates)
