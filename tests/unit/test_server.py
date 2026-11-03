"""Startup mode guards with dummy credentials and no processes or network."""

import sys

import pytest

from paper_review import server


def test_reading_only_start_needs_no_s2_key(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["server", "--reading-only"])
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    monkeypatch.setenv("PAPER_REVIEW_READING_ONLY", "false")
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    monkeypatch.setattr(server.command, "upgrade", lambda *args: None)
    events = []

    class Process:
        def __init__(self, *args, **kwargs):
            events.append("worker_started")

        def terminate(self):
            events.append("worker_stopped")

        def wait(self, **kwargs):
            pass

    monkeypatch.setattr(server.subprocess, "Popen", Process)
    monkeypatch.setattr(server.uvicorn, "run", lambda *args, **kwargs: events.append("api_started"))
    server.main()
    assert server.os.environ["PAPER_REVIEW_READING_ONLY"] == "true"
    assert events == ["worker_started", "api_started", "worker_stopped"]


@pytest.mark.parametrize("args", [[], ["--reading-only", "--host", "0.0.0.0"]])
def test_missing_required_key_or_public_token_refuses_start(monkeypatch, args):
    monkeypatch.setattr(sys, "argv", ["server", *args])
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    for name in ("SEMANTIC_SCHOLAR_API_KEY", "S2_API_KEY", "PAPER_REVIEW_ACCESS_TOKEN",
                 "PAPER_REVIEW_READING_ONLY"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit):
        server.main()
