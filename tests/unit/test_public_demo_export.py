"""Public static metadata must never include bodies, keys or embedding vectors."""

import json
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_demo_export_matches_checked_in_data_and_preserves_recorded_inventory():
    build = runpy.run_path(str(ROOT / "scripts/build_demo_data.py"))["build_demo_data"]
    data = build()
    assert data == json.loads((ROOT / "frontend/src/data/demo.json").read_text())
    assert data["live_analysis"] is False and data["stage1_quality_evaluable"] is False
    assert [len(p["candidates"]) for p in data["papers"]] == [40, 61, 35]
    assert sum(bool(c["diagnostic_probe"]) for p in data["papers"] for c in p["candidates"]) == 14
    assert data == build()


def test_demo_export_is_allowlisted_metadata_without_secrets_bodies_or_vectors():
    build = runpy.run_path(str(ROOT / "scripts/build_demo_data.py"))["build_demo_data"]
    forbidden = {
        "embedding",
        "vector",
        "raw_text",
        "abstract",
        "api_key",
        "x-api-key",
        "headers",
        "text",
        "citation_contexts",
        "target_reference_raw_text",
    }

    def check(value):
        if isinstance(value, dict):
            assert not forbidden.intersection(value)
            for item in value.values():
                check(item)
        elif isinstance(value, list):
            for item in value:
                check(item)

    check(build())
