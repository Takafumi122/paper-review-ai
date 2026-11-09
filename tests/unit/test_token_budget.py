"""Local tokenizer data is hash checked and usable with network forbidden."""

import pytest

from paper_review.application.full_analysis import fit_context, reasoning_windows
from paper_review.llm import token_budget


def test_bundled_encoder_handles_unicode_and_special_looking_source_text():
    text = "Attentionと逐次依存 <|endoftext|>"
    encoder = token_budget.encoding()
    assert encoder.decode(encoder.encode(text, disallowed_special=())) == text
    assert len(encoder.encode("hello world")) == 2


def test_context_fits_token_budget_instead_of_treating_bytes_as_tokens():
    spans = [
        {
            "span_id": str(i),
            "section_id": str(i),
            "text": "The recurrent state depends on the previous time step. " * 8,
        }
        for i in range(10)
    ]
    context = fit_context({"type": "object"}, "Use the sources.", spans, 2000)
    assert len(context) >= 4
    assert sum(len(s["text"].encode()) for s in context) > 2000
    assert (
        token_budget.estimate_input_tokens({"type": "object"}, context, "Use the sources.") <= 2000
    )


def test_reasoning_context_packs_contiguous_originals_without_crossing_a_source_boundary():
    def span(sid, offset, *, revision="v2", section="experiment"):
        return {"span_id": sid, "paper_id": "paper", "revision": revision,
                "section_id": section, "start_offset": str(offset),
                "end_offset": str(offset + 4), "text": "word"}

    setup, model, result = span("setup", 0), span("model", 4), span("result", 8)
    unrelated = span("unrelated", 12, section="discussion")
    old = span("old", 12, revision="v1")
    gap = span("gap", 17)
    spans = [setup, model, result, unrelated, old, gap]
    assert reasoning_windows(spans) == [(setup, model, result), (unrelated,), (old,), (gap,)]
    schema, instructions = {"type": "object"}, "Select exact original evidence."
    limit = token_budget.estimate_input_tokens(schema, (setup, model), instructions)
    selected = fit_context(schema, instructions, spans, limit, contiguous_windows=True)
    assert selected and all(s in (unrelated, old, gap) for s in selected)
    limit = token_budget.estimate_input_tokens(schema, (setup, model, result), instructions)
    assert fit_context(schema, instructions, spans, limit, contiguous_windows=True) == (
        setup, model, result,
    )


def test_reasoning_keeps_late_main_sections_before_appendix_and_spreads_long_sections():
    def span(sid, title, offset=0):
        return {"span_id": sid, "paper_id": "p", "revision": "v1", "section": title,
                "section_id": title, "start_offset": str(offset), "end_offset": str(offset + 4),
                "text": "word"}

    method = [span(f"method{i}", "2 Method", i * 4) for i in range(18)]
    results = [span(f"benchmark{i}", f"3.{i + 1} Benchmark") for i in range(8)]
    validity = span("validity", "4 Measurement validity")
    conclusion = span("conclusion", "5 Conclusion")
    appendix = span("appendix", "A.1 Additional results")
    original = [appendix, *method, *results, validity, conclusion]
    windows = reasoning_windows(original)
    assert windows[-1] == (appendix,)
    first = [window[0]["span_id"] for window in windows[:11]]
    assert "validity" in first and "conclusion" in first
    method_windows = [window for window in windows if window[0]["section_id"] == "2 Method"]
    assert [window[0]["span_id"] for window in method_windows[:3]] == [
        "method0", "method9", "method15",
    ]
    assert sorted(s["span_id"] for window in windows for s in window) == sorted(
        s["span_id"] for s in original
    )


def test_encoder_rejects_corrupt_or_changed_asset_hash(monkeypatch):
    token_budget.encoding.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(token_budget, "ASSET_SHA256", "invalid")
        with pytest.raises(RuntimeError, match="tokenizer_asset_hash_mismatch"):
            token_budget.encoding()
    token_budget.encoding.cache_clear()
