import asyncio
import json

import httpx
import pytest

from paper_review.application.provider_verification import CaptureTransport, response_diagnostics


def test_allowlisted_metadata_excludes_secrets_and_raw_error_body():
    response = httpx.Response(429, headers={
        "Date": "Tue, 03 Nov 2026 01:00:00 GMT", "Retry-After": "60",
        "x-request-id": "325150f9-b35c-487d-b88e-a460518974dc",
        "x-api-key": "dummy-private-value", "set-cookie": "dummy-private-value",
        "x-ratelimit-remaining": "dummy-private-value",
    }, json={"error": "dummy-private-value"})
    metadata = response_diagnostics(response)
    assert metadata["status"] == 429
    assert metadata["server_date"] == "2026-11-03T01:00:00+00:00"
    assert metadata["retry_after_seconds"] == 60
    assert metadata["request_ids"] == {"x-request-id": "325150f9-b35c-487d-b88e-a460518974dc"}
    assert "dummy-private-value" not in json.dumps(metadata)


@pytest.mark.parametrize("value", ["nan", "inf", "-2", "private-value", ""])
def test_invalid_retry_headers_are_unknown_not_zero(value):
    metadata = response_diagnostics(httpx.Response(429, headers={
        "Retry-After": value, "Date": "invalid", "x-request-id": "private-value",
    }))
    assert metadata["retry_after_seconds"] is None
    assert metadata["retry_after_date"] is None
    assert metadata["server_date"] is None
    assert metadata["request_ids"] == {}


def test_http_date_retry_after_and_one_transport_attempt():
    async def run():
        sent = []

        def handler(request):
            sent.append(request)
            return httpx.Response(429, headers={"Retry-After": "Tue, 03 Nov 2026 01:01:00 GMT"})

        capture = CaptureTransport(httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=capture) as client:
            await client.get("https://offline.invalid/probe")
            with pytest.raises(httpx.ConnectError, match="probe_budget_exhausted"):
                await client.get("https://offline.invalid/probe")
        assert len(sent) == capture.attempts == 1
        assert capture.diagnostics is not None
        assert capture.diagnostics["retry_after_date"] == "2026-11-03T01:01:00+00:00"

    asyncio.run(run())
