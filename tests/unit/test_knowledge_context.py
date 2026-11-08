"""Generated drafts are not factual context for later prerequisite teaching."""

import json

import pytest

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


@pytest.mark.parametrize(("second_kind", "second_title"), [
    ("approach", "2.2 Method"), ("result", "4.1 Sensitivity analysis"),
])
def test_small_knowledge_budget_keeps_distinct_methods_and_evaluation_conditions(
    second_kind, second_title,
):
    def source(sid, section, title):
        return {"span_id": sid, "paper_id": "p", "revision": "v1",
                "section_id": section, "section": title,
                "text": "An original mechanism or evaluation condition. " * 8}

    # One numbered family contains two different method steps. Its length
    # must not put the second mechanism and experiment behind the first step.
    first = source("first", "a", "2.1 Method")
    second = source("second", "b", second_title)
    condition = source("condition", "c", "3 Evaluation")
    abstract = source("abstract", "d", "Abstract")
    expansions = [source(f"expansion{i}", "a", "2.1 Method") for i in range(4)]
    flow = {"steps": [
        {"id": "a", "kind": "approach", "summary": "First operation",
         "evidence": [{"span_id": s["span_id"]} for s in [first, *expansions]]},
        {"id": "b", "kind": second_kind, "summary": "Second operation",
         "evidence": [{"span_id": "second"}]},
        {"id": "c", "kind": "experiment", "summary": "Evaluation setting",
         "evidence": [{"span_id": "abstract"}, {"span_id": "condition"}]},
    ], "edges": [
        {"from_step_id": "a", "to_step_id": "b", "relation": "leads_to"},
        {"from_step_id": "b", "to_step_id": "c", "relation": "evaluates"},
    ]}
    original = [abstract, first, *expansions, second, condition]
    ordered, extra, refs = short_knowledge_inputs(flow, original)
    schema = request_schema(KnowledgeBatch, bind_evidence=True, knowledge_edges=refs)
    task = {"span_id": "task-data", "text": json.dumps(extra, ensure_ascii=False)}
    effective, context, _ = prepare_source_request(schema, (first, second, condition, task))
    limit = estimate_input_tokens(effective, context, "Explain.")
    selected = fit_context(schema, "Explain.", ordered, limit, extra_span=task,
                           preserve_source_order=True)
    assert [s["span_id"] for s in selected] == ["first", "second", "condition", "task-data"]
    assert {s["span_id"]: s for s in ordered} == {s["span_id"]: s for s in original}
    assert all(s is original[original.index(s)] for s in ordered)


def test_named_mechanism_retrieval_uses_original_method_text_in_the_same_revision():
    def span(sid, title, text, offset=0):
        return {"span_id": sid, "paper_id": "p", "revision": "v1",
                "section_id": title, "section": title, "text": text,
                "start_offset": str(offset), "end_offset": str(offset + len(text))}

    overview = span("overview", "3 Method", "We introduce AlphaFormat and ScalePacking.")
    filler = span("filler", "3 Method", "Some intermediate context.", len(overview["text"]))
    alpha = span("alpha", "3 Method", "AlphaFormat represents the quantized values.",
                 len(overview["text"]) + len(filler["text"]))
    scale = span("scale", "3 Method", "ScalePacking quantizes the scaling constants.",
                 int(alpha["end_offset"]))
    old = {**alpha, "span_id": "old", "revision": "v0", "section": "2 Background"}
    foreign = {**scale, "span_id": "foreign", "paper_id": "other", "section": "2 Background"}
    noise = span("noise", "1 Introduction", "Long unrelated context.")
    result = span("result", "4 Results", "Scores.")
    flow = {"steps": [
        {"id": "m", "kind": "approach", "summary": "AlphaFormatとScalePackingを提案する。",
         "evidence": [{"span_id": "overview"}]},
        {"id": "r", "kind": "result", "summary": "Scores", "evidence": [{"span_id": "result"}]},
    ], "edges": [{"from_step_id": "m", "to_step_id": "r", "relation": "evaluates"}]}
    spans = [old, foreign, noise, overview, filler, alpha, scale, result]
    ordered, _, _ = short_knowledge_inputs(flow, spans)
    ids = [s["span_id"] for s in ordered]
    assert ids.index("alpha") < ids.index("result")
    assert ids.index("scale") < ids.index("result")
    assert ids.index("result") < ids.index("old")
    assert ids.index("result") < ids.index("foreign")
    assert {s["span_id"]: s for s in ordered} == {s["span_id"]: s for s in spans}


def test_theoretical_results_receive_fair_mechanism_context_before_repeated_introductions():
    def source(sid, section, title, text):
        return {"span_id": sid, "paper_id": "p", "revision": "v1", "section_id": section,
                "section": title, "text": text, "start_offset": "0",
                "end_offset": str(len(text))}

    intro = source("intro", "intro", "1 Introduction", "An overview.")
    method = source("method", "method", "4 Optimization", "The method is defined here.")
    parent = source("parent", "parent", "5 Theoretical analysis", "Theoretical analysis.")
    equivalent = source("equivalent", "equivalent", "5.1 Equivalence classes", "Invariant classes.")
    normalization = source(
        "normalization", "normalization", "5.2 Normalization", "A normalization.",
    )
    spans = [intro, method, parent, equivalent, normalization]
    flow = {"steps": [
        {"id": "method", "kind": "approach", "summary": "手法の概要。",
         "evidence": [{"span_id": "intro"}, {"span_id": "method"}]},
        {"id": "theorem", "kind": "result", "summary": "同値性の理論結果。",
         "evidence": [{"span_id": "equivalent"}, {"span_id": "normalization"}]},
    ], "edges": []}
    ordered, _, _ = short_knowledge_inputs(flow, spans)
    assert [s["span_id"] for s in ordered[:4]] == [
        "method", "equivalent", "normalization", "intro",
    ]
    assert {s["span_id"]: s for s in ordered} == {s["span_id"]: s for s in spans}


@pytest.mark.parametrize("variant", ["exact", "gap", "revision", "ambiguous"])
def test_preceding_definition_requires_unique_contiguous_original_interval(variant):
    definition = {
        "span_id": "definition", "paper_id": "p", "revision": "r", "section_id": "method",
        "text": "Definition.", "start_offset": "0", "end_offset": "11",
    }
    anchor = {**definition, "span_id": "anchor", "text": "Operation.",
              "start_offset": "11", "end_offset": "21"}
    noise = {**definition, "span_id": "noise", "section_id": "other"}
    spans = [noise, definition, anchor]
    if variant == "gap":
        anchor.update(start_offset="12", end_offset="22")
    elif variant == "revision":
        definition["revision"] = "different"
    elif variant == "ambiguous":
        spans.insert(1, {**definition, "span_id": "duplicate"})
    flow = {
        "steps": [{"id": "s", "kind": "approach", "summary": "Method",
                   "evidence": [{"span_id": "anchor"}]}], "edges": [],
    }
    ordered, _, _ = short_knowledge_inputs(flow, spans)
    assert ordered[0] is anchor
    assert ordered[1] is (definition if variant == "exact" else noise)
    assert definition["text"] == "Definition."


def test_generation_contract_selects_sources_before_claims_without_changing_stored_fields():
    from paper_review.domain.understanding import ReasoningFlowDraft

    knowledge = request_schema(KnowledgeBatch, bind_evidence=True,
                               knowledge_edges={"E0": ("a", "b")})
    item = knowledge["$defs"]["KnowledgeItem"]
    fields = list(item["properties"])
    assert fields.index("edge_id") < fields.index("evidence") < fields.index("short_explanation")
    assert fields.index("evidence") < fields.index("why_needed")
    assert set(item["required"]) == set(item["properties"])
    assert "quote" not in knowledge["$defs"]["Evidence"]["properties"]
    reasoning = request_schema(ReasoningFlowDraft, bind_evidence=True)
    fields = list(reasoning["$defs"]["ReasoningStepDraft"]["properties"])
    assert fields.index("evidence") < fields.index("summary")
    assert fields.index("reader_explanation_evidence") < fields.index("reader_explanation")
    # Old saved JSON with full quotes is still decoded by the same stored model.
    old = {"items": [{"id": "k", "concept": "操作", "from_step_id": "a", "to_step_id": "b",
                      "necessity_score": 3, "why_needed": "必要", "short_explanation": "説明",
                      "evidence": [{"span_id": "s", "quote": "Original."}]}]}
    assert KnowledgeBatch.model_validate(old).model_dump() == old
