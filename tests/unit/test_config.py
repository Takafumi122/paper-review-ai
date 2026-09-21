from pathlib import Path

import pytest
from pydantic import ValidationError

from paper_review.config import ROOT, Settings
from paper_review.llm.provider import ROUTING


def test_defaults_and_explicit_environment(monkeypatch):
    monkeypatch.setenv("PAPER_REVIEW_LEASE_SECONDS", "10")
    monkeypatch.setenv("PAPER_REVIEW_INITIAL_BUDGET_USD", "0.5")
    settings = Settings.from_environment()
    assert settings.lease_seconds == 10
    assert settings.initial_budget_usd == 0.5
    assert settings.provider == "fake"
    assert settings.database_path == ROOT / "paper-review.db"


@pytest.mark.parametrize(
    "values",
    [
        {"lease_seconds": 0},
        {"poll_seconds": -1},
        {"initial_budget_usd": float("nan")},
        {"detail_budget_usd": float("inf")},
        {"provider": "openai"},
        {"database_path": Path("/tmp/outside.db")},
        {"database_path": ROOT / ".env"},
    ],
)
def test_invalid_config(values):
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


def test_model_routing():
    assert ROUTING["reasoning"].model == "gpt-5.6-luna"
    assert ROUTING["contribution_relevance"].model == "gpt-5.6-luna"
    assert ROUTING["concept_necessity"].model == "gpt-5-nano"
    assert ROUTING["knowledge_detail"].model == "gpt-5-nano"
    assert ROUTING["reasoning"].max_output_tokens == 8000
    assert ROUTING["reasoning"].timeout == 90
