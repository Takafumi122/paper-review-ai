"""Request authentication uses dummy keys only; all HTTP is mocked."""

import asyncio
import logging
import traceback

import httpx
import pytest

from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SemanticScholarClient,
    SemanticScholarError,
)


@pytest.fixture(autouse=True)
def clear_keys(monkeypatch):
    monkeypatch.delenv("SEMANTIC_SCHOLAR_API_KEY", raising=False)
    monkeypatch.delenv("S2_API_KEY", raising=False)


@pytest.mark.parametrize("injected", [False, True])
@pytest.mark.parametrize("key", [None, "dummy-auth-key"])
def test_auth_across_request_paths(injected, key):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers.get("x-api-key") == key
        if injected:
            assert request.headers["X-Custom"] == "preserved"
        if request.url.path.endswith("references"):
            return httpx.Response(200, json={"data": []})
        item = {"paperId": "a" * 40, "title": "Target"}
        if request.method == "POST":
            assert "embedding.specter_v2" in request.url.params["fields"]
            return httpx.Response(200, json=[item])
        return httpx.Response(200, json=item)

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), headers={"X-Custom": "preserved"}
        ) as external:
            before = dict(external.headers)
            async with SemanticScholarClient(
                client=external if injected else None,
                transport=None if injected else httpx.MockTransport(handler),
                api_key=key,
                rate_limiter=RateLimiter(0),
            ) as client:
                await client.get_paper("a" * 40)
                await client.get_references("a" * 40)
                await client.get_specter2_papers(["a" * 40])
            assert dict(external.headers) == before
            assert not external.is_closed

    asyncio.run(run())
    assert len(calls) == 3


@pytest.mark.parametrize(
    "primary,fallback,explicit,expected",
    [
        ("primary-dummy", "fallback-dummy", None, "primary-dummy"),
        (None, "fallback-dummy", None, "fallback-dummy"),
        ("", "fallback-dummy", None, "fallback-dummy"),
        ("primary-dummy", "fallback-dummy", "explicit-dummy", "explicit-dummy"),
        (None, None, None, None),
    ],
)
def test_key_precedence(monkeypatch, primary, fallback, explicit, expected):
    for name, value in [("SEMANTIC_SCHOLAR_API_KEY", primary), ("S2_API_KEY", fallback)]:
        if value is not None:
            monkeypatch.setenv(name, value)
    observed = []

    def handler(request):
        observed.append(request.headers.get("x-api-key"))
        return httpx.Response(200, json={"paperId": "a" * 40, "title": "Target"})

    client = SemanticScholarClient(
        api_key=explicit, transport=httpx.MockTransport(handler), rate_limiter=RateLimiter(0)
    )
    client.get_paper_sync("a" * 40)
    assert observed == [expected]


@pytest.mark.parametrize("configured", [None, "configured-dummy"])
def test_conflicting_caller_key_fails_without_mutation_or_secret_diagnostic(configured):
    async def run():
        async with httpx.AsyncClient(headers={"X-API-Key": "caller-dummy"}) as external:
            before = dict(external.headers)
            async with SemanticScholarClient(
                client=external, api_key=configured, rate_limiter=RateLimiter(0)
            ) as client:
                with pytest.raises(SemanticScholarError) as caught:
                    await client.get_paper("a" * 40)
                assert str(caught.value) == "conflicting_semantic_scholar_authentication"
                assert client.http_attempts == 0
                assert "caller-dummy" not in repr(client)
            assert dict(external.headers) == before

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["http", "transport", "retry"])
def test_retry_auth_and_sanitized_errors(failure, caplog):
    key = "secret-dummy-not-real"
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["x-api-key"] == key
        if failure == "transport":
            raise httpx.ConnectError(key, request=request)
        if failure == "http":
            return httpx.Response(400, text=key)
        if len(calls) == 1:
            return httpx.Response(503, text=key)
        return httpx.Response(200, json=[{"paperId": "a" * 40, "title": "Target"}])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as external:
            async with SemanticScholarClient(
                client=external,
                api_key=key,
                rate_limiter=RateLimiter(0),
                max_attempts=2,
                backoff_factor=0,
            ) as client:
                assert key not in repr(client)
                if failure == "retry":
                    await client.get_specter2_papers(["a" * 40])
                else:
                    with pytest.raises(SemanticScholarError) as caught:
                        await client.get_specter2_papers(["a" * 40])
                    diagnostic = "".join(traceback.format_exception(caught.value))
                    assert key not in diagnostic

    with caplog.at_level(logging.DEBUG):
        asyncio.run(run())
    assert key not in caplog.text
    assert len(calls) == (1 if failure == "http" else 2)


def test_matching_caller_key_is_allowed_without_mutation():
    def handler(request):
        assert request.headers["x-api-key"] == "same-dummy"
        return httpx.Response(200, json={"paperId": "a" * 40, "title": "Target"})

    async def run():
        async with httpx.AsyncClient(
            headers={"x-api-key": "same-dummy"}, transport=httpx.MockTransport(handler)
        ) as external:
            before = dict(external.headers)
            async with SemanticScholarClient(
                client=external, api_key="same-dummy", rate_limiter=RateLimiter(0)
            ) as client:
                await client.get_paper("a" * 40)
            assert dict(external.headers) == before

    asyncio.run(run())
