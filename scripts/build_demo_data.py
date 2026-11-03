"""Export allowlisted recorded evaluation metadata for the static public demo.

No provider requests, paper bodies, bibliography text, credentials or vectors.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
TARGETS = [
    ("1706.03762", "Attention", "2017"),
    ("2106.09685", "LoRA", "2021"),
    ("2210.03629", "ReAct", "2022"),
]


def build_demo_data() -> dict[str, Any]:
    papers = []
    for arxiv, short, year in TARGETS:
        inventory_path = ROOT / f"evaluation/reports/candidate-inventory/{arxiv}-result.json"
        ranking_path = ROOT / f"evaluation/reports/specter2-integration/{arxiv}-result.json"
        inventory = json.loads(inventory_path.read_text())
        evaluation = json.loads(ranking_path.read_text())
        rankings = {r["paper_id"]: r for r in evaluation["ranking"]}
        probes = {p["paper_id"]: p["probe"] for p in evaluation["probes"]}
        candidates = []
        for candidate in inventory["inventory"]["candidates"]:
            row = rankings.get(candidate["paper_id"])
            candidates.append(
                {
                    "id": candidate["paper_id"],
                    "title": candidate["title"],
                    "authors": candidate["authors"],
                    "year": candidate["year"],
                    "external_ids": {
                        k: candidate["external_ids"].get(k)
                        for k in ("arxiv", "doi", "semantic_scholar")
                    },
                    "eligibility": candidate["eligibility_status"],
                    "exclusion_reason": candidate["exclusion_reason"],
                    "temporal_status": candidate["temporal_status"],
                    "temporal_uncertainty": candidate["temporal_uncertainty"],
                    "date_provenance": candidate["date_provenance"],
                    "resolution_status": candidate["resolution_status"],
                    "resolution_method": candidate["resolution_method"],
                    "reference_orders": [
                        ref["ref_order"] for ref in candidate["source_references"]
                    ],
                    "is_influential_citation": candidate["is_influential_citation"],
                    "diagnostic_probe": probes.get(candidate["paper_id"]),
                    "rank": row["rank"] if row else None,
                    "score": {
                        key: row["score"][key]
                        for key in (
                            "initial_score",
                            "semantic_similarity",
                            "citation_relation",
                            "citation_importance",
                            "coverage",
                            "status",
                            "missing_reasons",
                        )
                    }
                    if row
                    else None,
                }
            )
        candidates.sort(key=lambda c: (c["rank"] is None, c["rank"] or 0, c["id"]))
        papers.append(
            {
                "arxiv_id": arxiv,
                "short_title": short,
                "title": inventory["target"]["title"],
                "year": year,
                "published_at": inventory["target"]["published_at"],
                "statistics": {
                    key: inventory["statistics"][key]
                    for key in (
                        "parsed_reference_count",
                        "resolved_reference_count",
                        "unresolved_reference_count",
                        "distinct_candidate_count",
                        "eligible",
                    )
                },
                "embedding_available": evaluation["statistics"]["candidate_embeddings_available"],
                "embedding_missing_reason": evaluation["statistics"]["target_embedding_provenance"][
                    "missing_reason"
                ],
                "candidates": candidates,
            }
        )
    return {
        "schema_version": "1",
        "snapshot_date": "2026-10-06",
        "data_origin": "recorded_evaluation",
        "live_analysis": False,
        "stage1_quality_evaluable": False,
        "papers": papers,
    }


if __name__ == "__main__":
    output = ROOT / "frontend/src/data/demo.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(build_demo_data(), ensure_ascii=False, indent=2) + "\n")
    print("Exported 3 recorded papers; no live requests or vectors.")
