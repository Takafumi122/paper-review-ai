"""Identity-bound reuse after C IDs shift; no semantic model or network calls."""

import copy
import hashlib
import json

import pytest

from paper_review.application import citation_year_verification as verification


def fresh_audit(changed):
    return {"contexts": [{
        **{k: v for k, v in c.items() if k != "text"},
        "text_sha256": hashlib.sha256(c["text"].encode()).hexdigest(),
        "status": "completed", "classification": None, "support_id": None,
        "supporting_quote": None, "reason": "Synthetic abstention for binding test",
        "evidence_source_ids": [], "evidence_source_ranges": [],
    } for c in changed], "errors": []}


def test_one_batch_plan_keeps_s2_stopped_and_bounds_cost():
    plan = verification.plan()
    assert plan["max_logical_calls"] == 1 and plan["max_attempts"] == 3
    assert plan["new_s2_requests"] == plan["new_graph_calls"] == 0
    assert plan["input_tokens_estimate"] <= 8000
    assert plan["prior_used_or_reserved_usd"] < plan["authorized_cumulative_usd"] == 1.6
    assert len(plan["selected_context_ids"]) == 4


def test_reuse_requires_text_source_candidate_and_anchor_identity():
    _, contexts, old, changed = verification.inputs()
    merged = verification.merge(contexts, old, fresh_audit(changed))
    assert len(merged["contexts"]) == 61
    assert merged["reused_contexts"] == 57
    assert merged["new_or_changed_contexts"] == 4
    assert len({c["context_id"] for c in merged["contexts"]}) == 61
    originals = {verification.signature(c): c for c in old["contexts"]}
    for row in merged["contexts"]:
        if row["evaluation_origin"] == verification.previous.VERSION:
            original = originals[verification.signature(row)]
            assert row["classification"] == original["classification"]
            assert row.get("supporting_quote") == original.get("supporting_quote")
            if row.get("support_id"):
                assert row["support_id"] in {p["support_id"] for p in row["support_options"]}


@pytest.mark.parametrize("field", ["candidate_id", "text_sha256", "citation_anchors"])
def test_fresh_positional_id_cannot_reassign_a_different_source(field):
    _, contexts, old, changed = verification.inputs()
    fresh = fresh_audit(changed)
    fresh["contexts"][0][field] = ["wrong"] if field == "citation_anchors" else "wrong"
    with pytest.raises(ValueError, match="identity_mismatch"):
        verification.merge(contexts, old, fresh)


def test_changed_anchor_never_inherits_prior_role():
    _, contexts, old, changed = verification.inputs()
    changed_resnet = next(c for c in changed if "ResNet" in c["candidate_title"])
    fresh = fresh_audit(changed)
    merged = verification.merge(contexts, old, fresh)
    row = next(c for c in merged["contexts"] if c["context_id"] == changed_resnet["context_id"])
    assert row["evaluation_origin"] == verification.VERSION
    assert row["citation_anchors"] == ["Allen-Zhu & Li,\n2019"]
    assert "Allen-Zhu et al., 2019" not in row["citation_anchors"]


def test_reused_quote_requires_exact_source_range():
    _, contexts, old, changed = verification.inputs()
    altered = copy.deepcopy(old)
    supported = next(c for c in altered["contexts"] if c.get("support_id"))
    supported["evidence_source_ranges"][0]["start_offset"] += 1
    with pytest.raises(ValueError, match="quote_mismatch"):
        verification.merge(contexts, altered, fresh_audit(changed))


def test_stopped_window_is_not_retried(settings, monkeypatch):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    (output / "results.json").write_text(json.dumps({"status": "stopped"}))
    assert verification.evaluate(160)["reason"] == "previous_window_requires_review"
    assert verification.evaluate(50)["status"] == "blocked"
