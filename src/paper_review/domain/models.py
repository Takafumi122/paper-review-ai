"""Domain models for paper parsing, identity, and bibliography."""

from __future__ import annotations

import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ExternalIds(BaseModel):
    """External bibliographic identifiers supported by the platform.

    OpenAlex is reserved for future schema compatibility (MVP does not use it).
    Semantic Scholar is reserved for Phase 2 candidate retrieval.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    arxiv: str | None = None
    doi: str | None = None
    semantic_scholar: str | None = None
    openalex: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "arxiv": self.arxiv,
            "doi": self.doi,
            "semantic_scholar": self.semantic_scholar,
            "openalex": self.openalex,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ExternalIds:
        if not data:
            return cls()
        return cls(
            arxiv=data.get("arxiv"),
            doi=data.get("doi"),
            semantic_scholar=data.get("semantic_scholar"),
            openalex=data.get("openalex"),
        )


class Paper(BaseModel):
    """Logical paper identity (independent of specific versions or PDF files)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    primary_category: str | None = None
    categories: list[str] = Field(default_factory=list)
    published_at: str | None = None
    published_date_precision: str | None = None
    published_date_source: str | None = None
    external_ids: ExternalIds = Field(default_factory=ExternalIds)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_provenance: dict[str, str] = Field(default_factory=dict)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class PaperRevision(BaseModel):
    """arXiv version / parsed content revision.

    Raw PDFs are never permanently retained; this record tracks the content hash,
    source URL, and parser version used for reproducible evidence.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    paper_id: str
    arxiv_version: str
    content_hash: str | None = None
    pdf_source_url: str | None = None
    parser_version: str | None = None
    page_count: int | None = None
    created_at: float = Field(default_factory=time.time)


class PaperSection(BaseModel):
    """Parsed section text stored in DB for reproducible evidence.

    Section text is persisted in the database, but must NEVER be emitted
    into application logs or exceptions.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    revision_id: str
    paper_id: str
    section_index: int
    title: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    span_info: dict[str, Any] | None = None
    extraction_quality: dict[str, Any] | None = None
    created_at: float = Field(default_factory=time.time)

    def __repr__(self) -> str:
        # Deliberately NEVER output section text in repr to prevent log leaks.
        return (
            f"PaperSection(id={self.id!r}, paper_id={self.paper_id!r}, "
            f"section_index={self.section_index}, title={self.title!r}, "
            f"pages={self.page_start}-{self.page_end})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class Reference(BaseModel):
    """Target paper bibliography entry extracted during parsing."""

    model_config = ConfigDict(extra="forbid")

    id: str
    revision_id: str
    paper_id: str
    ref_order: int
    raw_text: str
    citation_key: str | None = None
    matched_paper_id: str | None = None
    matched_arxiv_id: str | None = None
    matched_doi: str | None = None
    resolution_status: str = "unresolved"
    created_at: float = Field(default_factory=time.time)

    def __repr__(self) -> str:
        return (
            f"Reference(id={self.id!r}, paper_id={self.paper_id!r}, "
            f"ref_order={self.ref_order}, status={self.resolution_status!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()


class ArXivVersionInfo(BaseModel):
    """Version record in an arXiv paper's revision history."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str  # e.g. "v1", "v7"
    submitted_date: str | None = None  # ISO 8601 formatted string


class ArXivMetadata(BaseModel):
    """Normalized metadata for a paper retrieved from arXiv.

    Free of any raw Atom/XML constructs; easily connects to ExternalIds, Paper,
    and PaperRevision domain models.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    arxiv_id: str  # e.g. "1706.03762"
    title: str
    authors: list[str]
    abstract: str
    primary_category: str
    categories: list[str]
    first_submitted_date: str  # ISO 8601 string, e.g. "2017-06-12T17:57:34Z"
    current_version: str  # e.g. "v7"
    current_version_date: str | None = None  # ISO 8601 string of retrieved version
    version_history: list[ArXivVersionInfo] | None = None  # None if full history unavailable
    pdf_url: str  # e.g. "https://arxiv.org/pdf/1706.03762v7.pdf"
    doi: str | None = None
    comment: str | None = None
    journal_ref: str | None = None

    def to_external_ids(self) -> ExternalIds:
        return ExternalIds(arxiv=self.arxiv_id, doi=self.doi)

    def to_paper(self) -> Paper:
        date_str = self.first_submitted_date[:10] if self.first_submitted_date else None
        return Paper(
            id=f"arxiv:{self.arxiv_id}",
            title=self.title,
            authors=self.authors,
            abstract=self.abstract,
            primary_category=self.primary_category,
            categories=self.categories,
            published_at=date_str,
            published_date_precision="day",
            published_date_source="arxiv_first_submitted",
            external_ids=self.to_external_ids(),
        )

    def to_paper_revision(
        self,
        *,
        content_hash: str | None = None,
        parser_version: str | None = None,
        page_count: int | None = None,
    ) -> PaperRevision:
        return PaperRevision(
            id=f"arxiv:{self.arxiv_id}:{self.current_version}",
            paper_id=f"arxiv:{self.arxiv_id}",
            arxiv_version=self.current_version,
            content_hash=content_hash,
            pdf_source_url=self.pdf_url,
            parser_version=parser_version,
            page_count=page_count,
        )


class SourceSpan(BaseModel):
    """Provenance tracking for a span of text extracted from a specific PDF page.

    start_offset and end_offset are 0-based character offsets within the section's text.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    page: int  # 1-indexed PDF page number
    start_offset: int
    end_offset: int


class ParsedSection(BaseModel):
    """Structured section extracted from PDF by the parser adapter."""

    model_config = ConfigDict(extra="forbid")

    section_index: int
    title: str | None = None
    text: str
    page_start: int | None = None
    page_end: int | None = None
    source_spans: list[SourceSpan] = Field(default_factory=list)
    extraction_quality: str = "good"  # "good" | "partial" | "low_quality" | "unsupported"
    is_appendix: bool = False

    def __repr__(self) -> str:
        # Deliberately NEVER output section text in repr to prevent log leaks.
        return (
            f"ParsedSection(index={self.section_index}, title={self.title!r}, "
            f"pages={self.page_start}-{self.page_end}, quality={self.extraction_quality!r}, "
            f"is_appendix={self.is_appendix})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def to_paper_section(self, revision_id: str, paper_id: str) -> PaperSection:
        """Convert to domain PaperSection for eventual DB persistence."""
        return PaperSection(
            id=f"{revision_id}:sec:{self.section_index}",
            revision_id=revision_id,
            paper_id=paper_id,
            section_index=self.section_index,
            title=self.title,
            text=self.text,
            page_start=self.page_start,
            page_end=self.page_end,
            span_info={
                "source_spans": [s.model_dump() for s in self.source_spans],
                "is_appendix": self.is_appendix,
            },
            extraction_quality={"status": self.extraction_quality},
        )


class ParsedReference(BaseModel):
    """Bibliography reference entry extracted from PDF."""

    model_config = ConfigDict(extra="forbid")

    ref_order: int
    raw_text: str
    citation_key: str | None = None
    page: int | None = None

    def __repr__(self) -> str:
        # Deliberately NEVER output reference raw_text in repr to prevent log leaks.
        return (
            f"ParsedReference(order={self.ref_order}, key={self.citation_key!r}, page={self.page})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def to_reference(self, revision_id: str, paper_id: str) -> Reference:
        """Convert to domain Reference for eventual DB persistence."""
        return Reference(
            id=f"{revision_id}:ref:{self.ref_order}",
            revision_id=revision_id,
            paper_id=paper_id,
            ref_order=self.ref_order,
            raw_text=self.raw_text,
            citation_key=self.citation_key,
            resolution_status="unresolved",
        )


class ParsedDocument(BaseModel):
    """Standardized document output from PDF Parser Adapter.

    Contains page count, structured sections, references, extraction warnings,
    and quality metrics. Decoupled from pdfplumber or any parsing library.
    """

    model_config = ConfigDict(extra="forbid")

    page_count: int
    sections: list[ParsedSection] = Field(default_factory=list)
    references: list[ParsedReference] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    parser_version: str = "pdfplumber-0.1.0"
    extraction_quality: str = "good"  # "good" | "partial" | "low_quality" | "unsupported"

    def __repr__(self) -> str:
        return (
            f"ParsedDocument(pages={self.page_count}, sections={len(self.sections)}, "
            f"references={len(self.references)}, quality={self.extraction_quality!r}, "
            f"warnings={self.warnings!r})"
        )

    def __str__(self) -> str:
        return self.__repr__()

    def to_paper_sections(self, revision_id: str, paper_id: str) -> list[PaperSection]:
        return [sec.to_paper_section(revision_id, paper_id) for sec in self.sections]

    def to_references(self, revision_id: str, paper_id: str) -> list[Reference]:
        return [ref.to_reference(revision_id, paper_id) for ref in self.references]

    def to_paper_revision(
        self,
        revision_id: str,
        paper_id: str,
        arxiv_version: str,
        *,
        content_hash: str | None = None,
        pdf_source_url: str | None = None,
    ) -> PaperRevision:
        return PaperRevision(
            id=revision_id,
            paper_id=paper_id,
            arxiv_version=arxiv_version,
            content_hash=content_hash,
            pdf_source_url=pdf_source_url,
            parser_version=self.parser_version,
            page_count=self.page_count,
        )


class DateProvenance(BaseModel):
    """Provenance for a publication date used in temporal eligibility analysis."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # kind: "earliest_verifiable" | "arxiv_first_submitted"
    #       | "publication_date" | "publication_year"
    kind: str
    value: str  # ISO date string: "YYYY-MM-DD" | "YYYY-MM" | "YYYY"
    precision: str  # "day" | "month" | "year"
    source_id: str  # "arxiv" | "semantic_scholar"


class CandidatePaper(BaseModel):
    """Normalized candidate predecessor paper retrieved for Research Lineage."""

    model_config = ConfigDict(extra="forbid")

    paper_id: str
    title: str
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    publication_date: str | None = None
    date_provenance: DateProvenance | None = None
    external_ids: ExternalIds = Field(default_factory=ExternalIds)
    citation_count: int | None = None
    influential_citation_count: int | None = None
    fields_of_study: list[str] = Field(default_factory=list)
    venue: str | None = None
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_provenance: dict[str, str] = Field(default_factory=dict)

    resolution_status: str = "legacy"
    resolution_method: str | None = None
    source_references: list[Reference] = Field(default_factory=list)
    metadata_provenance: dict[str, str] = Field(default_factory=dict)

    # Relationship to Target Paper
    is_target_reference: bool = True
    target_reference_order: int | None = None
    target_reference_raw_text: str | None = None
    is_influential_citation: bool | None = None
    citation_contexts: list[str] = Field(default_factory=list)
    citation_intents: list[str] = Field(default_factory=list)

    # Temporal status based on docs/research-lineage.md Section 3:
    # "eligible" | "contemporary_work" | "later_work" | "uncertain"
    temporal_status: str = "eligible"
    temporal_uncertainty: str | None = None

    # Candidate eligibility for Primary Lineage based on docs/research-lineage.md:
    # "eligible" | "excluded" | "uncertain"
    eligibility_status: str = "eligible"
    exclusion_reason: str | None = None


class ReferenceResolution(BaseModel):
    """One auditable resolution outcome per original reference occurrence."""

    model_config = ConfigDict(extra="forbid")
    reference_id: str
    reference_order: int
    candidate_id: str
    status: str
    method: str
    reason: str | None = None
    lookup_ids: list[str] = Field(default_factory=list)


class CandidateSet(BaseModel):
    """Collection of normalized and filtered candidate papers for a Target Paper."""

    model_config = ConfigDict(extra="forbid")

    target_paper_id: str
    target_s2_id: str | None = None
    candidates: list[CandidatePaper] = Field(default_factory=list)
    retrieved_at: float = Field(default_factory=time.time)
    reference_resolutions: list[ReferenceResolution] = Field(default_factory=list)
    retrieval_warnings: list[str] = Field(default_factory=list)

    @property
    def total_count(self) -> int:
        return len(self.candidates)

    @property
    def eligible_candidates(self) -> list[CandidatePaper]:
        return [c for c in self.candidates if c.eligibility_status == "eligible"]

    @property
    def excluded_candidates(self) -> list[CandidatePaper]:
        return [c for c in self.candidates if c.eligibility_status == "excluded"]

    @property
    def uncertain_candidates(self) -> list[CandidatePaper]:
        return [c for c in self.candidates if c.eligibility_status == "uncertain"]


class MetricValue(BaseModel):
    """Canonical representation of a lineage scoring metric (docs/scoring-lineage.md Section 2)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    value: float | None = None
    missing_reason: str | None = None
    source_ids: list[str] = Field(default_factory=list)
    normalization_version: str = "1"
    calculation_source: str | None = None


class Stage1Score(BaseModel):
    """Component breakdown and aggregated InitialScore for a candidate predecessor paper."""

    model_config = ConfigDict(extra="forbid")

    semantic_similarity: float | None = None
    citation_relation: float | None = None
    citation_importance: float | None = None
    initial_score: float | None = None
    selection_lower_bound: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    selection_upper_bound: float | None = Field(default=None, ge=0, le=1, allow_inf_nan=False)
    coverage: float = 0.0
    status: str = "unavailable"  # "complete" | "partial" | "unavailable"
    missing_components: list[str] = Field(default_factory=list)
    missing_reasons: dict[str, str] = Field(default_factory=dict)
    component_details: dict[str, MetricValue] = Field(default_factory=dict)
    scoring_version: str = "1"


class Stage1RankedCandidate(BaseModel):
    """Candidate paper paired with its Stage 1 score and deterministic rank."""

    model_config = ConfigDict(extra="forbid")

    candidate: CandidatePaper
    score: Stage1Score
    rank: int


class Stage1RankingResult(BaseModel):
    """Complete output of Stage 1 Candidate Ranking."""

    model_config = ConfigDict(extra="forbid")

    target_paper_id: str
    target_title: str | None = None
    scoring_version: str = "1"
    selection_policy_version: Literal["observed-renormalized-v1", "lower-bound-v1"] = (
        "observed-renormalized-v1"
    )
    weights: dict[str, float] = Field(default_factory=dict)
    ranked_candidates: list[Stage1RankedCandidate] = Field(default_factory=list)
    unranked_candidates: list[Stage1RankedCandidate] = Field(default_factory=list)
    excluded_candidates: list[CandidatePaper] = Field(default_factory=list)
    uncertain_candidates: list[CandidatePaper] = Field(default_factory=list)
    total_candidates_evaluated: int = 0
    shortlist_limit: int = Field(default=10, ge=0, le=10)

    def top_k(self, k: int = 10) -> list[Stage1RankedCandidate]:
        """Return the top-K ranked candidates (default K=10 per docs/scoring-lineage.md)."""
        if k < 0:
            raise ValueError("k must be non-negative")
        return self.ranked_candidates[: min(k, self.shortlist_limit)]
