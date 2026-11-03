"""Offline send-time checks, including independently spawned worker processes."""

import asyncio
import json
import multiprocessing
import time

import httpx
import pytest

from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.s2_request_gate import (
    S2GateStopped,
    SharedS2RequestGate,
)
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SemanticScholarClient,
    SemanticScholarHttpError,
    SemanticScholarRateLimitError,
)


def _process_sender(path, queue, start):
    async def run():
        gate = SharedS2RequestGate(path, stop_path=None, interval=0.12)

        async def handle(request):
            queue.put(("send", time.monotonic()))
            await asyncio.sleep(0.03)
            return httpx.Response(200, json={"paperId": "p", "title": "Paper"})

        queue.put(("ready", 0))
        await asyncio.to_thread(start.wait, 10)
        async with SemanticScholarClient(
            api_key="dummy", transport=httpx.MockTransport(handle),
            rate_limiter=RateLimiter(0), request_gate=gate,
        ) as client:
            await client.get_paper("p")

    asyncio.run(run())


def test_independent_processes_share_send_intervals(settings):
    path = settings.database_path.parent / "gate.json"
    ctx = multiprocessing.get_context("spawn")
    queue = ctx.Queue()
    start = ctx.Event()
    processes = [ctx.Process(target=_process_sender, args=(path, queue, start)) for _ in range(3)]
    try:
        for process in processes:
            process.start()
        assert [queue.get(timeout=15)[0] for _ in processes] == ["ready"] * 3
        start.set()
        sends = sorted(queue.get(timeout=15)[1] for _ in processes)
        assert all(b - a >= 0.12 for a, b in zip(sends, sends[1:], strict=False))
        for process in processes:
            process.join(10)
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(5)
        queue.close()


def test_default_interval_is_at_least_one_second():
    assert SharedS2RequestGate().interval >= 1


def test_local_zero_limiter_does_not_bypass_default_live_gate(settings, monkeypatch):
    gate = SharedS2RequestGate(
        settings.database_path.parent / "gate.json", stop_path=None, interval=0.04,
    )
    monkeypatch.setattr(
        "paper_review.infrastructure.academic_api.semantic_scholar.default_s2_request_gate",
        lambda: gate,
    )
    sends = []

    class OfflineLiveTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            sends.append(time.monotonic())
            return httpx.Response(200, json={"paperId": "p", "title": "Paper"})

    async def run():
        async def send():
            async with SemanticScholarClient(
                api_key="dummy", transport=OfflineLiveTransport(), rate_limiter=RateLimiter(0),
            ) as client:
                assert client.request_gate is gate
                await client.get_paper("p")

        await asyncio.gather(send(), send(), send())

    asyncio.run(run())
    assert len(sends) == 3
    assert all(b - a >= 0.04 for a, b in zip(sends, sends[1:], strict=False))


@pytest.mark.parametrize("failure", ["500", "transport"])
def test_retry_and_other_endpoints_use_same_gate(settings, failure):
    sends = []
    path = settings.database_path.parent / "gate.json"

    def handler(request):
        sends.append(time.monotonic())
        if len(sends) == 1:
            if failure == "transport":
                raise httpx.ConnectError("offline", request=request)
            return httpx.Response(500)
        return httpx.Response(200, json={"data": []} if "references" in str(request.url)
                              else {"paperId": "p", "title": "Paper"})

    async def run():
        for endpoint in ["paper", "references"]:
            async with SemanticScholarClient(
                api_key="dummy", transport=httpx.MockTransport(handler),
                rate_limiter=RateLimiter(0), backoff_factor=0,
                request_gate=SharedS2RequestGate(path, stop_path=None, interval=0.04),
            ) as client:
                if endpoint == "paper":
                    await client.get_paper("p")
                else:
                    await client.get_references("p")

    asyncio.run(run())
    assert len(sends) == 3
    assert all(b - a >= 0.04 for a, b in zip(sends, sends[1:], strict=False))


def test_429_stops_new_client_and_preserves_existing_circuit(settings):
    path = settings.database_path.parent / "gate.json"
    stop = path.with_name("historical-stop.json")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(429, headers={"Retry-After": "30"})

    async def run():
        for index in range(2):
            async with SemanticScholarClient(
                api_key="dummy", transport=httpx.MockTransport(handler),
                rate_limiter=RateLimiter(0), max_attempts=3,
                request_gate=SharedS2RequestGate(path, stop_path=stop, interval=0),
            ) as client:
                with pytest.raises(SemanticScholarRateLimitError) as caught:
                    await client.get_paper("p")
                assert client.http_attempts == (1 if index == 0 else 0)
                if index == 0:
                    assert caught.value.retry_after == 30
        # Historical stops are read, never erased or overwritten by the gate.
        original = b'{"status":429,"source":"historical"}\n'
        stop.write_bytes(original)
        other = SharedS2RequestGate(path.with_name("other.json"), stop_path=stop, interval=0)
        with pytest.raises(S2GateStopped):
            async with other.request():
                pytest.fail("must not send")
        assert stop.read_bytes() == original

    asyncio.run(run())
    assert len(calls) == 1
    assert json.loads(path.read_text())["stopped"] is True
    assert "dummy" not in path.read_text()


def test_cancellation_releases_lock_with_persistent_cooldown(settings):
    path = settings.database_path.parent / "gate.json"

    async def run():
        entered = asyncio.Event()

        async def owner():
            async with SharedS2RequestGate(path, stop_path=None, interval=0.05).request():
                entered.set()
                await asyncio.Event().wait()

        task = asyncio.create_task(owner())
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        cancelled_at = time.monotonic()
        async with SharedS2RequestGate(path, stop_path=None, interval=0.05).request():
            assert time.monotonic() - cancelled_at >= 0.045

    asyncio.run(run())


def test_redirect_cannot_send_an_unmetered_second_request(settings):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": "https://example.org/other"})

    client = SemanticScholarClient(
        api_key="dummy", transport=httpx.MockTransport(handler), rate_limiter=RateLimiter(0),
        request_gate=SharedS2RequestGate(
            settings.database_path.parent / "gate.json", stop_path=None, interval=0,
        ),
    )
    with pytest.raises(SemanticScholarHttpError, match="redirect refused"):
        client.get_paper_sync("p")
    assert len(calls) == client.http_attempts == 1


def test_cancelled_waiter_does_not_release_owner_lock(settings):
    path = settings.database_path.parent / "gate.json"

    async def run():
        entered = asyncio.Event()

        async def wait():
            async with SharedS2RequestGate(path, stop_path=None, interval=0).request():
                entered.set()

        async with SharedS2RequestGate(path, stop_path=None, interval=0).request():
            waiter = asyncio.create_task(wait())
            await asyncio.sleep(0.01)
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not entered.is_set()
            second = asyncio.create_task(wait())
            await asyncio.sleep(0.01)
            assert not entered.is_set()
        await asyncio.wait_for(second, 1)
        assert entered.is_set()

    asyncio.run(run())


def test_corrupt_state_fails_without_sending(settings):
    path = settings.database_path.parent / "gate.json"
    path.write_text('{"not_before": "broken"}')

    async def run():
        with pytest.raises(ValueError, match="invalid_s2_gate_state"):
            async with SharedS2RequestGate(path, stop_path=None, interval=0).request():
                pytest.fail("must not send with corrupt state")

    asyncio.run(run())
