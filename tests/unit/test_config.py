from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_review.config import ROOT, Settings
from paper_review.llm.provider import ROUTING


def test_defaults_and_explicit_environment(monkeypatch):
    monkeypatch.delenv("PAPER_REVIEW_DATABASE_PATH", raising=False)
    monkeypatch.delenv("PAPER_REVIEW_PROVIDER", raising=False)
    monkeypatch.setenv("PAPER_REVIEW_LEASE_SECONDS", "10")
    monkeypatch.setenv("PAPER_REVIEW_INITIAL_BUDGET_USD", "0.5")
    settings = Settings.from_environment()
    assert settings.lease_seconds == 10
    assert settings.initial_budget_usd == 0.5
    assert settings.provider == "fake"
    assert settings.database_path == ROOT / "paper-review.db"
    path = ROOT / ".work/config-test.db"
    monkeypatch.setenv("PAPER_REVIEW_DATABASE_PATH", str(path))
    assert Settings.from_environment().database_path == path


@pytest.mark.parametrize(
    "values",
    [
        {"lease_seconds": 0},
        {"poll_seconds": -1},
        {"initial_budget_usd": float("nan")},
        {"detail_budget_usd": float("inf")},
        {"provider": "unknown"},
        {"database_path": Path("/tmp/outside.db")},
        {"database_path": ROOT / ".env"},
    ],
)
def test_invalid_config(values):
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_model_routing():
    assert ROUTING["reasoning"].model == "gpt-6.1-sol"
    assert ROUTING["contribution_relevance"].model == "gpt-5.6-luna"
    assert ROUTING["concept_necessity"].model == "gpt-6.1-sol"
    assert ROUTING["knowledge_detail"].model == "gpt-6.1-terra"
    assert ROUTING["knowledge_detail_review"].model == "gpt-6.1-sol"
    assert ROUTING["reasoning"].max_output_tokens == 8000
    assert ROUTING["reasoning"].timeout == 90


def test_real_provider_and_gates_are_explicit(monkeypatch):
    monkeypatch.setenv("PAPER_REVIEW_PROVIDER", "openai")
    monkeypatch.setenv("PAPER_REVIEW_PIPELINE_MODE", "full")
    value = Settings.from_environment()
    assert value.provider == "openai" and value.pipeline_mode == "full"
    assert not value.specter2_verified and not value.stage1_audited
    assert value.adjustment_policy == "identity-v1"
