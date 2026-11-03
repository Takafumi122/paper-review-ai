"""Batch review must cover every item and cannot borrow another item's selected evidence."""

import asyncio

import pytest

from paper_review.application.contribution_review import audit_contributions
from paper_review.domain.understanding import Contributions


@pytest.mark.parametrize("fault", ["missing", "borrowed", "skipped_clause"])
def test_contribution_batch_review_reference_and_coverage_checks(fault):
    value = Contributions.model_validate({"problem": "研究課題", "problem_evidence": [
        {"span_id": "p", "quote": "A problem."}], "contributions": [{
        "id": "c", "description": "研究手法", "evidence": [{"span_id": "c", "quote": "A method."}],
    }]})

    async def call(schema, stage_id, task, prompt, spans, extra, validator, **kwargs):
        assert kwargs == {"preserve_source_order": True, "bind_evidence": False,
                          "require_all_sources": True}
        assert {s["span_id"] for s in spans} == {"p", "c"}
        items = [{"item_id": item["item_id"], "claims": [{
            "text": item["summary"], "decision": "supported",
            "evidence_span_ids": item["selected_source_ids"], "reason": "引用対応",
        }]} for item in extra["items"]]
        if fault == "missing":
            items.pop()
        elif fault == "borrowed":
            items[0]["claims"][0]["evidence_span_ids"] = ["c"]
        else:
            items[0]["claims"][0]["text"] = "研究"
        result = schema.model_validate({"items": items})
        validator(result)
        return result

    with pytest.raises(ValueError, match={
        "missing": "contribution_review_incomplete_items",
        "borrowed": "reasoning_review_unselected_evidence",
        "skipped_clause": "reasoning_review_incomplete_coverage",
    }[fault]):
        asyncio.run(audit_contributions(value, "stage", call))
