"""Identifier-first, conservative bibliography resolution. No LLM or embedding calls."""

from __future__ import annotations

import hashlib
import re
import time
from urllib.parse import unquote

from paper_review.domain.candidate_retrieval import (
    evaluate_content_eligibility,
    evaluate_temporal_eligibility,
    normalize_title,
)
from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    ExternalIds,
    Paper,
    Reference,
    ReferenceResolution,
)
from paper_review.infrastructure.academic_api.semantic_scholar import (
    S2Paper,
    S2Reference,
    SemanticScholarClient,
    SemanticScholarError,
    SemanticScholarRateLimitError,
)

# Require an arXiv/abs URL marker; arbitrary decimal numbers are not identifiers.
ARXIV_PATTERN = re.compile(
    r"(?:arxiv\s*:\s*|arxiv\.org/(?:abs|pdf)/\s*|\babs/\s*)"
    r"((?:\d{4}\s*\.\s*\d{4,5}|[a-z][a-z.-]+/\d{7})(?:v\d+)?)",
    re.I,
)
DOI_PATTERN = re.compile(r'\b10\.\d{4,9}/[^\s<>"]+', re.I)
YEAR_PATTERN = re.compile(r"(?<![\d.:/])(19\d{2}|20\d{2})(?:[a-z])?(?!\d|\.\d)")


def normalize_arxiv(value: str) -> str:
    return re.sub(r"v\d+$", "", re.sub(r"\s+", "", value), flags=re.I).lower()


def normalize_doi(value: str) -> str:
    value = unquote(value).strip().lower()
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value)
    value = value.rstrip(".,;")
    while value.endswith(")") and value.count(")") > value.count("("):
        value = value[:-1]
    return value


def extract_reference_ids(reference: Reference) -> dict[str, list[str]]:
    """Retain multiple conflicting IDs, rather than choosing an arbitrary first one."""
    text = unquote(reference.raw_text)
    text = re.sub(r"(10\.\d{4,9}/)\s+", r"\1", text)
    arxiv = {normalize_arxiv(m.group(1)) for m in ARXIV_PATTERN.finditer(text)}
    doi = {normalize_doi(m.group()) for m in DOI_PATTERN.finditer(text)}
    if reference.matched_arxiv_id:
        arxiv.add(normalize_arxiv(reference.matched_arxiv_id))
    if reference.matched_doi:
        doi.add(normalize_doi(reference.matched_doi))
    s2 = set()
    if reference.matched_paper_id and re.fullmatch(r"[a-fA-F0-9]{40}", reference.matched_paper_id):
        s2.add(reference.matched_paper_id.lower())
    return {"semantic_scholar_id": sorted(s2), "arxiv_id": sorted(arxiv), "doi": sorted(doi)}


def lookup_keys(ids: dict[str, list[str]]) -> list[tuple[str, str]]:
    prefixes = {"arxiv_id": "ARXIV:", "doi": "DOI:", "semantic_scholar_id": ""}
    return [
        (method, prefixes[method] + value) for method, values in ids.items() for value in values
    ]


def fallback_title(text: str) -> str:
    """Bibliographic hint only; never overwrite resolved API metadata with this."""
    text = re.sub(r"^\[\d+\]\s*", "", text).strip()
    # Author-year style, or a period after a full surname (not an initial).
    parts = re.split(r"\(?(?:19|20)\d{2}[a-z]?\)?\.\s+|(?<=[a-z])\.\s+(?=[A-Z])", text)
    title = parts[1] if len(parts) > 1 else text
    return re.split(r"\s+(?:arXiv:|https?://|In Proceedings|URL)\b", title)[0].strip()


def corroborated_title_matches(reference: Reference, papers: list[S2Paper]) -> list[S2Paper]:
    """Exact normalized full title plus author and year; title alone never proves identity."""
    raw = normalize_title(reference.raw_text)
    years = {int(m.group(1)) for m in YEAR_PATTERN.finditer(reference.raw_text)}
    matches: dict[str, S2Paper] = {}
    for paper in papers:
        title = normalize_title(paper.title)
        author = normalize_title(paper.authors[0].split()[-1]) if paper.authors else ""
        if (
            len(title) >= 15
            and title in raw
            and len(author) >= 3
            and author in raw
            and paper.year in years
        ):
            matches[paper.paper_id] = paper
    return list(matches.values())


def candidate_from_metadata(target: Paper, paper: S2Paper) -> CandidatePaper:
    temporal, uncertainty, provenance = evaluate_temporal_eligibility(
        paper.publication_date,
        paper.year,
        target.published_at,
    )
    eligibility, reason = evaluate_content_eligibility(
        paper.title,
        paper.abstract,
        temporal,
        paper.authors,
    )
    return CandidatePaper(
        paper_id=paper.paper_id,
        title=paper.title,
        abstract=paper.abstract,
        authors=paper.authors,
        year=paper.year,
        publication_date=paper.publication_date,
        date_provenance=provenance,
        external_ids=paper.external_ids,
        citation_count=paper.citation_count,
        influential_citation_count=paper.influential_citation_count,
        fields_of_study=paper.fields_of_study,
        venue=paper.venue,
        temporal_status=temporal,
        temporal_uncertainty=uncertainty,
        eligibility_status=eligibility,
        exclusion_reason=reason,
        resolution_status="resolved",
        is_target_reference=True,
        metadata_provenance={
            "bibliographic_metadata": f"https://api.semanticscholar.org/graph/v1/paper/{paper.paper_id}",
            "retrieved_at": str(time.time()),
            "date": "semantic_scholar_publication_date"
            if paper.publication_date
            else "semantic_scholar_publication_year",
        },
    )


def metadata_conflicts(paper: S2Paper, ids: dict[str, list[str]]) -> bool:
    """Reject contradictions even if another identifier resolved successfully."""
    known = paper.external_ids
    return bool(
        (ids["arxiv_id"] and known.arxiv and normalize_arxiv(known.arxiv) not in ids["arxiv_id"])
        or (ids["doi"] and known.doi and normalize_doi(known.doi) not in ids["doi"])
        or (ids["semantic_scholar_id"] and paper.paper_id not in ids["semantic_scholar_id"])
    )


async def resolve_candidate_inventory(
    target: Paper,
    references: list[Reference],
    client: SemanticScholarClient,
    *,
    allow_title_search: bool = True,
) -> CandidateSet:
    """Resolve all occurrences, deduplicate stable identities, retain every failure.

    Batch identifiers first. A bounded target-reference index corroborates title-only
    entries and provides citation-edge metadata; it never overwrites exact lookup
    metadata. 429 closes further requests for this inventory, not a hidden fallback.
    """
    extracted = {ref.id: extract_reference_ids(ref) for ref in references}
    keys = list(
        dict.fromkeys(
            key
            for ref in references
            if all(len(values) <= 1 for values in extracted[ref.id].values())
            for _, key in lookup_keys(extracted[ref.id])
        )
    )
    warnings: list[str] = []
    resolved: dict[str, S2Paper | None] = {}
    network_blocked = False
    identifier_unavailable = False
    if keys:
        try:
            resolved = await client.get_papers(keys)
        except SemanticScholarError as exc:
            warnings.append(f"identifier_lookup:{type(exc).__name__}")
            network_blocked = True
            identifier_unavailable = True
    edges: list[S2Reference] = []
    target_key = client.resolve_target_id(target.external_ids)
    if target_key and not network_blocked:
        try:
            edges = await client.get_references(target_key, max_results=500)
        except SemanticScholarError as exc:
            warnings.append(f"target_reference_index:{type(exc).__name__}")
            network_blocked = isinstance(exc, SemanticScholarRateLimitError)
    indexed_papers = [edge.cited_paper for edge in edges]
    edge_by_id = {edge.cited_paper.paper_id: edge for edge in edges}
    candidates: dict[str, CandidatePaper] = {}
    outcomes: list[ReferenceResolution] = []
    conflicting_candidates: set[str] = set()
    title_cache: dict[str, list[S2Paper]] = {}

    for reference in references:
        ids = extracted[reference.id]
        lookups = lookup_keys(ids)
        paper: S2Paper | None = None
        method = "unresolved"
        reason: str | None = None
        if any(len(values) > 1 for values in ids.values()):
            reason = "multiple_identifiers_possible_merged_reference"
        elif lookups:
            hits = [(kind, resolved[key]) for kind, key in lookups if resolved.get(key) is not None]
            unique = {hit.paper_id for _, hit in hits if hit is not None}
            if len(unique) > 1 or any(
                metadata_conflicts(hit, ids) for _, hit in hits if hit is not None
            ):
                reason = "identifier_conflict"
            elif hits:
                method, paper = hits[0]
            else:
                reason = (
                    "identifier_lookup_unavailable"
                    if identifier_unavailable
                    else "identifier_not_found"
                )
            # An identifier that failed is not permission for title guessing.
        else:
            matches = corroborated_title_matches(reference, indexed_papers)
            if not matches and allow_title_search and not network_blocked:
                title = fallback_title(reference.raw_text)
                try:
                    if title not in title_cache:
                        title_cache[title] = await client.search_papers(title)
                    matches = corroborated_title_matches(reference, title_cache[title])
                except SemanticScholarError as exc:
                    reason = f"title_lookup:{type(exc).__name__}"
                    warnings.append(reason)
                    if isinstance(exc, SemanticScholarRateLimitError):
                        network_blocked = True
            if len(matches) == 1:
                paper, method = matches[0], "exact_title"
            elif len(matches) > 1:
                reason = "ambiguous_title"
            elif reason is None:
                reason = "title_lookup_unavailable" if network_blocked else "title_not_corroborated"
        if paper is not None:
            candidate = candidate_from_metadata(target, paper)
            candidate.resolution_method = method
            edge = edge_by_id.get(paper.paper_id)
            if edge:
                candidate.is_influential_citation = edge.is_influential
                candidate.citation_contexts = edge.contexts
                candidate.citation_intents = edge.intents
        else:
            # Full bibliographic identity, not title similarity. Unresolved entries
            # cannot enter ranking; raw source is retained for future resolution.
            stable = (
                next((key for _, key in lookups), None)
                if reason
                not in {"identifier_conflict", "multiple_identifiers_possible_merged_reference"}
                else None
            )
            digest = hashlib.sha256(normalize_title(reference.raw_text).encode()).hexdigest()[:24]
            candidate = CandidatePaper(
                paper_id=f"unresolved:{stable or digest}",
                title=fallback_title(reference.raw_text),
                external_ids=ExternalIds(
                    arxiv=ids["arxiv_id"][0] if len(ids["arxiv_id"]) == 1 else None,
                    doi=ids["doi"][0] if len(ids["doi"]) == 1 else None,
                ),
                resolution_status="unresolved",
                resolution_method="unresolved",
                is_target_reference=False,
                temporal_status="uncertain",
                temporal_uncertainty="unresolved_identity",
                eligibility_status="uncertain",
                exclusion_reason=reason,
                metadata_provenance={"bibliographic_hint": "parsed_reference"},
            )
        previous = candidates.get(candidate.paper_id)
        if previous is None:
            candidates[candidate.paper_id] = candidate
        else:
            for field in ("arxiv", "doi"):
                old_value = getattr(previous.external_ids, field)
                new_value = getattr(candidate.external_ids, field)
                if old_value and new_value and old_value != new_value:
                    conflicting_candidates.add(previous.paper_id)
            candidate = previous
        candidate.source_references.append(reference.model_copy(deep=True))
        if candidate.target_reference_order is None:
            candidate.target_reference_order = reference.ref_order
            candidate.target_reference_raw_text = reference.raw_text
        outcomes.append(
            ReferenceResolution(
                reference_id=reference.id,
                reference_order=reference.ref_order,
                candidate_id=candidate.paper_id,
                status=candidate.resolution_status,
                method=method,
                reason=reason,
                lookup_ids=[key for _, key in lookups],
            )
        )
    # Different S2 paperIds sharing another stable identifier are a conflict,
    # not permission to silently merge identities or let both enter ranking.
    alias_owners: dict[tuple[str, str], set[str]] = {}
    for candidate in candidates.values():
        if candidate.resolution_status != "resolved":
            continue
        for field in ("arxiv", "doi"):
            value = getattr(candidate.external_ids, field)
            if value:
                normalized = normalize_arxiv(value) if field == "arxiv" else normalize_doi(value)
                alias_owners.setdefault((field, normalized), set()).add(candidate.paper_id)
    for owners in alias_owners.values():
        if len(owners) > 1:
            conflicting_candidates.update(owners)
    for paper_id in conflicting_candidates:
        candidate = candidates[paper_id]
        candidate.resolution_status = "ambiguous"
        candidate.eligibility_status = "uncertain"
        candidate.exclusion_reason = "conflicting_stable_identifiers"
        candidate.is_target_reference = False
        for outcome in outcomes:
            if outcome.candidate_id == paper_id:
                outcome.status = "ambiguous"
                outcome.reason = "conflicting_stable_identifiers"
    return CandidateSet(
        target_paper_id=target.id,
        target_s2_id=target_key,
        candidates=list(candidates.values()),
        reference_resolutions=outcomes,
        retrieval_warnings=warnings,
    )
