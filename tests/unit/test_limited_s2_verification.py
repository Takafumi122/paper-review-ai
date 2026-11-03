import asyncio
import hashlib
import json

import httpx
import pytest

from paper_review.application import limited_s2_verification as verification
from paper_review.application.limited_s2_gate import LimitedS2Gate
from paper_review.application.provider_verification import CaptureTransport
from paper_review.infrastructure.academic_api.s2_request_gate import (
    S2GateStopped,
    SharedS2RequestGate,
)


def gate_fixture(settings):
    folder = settings.database_path.parent
    path = folder / "gate.json"
    original = b'{"stopped":true,"not_before":0}\n'
    path.write_bytes(original)
    return path, original, hashlib.sha256(original).hexdigest()


def test_scoped_sends_keep_same_inode_and_stop_ordinary_clients(settings):
    path, original, digest = gate_fixture(settings)
    inode = path.stat().st_ino
    gate = LimitedS2Gate(path, expected_sha256=digest, archive=path.with_suffix(".archive"),
                         window_id="test", interval=0.01)

    async def run():
        async def ordinary():
            async with SharedS2RequestGate(path, stop_path=None).request():
                pytest.fail("ordinary client must never pass the reviewed stop")

        async with gate.request():
            task = asyncio.create_task(ordinary())
            await asyncio.sleep(0)
        with pytest.raises(S2GateStopped):
            await task
        assert json.loads(path.read_text())["stopped"] is True
        async with gate.request():
            pass
        with pytest.raises(S2GateStopped):
            async with gate.request():
                pytest.fail("third request must not send")

    asyncio.run(run())
    assert gate.sent == 2 and path.stat().st_ino == inode
    assert path.with_suffix(".archive").read_bytes() == original
    assert json.loads(path.read_text())["stopped"] is True


def test_fresh_429_or_cancellation_closes_remaining_window(settings):
    path, _, digest = gate_fixture(settings)
    gate = LimitedS2Gate(path, expected_sha256=digest, archive=path.with_suffix(".archive"),
                         window_id="test", interval=0)

    async def run():
        async with gate.request() as handle:
            gate.stop(handle)
        with pytest.raises(S2GateStopped):
            async with gate.request():
                pytest.fail("fresh 429 must not be bypassed")

    asyncio.run(run())
    assert gate.failed and json.loads(path.read_text())["stopped"]


def test_cancellation_restops_before_releasing_lock(settings):
    path, _, digest = gate_fixture(settings)
    gate = LimitedS2Gate(path, expected_sha256=digest, archive=path.with_suffix(".archive"),
                         window_id="test", interval=0)

    async def run():
        entered = asyncio.Event()

        async def send():
            async with gate.request():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(send())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert gate.failed and json.loads(path.read_text())["stopped"]


def test_unreviewed_state_never_reopens(settings):
    path, original, _ = gate_fixture(settings)
    gate = LimitedS2Gate(path, expected_sha256="wrong", archive=path.with_suffix(".archive"),
                         window_id="test", interval=0)

    async def run():
        with pytest.raises(S2GateStopped, match="state_changed"):
            async with gate.request():
                pytest.fail("changed state must not send")

    asyncio.run(run())
    assert gate.sent == 0 and path.read_bytes() == original


@pytest.mark.parametrize("failure", [None, "first429", "second429", "redirect", "timeout",
                                     "missing_vector", "wrong_identity"])
def test_two_request_probe_stops_on_failure_and_preserves_historical_stop(
    settings, monkeypatch, failure,
):
    path, original, digest = gate_fixture(settings)
    folder = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", folder)
    monkeypatch.setattr(verification, "GATE_PATH", path)
    monkeypatch.setattr(verification, "ROOT", folder)
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy")
    monkeypatch.setenv("S2_API_KEY", "dummy")
    historical = folder / "historical.json"
    historical.write_bytes(b'{"status":429}')
    calls = []
    contract = {"dimension": 3, "paper_id": "b"*40, "provider_model": "specter_v2"}
    contract_path = folder / ".work/live-specter2-contract.json"
    contract_path.parent.mkdir()
    contract_path.write_text(json.dumps(contract))

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == "dummy"
        if failure == "timeout":
            raise httpx.ReadTimeout("offline", request=request)
        if failure == "first429" or (failure == "second429" and len(calls) == 2):
            return httpx.Response(429, headers={"Retry-After": "60"})
        if failure == "redirect":
            return httpx.Response(302, headers={"Location": "https://offline.invalid/elsewhere"})
        if len(calls) == 1:
            assert request.method == "GET"
            return httpx.Response(200, json={"paperId": "a"*40, "title": "LoRA"})
        assert request.method == "POST"
        assert json.loads(request.content)["ids"] == verification.IDS
        return httpx.Response(200, json=[{
            "paperId": pid, "title": "Fixture",
            "embedding": None if failure == "missing_vector" else {
                "model": contract["provider_model"], "vector": [0.1]*contract["dimension"],
            },
        } for pid in ["c"*40 if failure == "wrong_identity" else "a"*40, contract["paper_id"]]])

    monkeypatch.setattr(verification, "CaptureTransport",
                        lambda: CaptureTransport(httpx.MockTransport(handler)))
    monkeypatch.setattr(verification, "LimitedS2Gate", lambda *args, **kwargs:
                        LimitedS2Gate(*args, **kwargs, interval=0))
    p = {"sha256": {str(path.relative_to(verification.ROOT)): digest},
         "metadata_fields": "paperId,title",
         "embedding_fields": "paperId,title,embedding.specter_v2",
         "expected_lora_s2_id": "a"*40}
    result, papers = asyncio.run(verification.probe(p))
    assert len(calls) == result["http_requests"] == (
        1 if failure in {"first429", "redirect", "timeout"} else 2
    )
    assert (result["status"] == "completed") == (failure is None)
    assert (papers is not None) == (failure is None)
    assert json.loads(path.read_text())["stopped"] is True
    assert (folder / "shared-stop-before.json").read_bytes() == original
    assert historical.read_bytes() == b'{"status":429}'


@pytest.mark.parametrize("connected", [True, False])
def test_lineage_only_runs_after_success_and_window_cannot_resume(settings, monkeypatch, connected):
    folder = settings.database_path.parent
    monkeypatch.setattr(verification, "OUTPUT", folder)
    historical = folder / "historical.json"
    historical.write_text('{"status":429}')
    monkeypatch.setattr(verification, "HISTORICAL_STOP", historical)
    monkeypatch.setattr(verification, "plan", lambda: {"frozen": True})
    (folder / "prepared-plan.json").write_text('{"frozen":true}')
    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "dummy")
    monkeypatch.setenv("OPENAI_API_KEY", "dummy")
    calls = []

    async def probe(p):
        return {"status": "completed" if connected else "stopped", "http_requests": 2}, (
            {"fixture": True} if connected else None
        )

    async def lineage(papers):
        calls.append(papers)
        return {"status": "completed"}

    monkeypatch.setattr(verification, "probe", probe)
    monkeypatch.setattr(verification, "lineage", lineage)
    assert verification.evaluate(160)["status"] == ("completed" if connected else "stopped")
    assert verification.evaluate(160)["status"] == ("completed" if connected else "blocked")
    assert len(calls) == int(connected)
