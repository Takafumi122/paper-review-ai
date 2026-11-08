"""Evaluation paper manifest domain model and loader."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from paper_review.config import ROOT
from paper_review.domain.models import MAX_PAPER_PAGES

DEFAULT_MANIFEST_PATH = ROOT / "evaluation" / "manifest" / "papers.json"


class PageLimitCheck(BaseModel):
    """ADR-0001 D3 alignment check for the appendix-inclusive page limit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    expected_page_count: int = Field(ge=1)
    main_text_page_count: int | None = Field(ge=1)
    appendix_page_count: int | None = Field(ge=0)
    limit_pages: int = Field(default=MAX_PAPER_PAGES, ge=1)
    exceeds_limit: bool = Field(
        validation_alias=AliasChoices("exceeds_limit", "exceeds_50_pages")
    )
    status: Literal["within_limit", "exceeds_limit"]
    d3_assessment: Literal["compliant", "pending_decision"]
    notes: str


class EvaluationPaperEntry(BaseModel):
    """Single evaluation paper entry according to docs/thesis-choosing.md."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_id: str
    title: str
    arxiv_id: str
    expected_version: str
    canonical_url: str
    category: str
    categories: list[str] = Field(default_factory=list)
    published_date: str
    date_provenance: str
    reason_for_selection: str
    initial_study_target: bool
    page_limit_check: PageLimitCheck


class EvaluationManifest(BaseModel):
    """Manifest of 10 canonical evaluation papers for Phase 1 and beyond."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    manifest_version: str
    generated_at: str
    target_domain: str
    description: str
    page_limit_rule: dict[str, Any]
    papers: list[EvaluationPaperEntry]

    def get_by_arxiv_id(self, arxiv_id: str) -> EvaluationPaperEntry | None:
        for paper in self.papers:
            if paper.arxiv_id == arxiv_id:
                return paper
        return None

    @property
    def initial_targets(self) -> list[EvaluationPaperEntry]:
        return [p for p in self.papers if p.initial_study_target]

    @property
    def compliant_papers(self) -> list[EvaluationPaperEntry]:
        return [p for p in self.papers if not p.page_limit_check.exceeds_limit]

    @property
    def pending_decision_papers(self) -> list[EvaluationPaperEntry]:
        return [p for p in self.papers if p.page_limit_check.exceeds_limit]


def load_manifest(path: Path | None = None) -> EvaluationManifest:
    target_path = path or DEFAULT_MANIFEST_PATH
    content = json.loads(target_path.read_text(encoding="utf-8"))
    return EvaluationManifest.model_validate(content)
