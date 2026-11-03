"""Unit tests for evaluation paper manifest."""

from paper_review.domain.manifest import load_manifest


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

    # 8 papers are within 50 pages; 2 papers exceed 50 pages (GPT-3: 75p, InstructGPT: 68p).
    assert len(compliant) == 8
    assert len(pending) == 2

    pending_ids = {p.arxiv_id for p in pending}
    assert pending_ids == {"2005.14165", "2203.02155"}

    for p in compliant:
        assert p.page_limit_check.status == "within_limit"
        assert p.page_limit_check.d3_assessment == "compliant"
        assert p.page_limit_check.expected_page_count <= 50

    for p in pending:
        assert p.page_limit_check.status == "exceeds_limit"
        assert p.page_limit_check.d3_assessment == "pending_decision"
        assert p.page_limit_check.expected_page_count > 50
        assert p.page_limit_check.main_text_page_count <= 50  # main text itself is <= 50
        assert "ADR-0001 D3" in p.page_limit_check.notes


def test_initial_study_targets():
    """Verify that the 3 initial study papers from thesis-choosing.md are marked."""
    manifest = load_manifest()
    initials = manifest.initial_targets
    assert len(initials) == 3
    initial_ids = {p.arxiv_id for p in initials}
    assert initial_ids == {"1706.03762", "2106.09685", "2210.03629"}
