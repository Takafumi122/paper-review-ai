"""Bounded live SPECTER2 evaluation, or offline replay of frozen response artifacts.

Vectors are saved only in compressed evaluation fixtures, never printed or logged.
Existing inventory snapshots and citation/date policies are unchanged.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx
from evaluate_stage1_ranking import TARGET_SPECS

from paper_review.application.stage1_shortlist import build_stage1_shortlist
from paper_review.domain.models import CandidateSet, Paper
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient

ROOT = Path(__file__).resolve().parents[1]
INPUT = ROOT / "evaluation/reports/candidate-inventory"
OUTPUT = ROOT / "evaluation/reports/specter2-integration"


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.inner = httpx.AsyncHTTPTransport()
        self.events: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        event: dict[str, Any] = {
            "method": request.method,
            "url": str(request.url),
            "body": json.loads(request.content),
            "retrieved_at": time.time(),
        }
        self.events.append(event)
        try:
            response = await self.inner.handle_async_request(request)
            await response.aread()
        except httpx.TransportError:
            event["transport_error"] = True
            raise
        event["status"] = response.status_code
        try:
            event["response"] = response.json()
        except ValueError:
            event["invalid_json"] = True
        return response

    async def aclose(self) -> None:
        await self.inner.aclose()


def summarize_candidate(candidate) -> dict[str, Any]:
    return {
        "paper_id": candidate.paper_id,
        "title": candidate.title,
        "eligibility": candidate.eligibility_status,
        "embedding_available": candidate.embedding is not None,
        "embedding_model": candidate.embedding_model,
        "dimension": len(candidate.embedding) if candidate.embedding is not None else None,
        "embedding_provenance": candidate.embedding_provenance,
    }


async def evaluate(*, live: bool) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    stopped = False
    limiter = RateLimiter(1 if live else 0)
    probe_data = json.loads((INPUT / "diagnostic-probes-results.json").read_text())
    for spec in TARGET_SPECS:
        aid = spec["arxiv_id"]
        source_path = INPUT / f"{aid}-result.json"
        source = json.loads(source_path.read_text())
        target = Paper.model_validate(source["target"])
        inventory = CandidateSet.model_validate(source["inventory"])
        fixture = OUTPUT / f"{aid}-responses.json.gz"
        ranking = None
        events: list[dict[str, Any]] = []
        if not live:
            events = json.loads(gzip.decompress(fixture.read_bytes()))
        if stopped:
            for obj in [target, *inventory.candidates]:
                obj.embedding = None
                obj.embedding_model = None
                obj.embedding_provenance = {
                    "status": "unavailable",
                    "missing_reason": "evaluation_stopped_after_429",
                }
            if not live:
                assert not events
            attempts = 0
        else:
            recorder = RecordingTransport() if live else None
            cursor = 0

            def replay(request, recorded=events):
                nonlocal cursor
                event = recorded[cursor]
                cursor += 1
                assert event["method"] == request.method
                assert event["url"] == str(request.url)
                assert event["body"] == json.loads(request.content)
                if event.get("transport_error"):
                    raise httpx.ConnectError("recorded_transport_error", request=request)
                if event.get("invalid_json"):
                    return httpx.Response(event["status"], content=b"invalid JSON")
                return httpx.Response(event["status"], json=event["response"])

            transport = recorder if recorder is not None else httpx.MockTransport(replay)
            async with httpx.AsyncClient(transport=transport, timeout=30) as http:
                async with SemanticScholarClient(
                    client=http if live else None, transport=None if live else transport,
                    max_attempts=1, max_http_attempts=1, rate_limiter=limiter
                ) as client:
                    analysis = await build_stage1_shortlist(target, inventory, client)
                    target, inventory, ranking = (
                        analysis.target,
                        analysis.inventory,
                        analysis.ranking,
                    )
                    attempts = client.http_attempts
            if recorder is not None:
                events = recorder.events
            else:
                assert cursor == len(events)
            stopped = any(event.get("status") == 429 for event in events)
            for obj in [target, *inventory.candidates]:
                obj.embedding_provenance.pop("attempted_at", None)
                obj.embedding_provenance["evaluation_fixture"] = fixture.name
                if events:
                    obj.embedding_provenance["retrieved_at"] = str(events[-1]["retrieved_at"])
        if live:
            fixture.write_bytes(
                gzip.compress(json.dumps(events, allow_nan=False).encode(), mtime=0)
            )
        if ranking is None:
            ranking = rank_stage1_candidates(
                target,
                inventory,
                semantic_strategy="specter2_canonical",
                target_references_fully_resolved=bool(inventory.reference_resolutions)
                and all(r.status == "resolved" for r in inventory.reference_resolutions),
            )
        eligible = inventory.eligible_candidates
        available = sum(c.embedding is not None for c in eligible)
        stats = {
            "target_embedding_available": target.embedding is not None,
            "target_s2_id": target.external_ids.semantic_scholar,
            "target_model": target.embedding_model,
            "target_dimension": len(target.embedding) if target.embedding is not None else None,
            "target_embedding_provenance": target.embedding_provenance,
            "eligible": len(eligible),
            "ranked": len(ranking.ranked_candidates),
            "candidate_embeddings_available": available,
            "candidate_embeddings_missing": len(eligible) - available,
            "candidate_embedding_coverage_percent": 100 * available / len(eligible)
            if eligible
            else 0,
            "candidate_dimensions": sorted({len(c.embedding) for c in eligible if c.embedding}),
            "candidate_missing_reasons": dict(
                Counter(
                    c.embedding_provenance.get("missing_reason", "unknown")
                    for c in eligible
                    if c.embedding is None
                )
            ),
            "metric_coverage": {
                name: sum(getattr(r.score, name) is not None for r in ranking.ranked_candidates)
                for name in ("semantic_similarity", "citation_relation", "citation_importance")
            },
            "score_status_counts": dict(Counter(r.score.status for r in ranking.ranked_candidates)),
            "http_attempts": attempts,
            "http_statuses": [e.get("status", "transport_error") for e in events],
        }
        rows = [
            dict(rank=r.rank, **summarize_candidate(r.candidate), score=r.score.model_dump())
            for r in ranking.ranked_candidates
        ]
        rank_by_id = {r["paper_id"]: r["rank"] for r in rows}
        candidates = {c.paper_id: c for c in inventory.candidates}
        probes = []
        for probe in probe_data:
            if probe["target"] != aid:
                continue
            candidate = candidates[probe["identifiers"]["semantic_scholar"]]
            rank = rank_by_id.get(candidate.paper_id)
            probes.append(
                dict(
                    probe=probe["probe"],
                    **summarize_candidate(candidate),
                    rank=rank,
                    top10=rank is not None and rank <= 10,
                )
            )
        result = {
            "target": spec["title"],
            "statistics": stats,
            "inventory_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
            "fixture_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "scoring_version": ranking.scoring_version,
            "selection_policy_version": ranking.selection_policy_version,
            "ranking": rows,
            "unranked": [
                dict(rank=r.rank, **summarize_candidate(r.candidate), score=r.score.model_dump())
                for r in ranking.unranked_candidates
            ],
            "shortlist_limit": ranking.shortlist_limit,
            "evidence": {
                "data_origin": ("live_provider" if live else "recorded_provider_success")
                if events and all(e.get("status") == 200 for e in events)
                else "error_fixture",
                # Successful HTTP alone does not verify the provider model contract.
                "provider_contract_verified": False,
            },
            "probes": probes,
            "candidate_embedding_outcomes": [summarize_candidate(c) for c in inventory.candidates],
        }
        (OUTPUT / f"{aid}-result.json").write_text(json.dumps(result, indent=2))
        print(aid, json.dumps(stats))
    write_tables()


def write_tables() -> None:
    lines = [
        "# Canonical SPECTER2 integration measurements",
        "",
        "Frozen Task 2.6 inventory; unchanged eligibility, citation metrics and scoring policy.",
        "No lexical/hybrid fallback. Probe selections are diagnostic, not gold labels.",
        "",
    ]
    for spec in TARGET_SPECS:
        result = json.loads((OUTPUT / f"{spec['arxiv_id']}-result.json").read_text())
        lines += [f"## {result['target']}", "", "| Measure | Value |", "|---|---|"]
        lines += [f"| {k} | {v} |" for k, v in result["statistics"].items()]
        lines += [
            "",
            "### Top 10",
            "",
            "| Rank | Title | InitialScore | SemanticSimilarity | CitationRelation "
            "| CitationImportance | Coverage | Embedding source/model |",
            "|---:|---|---:|---:|---:|---:|---:|---|",
        ]
        for row in result["ranking"][:10]:
            score = row["score"]
            values = [
                score[k]
                for k in (
                    "initial_score",
                    "semantic_similarity",
                    "citation_relation",
                    "citation_importance",
                    "coverage",
                )
            ]
            cells = " | ".join("null" if v is None else f"{v:.6f}" for v in values)
            lines.append(
                f"| {row['rank']} | {row['title']} | {cells} | "
                f"{row['embedding_model'] or 'unavailable'} |"
            )
        lines += [
            "",
            "### Diagnostic probes",
            "",
            "| Probe | Embedding | Eligibility | Rank | Top 10 |",
            "|---|---|---|---:|---|",
        ]
        for row in result["probes"]:
            lines.append(
                f"| {row['probe']} | {row['embedding_available']} | {row['eligibility']} "
                f"| {row['rank']} | {row['top10']} |"
            )
        lines += [""]
    (OUTPUT / "measurements.md").write_text("\n".join(lines))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", action="store_true")
    asyncio.run(evaluate(live=parser.parse_args().live))
