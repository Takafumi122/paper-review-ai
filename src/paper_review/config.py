"""Explicit non-secret settings; never load dotenv or provider credentials."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]
LINEAGE_VALIDATION_VERSION = "edge-grounding-v3"


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    database_path: Path = ROOT / "paper-review.db"
    lease_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    poll_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    initial_budget_usd: float = Field(default=1, gt=0, allow_inf_nan=False)
    detail_budget_usd: float = Field(default=0.1, gt=0, allow_inf_nan=False)
    provider: Literal["fake", "openai"] = "fake"
    pipeline_mode: Literal["phase0_fake", "phase1", "full"] = "phase0_fake"
    adjustment_policy: Literal["identity-v1"] = "identity-v1"
    reading_only: bool = False
    specter2_verified: bool = False
    stage1_audited: bool = False
    academic_backend: Literal["semantic_scholar", "openalex_local"] = "semantic_scholar"
    local_specter2_manifest: Path | None = None
    openalex_local_verified: bool = False
    openalex_local_audited: bool = False
    max_queued_jobs: int = Field(default=10, ge=1, le=50)
    max_daily_jobs: int = Field(default=25, ge=1, le=1000)

    @field_validator("local_specter2_manifest")
    @classmethod
    def local_model_manifest(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        path = value.resolve()
        if (not path.is_relative_to((ROOT / ".work").resolve()) or path.suffix != ".json"
                or path.name.startswith(".env")):
            raise ValueError("model manifest must be a workspace .work JSON file")
        return path

    def retrieval_configuration(self) -> dict[str, str]:
        if self.reading_only:
            return {"backend": "reading_only"}
        if self.academic_backend == "semantic_scholar":
            return {"backend": self.academic_backend}
        from paper_review.infrastructure.local_specter2 import bundle_identity

        return {"backend": self.academic_backend, "version": "openalex-local-v7",
                "embedding_model": bundle_identity(self.local_specter2_manifest)}

    def analysis_fingerprint(self, revision: str) -> str:
        version = ("fake-pipeline-v1" if self.pipeline_mode == "phase0_fake" else
                   f"{self.pipeline_mode}-pipeline-v44" if self.pipeline_mode == "full" else
                   f"{self.pipeline_mode}-pipeline-v1")
        if self.reading_only:
            version += "|reading-only-v1"
        elif self.pipeline_mode == "full" and self.academic_backend == "openalex_local":
            version += "|" + json.dumps(self.retrieval_configuration(), sort_keys=True)
        return hashlib.sha256(f"{revision}|{version}".encode()).hexdigest()

    @field_validator("database_path")
    @classmethod
    def local_database(cls, value: Path) -> Path:
        path = value.resolve()
        if not path.is_relative_to(ROOT) or path.name.startswith(".env"):
            raise ValueError("database must be inside the workspace")
        if path.suffix != ".db":
            raise ValueError("database must have a .db suffix")
        return path

    @classmethod
    def from_environment(cls) -> Settings:
        names = {
            "PAPER_REVIEW_DATABASE_PATH": "database_path",
            "PAPER_REVIEW_LEASE_SECONDS": "lease_seconds",
            "PAPER_REVIEW_POLL_SECONDS": "poll_seconds",
            "PAPER_REVIEW_INITIAL_BUDGET_USD": "initial_budget_usd",
            "PAPER_REVIEW_DETAIL_BUDGET_USD": "detail_budget_usd",
            "PAPER_REVIEW_PROVIDER": "provider",
            "PAPER_REVIEW_PIPELINE_MODE": "pipeline_mode",
            "PAPER_REVIEW_ADJUSTMENT_POLICY": "adjustment_policy",
            "PAPER_REVIEW_READING_ONLY": "reading_only",
            "PAPER_REVIEW_SPECTER2_VERIFIED": "specter2_verified",
            "PAPER_REVIEW_STAGE1_AUDITED": "stage1_audited",
            "PAPER_REVIEW_ACADEMIC_BACKEND": "academic_backend",
            "PAPER_REVIEW_LOCAL_SPECTER2_MANIFEST": "local_specter2_manifest",
            "PAPER_REVIEW_OPENALEX_LOCAL_VERIFIED": "openalex_local_verified",
            "PAPER_REVIEW_OPENALEX_LOCAL_AUDITED": "openalex_local_audited",
            "PAPER_REVIEW_MAX_QUEUED_JOBS": "max_queued_jobs",
            "PAPER_REVIEW_MAX_DAILY_JOBS": "max_daily_jobs",
        }
        return cls.model_validate(
            {field: os.environ[key] for key, field in names.items() if key in os.environ}
        )
