"""One authorized metadata request using the operator service's environment."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path

import httpx

from paper_review.application.limited_s2_gate import LimitedS2Gate
from paper_review.application.provider_verification import CaptureTransport
from paper_review.config import ROOT
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient

OUTPUT = ROOT / "evaluation/reports/s2-new-key-metadata-2026-11-03"
GATE = ROOT / ".work/s2-http-gate.json"


def evaluate(
    *, output: Path = OUTPUT, gate_path: Path = GATE,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict:
    """No retries, redirects, LLM calls, raw response storage or global reopening."""
    if not (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY")):
        return {"status": "blocked", "reason": "s2_key_missing", "http_requests": 0}
    raw = gate_path.read_bytes()
    if json.loads(raw).get("stopped") is not True:
        return {"status": "blocked", "reason": "reviewed_stopped_state_required"}
    output.mkdir(parents=True, exist_ok=True)
    destination = output / "results.json"
    try:
        with destination.open("x") as saved:
            json.dump({"status": "started", "max_http_requests": 1}, saved)
    except FileExistsError:
        return {"status": "blocked", "reason": "window_already_reserved", "http_requests": 0}
    gate = LimitedS2Gate(
        gate_path, expected_sha256=hashlib.sha256(raw).hexdigest(),
        archive=output / "shared-stop-before.json", window_id=output.name,
    )
    capture = CaptureTransport(transport)

    async def run() -> dict:
        error = None
        paper = None
        try:
            async with SemanticScholarClient(
                transport=capture, request_gate=gate, max_attempts=1, max_http_attempts=1,
                timeout_seconds=15,
            ) as client:
                paper = await client.get_paper("ARXIV:2106.09685", fields="paperId,title")
        except Exception as exc:
            error = type(exc).__name__
        return {
            "status": "completed" if paper is not None else "stopped",
            "reason": "metadata_verified" if paper is not None else "request_failed_no_retry",
            "http_status": capture.status, "http_requests": capture.attempts,
            "x_api_key_used": capture.x_api_key_used,
            "response_diagnostics": capture.diagnostics, "error_type": error,
            "fields": "paperId,title", "new_llm_calls": 0,
            "embedding_verified": False, "production_flags_changed": False,
        }

    result = asyncio.run(run())
    destination.write_text(json.dumps(result, indent=2) + "\n")
    return result
