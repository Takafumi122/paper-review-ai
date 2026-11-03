"""Local tokenizer data is hash checked and usable with network forbidden."""

import pytest

from paper_review.application.full_analysis import fit_context
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


def test_encoder_rejects_corrupt_or_changed_asset_hash(monkeypatch):
    token_budget.encoding.cache_clear()
    with monkeypatch.context() as patch:
        patch.setattr(token_budget, "ASSET_SHA256", "invalid")
        with pytest.raises(RuntimeError, match="tokenizer_asset_hash_mismatch"):
            token_budget.encoding()
    token_budget.encoding.cache_clear()
