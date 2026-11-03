"""The explicit recovery window is bounded, durable, and offline in tests."""

import asyncio
import json
import re

import httpx
import pytest

from paper_review.application import s2_reconnect_verification as verification
from paper_review.infrastructure.academic_api.s2_request_gate import SharedS2RequestGate


@pytest.mark.parametrize("failure_at", [None, 1, 2])
def test_bounded_recovery_preserves_stop_and_continues_only_on_success(
    settings, monkeypatch, failure_at,
):
    output = settings.database_path.parent / "reconnect"
    output.mkdir()
    stop = output / "historical.json"
    original = b'{"status":429,"source":"prior"}\n'
    stop.write_bytes(original)
    contract = output / "contract.json"
    contract.write_text('{"status":200,"contract_verified":true}')
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "GATE_PATH", output / "gate.json")
    monkeypatch.setattr(verification.stage1, "RUNTIME_LIMIT", stop)
    monkeypatch.setattr(verification.stage1, "CONTRACT", contract)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy")
    monkeypatch.setattr(
        verification, "SharedS2RequestGate",
        lambda path, stop_path: SharedS2RequestGate(path, stop_path=stop_path, interval=0),
    )
    calls = []
    continued = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "dummy"
        if len(calls) == failure_at:
            return httpx.Response(429)
        ids = json.loads(request.content)["ids"]
        return httpx.Response(200, json=[{
            "paperId": pid if re.fullmatch(r"[a-f0-9]{40}", pid) else "d" * 40,
            "title": "Fixture", "embedding": {"model": "specter_v2", "vector": [1, 2, 3]},
        } for pid in ids])

    async def audit():
        continued.append(True)
        return {"status": "completed", "quality_gate": "held"}

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **_: httpx.MockTransport(handler))
    monkeypatch.setattr(verification, "_lineage_audit", audit)
    (output / "prepared-plan.json").write_text(json.dumps(verification.plan()))
    result = verification.evaluate(160)
    assert len(calls) == result["http_requests"] == (failure_at or 2)
    assert result["status"] == ("stopped" if failure_at else "completed")
    assert bool(continued) == (failure_at is None)
    assert stop.read_bytes() == (output / "historical-stop.json").read_bytes() == original
    assert verification.evaluate(160)["status"] == ("blocked" if failure_at else "completed")
    assert len(calls) == (failure_at or 2)


def test_new_shared_stop_cannot_be_bypassed(settings, monkeypatch):
    output = settings.database_path.parent / "reconnect"
    output.mkdir()
    contract = output / "contract.json"
    contract.write_text('{"status":200,"contract_verified":true}')
    gate = output / "gate.json"
    gate.write_text('{"stopped":true,"not_before":0}')
    monkeypatch.setattr(verification, "OUTPUT", output)
    monkeypatch.setattr(verification, "GATE_PATH", gate)
    monkeypatch.setattr(verification.stage1, "CONTRACT", contract)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy")
    (output / "prepared-plan.json").write_text(json.dumps(verification.plan()))
    assert verification.evaluate(160)["reason"] == "new_shared_gate_stop_requires_review"
    assert verification.evaluate(50)["status"] == "blocked"
    assert not (output / "active.lock").exists()


def test_offline_lineage_audit_preserves_fixed_rankings(settings, monkeypatch):
    case = next(c for c in verification.classification.load_cases() if c["aid"] == "2106.09685")
    output = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", output)
    (output / "2106.09685-embeddings.json.gz").write_bytes(case["fixture"].read_bytes())
    result = asyncio.run(verification._lineage_audit())
    assert result["quality_gate"] == "held"
    assert result["new_llm_calls"] == 0 and result["graph_evaluated"] is False
    assert result["top_k"] == 10
    assert all(r["rank_before"] == r["rank_after"] for r in result["ranking"])
