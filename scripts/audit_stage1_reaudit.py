"""Offline audit of frozen Stage 1 JSON. Never makes provider requests.

Usage: uv run python scripts/audit_stage1_reaudit.py
Optional --quality-review JSON is a separate human review bound to an input hash.
Existing output is refused unless --overwrite is explicitly specified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from paper_review.application.stage1_audit import (
    EvaluationArtifact,
    QualityReview,
    audit_stage1,
    render_audit_markdown,
)

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="*", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "evaluation/reports/stage1-reaudit")
    parser.add_argument("--quality-review", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    inputs = args.inputs or sorted(
        (ROOT / "evaluation/reports/specter2-integration").glob("*-result.json")
    )
    if not inputs:
        parser.error("no evaluation inputs")
    if not args.output.resolve().is_relative_to(ROOT):
        parser.error("output must be inside workspace")
    destinations = [args.output / "result.json", args.output / "REPORT.md"]
    if not args.overwrite and any(p.exists() for p in destinations):
        parser.error("output exists; choose a new directory or explicitly use --overwrite")
    review = (
        QualityReview.model_validate_json(args.quality_review.read_bytes())
        if (args.quality_review)
        else None
    )
    reports = []
    review_matched = False
    for path in sorted(inputs):
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        bound_review = review if review and review.input_sha256 == digest else None
        review_matched |= bound_review is not None
        reports.append(
            audit_stage1(
                EvaluationArtifact.model_validate_json(raw),
                input_sha256=digest,
                quality_review=bound_review,
            )
        )
    if review and not review_matched:
        parser.error("quality review does not match any input")
    reports.sort(key=lambda r: r["target"])
    args.output.mkdir(parents=True, exist_ok=True)
    destinations[0].write_text(json.dumps(reports, indent=2, allow_nan=False) + "\n")
    destinations[1].write_text(render_audit_markdown(reports))
    for report in reports:
        print(report["target"], report["gate"])


if __name__ == "__main__":
    main()
