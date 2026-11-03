"""Paired audit records valid negative verdicts once, never regenerates for a preferred answer."""

import json

from paper_review.application import contribution_verification as evaluation
from paper_review.llm.provider import ProviderResponse


def test_paired_audit_uses_only_selected_quotes_and_reuses_result(settings, monkeypatch):
    tmp_path = settings.database_path.parent
    output = tmp_path / "new"
    old = tmp_path / "old"
    output.mkdir()
    old.mkdir()
    monkeypatch.setattr(evaluation, "OUTPUT", output)
    monkeypatch.setattr(evaluation.previous, "OUTPUT", old)
    monkeypatch.setattr(evaluation, "DATABASE", tmp_path / "audit.db")
    monkeypatch.setattr(evaluation, "prior_cost", lambda: .27)
    monkeypatch.setattr(evaluation, "plan", lambda: {"frozen": True})
    monkeypatch.setenv("OPENAI_API_KEY", "dummy-key")
    (old / "results.json").write_text(json.dumps({"lineage": {"contributions": {
        "problem": "制約", "contributions": [{"id": "c", "description": "広すぎる主張",
            "evidence": [{"span_id": "s", "quote": "Original method."}]}],
    }}}))
    sources = [{"span_id": "s", "text": "Original method.", "section_id": "method",
                "section": "1 Method"},
               {"span_id": "unused", "text": "Other context.", "section_id": "other"}]
    monkeypatch.setattr(evaluation, "load_cases", lambda: [{"aid": "2106.09685", "spans": sources}])
    calls = []

    class Provider:
        async def generate(self, request):
            calls.append(request)
            if request.task_type == "contribution_extraction":
                alias = next(s["span_id"] for s in request.context_spans
                             if s["text"] == "Original method.")
                value = {"problem": "制約", "contributions": [{"id": "c", "description": "狭い主張",
                         "evidence": [{"span_id": alias}]}]}
            else:
                context = {s["span_id"]: s["text"] for s in request.context_spans}
                assert set(context) == {"s", "task-data"}
                summary = json.loads(context["task-data"])["summary"]
                value = {"claims": [{"text": summary, "decision": (
                    "unsupported" if summary == "広すぎる主張" else "supported"
                ), "evidence_span_ids": [] if summary == "広すぎる主張" else ["s"],
                    "reason": "選択した引用との対応"}]}
            return ProviderResponse(output=value, request_id="mock", returned_model=request.model)

    monkeypatch.setattr(evaluation, "OpenAIProvider", Provider)
    assert evaluation.evaluate()["reason"] == "explicit_shared_ceiling_required"
    assert evaluation.evaluate(50)["reason"] == "prepared_plan_required_or_changed"
    (output / "prepared-plan.json").write_text('{"frozen":true}')
    result = evaluation.evaluate(50)
    assert result["status"] == "completed" and result["new_all_supported"], result
    assert [r["supported"] for r in result["reviews"]] == [False, True]
    assert len(calls) == 3  # The valid unsupported verdict did not trigger a retry.
    assert evaluation.evaluate(50) == result and len(calls) == 3
    (output / "results.json").write_text('{"status":"stopped"}')
    assert evaluation.evaluate(50)["reason"] == "previous_window_requires_review"
    assert len(calls) == 3
