"""Conservative candidate discovery from parsed references and OpenAlex metadata."""

from __future__ import annotations

import hashlib
import json
import re

from paper_review.domain.candidate_inventory import (
    YEAR_PATTERN,
    extract_reference_ids,
    fallback_title,
    normalize_arxiv,
)
from paper_review.domain.candidate_retrieval import (
    evaluate_content_eligibility,
    evaluate_temporal_eligibility,
    normalize_title,
)
from paper_review.domain.citation_identity import author_surname
from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    ExternalIds,
    Paper,
    Reference,
    ReferenceResolution,
)
from paper_review.infrastructure.academic_api.openalex import (
    OpenAlexClient,
    OpenAlexError,
    OpenAlexWork,
    doi_id,
)


def corroborated(reference: Reference, work: OpenAlexWork) -> bool:
    raw = normalize_title(reference.raw_text)
    title = normalize_title(work.title)
    surname = normalize_title(author_surname(work.authors[0])) if work.authors else ""
    years = {int(m[1]) for m in YEAR_PATTERN.finditer(reference.raw_text)}
    return bool(len(title) >= 15 and title in raw and len(surname) >= 3
                and surname in raw and (work.year in years or (
                    work.first_public_date and int(work.first_public_date[:4]) in years
                )))


def discovery_title(reference: Reference) -> str:
    """Drop trailing venue prose from discovery only; identity uses the entire reference."""
    title = re.split(r"\.\s+", fallback_title(reference.raw_text), maxsplit=1)[0]
    return re.sub(r",\s*(?:19|20)\d{2}[a-z]?\.?\s*$", "", title)


def identifierless_alias_root(hits: list[OpenAlexWork]) -> OpenAlexWork | None:
    """Reconcile only an ID-less duplicate with identical complete bibliographic data."""
    unique = {work.id: work for work in hits}
    identified = [w for w in unique.values() if w.arxiv_ids or w.doi_aliases]
    if len(unique) < 2 or len(identified) != 1:
        return None
    root = identified[0]
    if (len(root.authors) < 3 or root.year is None or root.date is None
            or len(root.arxiv_ids) > 1 or len(root.doi_aliases) > 1):
        return None
    identity = (normalize_title(root.title), tuple(normalize_title(a) for a in root.authors),
                root.year, root.date)
    if all((normalize_title(w.title), tuple(normalize_title(a) for a in w.authors),
            w.year, w.date) == identity for w in unique.values()):
        return root
    return None


def matches_ids(work: OpenAlexWork, ids: dict[str, list[str]]) -> bool:
    return bool(
        (ids["arxiv_id"] and ids["arxiv_id"][0] in work.arxiv_ids)
        or (ids["doi"] and ids["doi"][0] in work.doi_aliases)
    )


def conflicts(work: OpenAlexWork, ids: dict[str, list[str]]) -> bool:
    return bool(
        (ids["arxiv_id"] and work.arxiv_ids and ids["arxiv_id"][0] not in work.arxiv_ids)
        or (ids["doi"] and work.doi_aliases and ids["doi"][0] not in work.doi_aliases)
    )


def to_candidate(target: Paper, work: OpenAlexWork, *, cited: bool) -> CandidatePaper:
    temporal, uncertainty, date = evaluate_temporal_eligibility(
        work.first_public_date or work.date, work.year, target.published_at,
    )
    source = f"https://api.openalex.org/works/{work.id}"
    if date:
        date = date.model_copy(update={
            "source_id": work.first_public_source or source,
            **({"kind": "arxiv_first_submitted"} if work.first_public_date else {}),
        })
    eligibility, reason = evaluate_content_eligibility(
        work.title, work.abstract, temporal, work.authors,
    )
    if work.work_type in {"review", "dataset"}:
        eligibility, reason = "excluded", "survey" if work.work_type == "review" else "dataset"
    return CandidatePaper(
        paper_id=f"openalex:{work.id}", title=work.title, authors=work.authors,
        abstract=work.abstract, year=work.year,
        publication_date=work.first_public_date or work.date,
        date_provenance=date, external_ids=work.external_ids, citation_count=work.citation_count,
        resolution_status="resolved", resolution_method="openalex_identifier",
        is_target_reference=cited, temporal_status=temporal, temporal_uncertainty=uncertainty,
        eligibility_status=eligibility, exclusion_reason=reason,
        metadata_provenance={
            "source": "openalex", "bibliographic_metadata": source,
            "retrieved_at": str(work.retrieved_at), "content_hash": work.content_hash,
            "date": "arxiv_first_submitted" if work.first_public_date else
                    "openalex_publication_date" if work.date else "openalex_publication_year",
            "date_source": work.first_public_source or source,
            "date_source_hash": work.first_public_hash or work.content_hash,
            "openalex_publication_date": work.date or "unknown",
            "date_limitations": "arxiv_first_submission_verified" if work.first_public_date else
                                "primary_location_publication_not_verified_first_public_release",
            "candidate_source": "target_references" if cited else "openalex_related_works",
        },
    )


async def resolve_openalex_inventory(
    target: Paper, references: list[Reference], client: OpenAlexClient,
) -> CandidateSet:
    inventory = CandidateSet(target_paper_id=target.id, references_complete=False)
    works: dict[str, OpenAlexWork] = {}
    candidates: dict[str, CandidatePaper] = {}
    unresolved_candidates: dict[str, CandidatePaper] = {}
    cited_ids: set[str] = set()
    target_work: OpenAlexWork | None = None
    cached_works = client.cached_works()
    aliases: dict[str, str] = {}
    alias_hashes: dict[str, dict[str, str]] = {}

    def warn(exc: OpenAlexError) -> None:
        if str(exc) not in inventory.retrieval_warnings:
            inventory.retrieval_warnings.append(str(exc))

    def add(work: OpenAlexWork, *, cited: bool) -> CandidatePaper | None:
        if work.id in aliases:
            work = works[aliases[work.id]]
        if work.id == (target_work.id if target_work else None):
            return None
        pid = f"openalex:{work.id}"
        if pid not in candidates:
            if len(candidates) >= 50:
                if "candidate_limit_50" not in inventory.retrieval_warnings:
                    inventory.retrieval_warnings.append("candidate_limit_50")
                return None
            candidates[pid] = to_candidate(target, work, cited=cited)
        elif cited:
            candidates[pid].is_target_reference = True
        works[work.id] = work
        if work.id in alias_hashes:
            candidates[pid].metadata_provenance.update(
                identity_alias_rule="identifierless_exact_title_complete_authors_date",
                openalex_alias_hashes=json.dumps(alias_hashes[work.id], sort_keys=True),
            )
        return candidates[pid]

    # A target index is optional. A failure still leaves parsed references available.
    try:
        hits = (
            await client.get_works([target.external_ids.openalex]) if target.external_ids.openalex
            else await client.get_arxiv_works([normalize_arxiv(target.external_ids.arxiv)])
            if target.external_ids.arxiv else
            await client.find_doi(target.external_ids.doi) if target.external_ids.doi
            else await client.search(target.title) if target.title else []
        )
        hits = [w for w in hits if (
            (target.external_ids.openalex and w.id == target.external_ids.openalex)
            or (target.external_ids.doi and doi_id(target.external_ids.doi) in w.doi_aliases)
            or (target.external_ids.arxiv
                and normalize_arxiv(target.external_ids.arxiv) in w.arxiv_ids)
        )]
        if len(hits) == 1:
            target_work = hits[0]
            cited_ids = set(target_work.references)
            # Index requests are bounded independently of bibliography size.
            indexed = await client.get_works(target_work.references[:50])
            works.update((w.id, w) for w in indexed)
        else:
            inventory.retrieval_warnings.append("openalex_target_identity_unverified")
    except OpenAlexError as exc:
        warn(exc)

    # Identifier batches precede title discovery, including HTTP arXiv locations.
    arxiv_ids = list(dict.fromkeys(
        aid for ref in references for aid in extract_reference_ids(ref)["arxiv_id"]
    ))
    for offset in range(0, len(arxiv_ids), 50):
        try:
            identified = await client.get_arxiv_works(arxiv_ids[offset:offset + 50])
            works.update((w.id, w) for w in identified)
        except OpenAlexError as exc:
            warn(exc)
            break
    try:
        dated = await client.enrich_public_dates(list(works.values()))
        works.update((w.id, w) for w in dated)
    except OpenAlexError as exc:
        warn(exc)

    # Reserve the bounded pool for references with explicit stable identifiers
    # before spending it on title discovery. Preserve order within each group;
    # never use evaluation labels, citation count or ranking scores here.
    def lookup_order(reference: Reference) -> tuple[int, int, str]:
        ids = extract_reference_ids(reference)
        single_id = not any(len(values) > 1 for values in ids.values()) and bool(
            ids["arxiv_id"] or ids["doi"]
        )
        return (0 if single_id else 1, reference.ref_order, reference.id)

    for reference in sorted(references, key=lookup_order):
        ids = extract_reference_ids(reference)
        reason = None
        hits: list[OpenAlexWork] = []
        method = "unresolved"
        cached_identity = False
        if any(len(values) > 1 for values in ids.values()):
            reason = "multiple_identifiers_possible_merged_reference"
        else:
            try:
                if ids["arxiv_id"] or ids["doi"]:
                    hits = [w for w in works.values() if matches_ids(w, ids)]
                    if not hits:
                        hits = [w for w in cached_works if matches_ids(w, ids)]
                        cached_identity = bool(hits)
                    if not hits:
                        hits = (await client.find_doi(ids["doi"][0]) if ids["doi"] else
                                await client.search(discovery_title(reference)))
                        hits = [w for w in hits if matches_ids(w, ids)]
                    method = "openalex_identifier"
                else:
                    hits = [w for w in works.values() if corroborated(reference, w)]
                    if not hits:
                        hits = [w for w in cached_works if corroborated(reference, w)]
                        cached_identity = bool(hits)
                    if not hits:
                        hits = [w for w in await client.search(discovery_title(reference))
                                if corroborated(reference, w)]
                    method = "exact_title_author_year"
            except OpenAlexError as exc:
                reason = str(exc)
                warn(exc)
            if any(conflicts(w, ids) for w in hits):
                reason, hits = "identifier_conflict", []
            if len({w.id for w in hits}) > 1:
                root = identifierless_alias_root(hits)
                if root is not None and not any(
                    f"openalex:{w.id}" in candidates for w in hits if w.id != root.id
                ):
                    works[root.id] = root
                    for alias in hits:
                        if alias.id != root.id:
                            aliases[alias.id] = root.id
                            alias_hashes.setdefault(root.id, {})[alias.id] = alias.content_hash
                    hits = [root]
                else:
                    reason, hits = "ambiguous_identity", []
        candidate = add(hits[0], cited=True) if hits else None
        if candidate is None:
            digest = hashlib.sha256(reference.raw_text.encode()).hexdigest()[:24]
            pid = f"unresolved:openalex:{digest}"
            reason = reason or ("candidate_limit_50" if hits else "identity_not_corroborated")
            # Preserve the original occurrence even when the candidate budget is full.
            if len(unresolved_candidates) < 50:
                unresolved_candidates.setdefault(pid, CandidatePaper(
                    paper_id=pid, title=fallback_title(reference.raw_text),
                    external_ids=ExternalIds(
                        arxiv=ids["arxiv_id"][0] if len(ids["arxiv_id"]) == 1 else None,
                        doi=ids["doi"][0] if len(ids["doi"]) == 1 else None,
                    ),
                    resolution_status="unresolved", is_target_reference=False,
                    temporal_status="uncertain", eligibility_status="uncertain",
                    exclusion_reason=reason, metadata_provenance={"source": "parsed_reference"},
                ))
                candidate = unresolved_candidates[pid]
        else:
            pid = candidate.paper_id
            candidate.resolution_method = method
            if cached_identity:
                candidate.metadata_provenance["identity_lookup"] = "cached_openalex_metadata"
        if candidate:
            candidate.source_references.append(reference.model_copy(deep=True))
            if candidate.target_reference_order is None:
                candidate.target_reference_order = reference.ref_order
                candidate.target_reference_raw_text = reference.raw_text
        inventory.reference_resolutions.append(ReferenceResolution(
            reference_id=reference.id, reference_order=reference.ref_order, candidate_id=pid,
            status=candidate.resolution_status if candidate else "unresolved",
            method=method if hits else "unresolved", reason=reason,
            lookup_ids=[*ids["arxiv_id"], *ids["doi"]],
        ))

    for wid in target_work.references[:50] if target_work else []:
        if wid in works:
            add(works[wid], cited=True)
    if target_work:
        try:
            related = await client.get_works(target_work.related[:10])
            for work in related:
                add(work, cited=work.id in cited_ids)
        except OpenAlexError as exc:
            warn(exc)
    # Unknown bibliography hints must not evict identified API candidates.
    # All original occurrences remain in reference_resolutions even if their
    # placeholder does not fit the fixed 50-candidate pool.
    for pid, candidate in unresolved_candidates.items():
        if len(candidates) >= 50:
            break
        candidates[pid] = candidate
    # A shared external identifier across distinct OpenAlex records is ambiguous.
    owners: dict[tuple[str, str], set[str]] = {}
    for candidate in candidates.values():
        if candidate.resolution_status != "resolved":
            continue
        for field in ("doi", "arxiv"):
            value = getattr(candidate.external_ids, field)
            if value:
                owners.setdefault((field, value), set()).add(candidate.paper_id)
    ambiguous = {pid for ids in owners.values() if len(ids) > 1 for pid in ids}
    for pid in ambiguous:
        candidate = candidates[pid]
        candidate.resolution_status = "ambiguous"
        candidate.eligibility_status = "uncertain"
        candidate.is_target_reference = False
        candidate.exclusion_reason = "conflicting_stable_identifiers"
        for outcome in inventory.reference_resolutions:
            if outcome.candidate_id == pid:
                outcome.status = "ambiguous"
                outcome.reason = "conflicting_stable_identifiers"
    inventory.candidates = list(candidates.values())
    # Retrieval priority must not rewrite the target bibliography's ordering.
    inventory.reference_resolutions.sort(key=lambda r: (r.reference_order, r.reference_id))
    return inventory
