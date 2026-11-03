"""Integration test for full Stage 1 flow:

Target Paper -> Candidate Retrieval & Filtering -> Initial Score & Ranking.
"""

from __future__ import annotations

import httpx
import pytest

from paper_review.domain.candidate_retrieval import retrieve_and_filter_candidates
from paper_review.domain.models import ExternalIds, Paper, Reference
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.arxiv import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient

MOCK_S2_REFERENCES = {
    "data": [
        # 1. Jimmy Ba - Layer Normalization (influential methodology predecessor)
        {
            "isInfluential": True,
            "contexts": ["We employ layer normalization (Ba et al., 2016)..."],
            "intents": ["methodology"],
            "citedPaper": {
                "paperId": "ba_123",
                "title": "Layer Normalization",
                "abstract": "Training state-of-the-art deep neural networks is computationally...",
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
        # 2. Dzmitry Bahdanau - Attention Mechanism (background predecessor)
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
        # 3. Jonas Gehring - ConvS2S (concurrent/preceding baseline from May 2017)
        {
            "isInfluential": False,
            "contexts": ["Recently, convolutional sequence to sequence models (Gehring et al.)..."],
            "intents": ["background", "comparison"],
            "citedPaper": {
                "paperId": "gehring_789",
                "title": "Convolutional Sequence to Sequence Learning",
                "abstract": "The prevalent approach to sequence to sequence learning...",
                "year": 2017,
                "publicationDate": "2017-05-08",
                "citationCount": 4000,
                "influentialCitationCount": 600,
                "referenceCount": 40,
                "fieldsOfStudy": ["Computer Science"],
                "publicationVenue": {"name": "ICML"},
                "authors": [{"authorId": "3", "name": "Jonas Gehring"}],
                "externalIds": {"ArXiv": "1705.03122"},
            },
        },
        # 4. Excluded: Survey paper
        {
            "isInfluential": False,
            "contexts": [],
            "intents": [],
            "citedPaper": {
                "paperId": "survey_999",
                "title": "A Comprehensive Survey on Deep Learning for NLP",
                "abstract": "In this survey review, we overview recent progress...",
                "year": 2016,
                "publicationDate": "2016-01-01",
                "citationCount": 500,
                "influentialCitationCount": 10,
                "referenceCount": 150,
                "fieldsOfStudy": ["Computer Science"],
                "authors": [{"authorId": "4", "name": "Reviewer"}],
            },
        },
        # 5. Excluded: Later work (BERT, 2018)
        {
            "isInfluential": False,
            "contexts": [],
            "intents": [],
            "citedPaper": {
                "paperId": "bert_101",
                "title": "BERT: Pre-training of Deep Bidirectional Transformers",
                "abstract": "We introduce a new language representation model...",
                "year": 2018,
                "publicationDate": "2018-10-11",
                "citationCount": 70000,
                "influentialCitationCount": 12000,
                "referenceCount": 45,
                "fieldsOfStudy": ["Computer Science"],
                "authors": [{"authorId": "5", "name": "Jacob Devlin"}],
            },
        },
    ]
}


def test_end_to_end_retrieval_and_ranking_pipeline():
    """Verify entire pipeline:

    Target -> Candidate Retrieval -> CandidateSet -> Stage 1 Scoring -> Stage1RankingResult.
    """

    def handle_request(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "references" in url_str:
            return httpx.Response(200, json=MOCK_S2_REFERENCES)
        return httpx.Response(404, json={"message": "Not found"})

    client = SemanticScholarClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    target_paper = Paper(
        id="arxiv:1706.03762",
        title="Attention Is All You Need",
        abstract="We propose the Transformer, a model architecture eschewing recurrence...",
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
            raw_text="[1] Jimmy Ba, Jamie Ryan Kiros. Layer Normalization. 2016.",
            matched_arxiv_id="1607.06450",
        ),
        Reference(
            id="ref_2",
            revision_id="arxiv:1706.03762:v7",
            paper_id="arxiv:1706.03762",
            ref_order=2,
            raw_text="[2] Dzmitry Bahdanau. Neural Machine Translation. 2014.",
            matched_arxiv_id="1409.0473",
        ),
        Reference(
            id="ref_3",
            revision_id="arxiv:1706.03762:v7",
            paper_id="arxiv:1706.03762",
            ref_order=3,
            raw_text="[3] Jonas Gehring. Convolutional Sequence to Sequence Learning. 2017.",
            matched_arxiv_id="1705.03122",
        ),
    ]

    # Step 1: Candidate retrieval and filtering
    cset = retrieve_and_filter_candidates(target_paper, target_refs, client)
    assert cset.total_count == 5
    assert len(cset.eligible_candidates) == 3
    assert len(cset.excluded_candidates) == 2

    # Step 2: Stage 1 Ranking under canonical mode (vectors absent in mock -> embedding_unavailable)
    result_canonical = rank_stage1_candidates(
        target_paper,
        cset,
        semantic_strategy="specter2_canonical",
        citation_importance_mode="canonical",
    )

    # 3 eligible candidates scored
    assert len(result_canonical.ranked_candidates) == 3
    assert len(result_canonical.excluded_candidates) == 2
    assert len(result_canonical.unranked_candidates) == 0

    # Per docs/scoring-lineage.md line 129 ("低coverage候補が有利になる偏りを評価する"):
    # Bahdanau & Gehring have only C=1.0 observed (coverage=0.35), so score = (0.35*1.0)/0.35 = 1.0.
    # Layer Norm has C=1.0 and I=0.75 observed (coverage=0.60), so score = 0.5375/0.60 = 0.8958.
    # lower-bound-v1 keeps those InitialScores but ranks Layer Norm's observed
    # lower bound 0.5375 ahead of the two partially observed lower bounds 0.35.
    bahdanau_ranked = next(
        r for r in result_canonical.ranked_candidates if r.candidate.paper_id == "bahdanau_456"
    )
    gehring_ranked = next(
        r for r in result_canonical.ranked_candidates if r.candidate.paper_id == "gehring_789"
    )
    ba_ranked = next(
        r for r in result_canonical.ranked_candidates if r.candidate.paper_id == "ba_123"
    )

    assert bahdanau_ranked.rank == 2
    assert pytest.approx(bahdanau_ranked.score.coverage, abs=1e-5) == 0.35
    assert pytest.approx(bahdanau_ranked.score.initial_score, abs=1e-5) == 1.00

    assert gehring_ranked.rank == 3
    assert pytest.approx(gehring_ranked.score.coverage, abs=1e-5) == 0.35
    assert pytest.approx(gehring_ranked.score.initial_score, abs=1e-5) == 1.00

    assert ba_ranked.rank == 1
    assert pytest.approx(ba_ranked.score.coverage, abs=1e-5) == 0.60
    assert ba_ranked.score.citation_relation == 1.00
    assert ba_ranked.score.citation_importance == 0.75
    assert ba_ranked.score.semantic_similarity is None
    assert ba_ranked.score.missing_components == ["semantic_similarity"]
    assert pytest.approx(ba_ranked.score.initial_score, abs=1e-5) == 0.8958333333333334

    # Step 3: Stage 1 Ranking with lexical baseline enabled
    result_lexical = rank_stage1_candidates(
        target_paper,
        cset,
        semantic_strategy="lexical",
        citation_importance_mode="canonical",
    )
    assert len(result_lexical.ranked_candidates) == 3
    for r in result_lexical.ranked_candidates:
        assert r.score.semantic_similarity is not None
        assert 0.0 <= r.score.semantic_similarity <= 1.0
