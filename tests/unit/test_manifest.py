"""Unit tests for evaluation paper manifest."""

from paper_review.domain.manifest import PageLimitCheck, load_manifest
from paper_review.domain.models import MAX_PAPER_PAGES


def test_manifest_loads_all_10_papers():
    manifest = load_manifest()
    assert len(manifest.papers) == 10
    assert manifest.target_domain.startswith("Computer Science")


def test_manifest_minimum_required_fields():
    manifest = load_manifest()
    expected_ids = {
        "1706.03762",
        "2005.14165",
        "2005.11401",
        "2106.09685",
        "2203.02155",
        "2201.11903",
        "2210.03629",
        "2305.18290",
        "2305.14314",
        "2310.11511",
    }
    actual_ids = {p.arxiv_id for p in manifest.papers}
    assert actual_ids == expected_ids

    for paper in manifest.papers:
        assert paper.title, f"title missing for {paper.arxiv_id}"
        assert paper.arxiv_id, f"arxiv_id missing for {paper.title}"
        assert paper.expected_version.startswith("v"), (
            f"expected_version missing for {paper.arxiv_id}"
        )
        assert paper.category.startswith("cs."), f"category missing for {paper.arxiv_id}"
        assert paper.reason_for_selection, f"reason_for_selection missing for {paper.arxiv_id}"
        assert paper.paper_id == f"arxiv:{paper.arxiv_id}"


def test_adr0001_d3_page_limit_alignment():
    """Verify that ADR-0001 D3 (page limit check) is explicitly tracked."""
    manifest = load_manifest()
    compliant = manifest.compliant_papers
    pending = manifest.pending_decision_papers

    # The 75-page policy includes GPT-3 and InstructGPT without removing appendices.
    assert manifest.page_limit_rule["max_pages"] == MAX_PAPER_PAGES == 75
    assert len(compliant) == 10
    assert pending == []

    for p in compliant:
        assert p.page_limit_check.status == "within_limit"
        assert p.page_limit_check.d3_assessment == "compliant"
        assert p.page_limit_check.limit_pages == MAX_PAPER_PAGES
        assert p.page_limit_check.expected_page_count <= MAX_PAPER_PAGES


def test_historical_50_page_manifest_checks_remain_readable():
    check = PageLimitCheck.model_validate({
        "expected_page_count": 75, "main_text_page_count": 40,
        "appendix_page_count": 35, "limit_pages": 50, "exceeds_50_pages": True,
        "status": "exceeds_limit", "d3_assessment": "pending_decision",
        "notes": "Historical 50-page assessment",
    })
    assert check.limit_pages == 50
    assert check.exceeds_limit is True


def test_initial_study_targets():
    """Verify that the 3 initial study papers from thesis-choosing.md are marked."""
    manifest = load_manifest()
    initials = manifest.initial_targets
    assert len(initials) == 3
    initial_ids = {p.arxiv_id for p in initials}
    assert initial_ids == {"1706.03762", "2106.09685", "2210.03629"}


def test_observed_versions_and_missing_page_partitions():
    manifest = load_manifest()
    expected = {"2005.14165": ("v4", 75), "2203.02155": ("v1", 68),
                "2201.11903": ("v6", 43), "2305.18290": ("v2", 27),
                "2310.11511": ("v1", 30)}
    for aid, (version, pages) in expected.items():
        paper = manifest.get_by_arxiv_id(aid)
        assert paper is not None
        assert paper.expected_version == version
        assert paper.canonical_url.endswith(aid + version)
        assert paper.page_limit_check.expected_page_count == pages
        # Total pages are observed; a disjoint main/appendix count is not inferred.
        assert paper.page_limit_check.main_text_page_count is None
        assert paper.page_limit_check.appendix_page_count is None
        assert "SHA-256" in paper.page_limit_check.notes
