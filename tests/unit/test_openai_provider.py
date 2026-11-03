"""Responses transport/secret regressions. No real key or network."""

import asyncio
import json
import traceback
from dataclasses import replace

import httpx
import pytest

from paper_review.domain.understanding import Contributions
from paper_review.llm.openai_provider import OpenAIProvider, strict_schema
from paper_review.llm.provider import LLMRequest, ProviderFailure, ProviderTimeout


@pytest.mark.parametrize(("kind", "phase"), [
    (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
    (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
    (httpx.TimeoutException, "unknown"),
])
def test_timeout_phase_is_sanitized_and_transport_does_not_retry(kind, phase):
    calls = []

    def handler(req):
        calls.append(req)
        assert req.headers["X-Client-Request-Id"] == "attempt-uuid"
        raise kind("dummy-secret private paper body", request=req)

    provider = OpenAIProvider(api_key="dummy-secret", transport=httpx.MockTransport(handler))
    with pytest.raises(ProviderTimeout) as caught:
        asyncio.run(provider.generate(replace(request(), client_request_id="attempt-uuid")))
    assert caught.value.phase == phase
    assert len(calls) == 1
    visible = "".join(traceback.format_exception(caught.value))
    assert "dummy-secret" not in visible and "private paper body" not in visible


def request():
    return LLMRequest(
        job_id="j",
        paper_id="p",
        stage_id="s",
        logical_call_id="c",
        task_type="contribution_extraction",
        model="gpt-5-nano",
        prompt_hash="h",
        input_hash="i",
        output_schema=Contributions.model_json_schema(),
        max_input_tokens=8000,
        max_output_tokens=2000,
        attempt_timeout=30,
        task_deadline=90,
        cost_category="initial_analysis",
        remaining_budget=1,
        context_spans=({"span_id": "s", "text": "source"},),
        generation_settings={"instructions": "Only supplied sources"},
    )


def test_structured_responses_use_real_context_and_no_provider_storage():
    def handler(req):
        assert req.headers["Authorization"] == "Bearer dummy-key"
        body = json.loads(req.content)
        assert body["store"] is False and body["model"] == "gpt-5-nano"
        assert "source" in body["input"] and body["text"]["format"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "id": "resp-test",
                "model": "gpt-5-nano",
                "output": [
                    {
                        "content": [
                            {"type": "output_text", "text": '{"problem":"test","contributions":[]}'}
                        ]
                    }
                ],
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    provider = OpenAIProvider(api_key="dummy-key", transport=httpx.MockTransport(handler))
    value = asyncio.run(provider.generate(request()))
    assert value.output["problem"] == "test" and value.input_tokens == 100
    assert "dummy-key" not in repr(provider)


@pytest.mark.parametrize(("body", "code"), [
    ({"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}},
     "output_limit"),
    ({"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
     "incomplete"),
    ({"status": "completed", "output": []}, "invalid_response"),
    ({"status": "failed"}, "response_not_completed"),
])
def test_noncompleted_and_invalid_responses_keep_safe_reason(body, code):
    provider = OpenAIProvider(api_key="dummy", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=body)
    ))
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider.generate(request()))
    assert caught.value.code == code and caught.value.http_status == 200
    assert not caught.value.retryable


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "configuration"),
        (403, "configuration"),
        (404, "configuration"),
        (429, "rate_limit"),
        (500, "unavailable"),
        (400, "invalid_input"),
    ],
)
def test_provider_errors_are_sanitized(status, code):
    provider = OpenAIProvider(
        api_key="dummy-secret",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, text="dummy-secret private paper body")
        ),
    )
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider.generate(request()))
    assert caught.value.code == code
    assert caught.value.http_status == status
    assert "dummy-secret" not in "".join(traceback.format_exception(caught.value))


def test_strict_schema_makes_defaults_explicit_without_changing_original():
    schema = Contributions.model_json_schema()
    converted = strict_schema(schema)
    assert converted["required"] == list(converted["properties"])
    assert converted["additionalProperties"] is False
    assert all(
        v.get("additionalProperties") is False
        for v in converted["$defs"].values()
        if v.get("type") == "object"
    )


def test_retry_after_is_parsed_without_exposing_error_body():
    from paper_review.llm.openai_provider import retry_after_seconds

    assert retry_after_seconds("2.5") == 2.5
    assert retry_after_seconds("nan") is None
    assert retry_after_seconds("invalid") is None
    provider = OpenAIProvider(
        api_key="dummy",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, headers={"Retry-After": "7"}, text="private")
        ),
    )
    with pytest.raises(ProviderFailure) as caught:
        asyncio.run(provider.generate(request()))
    assert caught.value.retry_after == 7
