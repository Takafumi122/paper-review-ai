"""Verify the production default against frozen approved comparisons, with mocked S2 only."""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json

import httpx

from paper_review.application import page_boundary_verification as baseline
from paper_review.application.stage1_shortlist import (
    apply_context_classifications,
    build_stage1_shortlist,
)
from paper_review.config import ROOT
from paper_review.domain.models import CandidateSet, Paper
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient

OUTPUT = ROOT / "evaluation/reports/stage1-lower-bound-v32-2026-11-02"


async def verify() -> dict:
    fixed = json.loads((baseline.OUTPUT / "results.json").read_text())
    expected = json.loads((baseline.OUTPUT / "policy-comparison.json").read_text())
    results = []
    for item in baseline.inputs():
        case = item["case"]
        audit_case = next(c for c in fixed["cases"] if c["aid"] == case["aid"])
        payload = json.loads(gzip.decompress(case["fixture"].read_bytes()))
        calls = []

        def replay(request, payload=payload, calls=calls):
            assert request.method == "POST" and request.url.path.endswith("/paper/batch")
            calls.append(True)
            return httpx.Response(200, json=payload)

        async with SemanticScholarClient(
            api_key="offline-only", transport=httpx.MockTransport(replay),
            rate_limiter=RateLimiter(0), max_attempts=1, max_http_attempts=1,
        ) as scholar:
            analysis = await build_stage1_shortlist(
                Paper.model_validate(case["target"]),
                CandidateSet.model_validate(case["inventory"]),
                scholar,
            )
        # Exercise the same builder, classifier application and Top-K used by Worker.
        actual = apply_context_classifications(analysis, audit_case["audit"])
        assert actual.ranking.selection_policy_version == "lower-bound-v1"
        reference = expected["cases"][case["aid"]]["after"]["policies"]["lower_bound"]["ranked"]
        assert [r.candidate.paper_id for r in actual.ranking.ranked_candidates] == [
            r["id"] for r in reference
        ]
        old = {r["id"]: r for r in audit_case["after"]}
        for row in actual.ranking.ranked_candidates:
            before = old[row.candidate.paper_id]["score"]
            preserved = row.score.model_dump(exclude={
                "selection_lower_bound", "selection_upper_bound",
            })
            assert preserved == before
        assert len(calls) == 1
        results.append({
            "aid": case["aid"], "selection_policy_version": actual.ranking.selection_policy_version,
            "weights": actual.ranking.weights, "top_k": actual.ranking.shortlist_limit,
            "ranked": [{"id": r.candidate.paper_id, "title": r.candidate.title, "rank": r.rank,
                        "score": r.score.model_dump()} for r in actual.ranking.ranked_candidates],
            "top10_incomplete": sum(r.score.status != "complete" for r in actual.ranking.top_k()),
            "full_order_matches_approved_comparison": True,
            "original_scores_metrics_and_provenance_unchanged": True,
        })
    return {"status": "passed", "pipeline_version": "full-v32", "cases": results,
            "new_provider_calls": 0, "new_llm_cost_usd": 0, "readiness_changed": False,
            "comparison_artifacts_sha256": {
                name: hashlib.sha256((baseline.OUTPUT / name).read_bytes()).hexdigest()
                for name in ["results.json", "policy-comparison.json"]
            }}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify-existing", action="store_true")
    args = parser.parse_args()
    destination = OUTPUT / "results.json"
    if destination.exists() and not args.verify_existing:
        raise ValueError("existing_report_requires_verify_existing")
    value = asyncio.run(verify())
    if args.verify_existing:
        assert json.loads(destination.read_text()) == value
    else:
        OUTPUT.mkdir(parents=True, exist_ok=True)
        with destination.open("x") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"status": value["status"], "cases": len(value["cases"]),
                      "new_provider_calls": 0}))


if __name__ == "__main__":
    main()
