"""Operator-only, persistent follow-up window for frozen public inventories."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from paper_review.application.provider_verification import CaptureTransport
from paper_review.application.stage1_audit import EvaluationArtifact, audit_stage1
from paper_review.application.stage1_shortlist import build_stage1_shortlist
from paper_review.config import ROOT
from paper_review.domain.models import CandidatePaper, CandidateSet, Paper
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.s2_request_gate import SharedS2RequestGate
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient

# New explicitly requested window; never reuse or overwrite the October results.
OUTPUT = ROOT / "evaluation/reports/stage1-live-2026-11-01"
INPUT = ROOT / "evaluation/reports/candidate-inventory"
CONTRACT = ROOT / ".work/live-specter2-contract.json"
RUNTIME_LIMIT = ROOT / ".work/s2-rate-limited.json"
TARGETS = ("2106.09685", "2210.03629")
_LOCK = threading.Lock()


def _summary(candidate: CandidatePaper) -> dict[str, Any]:
    return {
        "paper_id": candidate.paper_id,
        "title": candidate.title,
        "eligibility": candidate.eligibility_status,
        "embedding_available": candidate.embedding is not None,
        "embedding_model": candidate.embedding_model,
        "dimension": len(candidate.embedding) if candidate.embedding is not None else None,
        "embedding_provenance": candidate.embedding_provenance,
    }


def verify_stage1_once() -> dict[str, Any]:
    """No arbitrary input, search, retries, LLM calls or runtime gate changes.

    Reserve before sending; an interrupted run requires explicit operator review,
    never automatic resumption. A failed batch stops all remaining requests.
    """
    with _LOCK:
        marker = OUTPUT / "run.json"
        if marker.exists():
            return json.loads(marker.read_text())
        contract = json.loads(CONTRACT.read_text()) if CONTRACT.exists() else {}
        if not contract.get("contract_verified") or contract.get("status") != 200:
            return {"status": "blocked", "reason": "verified_contract_required"}
        OUTPUT.mkdir(parents=True, exist_ok=True)
        try:
            with marker.open("x") as reservation:
                reservation.write(
                    json.dumps({"status": "started", "request_limit": len(TARGETS)}) + "\n"
                )
        except FileExistsError:
            # A concurrent process already reserved the window. Never resend.
            return json.loads(marker.read_text())
        if RUNTIME_LIMIT.exists():
            (OUTPUT / "prior-runtime-stop.json").write_bytes(RUNTIME_LIMIT.read_bytes())
        result = asyncio.run(_evaluate())
        marker.write_text(json.dumps(result, indent=2) + "\n")
        return result


async def _evaluate(
    *, output: Path | None = None, targets: tuple[str, ...] | None = None,
    request_gate: SharedS2RequestGate | None = None,
) -> dict[str, Any]:
    output = OUTPUT if output is None else output
    reports = []
    requests = 0
    limiter = RateLimiter(1.0)
    probes = json.loads((INPUT / "diagnostic-probes-results.json").read_text())
    for aid in (TARGETS if targets is None else targets):
        path = INPUT / f"{aid}-result.json"
        raw = path.read_bytes()
        source = json.loads(raw)
        target = Paper.model_validate(source["target"])
        inventory = CandidateSet.model_validate(source["inventory"])
        capture = CaptureTransport()
        async with SemanticScholarClient(
            transport=capture, max_attempts=1, max_http_attempts=1, rate_limiter=limiter,
            request_gate=request_gate,
        ) as client:
            if not client.api_key:
                return {"status": "key_missing", "http_requests": requests, "reports": reports}
            analysis = await build_stage1_shortlist(target, inventory, client)
        requests += capture.attempts
        if capture.status != 200:
            if capture.status == 429 and not RUNTIME_LIMIT.exists():
                RUNTIME_LIMIT.parent.mkdir(parents=True, exist_ok=True)
                RUNTIME_LIMIT.write_text(
                    json.dumps({"status": 429, "source": "stage1_verification"}) + "\n"
                )
            return {
                "status": "stopped",
                "http_status": capture.status,
                "http_requests": requests,
                "response_diagnostics": capture.diagnostics,
                "reports": reports,
            }
        # Public embedding response only: no credential headers or paper bodies.
        fixture = gzip.compress(json.dumps(capture.payload, allow_nan=False).encode(), mtime=0)
        (output / f"{aid}-embeddings.json.gz").write_bytes(fixture)
        ranking, target, inventory = analysis.ranking, analysis.target, analysis.inventory
        artifact = {
            "target": target.title,
            "statistics": {
                "target_embedding_available": target.embedding is not None,
                "target_model": target.embedding_model,
                "target_dimension": len(target.embedding) if target.embedding is not None else None,
                "target_embedding_provenance": target.embedding_provenance,
                "http_statuses": [capture.status],
                "response_diagnostics": capture.diagnostics,
            },
            "inventory_sha256": hashlib.sha256(raw).hexdigest(),
            "fixture_sha256": hashlib.sha256(fixture).hexdigest(),
            "scoring_version": ranking.scoring_version,
            "selection_policy_version": ranking.selection_policy_version,
            "shortlist_limit": ranking.shortlist_limit,
            "ranking": [
                dict(rank=r.rank, **_summary(r.candidate), score=r.score.model_dump())
                for r in ranking.ranked_candidates
            ],
            "unranked": [
                dict(rank=r.rank, **_summary(r.candidate), score=r.score.model_dump())
                for r in ranking.unranked_candidates
            ],
            "probes": [
                {"probe": p["probe"], "paper_id": p["identifiers"]["semantic_scholar"]}
                for p in probes
                if p["target"] == aid
            ],
            "candidate_embedding_outcomes": [_summary(c) for c in inventory.candidates],
            "evidence": {"data_origin": "live_provider", "provider_contract_verified": True},
        }
        encoded = json.dumps(artifact, indent=2, allow_nan=False).encode()
        (output / f"{aid}-result.json").write_bytes(encoded)
        audit = audit_stage1(
            EvaluationArtifact.model_validate(artifact),
            input_sha256=hashlib.sha256(encoded).hexdigest(),
        )
        (output / f"{aid}-audit.json").write_text(json.dumps(audit, indent=2) + "\n")
        reports.append(
            {
                "target": aid,
                "gate": audit["gate"],
                "counts": audit["counts"],
                "embeddings": audit["embeddings"],
            }
        )
        # Even HTTP 200 cannot authorize later batches when the model contract failed.
        if audit["gate"] == "NOT EVALUABLE":
            return {"status": "stopped", "http_requests": requests, "reports": reports}
    return {"status": "completed", "http_requests": requests, "reports": reports}
