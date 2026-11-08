import json

import httpx
import pytest

from paper_review.application.s2_key_probe import evaluate


@pytest.mark.parametrize("status", [200, 301, 401, 429, 503, None])
def test_one_request_and_persistent_stop(settings, monkeypatch, status):
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy-key")
    folder = settings.database_path.parent
    gate = folder / "gate.json"
    original = b'{"stopped":true,"not_before":0}'
    gate.write_bytes(original)
    inode = gate.stat().st_ino
    calls = []

    def respond(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "dummy-key"
        assert request.method == "GET"
        assert request.url.params["fields"] == "paperId,title"
        if status is None:
            raise httpx.ReadTimeout("dummy", request=request)
        return httpx.Response(status, json={"paperId": "a" * 40, "title": "LoRA"})

    output = folder / "report"
    result = evaluate(output=output, gate_path=gate, transport=httpx.MockTransport(respond))
    assert len(calls) == result["http_requests"] == 1
    assert result["status"] == ("completed" if status == 200 else "stopped")
    assert result["http_status"] == status
    assert result["new_llm_calls"] == 0
    assert json.loads(gate.read_text())["stopped"] is True
    assert gate.stat().st_ino == inode
    assert (output / "shared-stop-before.json").read_bytes() == original
    assert "dummy-key" not in (output / "results.json").read_text()
    assert evaluate(output=output, gate_path=gate)["reason"] == "window_already_reserved"
    assert len(calls) == 1


def test_no_key_does_not_reserve_or_send(settings, monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)
    folder = settings.database_path.parent
    result = evaluate(output=folder / "report", gate_path=folder / "absent.json")
    assert result["reason"] == "s2_key_missing"
    assert not (folder / "report").exists()
