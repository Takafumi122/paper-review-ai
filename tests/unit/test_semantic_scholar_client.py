"""Unit tests for SemanticScholarClient using HTTPX MockTransport."""

from __future__ import annotations

import httpx
import pytest

from paper_review.domain.models import ExternalIds
from paper_review.infrastructure.academic_api.arxiv import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SemanticScholarClient,
    SemanticScholarHttpError,
    SemanticScholarNotFoundError,
    SemanticScholarParseError,
    SemanticScholarRateLimitError,
)

SAMPLE_PAPER_JSON = {
    "paperId": "204e3073870fae3d05bcbc2f6a8e263d9b72e776",
    "title": "Attention is All you Need",
    "abstract": "The dominant sequence transduction models are based on complex recurrent...",
    "year": 2017,
    "publicationDate": "2017-06-12",
    "citationCount": 190000,
    "influentialCitationCount": 20000,
    "referenceCount": 41,
    "fieldsOfStudy": ["Computer Science"],
    "publicationVenue": {"name": "Neural Information Processing Systems"},
    "authors": [
        {"authorId": "1", "name": "Ashish Vaswani"},
        {"authorId": "2", "name": "Noam Shazeer"},
    ],
    "externalIds": {
        "ArXiv": "1706.03762",
        "CorpusId": 13756489,
        "DOI": "10.48550/arXiv.1706.03762",
    },
}

SAMPLE_REFERENCES_JSON = {
    "data": [
        {
            "isInfluential": True,
            "contexts": ["We rely on layer normalization (Ba et al., 2016)..."],
            "intents": ["methodology"],
            "citedPaper": {
                "paperId": "ba_layer_norm_123",
                "title": "Layer Normalization",
                "abstract": "Training state-of-the-art, deep neural networks is computationally...",
                "year": 2016,
                "publicationDate": "2016-07-21",
                "citationCount": 8000,
                "influentialCitationCount": 1500,
                "referenceCount": 25,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "arXiv"},
                "authors": [{"authorId": "10", "name": "Jimmy Ba"}],
                "externalIds": {"ArXiv": "1607.06450"},
            },
        },
        {
            "isInfluential": False,
            "contexts": ["Previous models used recurrent architectures (Bahdanau et al., 2014)"],
            "intents": ["background"],
            "citedPaper": {
                "paperId": "bahdanau_2014_456",
                "title": "Neural Machine Translation by Jointly Learning to Align and Translate",
                "abstract": "Neural machine translation is a recently proposed approach...",
                "year": 2014,
                "publicationDate": "2014-09-01",
                "citationCount": 25000,
                "influentialCitationCount": 4000,
                "referenceCount": 30,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "ICLR"},
                "authors": [{"authorId": "20", "name": "Dzmitry Bahdanau"}],
                "externalIds": {"ArXiv": "1409.0473"},
            },
        },
    ]
}


def test_target_identity_resolution():
    """Verify priority order: semantic_scholar > arxiv > doi."""
    # 1. Semantic Scholar ID takes top priority
    ext1 = ExternalIds(
        semantic_scholar="204e3073",
        arxiv="1706.03762",
        doi="10.1234/test",
    )
    assert SemanticScholarClient.resolve_target_id(ext1) == "204e3073"

    # 2. ArXiv ID takes second priority
    ext2 = ExternalIds(arxiv="1706.03762", doi="10.1234/test")
    assert SemanticScholarClient.resolve_target_id(ext2) == "ARXIV:1706.03762"

    # 3. DOI takes third priority
    ext3 = ExternalIds(doi="10.1234/test")
    assert SemanticScholarClient.resolve_target_id(ext3) == "DOI:10.1234/test"

    # 4. Empty external IDs returns None
    ext4 = ExternalIds()
    assert SemanticScholarClient.resolve_target_id(ext4) is None


def test_get_paper_success():
    """Verify paper metadata retrieval and parsing into S2Paper DTO."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_s = str(request.url)
        assert "paper/ARXIV%3A1706.03762" in url_s or "paper/ARXIV:1706.03762" in url_s
        assert "paper-review-ai" in request.headers.get("User-Agent", "")
        return httpx.Response(200, json=SAMPLE_PAPER_JSON)

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    paper = client.get_paper_sync("ARXIV:1706.03762")
    assert paper.paper_id == "204e3073870fae3d05bcbc2f6a8e263d9b72e776"
    assert paper.title == "Attention is All you Need"
    assert paper.year == 2017
    assert paper.publication_date == "2017-06-12"
    assert paper.citation_count == 190000
    assert paper.influential_citation_count == 20000
    assert paper.reference_count == 41
    assert paper.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert paper.venue == "Neural Information Processing Systems"
    assert paper.external_ids.semantic_scholar == paper.paper_id
    assert SemanticScholarClient.resolve_target_id(paper.external_ids) == paper.paper_id
    assert paper.external_ids.arxiv == "1706.03762"
    assert paper.external_ids.doi == "10.48550/arXiv.1706.03762"


def test_get_paper_not_found_404():
    """Verify 404 raises SemanticScholarNotFoundError."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Paper not found"})

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    with pytest.raises(SemanticScholarNotFoundError) as exc_info:
        client.get_paper_sync("ARXIV:9999.99999")
    assert exc_info.value.status_code == 404


def test_get_references_success():
    """Verify references retrieval, influence extraction, contexts, and DTO conversion."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        assert "references" in str(request.url)
        return httpx.Response(200, json=SAMPLE_REFERENCES_JSON)

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    refs = client.get_references_sync("204e3073870fae3d05bcbc2f6a8e263d9b72e776")
    assert len(refs) == 2

    ref1 = refs[0]
    assert ref1.is_influential is True
    assert len(ref1.contexts) == 1
    assert "layer normalization" in ref1.contexts[0]
    assert ref1.intents == ["methodology"]
    assert ref1.cited_paper.paper_id == "ba_layer_norm_123"
    assert ref1.cited_paper.title == "Layer Normalization"
    assert ref1.cited_paper.year == 2016
    assert ref1.cited_paper.publication_date == "2016-07-21"
    assert ref1.cited_paper.authors == ["Jimmy Ba"]
    assert ref1.cited_paper.external_ids.arxiv == "1607.06450"

    ref2 = refs[1]
    assert ref2.is_influential is False
    assert ref2.intents == ["background"]
    assert "Neural Machine Translation" in ref2.cited_paper.title


def test_rate_limit_stops_without_automatic_retry():
    """A 429 stops immediately while preserving Retry-After diagnostics."""
    attempts = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json=SAMPLE_PAPER_JSON)

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
        backoff_factor=0.01,
    )

    with pytest.raises(SemanticScholarRateLimitError) as caught:
        client.get_paper_sync("ARXIV:1706.03762")
    assert caught.value.retry_after == 0.01
    assert attempts == 1


def test_server_error_exhaustion():
    """Verify 500 error retries up to max_attempts then raises SemanticScholarHttpError."""
    attempts = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(500, text="Internal Server Error")

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
        max_attempts=2,
        backoff_factor=0.01,
    )

    with pytest.raises(SemanticScholarHttpError) as exc_info:
        client.get_paper_sync("ARXIV:1706.03762")
    assert attempts == 2
    assert exc_info.value.status_code == 500


def test_malformed_json_handling():
    """Verify invalid JSON raises SemanticScholarParseError."""

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>Invalid JSON</html>")

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    with pytest.raises(SemanticScholarParseError):
        client.get_paper_sync("ARXIV:1706.03762")
