"""Unit tests for Candidate Retrieval, temporal eligibility, and content filtering."""

from __future__ import annotations

import datetime

from paper_review.domain.candidate_retrieval import (
    evaluate_content_eligibility,
    evaluate_temporal_eligibility,
    match_reference_to_candidate,
    parse_date_to_interval,
)
from paper_review.domain.models import CandidatePaper, ExternalIds, Reference


def test_parse_date_to_interval():
    """Verify precision parsing for day, month, year, and fallback."""
    # Day precision
    start, end, prec = parse_date_to_interval("2017-06-12")
    assert prec == "day"
    assert start == datetime.date(2017, 6, 12)
    assert end == datetime.date(2017, 6, 12)

    # Month precision
    start, end, prec = parse_date_to_interval("2017-06")
    assert prec == "month"
    assert start == datetime.date(2017, 6, 1)
    assert end == datetime.date(2017, 6, 30)

    # Year precision from date_str
    start, end, prec = parse_date_to_interval("2016")
    assert prec == "year"
    assert start == datetime.date(2016, 1, 1)
    assert end == datetime.date(2016, 12, 31)

    # Year precision from integer year argument
    start, end, prec = parse_date_to_interval(None, year=2015)
    assert prec == "year"
    assert start == datetime.date(2015, 1, 1)
    assert end == datetime.date(2015, 12, 31)

    # Unknown
    start, end, prec = parse_date_to_interval("invalid", year=None)
    assert prec == "unknown"
    assert start is None and end is None


def test_evaluate_temporal_eligibility_strictly_prior():
    """Verify earlier paper is strictly eligible as predecessor."""
    # Candidate 2016 vs Target 2017-06-12
    status, uncertainty, prov = evaluate_temporal_eligibility(
        candidate_date_str="2016-07-21",
        candidate_year=2016,
        target_date_str="2017-06-12",
    )
    assert status == "eligible"
    assert uncertainty is None
    assert prov is not None
    assert prov.value == "2016-07-21"
    assert prov.precision == "day"

    # Candidate 2014 (year only) vs Target 2017-06-12
    status, uncertainty, _ = evaluate_temporal_eligibility(
        candidate_date_str=None,
        candidate_year=2014,
        target_date_str="2017-06-12",
    )
    assert status == "eligible"
    assert uncertainty is None


def test_evaluate_temporal_eligibility_same_year_different_dates():
    """Verify same year is distinguishable if day precision is available."""
    # Candidate May 2017 vs Target June 2017 (same year, candidate earlier)
    status, uncertainty, _ = evaluate_temporal_eligibility(
        candidate_date_str="2017-05-11",
        candidate_year=2017,
        target_date_str="2017-06-12",
    )
    assert status == "eligible"
    assert uncertainty is None


def test_evaluate_temporal_eligibility_overlapping_contemporary():
    """Verify overlapping intervals result in contemporary_work."""
    # Same exact date
    status, uncertainty, _ = evaluate_temporal_eligibility(
        candidate_date_str="2017-06-12",
        candidate_year=2017,
        target_date_str="2017-06-12",
    )
    assert status == "contemporary_work"
    assert "interval_overlap" in str(uncertainty)

    # Candidate year only (2017) vs Target (2017-06-12)
    status, uncertainty, _ = evaluate_temporal_eligibility(
        candidate_date_str=None,
        candidate_year=2017,
        target_date_str="2017-06-12",
    )
    assert status == "contemporary_work"
    assert "interval_overlap" in str(uncertainty)


def test_evaluate_temporal_eligibility_later_work():
    """Verify papers published after target are flagged as later_work."""
    status, uncertainty, _ = evaluate_temporal_eligibility(
        candidate_date_str="2018-01-15",
        candidate_year=2018,
        target_date_str="2017-06-12",
    )
    assert status == "later_work"
    assert "published_after_target" in str(uncertainty)


def test_evaluate_temporal_eligibility_missing_date():
    """Verify missing date results in uncertain status."""
    status, uncertainty, _ = evaluate_temporal_eligibility(
        candidate_date_str=None,
        candidate_year=None,
        target_date_str="2017-06-12",
    )
    assert status == "uncertain"
    assert uncertainty == "missing_verifiable_date"


def test_evaluate_content_eligibility_survey():
    """Verify survey/overview papers are excluded."""
    # Survey in title
    status, reason = evaluate_content_eligibility(
        title="A Survey of Attention Mechanisms in Deep Learning",
        abstract="Deep learning models have evolved...",
        temporal_status="eligible",
        authors=["Author A"],
    )
    assert status == "excluded"
    assert reason == "survey"

    # Overview in title
    status, reason = evaluate_content_eligibility(
        title="An Overview of Sequence-to-Sequence Architectures",
        abstract="We review seq2seq models...",
        temporal_status="eligible",
        authors=["Author B"],
    )
    assert status == "excluded"
    assert reason == "survey"

    # Survey in abstract
    status, reason = evaluate_content_eligibility(
        title="Deep Learning for NLP",
        abstract="This survey presents a comprehensive overview of recent methods...",
        temporal_status="eligible",
        authors=["Author C"],
    )
    assert status == "excluded"
    assert reason == "survey"


def test_evaluate_content_eligibility_dataset_benchmark():
    """Verify dataset and benchmark papers are excluded."""
    status, reason = evaluate_content_eligibility(
        title="GLUE: A Multi-Task Benchmark and Analysis Platform",
        abstract="We introduce GLUE, a collection of tools for evaluating...",
        temporal_status="eligible",
        authors=["Alex Wang"],
    )
    assert status == "excluded"
    assert reason == "dataset_benchmark"

    status, reason = evaluate_content_eligibility(
        title="SQuAD: 100,000+ Questions for Machine Comprehension of Text Dataset",
        abstract="We present the Stanford Question Answering Dataset...",
        temporal_status="eligible",
        authors=["Pranav Rajpurkar"],
    )
    assert status == "excluded"
    assert reason == "dataset_benchmark"


def test_evaluate_content_eligibility_temporal_cascading():
    """Verify temporal status takes priority and informs eligibility."""
    # Later work
    status, reason = evaluate_content_eligibility(
        title="Valid Research Paper Title",
        abstract="Novel algorithm for training transformers.",
        temporal_status="later_work",
        authors=["Author X"],
    )
    assert status == "excluded"
    assert reason == "published_after_target"

    # Contemporary work
    status, reason = evaluate_content_eligibility(
        title="Valid Research Paper Title",
        abstract="Novel algorithm for training transformers.",
        temporal_status="contemporary_work",
        authors=["Author Y"],
    )
    assert status == "uncertain"
    assert reason == "contemporary_work"


def test_evaluate_content_eligibility_metadata_deficiency():
    """Verify missing title or missing abstract+authors are excluded."""
    # Empty title
    status, reason = evaluate_content_eligibility(
        title="",
        abstract="Some abstract text",
        temporal_status="eligible",
        authors=["Author A"],
    )
    assert status == "excluded"
    assert reason == "unresolved_identity"

    # Missing abstract and authors
    status, reason = evaluate_content_eligibility(
        title="A Valid Title",
        abstract=None,
        temporal_status="eligible",
        authors=[],
    )
    assert status == "excluded"
    assert reason == "insufficient_metadata"


def test_evaluate_content_eligibility_valid():
    """Verify standard predecessor paper passes as eligible."""
    status, reason = evaluate_content_eligibility(
        title="Layer Normalization",
        abstract="Training state-of-the-art deep neural networks is computationally expensive...",
        temporal_status="eligible",
        authors=["Jimmy Ba", "Jamie Ryan Kiros", "Geoffrey E Hinton"],
    )
    assert status == "eligible"
    assert reason is None


def test_match_reference_to_candidate():
    """Verify matching candidate to Target references by ArXiv ID and title."""
    target_refs = [
        Reference(
            id="ref_1",
            revision_id="rev_1",
            paper_id="target_paper",
            ref_order=1,
            raw_text="[1] Jimmy Ba et al. Layer Normalization. arXiv:1607.06450, 2016.",
            matched_arxiv_id="1607.06450",
        ),
        Reference(
            id="ref_2",
            revision_id="rev_1",
            paper_id="target_paper",
            ref_order=2,
            raw_text=(
                "[2] Dzmitry Bahdanau et al. Neural Machine Translation by Jointly Learning "
                "to Align. ICLR 2015."
            ),
        ),
    ]

    # Candidate 1: matches by ArXiv ID
    cand1 = CandidatePaper(
        paper_id="s2_ba_123",
        title="Layer Normalization",
        external_ids=ExternalIds(arxiv="1607.06450"),
    )
    match_reference_to_candidate(cand1, target_refs)
    assert cand1.target_reference_order == 1
    assert "Layer Normalization" in str(cand1.target_reference_raw_text)

    # Candidate 2: matches by title substring in raw_text
    cand2 = CandidatePaper(
        paper_id="s2_bahdanau_456",
        title="Neural Machine Translation by Jointly Learning to Align",
    )
    match_reference_to_candidate(cand2, target_refs)
    assert cand2.target_reference_order == 2
    assert "Bahdanau" in str(cand2.target_reference_raw_text)
