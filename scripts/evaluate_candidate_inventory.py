"""Live inventory diagnostic with frozen metadata replay; never calls an LLM.

uv run python scripts/evaluate_candidate_inventory.py --live
uv run python scripts/evaluate_candidate_inventory.py --replay
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

import httpx
from evaluate_stage1_ranking import TARGET_SPECS

from paper_review.domain.candidate_inventory import (
    extract_reference_ids,
    resolve_candidate_inventory,
)
from paper_review.domain.candidate_retrieval import normalize_title
from paper_review.domain.models import ExternalIds, Paper, Reference
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.arxiv import ArXivClient, ArXivError
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.semantic_scholar import SemanticScholarClient
from paper_review.infrastructure.parsing.pdf_parser import parse_pdf

OUTPUT = Path(__file__).resolve().parents[1] / "evaluation/reports/candidate-inventory"


class RecordingTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.transport = httpx.AsyncHTTPTransport()
        self.events: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self.transport.handle_async_request(request)
        await response.aread()
        try:
            data = response.json()
        except ValueError:
            data = None
        self.events.append(
            dict(
                method=request.method,
                url=str(request.url),
                body=json.loads(request.content) if request.content else None,
                status=response.status_code,
                response=data,
            )
        )
        return response

    async def aclose(self) -> None:
        await self.transport.aclose()


async def evaluate(*, live: bool) -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    temporary_directory = OUTPUT.parents[2] / ".work" / "inventory-pdfs"
    temporary_directory.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(temporary_directory)
    for spec in TARGET_SPECS:
        aid = spec["arxiv_id"]
        source_path = OUTPUT / f"{aid}-input.json"
        if live:
            async with ArXivClient() as arxiv:
                metadata_warning = None
                try:
                    target = (await arxiv.get_metadata(aid)).to_paper()
                except ArXivError as exc:
                    metadata_warning = type(exc).__name__
                    target = Paper(
                        id=spec["id"],
                        title=spec["title"],
                        abstract=spec["abstract"],
                        published_at=spec["published_at"],
                        published_date_precision=spec["published_date_precision"],
                        external_ids=ExternalIds(arxiv=aid),
                    )
                async with arxiv.download_pdf_temp(aid + spec["version"]) as path:
                    content_hash = hashlib.sha256(path.read_bytes()).hexdigest()
                    document = parse_pdf(path)
                    references = document.to_references(f"{target.id}:{spec['version']}", target.id)
                source = dict(
                    target=target.model_dump(),
                    references=[r.model_dump() for r in references],
                    pdf_hash=content_hash,
                    parser_version=document.parser_version,
                    parsed_pages=document.page_count,
                    warnings=document.warnings,
                    target_metadata_warning=metadata_warning,
                    target_metadata_source="audit_spec" if metadata_warning else "arxiv_api",
                )
            source_path.write_text(json.dumps(source, indent=2))
            recorder = RecordingTransport()
            transport: httpx.AsyncBaseTransport = recorder
        else:
            source = json.loads(source_path.read_text())
            target = Paper.model_validate(source["target"])
            references = [Reference.model_validate(r) for r in source["references"]]
            events = json.loads((OUTPUT / f"{aid}-s2.json").read_text())
            cursor = 0

            def replay(request: httpx.Request, recorded=events) -> httpx.Response:
                nonlocal cursor
                if cursor >= len(recorded):
                    raise AssertionError("Unexpected replay request")
                event = recorded[cursor]
                cursor += 1
                assert event["method"] == request.method and event["url"] == str(request.url)
                assert event["body"] == (json.loads(request.content) if request.content else None)
                return httpx.Response(event["status"], json=event["response"])

            transport = httpx.MockTransport(replay)
        async with httpx.AsyncClient(transport=transport, timeout=30) as http:
            async with SemanticScholarClient(
                client=http if live else None, transport=None if live else transport,
                max_attempts=1, rate_limiter=RateLimiter(1 if live else 0)
            ) as s2:
                inventory = await resolve_candidate_inventory(target, references, s2)
                attempts = s2.http_attempts
        if live:
            (OUTPUT / f"{aid}-s2.json").write_text(json.dumps(recorder.events, indent=2))
        if not live:
            assert cursor == len(events), "Replay did not consume every recorded request"
            for candidate in inventory.candidates:
                provenance = candidate.metadata_provenance
                if "retrieved_at" in provenance:
                    provenance["replay_normalized_at"] = provenance.pop("retrieved_at")
                    provenance["replay_source"] = f"{aid}-s2.json"
                    provenance["original_retrieval_time"] = "not_recorded"
        canonical = rank_stage1_candidates(
            target,
            inventory,
            target_references_fully_resolved=all(
                r.status == "resolved" for r in inventory.reference_resolutions
            ),
        )
        lexical = rank_stage1_candidates(
            target,
            inventory,
            semantic_strategy="lexical",
            target_references_fully_resolved=all(
                r.status == "resolved" for r in inventory.reference_resolutions
            ),
        )
        outcomes = inventory.reference_resolutions
        stats = dict(
            parsed_reference_count=len(references),
            deduplicated_reference_count=len(references) - inventory.total_count,
            resolved_reference_count=sum(r.status == "resolved" for r in outcomes),
            unresolved_reference_count=sum(r.status != "resolved" for r in outcomes),
            distinct_candidate_count=inventory.total_count,
            resolved_distinct_papers=sum(
                c.resolution_status == "resolved" for c in inventory.candidates
            ),
            identifier_resolved_count=sum(
                r.status == "resolved" and r.method != "exact_title" for r in outcomes
            ),
            title_resolved_count=sum(
                r.status == "resolved" and r.method == "exact_title" for r in outcomes
            ),
            possible_merged_records=sum(
                any(len(v) > 1 for v in extract_reference_ids(r).values()) for r in references
            ),
            eligible=len(inventory.eligible_candidates),
            ranked=len(canonical.ranked_candidates),
            top10=len(canonical.top_k()),
            metric_coverage={
                component: sum(
                    getattr(row.score, component) is not None for row in canonical.ranked_candidates
                )
                for component in ("semantic_similarity", "citation_relation", "citation_importance")
            },
            fully_scored_count=sum(
                row.score.status == "complete" for row in canonical.ranked_candidates
            ),
            partial_scored_count=sum(
                row.score.status == "partial" for row in canonical.ranked_candidates
            ),
            http_attempts=attempts,
            retrieval_warnings=inventory.retrieval_warnings,
        )
        result = dict(
            target=target.model_dump(),
            statistics=stats,
            inventory=inventory.model_dump(),
            canonical_ranking=canonical.model_dump(),
            lexical_diagnostic=lexical.model_dump(),
        )
        (OUTPUT / f"{aid}-result.json").write_text(json.dumps(result, indent=2))
        print(aid, json.dumps(stats))
    write_tables()
    write_probes()


def write_tables() -> None:
    lines = [
        "# Candidate inventory evaluation",
        "",
        "Canonical scores below use unchanged weights and no embeddings. "
        "The explicit lexical diagnostic is a separate baseline, not canonical SPECTER2.",
        "",
    ]
    for spec in TARGET_SPECS:
        result = json.loads((OUTPUT / f"{spec['arxiv_id']}-result.json").read_text())
        lines += [f"## {spec['title']}", "", "| Measure | Value |", "|---|---|"]
        for key, value in result["statistics"].items():
            lines.append(f"| {key} | {value} |")
        for strategy in ("canonical_ranking", "lexical_diagnostic"):
            lines += [
                "",
                f"### {strategy} Top 10",
                "",
                "| Rank | Title | S2 ID | Score | Similarity | Importance | Source refs |",
                "|---:|---|---|---:|---:|---:|---|",
            ]
            for r in result[strategy]["ranked_candidates"][:10]:
                c, s = r["candidate"], r["score"]
                orders = [ref["ref_order"] for ref in c["source_references"]]
                lines.append(
                    f"| {r['rank']} | {c['title']} | {c['paper_id']} | "
                    f"{s['initial_score']:.6f} | {s['semantic_similarity']} | "
                    f"{s['citation_importance']} | {orders} |"
                )
        lines += [
            "",
            "### Every reference resolution",
            "",
            "| Ref | Status | Method | Title | Reason |",
            "|---:|---|---|---|---|",
        ]
        candidates = {c["paper_id"]: c for c in result["inventory"]["candidates"]}
        for r in result["inventory"]["reference_resolutions"]:
            c = candidates[r["candidate_id"]]
            lines.append(
                f"| {r['reference_order']} | {r['status']} | {r['method']} | "
                f"{c['title']} | {r['reason'] or ''} |"
            )
        lines += [""]
    (OUTPUT / "measurements.md").write_text("\n".join(lines))


def write_probes() -> None:
    probes = json.loads((OUTPUT / "probes.json").read_text())
    before = json.loads((OUTPUT.parent / "stage1-audit/before.json").read_text())
    lines = [
        "# Diagnostic predecessor presence",
        "",
        probes["purpose"],
        "",
        "Before ranks are the old lexical baseline; after ranks use the canonical formula "
        "with missing embeddings. These are not directly comparable quality scores.",
        "",
    ]
    rows = []
    for spec, snapshot in zip(TARGET_SPECS, before, strict=True):
        aid = spec["arxiv_id"]
        result = json.loads((OUTPUT / f"{aid}-result.json").read_text())
        canonical = {
            r["candidate"]["paper_id"]: r["rank"]
            for r in result["canonical_ranking"]["ranked_candidates"]
        }
        lexical = {
            r["candidate"]["paper_id"]: r["rank"]
            for r in result["lexical_diagnostic"]["ranked_candidates"]
        }
        lines += [
            f"## {spec['title']}",
            "",
            "| Probe | Independent before → after | Old rank | Resolved identifier(s) "
            "| Eligibility "
            "| Method | Canonical rank | Top 10 | Lexical rank | Source refs |",
            "|---|---|---:|---|---|---|---:|---|---:|---|",
        ]
        for label, fragment, old_order in probes[aid]:
            matches = [
                c
                for c in result["inventory"]["candidates"]
                if normalize_title(fragment) in normalize_title(c["title"])
            ]
            if len(matches) != 1:
                raise ValueError(f"Probe {label}: {len(matches)} independent matches")
            c = matches[0]
            old_rank = next(
                (
                    r["rank"]
                    for r in snapshot["ranking"]["ranked_candidates"]
                    if r["candidate"]["target_reference_order"] == old_order
                ),
                None,
            )
            rank = canonical.get(c["paper_id"])
            identifiers = {k: v for k, v in c["external_ids"].items() if v}
            row = dict(
                target=aid,
                probe=label,
                independent_before=old_order is not None,
                independent_after=True,
                old_lexical_rank=old_rank,
                identifiers=identifiers,
                resolution_status=c["resolution_status"],
                resolution_method=c["resolution_method"],
                eligibility=c["eligibility_status"],
                canonical_rank=rank,
                top10=rank is not None and rank <= 10,
                lexical_rank=lexical.get(c["paper_id"]),
                source_references=[r["ref_order"] for r in c["source_references"]],
            )
            rows.append(row)
            lines.append(
                f"| {label} | {'Yes' if old_order is not None else 'No (merged only)'} → Yes "
                f"| {old_rank or '—'} | {identifiers} | {row['eligibility']} "
                f"| {row['resolution_method']} | {rank or '—'} "
                f"| {row['top10']} | {row['lexical_rank'] or '—'} | {row['source_references']} |"
            )
        lines += [""]
    (OUTPUT / "diagnostic-probes.md").write_text("\n".join(lines))
    (OUTPUT / "diagnostic-probes-results.json").write_text(json.dumps(rows, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true")
    mode.add_argument("--replay", action="store_true")
    args = parser.parse_args()
    asyncio.run(evaluate(live=args.live))
