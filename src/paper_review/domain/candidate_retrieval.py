"""Candidate retrieval, temporal eligibility evaluation, and content filtering for Research Lineage.

Implements the deterministic candidate pipeline:
  Target Paper
      ↓
  Semantic Scholar identity resolution
      ↓
  Target metadata & references retrieval
      ↓
  Candidate normalization
      ↓
  Temporal eligibility (earliest verifiable date, interval comparisons)
      ↓
  Candidate filtering (survey, dataset/benchmark, later work, contemporary work, metadata)
      ↓
  CandidateSet / CandidatePaper list
"""

from __future__ import annotations

import calendar
import datetime
import re

from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    DateProvenance,
    Paper,
    Reference,
)
from paper_review.infrastructure.academic_api.semantic_scholar import (
    S2Reference,
    SemanticScholarClient,
    SemanticScholarNotFoundError,
)

# Regex for detecting Survey / Overview papers
SURVEY_PATTERN = re.compile(
    r"\b(survey|overview|review|tutorial|meta-analysis)\b",
    re.IGNORECASE,
)

# Regex for detecting Dataset / Benchmark papers
DATASET_BENCHMARK_PATTERN = re.compile(
    r"\b(dataset|datasets|benchmark|benchmarks|corpus|corpora|benchmarking)\b",
    re.IGNORECASE,
)


class TargetIdentityResolutionError(Exception):
    """Raised when Target Paper cannot be resolved to a trusted Semantic Scholar identity."""


def parse_date_to_interval(
    date_str: str | None,
    year: int | None = None,
) -> tuple[datetime.date | None, datetime.date | None, str]:
    """Parse date and/or year into an inclusive [start_date, end_date] interval and precision.

    Precision hierarchy:
      - Day: YYYY-MM-DD -> [YYYY-MM-DD, YYYY-MM-DD]
      - Month: YYYY-MM -> [YYYY-MM-01, YYYY-MM-last_day]
      - Year: YYYY -> [YYYY-01-01, YYYY-12-31]
    """
    if date_str:
        cleaned = date_str.strip()[:10]
        # Check YYYY-MM-DD
        m_day = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", cleaned)
        if m_day:
            y, m, d = int(m_day.group(1)), int(m_day.group(2)), int(m_day.group(3))
            try:
                dt = datetime.date(y, m, d)
                return dt, dt, "day"
            except ValueError:
                pass

        # Check YYYY-MM
        m_month = re.fullmatch(r"(\d{4})-(\d{2})", cleaned[:7])
        if m_month:
            y, m = int(m_month.group(1)), int(m_month.group(2))
            try:
                last_day = calendar.monthrange(y, m)[1]
                return datetime.date(y, m, 1), datetime.date(y, m, last_day), "month"
            except ValueError:
                pass

        # Check YYYY in date_str
        m_year = re.match(r"^(\d{4})", cleaned)
        if m_year:
            y = int(m_year.group(1))
            return datetime.date(y, 1, 1), datetime.date(y, 12, 31), "year"

    if year is not None and 1900 <= year <= 2100:
        return datetime.date(year, 1, 1), datetime.date(year, 12, 31), "year"

    return None, None, "unknown"


def evaluate_temporal_eligibility(
    candidate_date_str: str | None,
    candidate_year: int | None,
    target_date_str: str | None,
    target_year: int | None = None,
) -> tuple[str, str | None, DateProvenance | None]:
    """Evaluate temporal relationship between Candidate and Target per docs/research-lineage.md.

    Rules:
      1. Earliest verifiable public availability / publication date / publication year.
      2. If Candidate latest_date < Target earliest_date:
         Candidate strictly precedes Target -> 'eligible'
      3. If Candidate earliest_date > Target latest_date:
         Candidate is strictly after Target -> 'later_work'
      4. If intervals overlap (same day, overlapping month, overlapping year):
         Order cannot be definitively confirmed -> 'contemporary_work'
      5. If date is missing on either:
         Cannot be determined -> 'uncertain'

    Returns:
      (temporal_status, uncertainty_explanation, date_provenance)
    """
    cand_start, cand_end, cand_prec = parse_date_to_interval(candidate_date_str, candidate_year)
    targ_start, targ_end, _ = parse_date_to_interval(target_date_str, target_year)

    provenance: DateProvenance | None = None
    if cand_start and cand_end:
        val = candidate_date_str if candidate_date_str else str(candidate_year)
        kind = "publication_date" if cand_prec in {"day", "month"} else "publication_year"
        provenance = DateProvenance(
            kind=kind,
            value=val,
            precision=cand_prec,
            source_id="semantic_scholar",
        )

    if cand_start is None or cand_end is None or targ_start is None or targ_end is None:
        return "uncertain", "missing_verifiable_date", provenance

    if cand_end < targ_start:
        # Candidate was completed/published strictly before Target earliest date
        return "eligible", None, provenance

    if cand_start > targ_end:
        # Candidate was published strictly after Target latest date
        msg = f"published_after_target: candidate >= {cand_start} vs target <= {targ_end}"
        return "later_work", msg, provenance

    # Intervals overlap
    uncertainty = (
        f"interval_overlap: candidate [{cand_start}..{cand_end}] vs "
        f"target [{targ_start}..{targ_end}]"
    )
    return "contemporary_work", uncertainty, provenance


def evaluate_content_eligibility(
    title: str,
    abstract: str | None,
    temporal_status: str,
    authors: list[str],
) -> tuple[str, str | None]:
    """Evaluate content and structural eligibility of candidate paper.

    Rules per docs/research-lineage.md:
      - Surveys, Overviews, and Tutorials are excluded from Primary Lineage.
      - Dataset-only and Benchmark-only papers are excluded from Primary Lineage.
      - Clearly later work is excluded.
      - Contemporary work is preserved as 'uncertain' (not primary predecessor).
      - Papers with empty titles are excluded as 'unresolved_identity'.
      - Papers lacking both abstract and authors are excluded as 'insufficient_metadata'.
    """
    if not title or not title.strip():
        return "excluded", "unresolved_identity"

    if temporal_status == "later_work":
        return "excluded", "published_after_target"

    if temporal_status == "contemporary_work":
        return "uncertain", "contemporary_work"

    if temporal_status == "uncertain":
        return "uncertain", "uncertain_publication_date"

    # Check for survey papers
    if SURVEY_PATTERN.search(title):
        return "excluded", "survey"
    survey_abstract_re = re.compile(
        r"\b(?:this (?:paper|article) (?:surveys|provides a comprehensive survey)"
        r"|this survey\b)",
        re.I,
    )
    if abstract and survey_abstract_re.search(abstract):
        return "excluded", "survey"

    # Check for dataset / benchmark papers
    if DATASET_BENCHMARK_PATTERN.search(title):
        return "excluded", "dataset_benchmark"

    # Self-description can flag missing dataset/benchmark type metadata, but
    # cannot prove dataset-only scope. Preserve uncertainty instead of excluding
    # method papers merely because they evaluate on a dataset.
    if abstract:
        introductions = re.finditer(
            r"\b(?:we|this paper)\s+(?:introduce|introduces|present|presents|release|releases)\b"
            r"(?P<object>[^.]{0,100}?)\b(?:dataset|benchmark|challenge|corpus)\b",
            abstract,
            re.I,
        )
        resource_claim = any(
            not re.search(
                r"\b(?:on|using|experiments?|results?|evaluat\w*)\b",
                m.group("object"),
                re.I,
            )
            for m in introductions
        )
        challenge_claim = bool(
            re.search(r"\bchallenge\b", title, re.I) and re.search(r"\bbenchmark\b", abstract, re.I)
        )
        environment_claim = bool(
            re.search(r"\bbenchmarks?\b", abstract, re.I)
            and re.search(
                r"\bwe (?:develop|introduce|create)\b[^.]{0,100}\benvironment\b", abstract, re.I
            )
        )
        if resource_claim or challenge_claim or environment_claim:
            return "uncertain", "possible_dataset_benchmark"

    # Check for metadata sufficiency
    if not abstract and len(authors) == 0:
        return "excluded", "insufficient_metadata"

    return "eligible", None


def normalize_title(raw_title: str) -> str:
    """Normalize paper title for fuzzy lookup: lowercase alphanumeric only."""
    return re.sub(r"[^a-z0-9]", "", raw_title.lower())


def match_reference_to_candidate(
    candidate_paper: CandidatePaper,
    target_references: list[Reference],
) -> None:
    """Match a CandidatePaper against Target Paper's parsed bibliography references."""
    cand_norm_title = normalize_title(candidate_paper.title)
    cand_arxiv = (
        candidate_paper.external_ids.arxiv.lower() if candidate_paper.external_ids.arxiv else None
    )
    cand_doi = (
        candidate_paper.external_ids.doi.lower() if candidate_paper.external_ids.doi else None
    )

    for ref in target_references:
        # Match by ArXiv ID
        if cand_arxiv and (
            (ref.matched_arxiv_id and ref.matched_arxiv_id.lower() == cand_arxiv)
            or (cand_arxiv in ref.raw_text.lower())
        ):
            candidate_paper.target_reference_order = ref.ref_order
            candidate_paper.target_reference_raw_text = ref.raw_text
            return

        # Match by DOI
        if cand_doi and (
            (ref.matched_doi and ref.matched_doi.lower() == cand_doi)
            or (cand_doi in ref.raw_text.lower())
        ):
            candidate_paper.target_reference_order = ref.ref_order
            candidate_paper.target_reference_raw_text = ref.raw_text
            return

        # Match by title substring or normalized title
        if len(cand_norm_title) >= 15:
            ref_norm_text = normalize_title(ref.raw_text)
            if cand_norm_title in ref_norm_text:
                candidate_paper.target_reference_order = ref.ref_order
                candidate_paper.target_reference_raw_text = ref.raw_text
                return


def retrieve_and_filter_candidates(
    target_paper: Paper,
    target_references: list[Reference],
    s2_client: SemanticScholarClient,
    *,
    max_candidates: int = 100,
) -> CandidateSet:
    """Execute end-to-end candidate retrieval, temporal analysis, and filtering for Target Paper.

    1. Resolves Target Paper identity to Semantic Scholar lookup key.
    2. Fetches references cited by Target Paper.
    3. Normalizes each candidate and applies temporal eligibility and content filters.
    4. Matches candidates to Target Paper parsed bibliography entries.
    5. Returns unified CandidateSet domain model.
    """
    s2_lookup_id = s2_client.resolve_target_id(target_paper.external_ids)
    if not s2_lookup_id:
        raise TargetIdentityResolutionError(
            f"Cannot resolve Semantic Scholar identity for paper {target_paper.id}: "
            f"external_ids={target_paper.external_ids.to_dict()}"
        )

    try:
        s2_refs: list[S2Reference] = s2_client.get_references_sync(
            s2_lookup_id,
            limit=max_candidates,
            max_results=max_candidates,
        )
    except SemanticScholarNotFoundError as exc:
        raise TargetIdentityResolutionError(
            f"Target paper {target_paper.id} (lookup key {s2_lookup_id}) "
            f"not found in Semantic Scholar: {exc}"
        ) from exc

    candidates: list[CandidatePaper] = []
    seen_paper_ids: set[str] = set()

    for s2_ref in s2_refs:
        cp = s2_ref.cited_paper
        if cp.paper_id in seen_paper_ids:
            continue
        seen_paper_ids.add(cp.paper_id)

        # 1. Temporal eligibility evaluation
        temporal_status, temp_uncertainty, provenance = evaluate_temporal_eligibility(
            candidate_date_str=cp.publication_date,
            candidate_year=cp.year,
            target_date_str=target_paper.published_at,
        )

        # 2. Content & eligibility filtering
        eligibility_status, exclusion_reason = evaluate_content_eligibility(
            title=cp.title,
            abstract=cp.abstract,
            temporal_status=temporal_status,
            authors=cp.authors,
        )

        # 3. Construct CandidatePaper domain object
        candidate = CandidatePaper(
            paper_id=cp.paper_id,
            title=cp.title,
            abstract=cp.abstract,
            authors=cp.authors,
            year=cp.year,
            publication_date=cp.publication_date,
            date_provenance=provenance,
            external_ids=cp.external_ids,
            citation_count=cp.citation_count,
            influential_citation_count=cp.influential_citation_count,
            fields_of_study=cp.fields_of_study,
            venue=cp.venue,
            is_target_reference=True,
            is_influential_citation=s2_ref.is_influential,
            citation_contexts=s2_ref.contexts,
            citation_intents=s2_ref.intents,
            temporal_status=temporal_status,
            temporal_uncertainty=temp_uncertainty,
            eligibility_status=eligibility_status,
            exclusion_reason=exclusion_reason,
        )

        # 4. Match against Target Paper parsed references
        match_reference_to_candidate(candidate, target_references)

        candidates.append(candidate)

    return CandidateSet(
        target_paper_id=target_paper.id,
        target_s2_id=s2_lookup_id,
        candidates=candidates,
    )
