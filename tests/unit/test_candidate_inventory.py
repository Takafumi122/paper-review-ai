"""Offline identifier-first inventory and provenance regression tests."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from paper_review.domain.candidate_inventory import (
    extract_reference_ids,
    lookup_keys,
    resolve_candidate_inventory,
)
from paper_review.domain.candidate_retrieval import evaluate_content_eligibility
from paper_review.domain.models import Paper, Reference, SourceSpan
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.infrastructure.parsing.pdf_parser import extract_references


def ref(text: str, n: int = 1, **kw) -> Reference:
    return Reference(
        id=f"r{n}", paper_id="target", revision_id="v1", ref_order=n, raw_text=text, **kw
    )


def paper(pid="a" * 40, title="Canonical Method", **kw):
    return dict(
        paperId=pid,
        title=title,
        authors=[{"name": "Alice Author"}],
        year=2020,
        externalIds={"ArXiv": "2001.00001", "DOI": "10.1234/example"},
        **kw,
    )


def run(refs, handler, **kw):
    async def resolve():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with SemanticScholarClient(
                client=http, rate_limiter=RateLimiter(0), max_attempts=1
            ) as client:
                return await resolve_candidate_inventory(
                    Paper(id="target", title="Target", published_at="2022-06-01"),
                    refs,
                    client,
                    **kw,
                )

    return asyncio.run(resolve())


def test_hanging_layout_separates_entries_not_long_authors():
    text = (
        "Alice Author, Bob Builder, Carol Contributor,\n"
        "Daniel Designer, Evelyn Expert, and Fiona Fellow.\n"
        "A reliable method. 2020.\n"
        "George Graduate and Helen Helper. Another method.\n"
        "Proceedings of Research, 2021."
    )
    entries = extract_references(
        text,
        [SourceSpan(page=3, start_offset=0, end_offset=len(text))],
        line_indents=[72, 82, 82, 72, 82],
    )
    assert len(entries) == 2
    assert "Fiona Fellow" in entries[0].raw_text
    assert entries[1].raw_text.startswith("George Graduate")
    assert all(e.page == 3 for e in entries)


def test_text_only_long_author_list_does_not_split_on_length():
    text = (
        "Alice Author, Bob Builder, Carol Contributor,\n"
        "Daniel Designer, Evelyn Expert, and Fiona Fellow. A method. 2020.\n"
        "George Graduate and Helen Helper. Another method. 2021."
    )
    entries = extract_references(text, [])
    assert len(entries) == 2
    assert "Daniel Designer" in entries[0].raw_text


def test_numbered_two_entries_and_repeated_line_page_provenance():
    text = "[1] Alice Author. Method. 2020.\n[2] Alice Author. Method. 2020."
    entries = extract_references(
        text,
        [
            SourceSpan(page=1, start_offset=0, end_offset=30),
            SourceSpan(page=2, start_offset=30, end_offset=len(text)),
        ],
    )
    assert len(entries) == 2
    assert entries[1].page == 2


@pytest.mark.parametrize(
    "text,expected",
    [
        ("arXiv:2001.00001v2", "2001.00001"),
        ("https://arxiv.org/pdf/2001.00001v3.pdf", "2001.00001"),
        ("CoRR, abs/2001. 00001, 2020.", "2001.00001"),
        ("arXiv:cs/0101001", "cs/0101001"),
    ],
)
def test_arxiv_extraction(text, expected):
    assert extract_reference_ids(ref(text))["arxiv_id"] == [expected]


def test_doi_and_known_semantic_scholar_id():
    ids = extract_reference_ids(ref("https://doi.org/10.1234/EXAMPLE.", matched_paper_id="a" * 40))
    assert ids["doi"] == ["10.1234/example"]
    assert ids["semantic_scholar_id"] == ["a" * 40]


def test_exact_identifier_resolution_dedup_and_provenance():
    calls = []

    def handle(request):
        import json

        ids = json.loads(request.content)["ids"]
        calls.append(ids)
        assert request.method == "POST"
        return httpx.Response(200, json=[paper() for _ in ids])

    result = run(
        [
            ref("A. Garbledfusedtitle. arXiv:2001.00001v2"),
            ref("Alice Author. Another spelling. doi:10.1234/example.", 2),
        ],
        handle,
    )
    assert calls == [["ARXIV:2001.00001", "DOI:10.1234/example"]]
    assert result.total_count == 1
    candidate = result.candidates[0]
    assert candidate.paper_id == "a" * 40
    assert candidate.external_ids.semantic_scholar == "a" * 40
    assert candidate.title == "Canonical Method"
    assert [r.id for r in candidate.source_references] == ["r1", "r2"]
    assert [r.ref_order for r in candidate.source_references] == [1, 2]
    assert candidate.source_references[0].raw_text.startswith("A. Garbled")
    assert candidate.date_provenance is not None
    assert candidate.date_provenance.precision == "year"
    assert candidate.publication_date is None
    assert candidate.date_provenance.value == "2020"
    assert candidate.eligibility_status == "eligible"
    assert [r.method for r in result.reference_resolutions] == ["arxiv_id", "doi"]
    assert candidate.metadata_provenance["bibliographic_metadata"].endswith("a" * 40)
    ranking = rank_stage1_candidates(Paper(id="target"), result)
    assert ranking.ranked_candidates[0].score.semantic_similarity is None


def test_identifier_not_found_does_not_fall_back_to_title():
    calls = []

    def handle(request):
        calls.append(str(request.url))
        return httpx.Response(200, json=[None])

    result = run([ref("Alice Author. Canonical Method. 2020. arXiv:2001.00001")], handle)
    assert len(calls) == 1
    assert result.reference_resolutions[0].reason == "identifier_not_found"
    assert result.candidates[0].resolution_status == "unresolved"
    assert result.candidates[0].eligibility_status == "uncertain"
    assert not rank_stage1_candidates(Paper(id="t"), result).ranked_candidates


def test_multiple_ids_in_unsplit_reference_remain_uncertain():
    def handle(request):
        pytest.fail("Ambiguous merged reference must not make an arbitrary ID lookup")

    result = run([ref("A. First. arXiv:2001.00001. B. Second. arXiv:2001.00002.")], handle)
    assert (
        result.reference_resolutions[0].reason == "multiple_identifiers_possible_merged_reference"
    )


def test_ambiguous_title_is_not_accepted():
    def handle(request):
        return httpx.Response(200, json={"total": 2, "data": [paper(), paper(pid="b" * 40)]})

    result = run([ref("Alice Author. Canonical Method. 2020.")], handle)
    assert result.reference_resolutions[0].reason == "ambiguous_title"


def test_title_alone_without_author_corroboration_is_not_accepted():
    result = run(
        [ref("Bob Builder. Canonical Method. 2020.")],
        lambda _: httpx.Response(200, json={"total": 1, "data": [paper()]}),
    )
    assert result.reference_resolutions[0].status == "unresolved"


def test_unique_corroborated_title_is_accepted():
    result = run(
        [ref("Alice Author. Canonical Method. 2020.")],
        lambda _: httpx.Response(200, json={"total": 1, "data": [paper()]}),
    )
    assert result.reference_resolutions[0].method == "exact_title"
    assert result.candidates[0].title == "Canonical Method"


def test_conflicting_identifiers_are_not_unified():
    result = run(
        [ref("arXiv:2001.00001 doi:10.1234/example")],
        lambda _: httpx.Response(200, json=[paper(), paper(pid="b" * 40)]),
    )
    assert result.reference_resolutions[0].reason == "identifier_conflict"


def test_same_year_remains_uncertain():
    async def resolve():
        async with SemanticScholarClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[paper()])),
            rate_limiter=RateLimiter(0),
        ) as client:
            return await resolve_candidate_inventory(
                Paper(id="t", published_at="2020-06-01"), [ref("arXiv:2001.00001")], client
            )

    assert asyncio.run(resolve()).candidates[0].eligibility_status == "uncertain"


def test_richer_dataset_metadata_flags_uncertainty_not_method_exclusion():
    assert evaluate_content_eligibility(
        "Resource X", "We introduce a new dataset for evaluation.", "eligible", ["A"]
    ) == ("uncertain", "possible_dataset_benchmark")
    assert evaluate_content_eligibility(
        "Method X",
        "This paper presents a new model. We evaluate it on a dataset.",
        "eligible",
        ["A"],
    ) == ("eligible", None)


def test_experiments_on_dataset_does_not_imply_dataset_paper():
    assert evaluate_content_eligibility(
        "General method",
        "We present both experiments on the example dataset to illustrate this.",
        "eligible",
        ["Author"],
    ) == ("eligible", None)


def test_challenge_and_environment_are_conservative_uncertain_types():
    for title, abstract in [
        ("A new challenge", "It provides a common benchmark for generation."),
        (
            "A new environment",
            "Existing benchmarks are limited. We develop a simulated environment.",
        ),
    ]:
        assert evaluate_content_eligibility(title, abstract, "eligible", ["Author"]) == (
            "uncertain",
            "possible_dataset_benchmark",
        )


def test_distinct_ids_do_not_merge_on_similar_titles():
    import json

    def handle(request):
        ids = json.loads(request.content)["ids"]
        payload = []
        for i, key in enumerate(ids):
            obj = paper(pid=("a" if i == 0 else "b") * 40)
            obj["externalIds"] = {"ArXiv": key.removeprefix("ARXIV:")}
            payload.append(obj)
        return httpx.Response(200, json=payload)

    result = run([ref("arXiv:2001.00001"), ref("arXiv:2001.00002", 2)], handle)
    assert result.total_count == 2
    assert all(c.resolution_status == "resolved" for c in result.candidates)


def test_shared_stable_id_with_conflicting_s2_ids_remains_ambiguous():
    def handle(request):
        return httpx.Response(200, json=[paper(), paper(pid="b" * 40)])

    result = run([ref("arXiv:2001.00001"), ref("doi:10.1234/example", 2)], handle)
    assert result.total_count == 2
    assert all(c.resolution_status == "ambiguous" for c in result.candidates)
    assert all(c.eligibility_status == "uncertain" for c in result.candidates)


def test_truncated_search_cannot_prove_unique_title():
    result = run(
        [ref("Alice Author. Canonical Method. 2020.")],
        lambda _: httpx.Response(200, json={"total": 11, "next": 10, "data": [paper()]}),
    )
    assert result.reference_resolutions[0].status == "unresolved"


def test_rate_limit_stops_further_title_network_requests():
    calls = []

    def handle(request):
        calls.append(request)
        return httpx.Response(429, json={"message": "Too many requests"})

    result = run(
        [ref("Alice Author. One title. 2020."), ref("Bob Builder. Another title. 2020.", 2)], handle
    )
    assert len(calls) == 1
    assert result.retrieval_warnings == ["title_lookup:SemanticScholarRateLimitError"]
    assert all(r.status == "unresolved" for r in result.reference_resolutions)


def test_batch_null_is_not_turned_into_unavailable_after_later_search_limit():
    def handle(request):
        return (
            httpx.Response(200, json=[None])
            if request.method == "POST"
            else httpx.Response(429, json={"message": "Too many requests"})
        )

    result = run([ref("Alice Author. A title. 2020."), ref("arXiv:2001.00001", 2)], handle)
    assert result.reference_resolutions[1].reason == "identifier_not_found"


def test_malformed_batch_payload_is_explicit_failure():
    result = run([ref("arXiv:2001.00001")], lambda _: httpx.Response(200, text="broken json"))
    assert result.reference_resolutions[0].status == "unresolved"
    assert result.retrieval_warnings == ["identifier_lookup:SemanticScholarParseError"]


def test_known_s2_id_precedes_arxiv_and_doi():
    reference = ref("arXiv:2001.00001 doi:10.1234/example", matched_paper_id="a" * 40)
    assert lookup_keys(extract_reference_ids(reference)) == [
        ("semantic_scholar_id", "a" * 40),
        ("arxiv_id", "ARXIV:2001.00001"),
        ("doi", "DOI:10.1234/example"),
    ]


def test_known_s2_identity_is_used_as_resolution_method():
    reference = ref("arXiv:2001.00001 doi:10.1234/example", matched_paper_id="a" * 40)

    def handler(request):
        assert request.url.path.endswith("/paper/batch")
        return httpx.Response(200, json=[paper(), paper(), paper()])

    inventory = run([reference], handler)
    assert inventory.reference_resolutions[0].method == "semantic_scholar_id"
    assert inventory.candidates[0].resolution_method == "semantic_scholar_id"
    assert inventory.candidates[0].paper_id == "a" * 40
