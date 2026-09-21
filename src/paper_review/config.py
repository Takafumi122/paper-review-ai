"""Explicit non-secret settings; never load dotenv or provider credentials."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    database_path: Path = ROOT / "paper-review.db"
    lease_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)
    poll_seconds: float = Field(default=1, gt=0, allow_inf_nan=False)
    initial_budget_usd: float = Field(default=1, gt=0, allow_inf_nan=False)
    detail_budget_usd: float = Field(default=0.1, gt=0, allow_inf_nan=False)
    provider: str = "fake"

    @field_validator("database_path")
    @classmethod
    def local_database(cls, value: Path) -> Path:
        path = value.resolve()
        if not path.is_relative_to(ROOT) or path.name.startswith(".env"):
            raise ValueError("database must be inside the workspace")
        if path.suffix != ".db":
            raise ValueError("database must have a .db suffix")
        return path

    @field_validator("provider")
    @classmethod
    def fake_only(cls, value: str) -> str:
        if value != "fake":
            raise ValueError("Phase 0 supports only the fake provider")
        return value

    @classmethod
    def from_environment(cls) -> Settings:
        names = {
            "PAPER_REVIEW_DATABASE_PATH": "database_path",
            "PAPER_REVIEW_LEASE_SECONDS": "lease_seconds",
            "PAPER_REVIEW_POLL_SECONDS": "poll_seconds",
            "PAPER_REVIEW_INITIAL_BUDGET_USD": "initial_budget_usd",
            "PAPER_REVIEW_DETAIL_BUDGET_USD": "detail_budget_usd",
            "PAPER_REVIEW_PROVIDER": "provider",
        }
        return cls.model_validate(
            {field: os.environ[key] for key, field in names.items() if key in os.environ}
        )
