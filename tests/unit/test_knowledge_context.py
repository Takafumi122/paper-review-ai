"""Generated drafts are not factual context for later prerequisite teaching."""

import json

from paper_review.application.full_analysis import fit_context
from paper_review.application.knowledge_prompts import detail_context, short_knowledge_inputs
from paper_review.domain.understanding import KnowledgeBatch
from paper_review.llm.gateway import prepare_source_request, request_schema
from paper_review.llm.token_budget import estimate_input_tokens


def test_detail_uses_selected_transition_without_propagating_false_draft():
    item = {
        "concept": "Recurrent dependency",
        "from_step_id": "a",
        "to_step_id": "b",
        "short_explanation": "Incorrect draft",
        "why_needed": "Unsupported rationale",
        "evidence": [{"span_id": "source", "quote": "Duplicated quote"}],
    }
    reasoning = {
        "steps": [
            {"id": "a", "kind": "problem", "summary": "Sequence dependence"},
            {"id": "b", "kind": "approach", "summary": "Parallel positions"},
            {"id": "c", "kind": "result", "summary": "Unrelated result"},
        ]
    }
    data = detail_context(item, reasoning)
    assert [s["id"] for s in data["steps"]] == ["a", "b"]
    assert "Incorrect draft" not in str(data)
    assert "Unsupported rationale" not in str(data)
    assert "Duplicated quote" not in str(data)


def test_short_knowledge_keeps_mechanism_sources_before_unrelated_sections_and_results():
    def span(sid, section, text, start):
        return {
            "span_id": sid,
            "paper_id": "p",
            "revision": "r",
            "section_id": section,
            "text": text,
            "start_offset": str(start),
            "end_offset": str(start + len(text)),
        }

    a = span("a", "method", "Reported parameterization.", 0)
    b = span("b", "method", "The original weights remain fixed.", len(a["text"]))
    distractors = [span(f"noise{i}", f"other{i}", "Unrelated result. " * 20, 0) for i in range(20)]
    result = span("score", "results", "Task scores.", 0)
    flow = {
        "steps": [
            {"id": "1", "kind": "approach", "summary": "Method", "evidence": [{"span_id": "a"}]},
            {"id": "2", "kind": "result", "summary": "Result", "evidence": [{"span_id": "score"}]},
        ],
        "edges": [{"from_step_id": "1", "to_step_id": "2", "relation": "evaluates"}],
    }
    ordered, extra, edges = short_knowledge_inputs(flow, [*distractors, result, b, a])
    assert [s["span_id"] for s in ordered[:3]] == ["a", "b", "score"]
    assert ordered[0] is a and ordered[1] is b  # no quote rewriting
    assert "evidence" not in extra["flow"]["steps"][0]
    assert extra["source_scope"] == "selected_original_excerpts_not_exhaustive_paper"
    schema = request_schema(KnowledgeBatch, bind_evidence=True, knowledge_edges=edges)
    task = {"span_id": "task-data", "text": json.dumps(extra)}
    effective, context, _ = prepare_source_request(schema, (a, b, task))
    limit = estimate_input_tokens(effective, context, "Explain the mechanism.")
    selected = fit_context(
        schema,
        "Explain the mechanism.",
        ordered,
        limit,
        extra_span=task,
        preserve_source_order=True,
    )
    assert [s["span_id"] for s in selected if s["span_id"] != "task-data"] == ["a", "b"]


def test_short_knowledge_does_not_guess_continuation_across_source_gaps():
    a = {
        "span_id": "a",
        "paper_id": "p",
        "revision": "r",
        "section_id": "method",
        "text": "First.",
        "start_offset": "0",
        "end_offset": "6",
    }
    b = {**a, "span_id": "b", "text": "Next.", "start_offset": "7", "end_offset": "12"}
    noise = {**a, "span_id": "noise", "section_id": "other"}
    flow = {
        "steps": [
            {"id": "1", "kind": "approach", "summary": "Method", "evidence": [{"span_id": "a"}]}
        ],
        "edges": [],
    }
    ordered, _, _ = short_knowledge_inputs(flow, [noise, a, b])
    assert [s["span_id"] for s in ordered] == ["a", "noise", "b"]
