"""Unit tests for arXiv ID and URL normalization."""

import pytest

from paper_review.domain.jobs import (
    arxiv_identity,
    normalize_arxiv_id,
)


@pytest.mark.parametrize(
    (
        "input_val",
        "expected_paper_id",
        "expected_arxiv_id",
        "expected_version",
        "expected_revision",
    ),
    [
        ("1706.03762", "arxiv:1706.03762", "1706.03762", None, "1706.03762"),
        ("1706.03762v7", "arxiv:1706.03762", "1706.03762", "v7", "1706.03762v7"),
        ("arxiv:1706.03762", "arxiv:1706.03762", "1706.03762", None, "1706.03762"),
        ("arXiv:1706.03762v1", "arxiv:1706.03762", "1706.03762", "v1", "1706.03762v1"),
        (
            "https://arxiv.org/abs/1706.03762",
            "arxiv:1706.03762",
            "1706.03762",
            None,
            "1706.03762",
        ),
        (
            "https://arxiv.org/abs/1706.03762v7",
            "arxiv:1706.03762",
            "1706.03762",
            "v7",
            "1706.03762v7",
        ),
        (
            "https://arxiv.org/pdf/1706.03762",
            "arxiv:1706.03762",
            "1706.03762",
            None,
            "1706.03762",
        ),
        (
            "https://arxiv.org/pdf/1706.03762.pdf",
            "arxiv:1706.03762",
            "1706.03762",
            None,
            "1706.03762",
        ),
        (
            "https://arxiv.org/pdf/1706.03762v7.pdf",
            "arxiv:1706.03762",
            "1706.03762",
            "v7",
            "1706.03762v7",
        ),
        (
            "http://export.arxiv.org/abs/1706.03762",
            "arxiv:1706.03762",
            "1706.03762",
            None,
            "1706.03762",
        ),
        # Legacy arXiv identifiers (pre-2007 format)
        ("cs/0101001", "arxiv:cs/0101001", "cs/0101001", None, "cs/0101001"),
        (
            "math.PR/0501001v2",
            "arxiv:math.PR/0501001",
            "math.PR/0501001",
            "v2",
            "math.PR/0501001v2",
        ),
        (
            "https://arxiv.org/abs/cs/0101001v1",
            "arxiv:cs/0101001",
            "cs/0101001",
            "v1",
            "cs/0101001v1",
        ),
    ],
)
def test_normalize_arxiv_id_equivalence(
    input_val, expected_paper_id, expected_arxiv_id, expected_version, expected_revision
):
    parsed = normalize_arxiv_id(input_val)
    assert parsed.paper_id == expected_paper_id
    assert parsed.arxiv_id == expected_arxiv_id
    assert parsed.version == expected_version
    assert parsed.raw_revision == expected_revision
    assert parsed.canonical_abs_url == f"https://arxiv.org/abs/{expected_revision}"
    assert parsed.canonical_pdf_url == f"https://arxiv.org/pdf/{expected_revision}.pdf"


def test_arxiv_identity_compatibility():
    """Verify that existing arxiv_identity function still works as expected by Phase 0."""
    paper_id, revision = arxiv_identity("https://arxiv.org/abs/1706.03762v7")
    assert paper_id == "arxiv:1706.03762"
    assert revision == "1706.03762v7"


@pytest.mark.parametrize(
    "invalid_input",
    [
        "",
        "   ",
        "1706.03762 with spaces",
        "file:///tmp/paper.pdf",
        "https://evil.org/abs/1706.03762",
        "https://arxiv.org.attacker.com/abs/1706.03762",
        "https://arxiv.org/abs/nope",
        "https://arxiv.org/abs/1706.03762?query=1",
        "https://arxiv.org/abs/1706.03762#frag",
        "invalid_non_numeric_id",
        12345,  # non-string
    ],
)
def test_normalize_arxiv_id_rejects_invalid(invalid_input):
    with pytest.raises(ValueError):
        normalize_arxiv_id(invalid_input)  # type: ignore[arg-type]
