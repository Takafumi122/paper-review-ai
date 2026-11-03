"""Explicit non-secret settings; never load dotenv or provider credentials."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]


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
    specter2_verified: bool = False
    stage1_audited: bool = False
    max_queued_jobs: int = Field(default=10, ge=1, le=50)
    max_daily_jobs: int = Field(default=25, ge=1, le=1000)

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
            "PAPER_REVIEW_SPECTER2_VERIFIED": "specter2_verified",
            "PAPER_REVIEW_STAGE1_AUDITED": "stage1_audited",
            "PAPER_REVIEW_MAX_QUEUED_JOBS": "max_queued_jobs",
            "PAPER_REVIEW_MAX_DAILY_JOBS": "max_daily_jobs",
        }
        return cls.model_validate(
            {field: os.environ[key] for key, field in names.items() if key in os.environ}
        )
