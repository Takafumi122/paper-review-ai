"""Evidence-grounded outputs for the three reading-support features."""

from __future__ import annotations

import math
import re
from collections import Counter
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from paper_review.domain.candidate_retrieval import evaluate_temporal_eligibility
from paper_review.domain.citation_identity import author_year_matches
from paper_review.domain.models import CandidatePaper, Paper, Stage1Score


class OutputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Evidence(OutputModel):
    span_id: str
    quote: str = Field(min_length=1, max_length=1800)


class Contribution(OutputModel):
    id: str
    description: str
    evidence: list[Evidence] = Field(min_length=1, max_length=4)


class Contributions(OutputModel):
    problem: str
    problem_evidence: list[Evidence] = Field(default_factory=list, max_length=4)
    contributions: list[Contribution] = Field(max_length=3)


class Relevance(OutputModel):
    candidate_id: str
    score: int | None = Field(ge=0, le=4, strict=True)
    contribution_ids: list[str]
    reason: str
    evidence: list[Evidence] = Field(max_length=4)


class RelevanceBatch(OutputModel):
    evaluations: list[Relevance] = Field(max_length=10)


class EvidenceConfidence(float, Enum):
    NONE = 0.0
    INDIRECT = 0.5
    STRONG = 0.75
    DIRECT = 1.0


class LineageEdge(OutputModel):
    from_paper_id: str
    to_paper_id: str
    relationship: str
    relationship_score: int = Field(ge=0, le=4, strict=True)
    evidence_confidence: EvidenceConfidence
    evidence: list[Evidence] = Field(max_length=4)


class MergeBranchSupport(OutputModel):
    from_paper_id: str
    contribution_id: str
    evidence: list[Evidence] = Field(min_length=1, max_length=4)


class LineageMergeSupport(OutputModel):
    node_id: str
    branches: list[MergeBranchSupport] = Field(min_length=2, max_length=2)
    integration_evidence: list[Evidence] = Field(min_length=1, max_length=4)


class LineageProposal(OutputModel):
    predecessor_ids: list[str] = Field(max_length=5)
    edges: list[LineageEdge] = Field(max_length=5)
    merge_reason: str | None
    missing_reason: str | None
    merge_support: LineageMergeSupport | None = None


class LineageProposalDraft(LineageProposal):
    # Old stored proposals remain readable; new generation explicitly supplies
    # null for a single path, or the complete grounding for a merge.
    merge_support: LineageMergeSupport | None = Field(...)


class ReasoningStep(OutputModel):
    id: str
    kind: Literal[
        "problem",
        "observation",
        "hypothesis",
        "approach",
        "experiment",
        "result",
        "interpretation",
        "conclusion",
    ]
    summary: str
    evidence: list[Evidence] = Field(min_length=1, max_length=4)


class ReasoningEdge(OutputModel):
    from_step_id: str
    to_step_id: str
    relation: Literal["leads_to", "evaluates", "supports"]


class ReasoningFlow(OutputModel):
    steps: list[ReasoningStep] = Field(min_length=1, max_length=10)
    edges: list[ReasoningEdge] = Field(max_length=20)
    limitations: list[str] = Field(max_length=10)


class ReasoningStepDraft(ReasoningStep):
    summary: str = Field(
        min_length=1,
        description="Japanese summary of facts explicitly reported in the selected evidence. "
        "Do not add a purpose, intent or causal preface absent from those quotations.",
    )
    reader_explanation: str | None = Field(
        min_length=1,
        description="Optional Japanese reader-oriented organization or qualified inference "
        "from the same selected evidence; null when unnecessary. No new factual claims, "
        "unquoted authorial intent or guessed continuation. Also subject to source review.",
    )
    reader_explanation_context: str | None = Field(
        default=None,
        min_length=1,
        description="Self-contained Japanese identification of the subjects and exact settings "
        "needed to understand reader_explanation. For a comparison, name each model, dataset, "
        "judge, metric and compared setting as applicable. This context is displayed and "
        "source-reviewed together with the explanation using reader_explanation_evidence. "
        "No extra facts, guessed referents or source labels. Null iff explanation is null.",
    )
    reader_explanation_evidence: list[Evidence] = Field(
        max_length=4,
        description="Explicit supporting sources for reader_explanation, including every "
        "premise used there. Empty iff reader_explanation is null. The union with the "
        "factual evidence list must contain at most four distinct source spans.",
    )

    def combined_evidence(self) -> list[Evidence]:
        combined: dict[str, Evidence] = {}
        for part in (self.evidence, self.reader_explanation_evidence):
            for e in part:
                if e.span_id in combined and combined[e.span_id].quote != e.quote:
                    raise ValueError("reasoning_part_conflicting_quote")
                combined[e.span_id] = e
        if len(combined) > 4:
            raise ValueError("reasoning_combined_evidence_limit")
        return list(combined.values())

    def explanation_text(self) -> str | None:
        if self.reader_explanation is None:
            return None
        if self.reader_explanation_context is None:
            return self.reader_explanation  # Reading historical outputs remains supported.
        return self.reader_explanation_context + "\n" + self.reader_explanation

    @model_validator(mode="after")
    def explicit_part_sources(self) -> ReasoningStepDraft:
        if self.reader_explanation is None and self.reader_explanation_context is not None:
            raise ValueError("reasoning_unused_explanation_context")
        if (self.reader_explanation is None) != (not self.reader_explanation_evidence):
            raise ValueError("reasoning_explanation_evidence_missing_or_unused")
        self.combined_evidence()
        return self


class ReasoningFlowDraft(OutputModel):
    steps: list[ReasoningStepDraft] = Field(min_length=1, max_length=10)
    edges: list[ReasoningEdge] = Field(max_length=20)
    limitations: list[str] = Field(max_length=10)

    def to_flow(self) -> ReasoningFlow:
        return ReasoningFlow(
            steps=[
                ReasoningStep(
                    id=s.id,
                    kind=s.kind,
                    evidence=s.combined_evidence(),
                    summary=s.summary
                    + (
                        f"\n\n補足（引用に基づく解釈）\n{s.explanation_text()}"
                        if s.reader_explanation is not None
                        else ""
                    ),
                )
                for s in self.steps
            ],
            edges=self.edges,
            limitations=self.limitations,
        )


class ReasoningClaimReview(OutputModel):
    text: str = Field(min_length=1, description="Exact contiguous part of the original summary.")
    decision: Literal["supported", "unsupported", "uncertain", "wrong_scope"]
    evidence_span_ids: list[str] = Field(max_length=4)
    reason: str = Field(min_length=1, max_length=600)


class ReasoningStepReview(OutputModel):
    claims: list[ReasoningClaimReview] = Field(min_length=1, max_length=40)

    @property
    def supported(self) -> bool:
        return all(claim.decision == "supported" for claim in self.claims)


def validate_reasoning_review(review: ReasoningStepReview, step: ReasoningStep) -> None:
    # Exact ordered coverage prevents a reviewer from silently skipping a clause.
    if "".join(claim.text for claim in review.claims) != step.summary:
        raise ValueError("reasoning_review_incomplete_coverage")
    selected = {e.span_id for e in step.evidence}
    for claim in review.claims:
        ids = claim.evidence_span_ids
        if len(ids) != len(set(ids)) or any(i not in selected for i in ids):
            raise ValueError("reasoning_review_unselected_evidence")
        if claim.decision == "supported" and not ids:
            raise ValueError("reasoning_review_missing_evidence")


class KnowledgeItem(OutputModel):
    id: str
    concept: str = Field(
        min_length=2,
        max_length=80,
        description="Short prerequisite concept name; never a claim or step summary.",
    )
    from_step_id: str
    to_step_id: str
    necessity_score: int = Field(ge=0, le=3, strict=True)
    why_needed: str = Field(description="Specific missing causal link for this concept.")
    short_explanation: str = Field(
        description="Teach the prerequisite mechanism in this paper context."
    )
    evidence: list[Evidence] = Field(min_length=1, max_length=4)


class KnowledgeBatch(OutputModel):
    items: list[KnowledgeItem] = Field(max_length=20)


class KnowledgeDetail(OutputModel):
    explanation: str
    evidence: list[Evidence] = Field(min_length=1, max_length=6)


class KnowledgeDetailDraft(OutputModel):
    mechanism: str = Field(
        min_length=1, description="Actual causal mechanism in Japanese; no preface."
    )
    transition: str = Field(
        min_length=1,
        description="Explain why the specified reasoning step follows from the prior step.",
    )
    limitations: str | None = Field(description="Source-supported limitations, or null.")
    evidence: list[Evidence] = Field(min_length=1, max_length=6)

    def to_detail(self) -> KnowledgeDetail:
        paragraphs = [f"仕組み\n{self.mechanism}", f"論文中のつながり\n{self.transition}"]
        if self.limitations:
            paragraphs.append(f"説明の限界\n{self.limitations}")
        return KnowledgeDetail(explanation="\n\n".join(paragraphs), evidence=self.evidence)


class KnowledgeDetailReview(OutputModel):
    decision: Literal["supported", "non_answer", "unsupported_claim", "wrong_scope"]
    reason: str = Field(min_length=1, max_length=600)


def normalize_quote(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def validate_evidence(evidence: list[Evidence], sources: dict[str, dict[str, str]]) -> None:
    for item in evidence:
        source = sources.get(item.span_id)
        if source is None:
            raise ValueError("unverified_evidence_unknown_span")
        if normalize_quote(item.quote) not in normalize_quote(source["text"]):
            raise ValueError("unverified_evidence_quote_mismatch")


def validate_reasoning(flow: ReasoningFlow, sources: dict[str, dict[str, str]]) -> None:
    ids = [step.id for step in flow.steps]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate_reasoning_step")
    for step in flow.steps:
        validate_evidence(step.evidence, sources)
    seen: set[tuple[str, str]] = set()
    for edge in flow.edges:
        pair = (edge.from_step_id, edge.to_step_id)
        if (
            pair in seen
            or any(i not in ids for i in pair)
            or ids.index(pair[0]) >= ids.index(pair[1])
        ):
            raise ValueError("invalid_reasoning_edge")
        seen.add(pair)
    if len(ids) > 1:
        incident = {i for pair in seen for i in pair}
        if any(i not in incident for i in ids):
            raise ValueError("orphan_reasoning_step")
        incoming = {b for _, b in seen}
        if any(step.kind == "result" and step.id not in incoming for step in flow.steps):
            raise ValueError("result_without_predecessor")


def final_score(stage1: Stage1Score, relevance: Relevance, *, adjustment_policy: str) -> dict:
    if adjustment_policy != "identity-v1":
        return {"value": None, "coverage": 0.0, "status": "blocked_configuration"}
    if relevance.score is None or not relevance.evidence:
        return {"value": None, "coverage": 0.0, "status": "unavailable"}
    adjusted = relevance.score / 4
    values = [
        (0.40, adjusted),
        (0.25, stage1.semantic_similarity),
        (0.20, stage1.citation_relation),
        (0.15, stage1.citation_importance),
    ]
    if any(
        value is not None and (not math.isfinite(value) or not 0 <= value <= 1)
        for _, value in values
    ):
        raise ValueError("invalid_stage2_component")
    coverage = sum(w for w, value in values if value is not None)
    return {
        "value": sum(w * v for w, v in values if v is not None) / coverage,
        "coverage": coverage,
        "adjusted_contribution_relevance": adjusted,
        "adjustment_policy_version": adjustment_policy,
        "status": "complete" if coverage == 1 else "partial",
    }


def candidate_is_identified(
    candidate: CandidatePaper, evidence: list[Evidence], sources: dict[str, dict[str, str]]
) -> bool:
    for item in evidence:
        source = sources.get(item.span_id, {})
        quote = normalize_quote(item.quote).lower()
        if source.get("paper_id") == candidate.paper_id:
            return True
        labels = {
            int(ref.citation_key[1:-1]) for ref in candidate.source_references
            if ref.citation_key and re.fullmatch(r"\[\d+\]", ref.citation_key)
        }
        if any(
            labels.intersection(int(n) for n in re.findall(r"\d+", match[0]))
            for match in re.finditer(r"\[\s*\d+(?:\s*[,;]\s*\d+)*\s*\]", quote)
        ):
            return True
        if next(author_year_matches(candidate, item.quote), None) is not None:
            return True
    return False


def validate_lineage(
    proposal: LineageProposal,
    target: Paper,
    candidates: dict[str, CandidatePaper],
    sources: dict[str, dict[str, str]],
    *, contributions: Contributions | None = None,
) -> list[dict]:
    """Validate topology/chronology/quotes before assigning code-owned confidence."""
    predecessors = proposal.predecessor_ids
    if len(set(predecessors)) != len(predecessors) or target.id in predecessors:
        raise ValueError("invalid_lineage_nodes")
    if any(
        i not in candidates or candidates[i].eligibility_status != "eligible" for i in predecessors
    ):
        raise ValueError("ineligible_lineage_node")
    nodes = set(predecessors) | {target.id}
    incoming: Counter[str] = Counter()
    outgoing: dict[str, str] = {}
    validated = []
    for edge in proposal.edges:
        a, b = edge.from_paper_id, edge.to_paper_id
        if a not in predecessors or b not in nodes or a == b or a in outgoing:
            raise ValueError("invalid_lineage_edge")
        incoming[b] += 1
        outgoing[a] = b
        earlier = candidates[a]
        later_candidate = candidates.get(b)
        later_date = (
            target.published_at
            if b == target.id
            else (
                later_candidate.date_provenance.value
                if later_candidate and later_candidate.date_provenance
                else None
            )
        )
        temporal, _, _ = evaluate_temporal_eligibility(
            earlier.date_provenance.value if earlier.date_provenance else earlier.publication_date,
            earlier.year,
            later_date,
        )
        if temporal != "eligible":
            raise ValueError("lineage_chronology_unverified")
        validate_evidence(edge.evidence, sources)
        if edge.evidence and not candidate_is_identified(earlier, edge.evidence, sources):
            raise ValueError("lineage_candidate_not_identified")
        if any(sources[e.span_id].get("paper_id") not in {a, b, target.id} for e in edge.evidence):
            raise ValueError("lineage_evidence_wrong_paper")
        evidence_confidence = edge.evidence_confidence if edge.evidence else 0.0
        confidence = 0.60 * (edge.relationship_score / 4) + 0.40 * evidence_confidence
        status = (
            "insufficient_evidence"
            if evidence_confidence == 0
            else (
                "supported"
                if confidence >= 0.8
                else "uncertain"
                if confidence >= 0.5
                else "insufficient_evidence"
            )
        )
        validated.append(dict(**edge.model_dump(), confidence=confidence, status=status))
    if any(n > 2 for n in incoming.values()) or sum(n == 2 for n in incoming.values()) > 1:
        raise ValueError("lineage_merge_limit")
    if any(n == 2 for n in incoming.values()) and not proposal.merge_reason:
        raise ValueError("lineage_merge_unjustified")
    for paper_id in predecessors:
        node, seen = paper_id, set()
        while node != target.id:
            if node in seen or node not in outgoing or len(seen) >= 3:
                raise ValueError("lineage_cycle_depth_or_disconnection")
            seen.add(node)
            node = outgoing[node]
    merges = [node for node, count in incoming.items() if count == 2]
    support = proposal.merge_support
    if not merges:
        if support is not None:
            raise ValueError("lineage_merge_support_without_merge")
        if proposal.merge_reason is not None:
            raise ValueError("lineage_merge_reason_without_merge")
        return validated
    if support is None:
        raise ValueError("lineage_merge_support_required")
    if contributions is None or not contributions.contributions:
        raise ValueError("lineage_merge_contributions_required")
    if support.node_id != merges[0]:
        raise ValueError("lineage_merge_node_mismatch")
    incoming_ids = {e.from_paper_id for e in proposal.edges if e.to_paper_id == support.node_id}
    branch_ids = [b.from_paper_id for b in support.branches]
    if len(set(branch_ids)) != 2 or set(branch_ids) != incoming_ids:
        raise ValueError("lineage_merge_branch_coverage_mismatch")
    contribution_ids = {c.id for c in contributions.contributions}
    assigned = [b.contribution_id for b in support.branches]
    if any(cid not in contribution_ids for cid in assigned):
        raise ValueError("lineage_merge_unknown_contribution")
    # The same major contribution can explicitly integrate two research lines.
    # Their distinctness and integration are semantic checks, not ID inequality.
    for branch in support.branches:
        validate_evidence(branch.evidence, sources)
        if not candidate_is_identified(candidates[branch.from_paper_id], branch.evidence, sources):
            raise ValueError("lineage_merge_branch_not_identified")
        if any(sources[e.span_id].get("paper_id") not in {
            branch.from_paper_id, support.node_id, target.id,
        } or "reference" in sources[e.span_id].get("section", "").lower()
               for e in branch.evidence):
            raise ValueError("lineage_merge_evidence_wrong_scope")
    validate_evidence(support.integration_evidence, sources)
    if any(sources[e.span_id].get("paper_id") not in {support.node_id, target.id}
           or "reference" in sources[e.span_id].get("section", "").lower()
           for e in support.integration_evidence):
        raise ValueError("lineage_merge_evidence_wrong_scope")
    return validated
