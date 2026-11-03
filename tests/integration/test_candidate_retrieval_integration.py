"""Integration test for end-to-end Semantic Scholar candidate retrieval and filtering."""

from __future__ import annotations

import httpx
import pytest

from paper_review.domain.candidate_retrieval import (
    TargetIdentityResolutionError,
    retrieve_and_filter_candidates,
)
from paper_review.domain.models import ExternalIds, Paper, Reference
from paper_review.infrastructure.academic_api.arxiv import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SemanticScholarClient,
)

TARGET_S2_ID = "204e3073870fae3d05bcbc2f6a8e263d9b72e776"

MOCK_REFERENCES_PAYLOAD = {
    "data": [
        # 1. Valid core predecessor (Layer Norm)
        {
            "isInfluential": True,
            "contexts": ["We employ layer normalization (Ba et al., 2016)..."],
            "intents": ["methodology"],
            "citedPaper": {
                "paperId": "ba_123",
                "title": "Layer Normalization",
                "abstract": "Training state-of-the-art deep neural networks...",
                "year": 2016,
                "publicationDate": "2016-07-21",
                "citationCount": 8000,
                "influentialCitationCount": 1500,
                "referenceCount": 25,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "arXiv"},
                "authors": [{"authorId": "1", "name": "Jimmy Ba"}],
                "externalIds": {"ArXiv": "1607.06450"},
            },
        },
        # 2. Valid predecessor (Bahdanau Attention)
        {
            "isInfluential": False,
            "contexts": ["Bahdanau et al. (2014) introduced attention..."],
            "intents": ["background"],
            "citedPaper": {
                "paperId": "bahdanau_456",
                "title": "Neural Machine Translation by Jointly Learning to Align and Translate",
                "abstract": (
                    "We hypothesize that the use of a fixed-length vector is a bottleneck..."
                ),
                "year": 2014,
                "publicationDate": "2014-09-01",
                "citationCount": 25000,
                "influentialCitationCount": 4000,
                "referenceCount": 30,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "ICLR"},
                "authors": [{"authorId": "2", "name": "Dzmitry Bahdanau"}],
                "externalIds": {"ArXiv": "1409.0473"},
            },
        },
        # 3. Survey paper (Excluded)
        {
            "isInfluential": False,
            "contexts": [],
            "intents": ["background"],
            "citedPaper": {
                "paperId": "survey_789",
                "title": "A Comprehensive Survey on Deep Learning for NLP",
                "abstract": "In this survey, we review deep learning advances in NLP...",
                "year": 2016,
                "publicationDate": "2016-01-15",
                "citationCount": 1200,
                "influentialCitationCount": 100,
                "referenceCount": 150,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "ACM Computing Surveys"},
                "authors": [{"authorId": "3", "name": "Survey Author"}],
                "externalIds": {"ArXiv": "1601.00001"},
            },
        },
        # 4. Dataset / Benchmark paper (Excluded)
        {
            "isInfluential": False,
            "contexts": [],
            "intents": [],
            "citedPaper": {
                "paperId": "dataset_101",
                "title": "WMT 2014 English-to-German Dataset and Benchmark",
                "abstract": "This paper describes the translation task dataset...",
                "year": 2014,
                "publicationDate": "2014-06-25",
                "citationCount": 500,
                "influentialCitationCount": 50,
                "referenceCount": 15,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "WMT"},
                "authors": [{"authorId": "4", "name": "Bojar Oleg"}],
                "externalIds": {},
            },
        },
        # 5. Clearly later work (Excluded)
        {
            "isInfluential": False,
            "contexts": [],
            "intents": [],
            "citedPaper": {
                "paperId": "later_202",
                "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                "abstract": "We introduce BERT...",
                "year": 2018,
                "publicationDate": "2018-10-11",
                "citationCount": 70000,
                "influentialCitationCount": 10000,
                "referenceCount": 40,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "NAACL"},
                "authors": [{"authorId": "5", "name": "Jacob Devlin"}],
                "externalIds": {"ArXiv": "1810.04805"},
            },
        },
        # 6. Contemporary work (overlapping year / same date)
        {
            "isInfluential": False,
            "contexts": [],
            "intents": [],
            "citedPaper": {
                "paperId": "contemporary_303",
                "title": "Convolutional Sequence to Sequence Learning",
                "abstract": "We present a fully convolutional architecture...",
                "year": 2017,
                "publicationDate": "2017-05-08",  # Before Target June 12
                "citationCount": 3500,
                "influentialCitationCount": 400,
                "referenceCount": 30,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "ICML"},
                "authors": [{"authorId": "6", "name": "Jonas Gehring"}],
                "externalIds": {"ArXiv": "1705.03122"},
            },
        },
    ]
}


def test_end_to_end_candidate_retrieval_and_filtering():
    """Verify flow: Target Paper -> S2 lookup -> references -> filtering -> CandidateSet."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "references" in url_str:
            return httpx.Response(200, json=MOCK_REFERENCES_PAYLOAD)
        return httpx.Response(404, json={"message": "Not found"})

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    target_paper = Paper(
        id="arxiv:1706.03762",
        title="Attention Is All You Need",
        published_at="2017-06-12",
        published_date_precision="day",
        external_ids=ExternalIds(arxiv="1706.03762"),
    )

    target_refs = [
        Reference(
            id="ref_1",
            revision_id="arxiv:1706.03762:v7",
            paper_id="arxiv:1706.03762",
            ref_order=1,
            raw_text=(
                "[1] Jimmy Ba, Jamie Ryan Kiros, and Geoffrey E Hinton. Layer Normalization. 2016."
            ),
            matched_arxiv_id="1607.06450",
        ),
        Reference(
            id="ref_2",
            revision_id="arxiv:1706.03762:v7",
            paper_id="arxiv:1706.03762",
            ref_order=2,
            raw_text=(
                "[2] Dzmitry Bahdanau, Kyunghyun Cho, Yoshua Bengio. Neural Machine Trans. 2014."
            ),
            matched_arxiv_id="1409.0473",
        ),
    ]

    candidate_set = retrieve_and_filter_candidates(target_paper, target_refs, client)

    # 1. Check CandidateSet aggregates
    assert candidate_set.target_paper_id == "arxiv:1706.03762"
    assert candidate_set.target_s2_id == "ARXIV:1706.03762"
    assert candidate_set.total_count == 6

    # 2. Check eligible predecessors
    eligible = candidate_set.eligible_candidates
    # Both Layer Norm and Bahdanau and Gehring (May 2017 < June 2017) are strictly prior
    eligible_titles = [c.title for c in eligible]
    assert "Layer Normalization" in eligible_titles
    assert any("Neural Machine Translation" in t for t in eligible_titles)
    assert "Convolutional Sequence to Sequence Learning" in eligible_titles

    # Verify Ba has matched target reference info
    ba_cand = next(c for c in eligible if c.title == "Layer Normalization")
    assert ba_cand.target_reference_order == 1
    assert "Layer Normalization" in str(ba_cand.target_reference_raw_text)
    assert ba_cand.is_influential_citation is True
    assert len(ba_cand.citation_contexts) == 1
    assert ba_cand.citation_intents == ["methodology"]
    assert ba_cand.temporal_status == "eligible"
    assert ba_cand.eligibility_status == "eligible"
    assert ba_cand.exclusion_reason is None

    # 3. Check excluded candidates and reasons
    excluded = candidate_set.excluded_candidates
    excluded_reasons = {c.title: c.exclusion_reason for c in excluded}

    assert excluded_reasons["A Comprehensive Survey on Deep Learning for NLP"] == "survey"
    assert (
        excluded_reasons["WMT 2014 English-to-German Dataset and Benchmark"] == "dataset_benchmark"
    )
    assert (
        excluded_reasons["BERT: Pre-training of Deep Bidirectional Transformers"]
        == "published_after_target"
    )


def test_target_resolution_failure_handling():
    """Verify paper with no resolvable external IDs raises TargetIdentityResolutionError."""
    target_paper = Paper(
        id="unknown_paper",
        title="Unknown Paper",
        external_ids=ExternalIds(),  # Empty
    )

    client = SemanticScholarClient(rate_limiter=RateLimiter(min_interval_seconds=0))

    with pytest.raises(TargetIdentityResolutionError) as exc_info:
        retrieve_and_filter_candidates(target_paper, [], client)
    assert "Cannot resolve Semantic Scholar identity" in str(exc_info.value)
