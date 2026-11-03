"""Navigation must preserve exact source/page provenance and selected quote limits."""

from paper_review.application.full_analysis import source_spans
from paper_review.domain.models import PaperSection
from paper_review.domain.understanding import ReasoningFlow
from paper_review.llm.gateway import bind_source_evidence, prepare_source_request, request_schema


def test_known_page_separator_is_preserved_without_inventing_a_page():
    section = PaperSection(
        id="s", revision_id="r", paper_id="p", section_index=0, title="Method",
        text="before\nafter", page_start=1, page_end=2,
        span_info={"source_spans": [
            {"start_offset": 0, "end_offset": 6, "page": 1},
            {"start_offset": 7, "end_offset": 12, "page": 2},
        ]},
    )
    spans = source_spans([section])
    assert "".join(s["text"] for s in spans) == section.text
    assert [s["page"] for s in spans] == ["1", "unknown", "2"]
    assert spans[1]["text"] == "\n" and spans[1]["source_kind"] == "stored_separator"
    assert spans[1]["start_offset"] == "6" and spans[1]["end_offset"] == "7"


def test_missing_prose_between_pages_is_never_treated_as_a_separator():
    section = PaperSection(
        id="s", revision_id="r", paper_id="p", section_index=0, title="Method",
        text="beforeMISSINGafter", page_start=1, page_end=2,
        span_info={"source_spans": [
            {"start_offset": 0, "end_offset": 6, "page": 1},
            {"start_offset": 13, "end_offset": 18, "page": 2},
        ]},
    )
    spans = source_spans([section])
    assert len(spans) == 2
    assert not any(s.get("source_kind") == "stored_separator" for s in spans)


def test_continuation_navigation_survives_alias_reordering_without_expanding_quotes():
    # Put the comparison partner after a split, as in the observed live failure.
    text = "x" * 375 + "We compare quality with prior models."
    section = PaperSection(
        id="section",
        revision_id="rev",
        paper_id="paper",
        section_index=0,
        title="Results",
        text=text,
        page_start=8,
        page_end=8,
    )
    spans = source_spans([section])
    assert len(spans) == 2
    assert "prior models" not in spans[0]["text"]
    assert spans[0]["text"] + spans[1]["text"] == text
    schema, context, refs = prepare_source_request(
        request_schema(ReasoningFlow, bind_evidence=True), tuple(reversed(spans))
    )
    assert context[1]["next_span_id"] == "S0"
    assert context[0]["previous_span_id"] == "S1"
    assert schema["$defs"]["ReasoningStep"]["properties"]["evidence"]["maxItems"] == 4
    selected = bind_source_evidence(
        {"evidence": [{"span_id": "S1"}]},
        {alias: ref[1] for alias, ref in refs.items()},
        original_ids={alias: ref[0] for alias, ref in refs.items()},
    )
    assert selected == {"evidence": [{"span_id": spans[0]["span_id"], "quote": spans[0]["text"]}]}
    assert context[0]["page"] == context[1]["page"] == "8"
    assert context[0]["text"] == spans[1]["text"]
    assert spans[0]["start_offset"] == "0" and spans[0]["end_offset"] == "400"


def test_navigation_never_points_to_omitted_or_unrelated_sources():
    section = PaperSection(
        id="section",
        revision_id="rev",
        paper_id="paper",
        section_index=0,
        title="Results",
        text="x" * 800,
        page_start=1,
        page_end=1,
    )
    a, b = source_spans([section])
    schema = request_schema(ReasoningFlow, bind_evidence=True)
    for other in [
        None,
        {**b, "paper_id": "other"},
        {**b, "revision": "other"},
        {**b, "section_id": "other"},
        {**b, "start_offset": "401"},
        {**b, "start_offset": "unknown"},
        {k: v for k, v in b.items() if k not in {"start_offset", "end_offset"}},
    ]:
        sources = (a, other) if other else (a,)
        _, context, _ = prepare_source_request(schema, sources)
        assert all("next_span_id" not in s and "previous_span_id" not in s for s in context)


def test_page_boundary_offsets_preserve_original_page_identity():
    section = PaperSection(
        id="section",
        revision_id="rev",
        paper_id="paper",
        section_index=0,
        title="Results",
        text="beforeafter",
        page_start=1,
        page_end=2,
        span_info={
            "source_spans": [
                {"start_offset": 0, "end_offset": 6, "page": 1},
                {"start_offset": 6, "end_offset": 11, "page": 2},
            ]
        },
    )
    spans = source_spans([section])
    _, context, _ = prepare_source_request(
        request_schema(ReasoningFlow, bind_evidence=True), tuple(spans)
    )
    assert context[0]["next_span_id"] == "S1"
    assert [s["page"] for s in context] == ["1", "2"]
    assert [s["text"] for s in context] == ["before", "after"]
    assert [s["start_offset"] for s in context] == ["0", "6"]


def test_ambiguous_boundaries_and_stale_links_are_not_navigation_evidence():
    common = {"paper_id": "p", "revision": "r", "section_id": "s"}
    spans = (
        {
            **common,
            "span_id": "a",
            "text": "abcd",
            "start_offset": "0",
            "end_offset": "4",
            "next_span_id": "outside-request",
        },
        {**common, "span_id": "overlap", "text": "bcd", "start_offset": "1", "end_offset": "4"},
        {**common, "span_id": "b", "text": "ef", "start_offset": "4", "end_offset": "6"},
    )
    _, context, _ = prepare_source_request(request_schema(ReasoningFlow, bind_evidence=True), spans)
    assert all("previous_span_id" not in s and "next_span_id" not in s for s in context)
