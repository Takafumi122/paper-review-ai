"""Candidate-specific, exact original evidence preparation for Stage 2."""

import json
import re

from pydantic import Field

from paper_review.domain.models import CandidatePaper
from paper_review.domain.understanding import (
    Contributions,
    Evidence,
    OutputModel,
    Relevance,
    RelevanceBatch,
    candidate_is_identified,
)


class CandidateRelevanceSelection(OutputModel):
    candidate_id: str
    score: int | None = Field(ge=0, le=4, strict=True)
    contribution_ids: list[str]
    reason: str
    identifying_evidence: list[Evidence] = Field(max_length=1)
    evidence: list[Evidence] = Field(max_length=3)


class RelevanceSelectionBatch(OutputModel):
    evaluations: list[CandidateRelevanceSelection] = Field(max_length=10)


def selected_relevance(
    value: RelevanceSelectionBatch, spans: list[dict[str, str]],
) -> RelevanceBatch:
    """Require an explicitly selected identity anchor; never attach one on the model's behalf."""
    direct = {s["span_id"]: set(json.loads(s["identifies_candidate_ids"])) for s in spans}
    rows = []
    for item in value.evaluations:
        if item.score is not None and not item.identifying_evidence:
            raise ValueError("relevance_identity_source_required")
        if any(item.candidate_id not in direct.get(e.span_id, set())
               for e in item.identifying_evidence):
            raise ValueError("relevance_invalid_identity_source")
        combined = {e.span_id: e for e in [*item.identifying_evidence, *item.evidence]}
        rows.append(Relevance(candidate_id=item.candidate_id, score=item.score,
                              contribution_ids=item.contribution_ids, reason=item.reason,
                              evidence=list(combined.values())))
    return RelevanceBatch(evaluations=rows)


def validate_contribution_language(value: Contributions) -> None:
    """Reject English-only explanations; proper names/formulas may stay in English."""
    if any(not re.search(r"[ぁ-んァ-ヶ一-龯]", text)
           for text in [value.problem, *(c.description for c in value.contributions)]):
        raise ValueError("contribution_explanation_not_japanese")


def prepare_candidate_evidence(
    candidates: dict[str, CandidatePaper], spans: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, set[str]]]:
    """Direct citation-bearing spans first, then contiguous neighbours, fairly by candidate.

    Empty evidence means unavailable preparation, not proven absence from the paper.
    Never join text across missing offsets/sections or generate replacement quotes.
    """
    sources = {s["span_id"]: s for s in spans}
    direct: dict[str, list[str]] = {}
    neighbours: dict[str, list[str]] = {}
    for cid, candidate in candidates.items():
        direct[cid] = [s["span_id"] for s in spans if candidate_is_identified(
            candidate, [Evidence(span_id=s["span_id"], quote=s["text"])], sources,
        )]
        neighbours[cid] = []
        for sid in direct[cid]:
            anchor = sources[sid]
            for span in spans:
                if (
                    span["paper_id"] == anchor["paper_id"]
                    and span["revision"] == anchor["revision"]
                    and span["section_id"] == anchor["section_id"]
                    and (span["end_offset"] == anchor["start_offset"]
                         or span["start_offset"] == anchor["end_offset"])
                    and span["span_id"] not in direct[cid]
                    and span["span_id"] not in neighbours[cid]
                ):
                    neighbours[cid].append(span["span_id"])
    allowed = {cid: set(direct[cid] + neighbours[cid]) for cid in candidates}
    ordered: list[str] = []
    for group in (direct, neighbours):
        for i in range(max((len(ids) for ids in group.values()), default=0)):
            for ids in group.values():
                if i < len(ids) and ids[i] not in ordered:
                    ordered.append(ids[i])
    return [
        {**sources[sid], "identifies_candidate_ids": json.dumps(sorted(
            cid for cid, ids in direct.items() if sid in ids
        )), "candidate_ids": json.dumps(sorted(
            cid for cid, ids in allowed.items() if sid in ids
        ))} for sid in ordered
    ], allowed
