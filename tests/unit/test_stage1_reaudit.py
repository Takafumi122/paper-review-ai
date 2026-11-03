"""Gate tests simulate attestations; no fixture here is real successful provider data."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from paper_review.application.stage1_audit import (
    EvaluationArtifact,
    QualityReview,
    audit_stage1,
    render_audit_markdown,
)

ROOT = Path(__file__).resolve().parents[2]
MODEL = "semantic-scholar:specter_v2"


def snapshot():
    return json.loads(
        (ROOT / "evaluation/reports/specter2-integration/1706.03762-result.json").read_text()
    )


def simulated_success():
    """Simulated verified-provider attestation, solely for testing gate logic."""
    raw = snapshot()
    raw["evidence"] = {
        "data_origin": "recorded_provider_success",
        "provider_contract_verified": True,
    }
    p = {
        "source": "https://api.semanticscholar.org/graph/v1/paper/batch",
        "requested_field": "embedding.specter_v2",
        "status": "available",
        "response_model": "specter_v2",
    }
    raw["statistics"].update(
        target_embedding_available=True,
        target_model=MODEL,
        target_dimension=2,
        target_embedding_provenance=p,
        http_statuses=[200],
    )
    for c in raw["candidate_embedding_outcomes"]:
        if c["eligibility"] == "eligible":
            c.update(
                embedding_available=True,
                embedding_model=MODEL,
                dimension=2,
                embedding_provenance=copy.deepcopy(p),
            )
    for row in raw["ranking"]:
        row.update(
            embedding_available=True,
            embedding_model=MODEL,
            dimension=2,
            embedding_provenance=copy.deepcopy(p),
        )
        row["score"]["semantic_similarity"] = 0.5
        row["score"]["component_details"]["semantic_similarity"].update(
            value=0.5, missing_reason=None, calculation_source="specter2"
        )
    return raw


def run(raw, **kwargs):
    return audit_stage1(EvaluationArtifact.model_validate(raw), input_sha256="a" * 64, **kwargs)


def test_zero_coverage_is_not_evaluable_and_retains_probes_missing_metrics():
    report = run(snapshot())
    assert report["gate"] == "NOT EVALUABLE"
    assert report["counts"] == {
        "candidates": 40,
        "eligible": 34,
        "ranked": 34,
        "top_k": 10,
        "top_k_limit": 10,
    }
    assert report["embeddings"]["coverage_percent"] == 0
    assert report["metric_coverage"]["semantic_similarity"] == {"observed": 0, "missing": 34}
    assert len(report["probes"]) == 5 and all(p["present"] for p in report["probes"])
    assert report["top10"][0]["score"]["semantic_similarity"] is None
    assert report["top10"][0]["score"]["citation_importance"] is None
    assert report["score_counts"] == {"complete": 0, "partial": 34, "unavailable": 0}


def test_missing_target_cannot_certify_ready():
    raw = simulated_success()
    raw["statistics"]["target_embedding_available"] = False
    assert "target_embedding_missing" in run(raw)["gate_reasons"]


@pytest.mark.parametrize("origin", ["synthetic", "error_fixture", "unknown"])
def test_synthetic_and_error_data_cannot_certify_ready(origin):
    raw = simulated_success()
    raw["evidence"]["data_origin"] = origin
    assert run(raw)["gate"] == "NOT EVALUABLE"


def test_verified_success_can_enter_quality_audit_but_not_acceptance():
    report = run(simulated_success())
    assert report["gate"] == "READY FOR QUALITY AUDIT"
    assert report["quality_review"] is None and report["stage2_ready"] is False


def test_separate_hash_bound_human_quality_review():
    review = QualityReview(
        input_sha256="a" * 64,
        reviewer="test reviewer",
        rubric_version="test",
        decision="inconclusive",
        notes="Simulated review only",
    )
    result = run(simulated_success(), quality_review=review)
    assert result["gate"] == "QUALITY AUDIT RESULT"
    assert result["quality_review"]["decision"] == "inconclusive"
    assert run(snapshot(), quality_review=review)["gate"] == "NOT EVALUABLE"
    with pytest.raises(ValueError, match="hash_mismatch"):
        run(
            simulated_success(), quality_review=review.model_copy(update={"input_sha256": "b" * 64})
        )


def test_missing_probe_is_reported_without_omitting_it():
    raw = snapshot()
    raw["probes"].append({"probe": "Absent", "paper_id": "not-present"})
    report = run(raw)
    absent = next(p for p in report["probes"] if p["probe"] == "Absent")
    assert absent["present"] is False and absent["rank"] is None


def test_zero_score_is_observed_and_unavailable_scores_are_counted():
    raw = snapshot()
    row = raw["ranking"][0]
    row["score"]["semantic_similarity"] = 0.0
    row["score"]["initial_score"] = 0.0
    # Keep ranking validity; this test only checks missing-vs-zero reporting.
    report = run(raw)
    assert report["top10"][0]["score"]["initial_score"] == 0.0
    assert report["metric_coverage"]["semantic_similarity"]["observed"] == 1
    row = raw["ranking"].pop()
    row["rank"] = 0
    row["score"].update(initial_score=None, status="unavailable")
    raw["unranked"] = [row]
    report = run(raw)
    assert report["score_counts"]["unavailable"] == 1
    assert report["counts"]["ranked"] == 33


def test_deterministic_order_top_k_and_no_vectors_in_markdown():
    raw = snapshot()
    raw["shortlist_limit"] = 3
    raw["ranking"][0]["embedding"] = [123456.789, 987654.321]
    a = run(raw)
    raw["candidate_embedding_outcomes"].reverse()
    raw["ranking"].reverse()
    raw["probes"].reverse()
    b = run(raw)
    assert a == b
    assert a["counts"]["top_k"] == 3
    md = render_audit_markdown([a])
    assert "123456.789" not in md and "987654.321" not in md
    assert "vector" not in md.lower()
    assert (
        hashlib.sha256(md.encode()).hexdigest()
        == hashlib.sha256(render_audit_markdown([b]).encode()).hexdigest()
    )


@pytest.mark.parametrize("change", ["v1", "dimension", "lexical", "http429", "unverified"])
def test_contract_or_score_inconsistency_fails_closed(change):
    raw = simulated_success()
    if change == "v1":
        raw["statistics"]["target_model"] = "semantic-scholar:specter_v1"
    elif change == "dimension":
        raw["statistics"]["target_dimension"] = 768
    elif change == "lexical":
        for row in raw["ranking"]:
            row["score"]["component_details"]["semantic_similarity"]["calculation_source"] = (
                "lexical"
            )
    elif change == "http429":
        raw["statistics"]["http_statuses"] = [429]
    else:
        raw["evidence"]["provider_contract_verified"] = False
    assert run(raw)["gate"] == "NOT EVALUABLE"


def test_all_fourteen_existing_probes_survive():
    reports = []
    for path in sorted((ROOT / "evaluation/reports/specter2-integration").glob("*-result.json")):
        reports.append(run(json.loads(path.read_text())))
    assert sum(len(r["probes"]) for r in reports) == 14
    assert all(p["present"] for r in reports for p in r["probes"])


@pytest.mark.parametrize("change", ["provider_v1", "hash", "row_mismatch"])
def test_inconsistent_artifact_does_not_claim_readiness(change):
    raw = simulated_success()
    if change == "provider_v1":
        raw["statistics"]["target_embedding_provenance"]["response_model"] = "specter@v0.1.1"
        for c in raw["candidate_embedding_outcomes"]:
            c["embedding_provenance"]["response_model"] = "specter@v0.1.1"
    elif change == "hash":
        raw["fixture_sha256"] = "invalid"
    else:
        raw["ranking"][0]["embedding_available"] = False
    assert run(raw)["gate"] == "NOT EVALUABLE"
