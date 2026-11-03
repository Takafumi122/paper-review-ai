"""OpenAI Responses adapter. No retries here: the persistent gateway owns retries."""

from __future__ import annotations

import json
import math
import os
import time
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from paper_review.llm.provider import LLMRequest, ProviderFailure, ProviderResponse, ProviderTimeout


def strict_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Make Pydantic's optional/default fields explicit for strict Structured Outputs."""
    result = dict(schema)
    if result.get("type") == "object":
        properties = result.get("properties", {})
        result["additionalProperties"] = False
        result["required"] = list(properties)
    return {
        k: strict_schema(v)
        if isinstance(v, dict)
        else [strict_schema(item) if isinstance(item, dict) else item for item in v]
        if isinstance(v, list)
        else v
        for k, v in result.items()
        if k != "default"
    }


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, seconds) if math.isfinite(seconds) else None


class OpenAIProvider:
    """Key is read only at construction; never included in repr or error messages."""

    def __init__(
        self, *, api_key: str | None = None, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._key = api_key or os.environ.get("OPENAI_API_KEY")
        self._transport = transport

    def __repr__(self) -> str:
        return "OpenAIProvider(credentials=redacted)"

    async def generate(self, request: LLMRequest) -> ProviderResponse:
        if not self._key:
            raise ProviderFailure("configuration")
        instructions = str(request.generation_settings.get("instructions", ""))
        body = {
            "model": request.model,
            "store": False,
            "instructions": instructions,
            "input": json.dumps(
                {"sources": request.context_spans, "repair_error": request.repair_error},
                ensure_ascii=False,
            ),
            "max_output_tokens": request.max_output_tokens,
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "paper_understanding",
                    "strict": True,
                    "schema": strict_schema(request.output_schema),
                }
            },
        }
        headers = {"Authorization": f"Bearer {self._key}"}
        if request.client_request_id:
            headers["X-Client-Request-Id"] = request.client_request_id
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=request.attempt_timeout,
                headers=headers,
            ) as client:
                response = await client.post("https://api.openai.com/v1/responses", json=body)
        except httpx.TimeoutException as exc:
            phase = next((name for kind, name in (
                (httpx.ConnectTimeout, "connect"), (httpx.ReadTimeout, "read"),
                (httpx.WriteTimeout, "write"), (httpx.PoolTimeout, "pool"),
            ) if isinstance(exc, kind)), "unknown")
            raise ProviderTimeout(phase) from None
        except httpx.TransportError:
            raise ProviderFailure("unavailable", retryable=True) from None
        if response.status_code in {401, 403, 404}:
            raise ProviderFailure("configuration", http_status=response.status_code)
        if response.status_code == 429:
            raise ProviderFailure(
                "rate_limit",
                retryable=True,
                retry_after=retry_after_seconds(response.headers.get("retry-after")),
                http_status=response.status_code,
            )
        if response.status_code >= 500:
            raise ProviderFailure(
                "unavailable",
                retryable=True,
                retry_after=retry_after_seconds(response.headers.get("retry-after")),
                http_status=response.status_code,
            )
        if response.status_code != 200:
            raise ProviderFailure("invalid_input", http_status=response.status_code)
        try:
            data = response.json()
            output = data.get("output", [])
            contents = [c for item in output for c in item.get("content", [])]
            if any(c.get("type") == "refusal" for c in contents):
                raise ProviderFailure("refusal")
            if data.get("status") != "completed":
                reason = (data.get("incomplete_details") or {}).get("reason")
                code = (
                    "response_not_completed" if data.get("status") != "incomplete"
                    else "output_limit" if reason == "max_output_tokens" else "incomplete"
                )
                raise ProviderFailure(code, http_status=response.status_code)
            text = "".join(c.get("text", "") for c in contents if c.get("type") == "output_text")
            usage = data["usage"]
            return ProviderResponse(
                output=json.loads(text),
                request_id=data["id"],
                returned_model=data["model"],
                input_tokens=usage["input_tokens"],
                output_tokens=usage["output_tokens"],
                cached_input_tokens=usage.get("input_tokens_details", {}).get("cached_tokens"),
                reasoning_tokens=usage.get("output_tokens_details", {}).get("reasoning_tokens"),
            )
        except (KeyError, ValueError, TypeError, AttributeError):
            raise ProviderFailure("invalid_response", http_status=response.status_code) from None
