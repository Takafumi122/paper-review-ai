"""Release canary cannot rerun paid work or label partial/unknown results passed."""

import runpy
from pathlib import Path

import pytest

from paper_review.config import Settings

MODULE = runpy.run_path(str(Path(__file__).resolve().parents[2]
                           / "deployment/verify_railway_graph.py"))


@pytest.mark.parametrize("status,unknown,cost,attempts,edges,passed", [
    ("completed", False, 0.4, 25, [1], True),
    ("partial_completed", False, 0.4, 25, [1], False),
    ("completed", True, 0.4, 25, [1], False),
    ("completed", False, 0.51, 25, [1], False),
    ("completed", False, 0.4, 31, [1], False),
    ("completed", False, 0.4, 25, [], False),
])
def test_release_summary_preserves_failure(status, unknown, cost, attempts, edges, passed):
    result = MODULE["summarize"]({
        "status": status, "result": {"lineage": {"edges": edges}},
        "cost": {"estimated_usd": cost, "has_unknown_cost": unknown},
    }, attempts)
    assert (result["release_canary"] == "passed") is passed


def test_existing_marker_prevents_any_new_http_or_job(tmp_path, monkeypatch):
    run = MODULE["run"]
    marker = tmp_path / "canary.json"
    marker.write_text('{"state":"claimed"}')
    monkeypatch.setitem(run.__globals__, "MARKER", marker)
    monkeypatch.setattr(Settings, "from_environment", classmethod(lambda cls: Settings(
        initial_budget_usd=0.5, openalex_local_verified=True, openalex_local_audited=True)))
    monkeypatch.delenv("PAPER_REVIEW_ACCESS_TOKEN", raising=False)
    assert run() == {"release_canary": "already_claimed_no_new_job"}
    assert marker.read_text() == '{"state":"claimed"}'


def test_over_budget_configuration_cannot_claim_canary(tmp_path, monkeypatch):
    run = MODULE["run"]
    marker = tmp_path / "canary.json"
    monkeypatch.setitem(run.__globals__, "MARKER", marker)
    monkeypatch.setattr(Settings, "from_environment", classmethod(lambda cls: Settings(
        initial_budget_usd=1, openalex_local_verified=True, openalex_local_audited=True)))
    with pytest.raises(ValueError, match="configuration_not_ready"):
        run()
    assert not marker.exists()
