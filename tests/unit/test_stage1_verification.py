"""Operator verification controls with dummy credentials and mocked HTTP only."""

import json
import re

import httpx
import pytest

from paper_review.application import stage1_verification as verification


@pytest.mark.parametrize("failure_at", [None, 1, 2])
@pytest.mark.parametrize("preexisting_stop", [False, True])
def test_fixed_batches_signed_bounded_persisted_and_stop_on_failure(
    settings, monkeypatch, failure_at, preexisting_stop
):
    output = settings.database_path.parent / "operator-evaluation"
    contract = settings.database_path.parent / "contract.json"
    contract.write_text(json.dumps({"status": 200, "contract_verified": True}))
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "CONTRACT", contract)
    circuit = settings.database_path.parent / "rate-limited.json"
    monkeypatch.setattr(verification, "RUNTIME_LIMIT", circuit)
    prior = b'{"status":429,"source":"historical-window"}\n'
    if preexisting_stop:
        circuit.write_bytes(prior)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy-key")
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "dummy-key"
        assert request.method == "POST"
        assert request.url.path.endswith("/paper/batch")
        assert "embedding.specter_v2" in request.url.params["fields"]
        if len(calls) == failure_at:
            return httpx.Response(429, json={"message": "limited"})
        ids = json.loads(request.content)["ids"]
        assert len(ids) <= 100
        return httpx.Response(
            200,
            json=[
                {
                    "paperId": pid if re.fullmatch(r"[a-f0-9]{40}", pid) else "d" * 40,
                    "title": "Fixture",
                    "embedding": {"model": "specter_v2", "vector": [1.0, 2.0, 3.0]},
                }
                for pid in ids
            ],
        )

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: httpx.MockTransport(handler))
    result = verification.verify_stage1_once()
    assert result["http_requests"] == (failure_at or 2)
    assert result["status"] == ("stopped" if failure_at else "completed")
    assert verification.verify_stage1_once() == result
    assert len(calls) == (failure_at or 2)
    assert circuit.exists() == (preexisting_stop or failure_at is not None)
    if preexisting_stop:
        assert circuit.read_bytes() == prior
        assert (output / "prior-runtime-stop.json").read_bytes() == prior
    elif failure_at:
        assert json.loads(circuit.read_text())["status"] == 429
    assert all(b"dummy-key" not in p.read_bytes() for p in output.iterdir())


def test_unverified_contract_never_sends_or_reserves(settings, monkeypatch):
    output = settings.database_path.parent / "blocked-evaluation"
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "CONTRACT", settings.database_path.parent / "missing.json")
    assert verification.verify_stage1_once()["status"] == "blocked"
    assert not output.exists()


def test_interrupted_window_never_resumes_or_clears_existing_stop(settings, monkeypatch):
    output = settings.database_path.parent / "interrupted"
    output.mkdir()
    reserved = {"status": "started", "request_limit": 2}
    (output / "run.json").write_text(json.dumps(reserved))
    circuit = settings.database_path.parent / "stop.json"
    original = b'{"status":429,"source":"prior-window"}\n'
    circuit.write_bytes(original)
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "RUNTIME_LIMIT", circuit)
    assert verification.verify_stage1_once() == reserved
    assert circuit.read_bytes() == original
