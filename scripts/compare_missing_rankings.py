"""Offline evidence audit and hypothetical shortlist policies; never production routing.

Run with uv run --no-env-file python scripts/compare_missing_rankings.py.
No provider client is constructed and missing metric values remain null.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path
from typing import Literal

from paper_review.application.citation_quality_evaluation import load_cases
from paper_review.application.citation_year_verification import OUTPUT as PREVIOUS
from paper_review.config import ROOT
from paper_review.domain.stage1_scoring import DEFAULT_STAGE1_WEIGHTS

Policy = Literal["current", "coverage_first", "lower_bound", "complete_only"]
POLICIES: tuple[Policy, ...] = ("current", "coverage_first", "lower_bound", "complete_only")
OUTPUT = ROOT / "evaluation/reports/missing-policy-comparison-2026-11-02"
ATTENTION = ROOT / "evaluation/reports/ranking-quality-v29-2026-11-02/classify-results.json"


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


def bounds(score: dict, weights: dict[str, float]) -> tuple[float, float]:
    """Worst/best admissible totals, not observed zeros or point imputations."""
    if not weights or any(not math.isfinite(w) or w < 0 for w in weights.values()):
        raise ValueError("invalid_weights")
    if not math.isclose(sum(weights.values()), 1):
        raise ValueError("weights_must_sum_to_one")
    lower = missing_weight = 0.0
    for name, weight in weights.items():
        value = score[name]
        if value is None:
            missing_weight += weight
        elif not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError("invalid_metric")
        else:
            lower += weight * value
    return lower, lower + missing_weight


def compare(rows: list[dict], weights: dict[str, float], top_k: int = 10) -> dict:
    """Evaluate fixed alternatives; do not edit canonical scores, metrics or candidates."""
    if len({r["id"] for r in rows}) != len(rows):
        raise ValueError("duplicate_candidate")
    if top_k < 1:
        raise ValueError("invalid_top_k")
    tables = {}
    for policy in POLICIES:
        ranked = []
        withheld = []
        for row in rows:
            score = row["score"]
            lower, upper = bounds(score, weights)
            complete = all(score[k] is not None for k, w in weights.items() if w > 0)
            item = {**copy.deepcopy(row), "lower_bound": lower, "upper_bound": upper}
            if score["initial_score"] is None or (policy == "complete_only" and not complete):
                withheld.append(item)
                continue
            item["selection_value"] = lower if policy == "lower_bound" else score["initial_score"]
            ranked.append(item)

        def key(item: dict, policy: Policy = policy) -> tuple:
            score = item["score"]
            if policy == "coverage_first":
                return (-score["coverage"], -score["initial_score"], item["id"])
            return (-item["selection_value"], -score["coverage"], item["id"])

        ranked.sort(key=key)
        for i, item in enumerate(ranked, 1):
            item["comparison_rank"] = i
        tables[policy] = {
            "ranked": ranked, "withheld": sorted(withheld, key=lambda r: r["id"]),
            "ranked_count": len(ranked), "withheld_count": len(withheld),
            "top_k_count": min(top_k, len(ranked)),
            "top_k_incomplete": sum(bool(r["score"]["missing_components"])
                                    for r in ranked[:top_k]),
        }
    baseline = {r["id"] for r in tables["current"]["ranked"][:top_k]}
    for table in tables.values():
        table["top_k_overlap_with_current"] = len(
            baseline & {r["id"] for r in table["ranked"][:top_k]}
        )
    return {"weights": weights, "top_k": top_k, "policies": tables}


def source_audit(case: dict, database: Path) -> dict:
    """Inspect persisted sections; absence in extracted text never proves non-citation."""
    revision = case["spans"][0]["revision"]
    with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
        sections = [dict(zip(
            ["id", "index", "title", "text", "page_start", "page_end", "span_info"], r,
            strict=True,
        )) for r in connection.execute(
            "select id,section_index,title,text,page_start,page_end,span_info "
            "from paper_sections where revision_id=? order by section_index", (revision,),
        )]
    if not sections:
        raise ValueError("persisted_sections_required")
    checks = []
    for surname, marker in [("Khodak", "Initialization and Regularization"),
                            ("Lepikhin", "GShard"), ("Loshchilov", "Weight Decay"),
                            ("Socher", "Recursive Deep"), ("Allen-Zhu", "Convergence Theory")]:
        candidate = next(c for c in case["inventory"]["candidates"] if marker in c["title"])
        terms = [surname, "GShard"] if surname == "Lepikhin" else [surname]
        found = []
        for section in sections:
            for term in terms:
                for match in re.finditer(re.escape(term), section["text"], re.I):
                    found.append({
                        "section_id": section["id"], "start": match.start(), "end": match.end(),
                        "section_title": section["title"],
                        "in_bibliography": "reference" in (section["title"] or "").lower(),
                        "term": term,
                    })
        checks.append({
            "candidate_id": candidate["paper_id"], "title": candidate["title"],
            "matches_in_persisted_sections": found,
            "reference_ids": [r["id"] for r in candidate["source_references"]],
            "non_citation_confirmed": False,
        })
    # The GLUE list continuation is present in the same stored section. Record the
    # omitted character from that section; never join unknown gaps or assign a page.
    section = next(s for s in sections if "DATASET DETAILS" in (s["title"] or ""))
    spans = sorted([s for s in case["spans"] if s["section_id"] == section["id"]],
                   key=lambda s: int(s["start_offset"]))
    gaps = []
    for left, right in zip(spans, spans[1:], strict=False):
        start, end = int(left["end_offset"]), int(right["start_offset"])
        if start < end:
            gaps.append({
                "section_id": section["id"], "start": start, "end": end,
                "stored_text": section["text"][start:end],
                "left_source_id": left["span_id"], "right_source_id": right["span_id"],
                "unknown_page_for_gap": True,
            })
    return {
        "revision": revision, "source_pack_sha256": digest(case["spans"]),
        "persisted_section_hashes": {s["id"]: hashlib.sha256(s["text"].encode()).hexdigest()
                                     for s in sections},
        "candidate_checks": checks, "dataset_details_span_gaps": gaps,
        "dataset_list_continuation_present": "and STS-B" in section["text"],
        "stored_text_and_sources_modified": False,
        "limitations": "PDF extraction is not a complete citation oracle; no new PDF retrieved",
    }


def run() -> dict:
    if (OUTPUT / "comparison.json").exists():
        raise ValueError("report_already_exists_do_not_overwrite")
    protected = [ROOT / name for name in (
        "docs/scoring-lineage.md", "docs/research-lineage.md", "docs/requirements.md",
        "ADR/0001-mvp-decisions.md", "src/paper_review/domain/stage1_scoring.py",
        ".work/s2-rate-limited.json", ".work/s2-http-gate.json",
    )]
    hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in protected}
    attention = next(c for c in json.loads(ATTENTION.read_text())["cases"]
                     if c["aid"] == "1706.03762")
    lora = json.loads((PREVIOUS / "comparison.json").read_text())
    cases = {
        "1706.03762": [{"id": r["id"], "title": r["title"], "canonical_rank": r["rank"],
                       "score": r["score"]} for r in attention["after"]],
        "2106.09685": [{"id": r["candidate_id"], "title": r["title"],
                       "canonical_rank": r["rank_after"], "score": r["score_after"]}
                      for r in lora["ranking"]],
    }
    frozen_input = digest(cases)
    result = {"scope": "hypothetical_policy_comparison_only", "production_policy_changed": False,
              "cases": {aid: compare(rows, DEFAULT_STAGE1_WEIGHTS) for aid, rows in cases.items()}}
    # Prove exact reproduction before interpreting hypothetical changes.
    for aid, rows in cases.items():
        expected = [r["id"] for r in sorted(rows, key=lambda r: r["canonical_rank"])]
        assert expected == [r["id"] for r in result["cases"][aid]["policies"]["current"]["ranked"]]
    case = next(c for c in load_cases() if c["aid"] == "2106.09685")
    audit = source_audit(case, ROOT / ".work/app.db")
    assert frozen_input == digest(cases)
    assert hashes == {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in protected}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    for name, data in [("comparison.json", result), ("source-audit.json", audit),
                       ("verification.json", {"protected_files_sha256": hashes,
                        "production_policy_changed": False, "new_provider_calls": 0,
                        "new_llm_cost_usd": 0, "cumulative_used_or_reserved_usd": json.loads(
                            (PREVIOUS / "verification.json").read_text()
                        )["cumulative_used_or_reserved_usd"],
                        "frozen_candidate_metrics_sha256": frozen_input,
                        "input_artifact_sha256": {
                            str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                            for p in [ATTENTION, PREVIOUS / "comparison.json",
                                      PREVIOUS / "verification.json"]
                        },
                        "formal_quality_metrics": "N/A: no independent gold annotation"})]:
        with (OUTPUT / name).open("x") as handle:
            handle.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    return {"status": "completed", "cases": len(cases), "policies_per_case": len(POLICIES),
            "new_provider_calls": 0}


if __name__ == "__main__":
    print(json.dumps(run()))
