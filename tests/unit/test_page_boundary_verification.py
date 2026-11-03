"""Real frozen-source regression plus bounded operator-window controls."""

import hashlib
import json

import pytest

from paper_review.application import page_boundary_verification as verification


def test_fixed_plan_is_bounded_and_has_no_s2_or_graph_calls():
    plan = verification.plan()
    assert plan["max_logical_calls"] == 3 and plan["max_attempts"] == 9
    assert plan["new_s2_calls"] == plan["new_graph_calls"] == 0
    assert plan["prior_used_or_reserved_usd"] < plan["cumulative_ceiling_usd"] == 1.6
    assert [len(c["changed_context_ids"]) for c in plan["cases"]] == [10, 11]
    assert all(t <= 8000 for c in plan["cases"] for t in c["input_tokens_estimates"])


def test_original_sources_stay_exact_and_recovered_socher_quote_has_real_ranges():
    for item in verification.inputs():
        sources = {s["span_id"]: s for s in item["case"]["spans"]}
        assert all(sources[s["span_id"]] == s for s in item["original_case"]["spans"])
        assert all(s["page"] == "unknown" and s["text"].isspace()
                   for s in sources.values() if s.get("source_kind") == "stored_separator")
        if item["case"]["aid"] == "2106.09685":
            context = next(c for c in item["changed"] if "Recursive Deep" in c["candidate_title"])
            assert context["support_options"]
            for option in context["support_options"]:
                quote = "".join(sources[r["span_id"]]["text"][
                    r["start_offset"] - int(sources[r["span_id"]]["start_offset"]):
                    r["end_offset"] - int(sources[r["span_id"]]["start_offset"])
                ] for r in option["source_ranges"])
                assert quote == context["text"][option["start"]:option["end"]]
                assert "Socher et al. (2013)" in quote and "and STS-B" in quote
                assert any(sources[sid]["page"] == "unknown" for sid in option["source_ids"])


def test_repaired_context_merge_keeps_old_decisions_only_when_identity_matches():
    for item in verification.inputs():
        records = []
        for context in item["changed"]:
            records.append({
                **{k: v for k, v in context.items() if k != "text"},
                "text_sha256": hashlib.sha256(context["text"].encode()).hexdigest(),
                "status": "completed", "classification": None, "support_id": None,
                "supporting_quote": None, "reason": "Synthetic binding test only",
            })
        result = verification.previous.merge(
            item["contexts"], item["old_audit"], {"contexts": records, "errors": []},
            new_origin=verification.VERSION,
        )
        assert len(result["contexts"]) == len(item["old_audit"]["contexts"])
        assert result["new_or_changed_contexts"] == len(item["changed"])


@pytest.mark.parametrize("status", ["started", "stopped"])
def test_interrupted_or_failed_window_never_resumes(settings, monkeypatch, status):
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    (output / "results.json").write_text(json.dumps({"status": status}))
    assert verification.evaluate(160)["reason"] == "previous_window_requires_review"
    assert verification.evaluate(50)["status"] == "blocked"
