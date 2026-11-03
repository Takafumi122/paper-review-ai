"""One-shot authenticated S2 contract probe; safe diagnostics, local operator only."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import math
import threading
import uuid
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from paper_review.config import ROOT
from paper_review.infrastructure.academic_api.semantic_scholar import (
    SemanticScholarClient,
    SemanticScholarError,
)

_LOCK = threading.Lock()
RESULT = ROOT / ".work/live-specter2-contract.json"


def response_diagnostics(response: httpx.Response) -> dict:
    """Strictly typed response metadata; never copy arbitrary headers or bodies."""
    dates = {}
    for name in ("date", "retry-after"):
        raw = response.headers.get(name, "")
        try:
            value = parsedate_to_datetime(raw)
            if value.tzinfo is not None:
                dates[name] = value.isoformat()
        except (ValueError, TypeError, OverflowError):
            pass
    seconds = None
    try:
        raw_seconds = float(response.headers.get("retry-after", ""))
        if math.isfinite(raw_seconds) and raw_seconds >= 0:
            seconds = raw_seconds
    except ValueError:
        pass
    request_ids = {}
    for name in ("x-request-id", "x-amzn-requestid"):
        try:
            request_ids[name] = str(uuid.UUID(response.headers.get(name, "")))
        except ValueError:
            pass
    return {"status": response.status_code, "server_date": dates.get("date"),
            "retry_after_seconds": seconds, "retry_after_date": dates.get("retry-after"),
            "request_ids": request_ids}


class CaptureTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)
        self.status: int | None = None
        self.diagnostics: dict | None = None
        self.payload: Any = None
        self.attempts = 0
        self.x_api_key_used = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.attempts:
            raise httpx.ConnectError("probe_budget_exhausted", request=request)
        self.attempts += 1
        self.x_api_key_used = bool(request.headers.get("x-api-key"))
        response = await self.inner.handle_async_request(request)
        await response.aread()
        self.status = response.status_code
        self.diagnostics = response_diagnostics(response)
        try:
            self.payload = response.json()
        except ValueError:
            pass
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def verify_once() -> dict:
    with _LOCK:
        if RESULT.exists():
            return json.loads(RESULT.read_text())
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        # Reserve the one-shot probe before sending, including across restarts.
        RESULT.write_text(json.dumps({"status": "started", "contract_verified": False}))

        async def run():
            capture = CaptureTransport()
            async with SemanticScholarClient(
                transport=capture, max_attempts=1, max_http_attempts=1
            ) as client:
                if not client.api_key:
                    return {"status": "key_missing", "contract_verified": False, "http_requests": 0}
                try:
                    papers = await client.get_papers(
                        ["ARXIV:1706.03762"], fields="paperId,title,embedding.specter_v2"
                    )
                    paper = papers.get("ARXIV:1706.03762")
                except SemanticScholarError:
                    paper = None
                result = {
                    "status": capture.status,
                    "http_requests": capture.attempts,
                    "contract_verified": False,
                    "x_api_key_used": True,
                    "fields": "paperId,title,embedding.specter_v2",
                    "response_diagnostics": capture.diagnostics,
                }
                data = capture.payload
                if capture.status == 200 and isinstance(data, list) and len(data) == 1:
                    item = data[0] or {}
                    embedding = item.get("embedding") or {}
                    vector = embedding.get("vector")
                    valid = (
                        isinstance(vector, list)
                        and bool(vector)
                        and all(type(v) in (int, float) and math.isfinite(v) for v in vector)
                    )
                    result.update(
                        provider_model=embedding.get("model"),
                        dimension=len(vector) if isinstance(vector, list) else None,
                        paper_id=item.get("paperId"),
                        finite=valid,
                        nonzero=valid and isinstance(vector, list) and any(vector),
                        contract_verified=paper is not None and paper.embedding is not None,
                    )
                    raw = json.dumps(data, allow_nan=False).encode()
                    result["response_sha256"] = hashlib.sha256(raw).hexdigest()
                    (ROOT / ".work/live-specter2-response.json.gz").write_bytes(gzip.compress(raw))
                return result

        result = asyncio.run(run())
        RESULT.write_text(json.dumps(result, indent=2) + "\n")
        return result
