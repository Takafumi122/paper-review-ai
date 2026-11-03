"""Replay the frozen Stage 1 audit input without network or LLM calls.

Usage: uv run python scripts/audit_stage1_shortlist.py
The baseline was captured before audit fixes, using the original lexical evaluator.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from evaluate_stage1_ranking import build_candidates_from_references

from paper_review.domain.models import Paper, Reference, Stage1RankingResult
from paper_review.domain.stage1_scoring import rank_stage1_candidates

ROOT = Path(__file__).resolve().parents[1] / "evaluation/reports/stage1-audit"


def coverage(result: Stage1RankingResult) -> dict[str, Any]:
    ranked = result.ranked_candidates
    n = len(ranked)

    def count_pct(count: int) -> str:
        return f"{count}/{n} ({100 * count / n:.1f}%)" if n else "0/0 (N/A)"

    return {
        "total": result.total_candidates_evaluated,
        "eligible": n + len(result.unranked_candidates),
        "ranked": n,
        "top10": len(result.top_k(10)),
        "excluded": len(result.excluded_candidates),
        "uncertain": len(result.uncertain_candidates),
        "candidate_embedding_available": count_pct(
            sum(r.candidate.embedding is not None for r in ranked)
        ),
        "semantic_observed": count_pct(
            sum(r.score.semantic_similarity is not None for r in ranked)
        ),
        "semantic_missing": sum(r.score.semantic_similarity is None for r in ranked),
        "lexical_explicit": count_pct(
            sum(
                r.score.component_details["semantic_similarity"].calculation_source == "lexical"
                for r in ranked
            )
        ),
        "lexical_fallback": count_pct(
            sum(
                r.score.component_details["semantic_similarity"].calculation_source
                == "lexical_fallback"
                for r in ranked
            )
        ),
        "relation_1": sum(r.score.citation_relation == 1 for r in ranked),
        "relation_0": sum(r.score.citation_relation == 0 for r in ranked),
        "relation_missing": sum(r.score.citation_relation is None for r in ranked),
        "importance_observed": sum(r.score.citation_importance is not None for r in ranked),
        "importance_missing": sum(r.score.citation_importance is None for r in ranked),
        "full_coverage": sum(r.score.status == "complete" for r in ranked),
        "partial_coverage": sum(r.score.status == "partial" for r in ranked),
        "semantic_zero": sum(r.score.semantic_similarity == 0 for r in ranked),
    }


def main() -> None:
    raw = (ROOT / "before.json").read_bytes()
    snapshots = json.loads(raw)
    output = []
    lines = [
        "# Stage 1 audit measurements",
        "",
        f"Frozen input SHA256: `{hashlib.sha256(raw).hexdigest()}`",
        "",
        "Sources: cached Attention v7 references; freshly parsed LoRA v2 and ReAct v2.",
        "Counts describe parser candidate records, not verified distinct papers.",
        "Before = original lexical run. After = same references with generic bug fixes.",
        "All percentages below use ranked candidates as the denominator.",
        "",
    ]
    for snapshot in snapshots:
        target = Paper.model_validate(snapshot["target"])
        references = [Reference.model_validate(r) for r in snapshot["references"]]
        before = Stage1RankingResult.model_validate(snapshot["ranking"])
        # Provenance is known from the captured execution's explicit strategy.
        for r in before.ranked_candidates:
            m = r.score.component_details["semantic_similarity"]
            r.score.component_details["semantic_similarity"] = m.model_copy(
                update={"calculation_source": "lexical" if m.value is not None else "unavailable"}
            )
        candidates = build_candidates_from_references(target, references)
        after = rank_stage1_candidates(target, candidates, semantic_strategy="lexical")
        canonical = rank_stage1_candidates(target, candidates)
        output.append(
            dict(
                target_id=target.id,
                before=coverage(before),
                after=coverage(after),
                canonical=coverage(canonical),
                ranking=after.model_dump(),
                canonical_ranking=canonical.model_dump(),
            )
        )
        lines += [f"## {target.title}", "", "| Metric | Before | After |", "|---|---:|---:|"]
        for k, v in coverage(before).items():
            lines.append(f"| {k} | {v} | {coverage(after)[k]} |")
        for label, result in [("Before", before), ("After", after)]:
            lines += [
                "",
                f"### {label} Top 10",
                "",
                "| Rank | Ref | Title | Similarity | InitialScore | Source |",
                "|---:|---:|---|---:|---:|---|",
            ]
            for r in result.top_k(10):
                source = r.score.component_details["semantic_similarity"].calculation_source
                lines.append(
                    f"| {r.rank} | {r.candidate.target_reference_order} | "
                    f"{r.candidate.title} | {r.score.semantic_similarity:.6f} | "
                    f"{r.score.initial_score:.6f} | {source} (explicit baseline) |"
                )
        lines += [
            "",
            "### All candidates after fixes",
            "",
            "| Ref | Extracted title | Year | Eligibility | Rank |",
            "|---:|---|---:|---|---:|",
        ]
        ranks = {r.candidate.paper_id: r.rank for r in after.ranked_candidates}
        for c in candidates.candidates:
            lines.append(
                f"| {c.target_reference_order} | {c.title} | {c.year} | "
                f"{c.eligibility_status} | {ranks.get(c.paper_id, '—')} |"
            )
        lines += [""]
    (ROOT / "measurements.json").write_text(json.dumps(output, indent=2))
    (ROOT / "measurements.md").write_text("\n".join(lines))


if __name__ == "__main__":
    main()
