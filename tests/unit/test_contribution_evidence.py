"""Input scheduling preserves mechanisms and result coverage within the real token cap."""

from paper_review.application.citation_quality_evaluation import load_cases
from paper_review.application.contribution_evidence import CONTRIBUTION_PROMPT, contribution_sources
from paper_review.application.full_analysis import BASE_PROMPT, fit_context
from paper_review.domain.understanding import Contributions
from paper_review.llm.gateway import request_schema


def test_real_source_windows_keep_formula_and_attention_results_without_rewriting():
    expected = {
        "2106.09685": ("sec:6:p4:c400", "sec:13:p8:c0"),
        "1706.03762": ("sec:7:p4:c400", "sec:20:"),
    }
    for case in load_cases():
        if case["aid"] not in expected:
            continue
        original = {s["span_id"]: s for s in case["spans"]}
        prepared = contribution_sources(case["spans"])
        assert len(prepared) == len(original)
        assert all(s == original[s["span_id"]] for s in prepared)
        context = fit_context(
            request_schema(Contributions, bind_evidence=True), BASE_PROMPT + CONTRIBUTION_PROMPT,
            prepared, 8000, preserve_source_order=True,
        )
        for suffix in expected[case["aid"]]:
            assert any(suffix in s["span_id"] for s in context), (case["aid"], suffix)


def test_missing_parent_header_can_inherit_experimental_sibling_role():
    spans = [{"span_id": str(i), "section_id": str(i), "section": title, "text": "original"}
             for i, title in enumerate([
                 "1 Introduction", "2 Method", "3.1 A task", "3.2 Model Variations",
                 "4 Background",
             ])]
    result = contribution_sources(spans)
    assert [s["section"] for s in result[:3]] == ["2 Method", "1 Introduction", "3.1 A task"]
    assert len({s["span_id"] for s in result}) == len(spans)
