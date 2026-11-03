"""One authorized two-request recovery window, followed by offline Lineage audit.

The historical stop is preserved. Only this bounded runner ignores that marker;
ordinary clients remain blocked. A new shared-gate 429 is never bypassed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os

from paper_review.application import citation_quality_evaluation as classification
from paper_review.application import ranking_quality_verification as previous
from paper_review.application import roadmap_verification as roadmap
from paper_review.application import stage1_verification as stage1
from paper_review.application.stage1_shortlist import apply_context_classifications
from paper_review.config import ROOT
from paper_review.infrastructure.academic_api.s2_request_gate import SharedS2RequestGate

OUTPUT = ROOT / "evaluation/reports/s2-reconnect-lineage-2026-11-02"
GATE_PATH = ROOT / ".work/s2-http-gate.json"
VERSION = "s2-reconnect-lineage-v1"


def _write(name: str, value: dict) -> None:
    (OUTPUT / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def plan() -> dict:
    paths = [stage1.INPUT / f"{aid}-result.json" for aid in stage1.TARGETS]
    paths += [previous.OUTPUT / "classify-results.json"]
    paths += [ROOT / "src/paper_review" / name for name in (
        "application/s2_reconnect_verification.py", "application/stage1_verification.py",
        "infrastructure/academic_api/semantic_scholar.py",
        "infrastructure/academic_api/s2_request_gate.py",
    )]
    return {
        "version": VERSION, "max_http_requests": 2, "max_attempts_per_request": 1,
        "targets": list(stage1.TARGETS), "new_llm_calls": 0, "new_graph_calls": 0,
        "authorized_cumulative_usd": 1.6,
        "historical_circuit_sha256": hashlib.sha256(stage1.RUNTIME_LIMIT.read_bytes()).hexdigest()
        if stage1.RUNTIME_LIMIT.exists() else None,
        "contract_sha256": hashlib.sha256(stage1.CONTRACT.read_bytes()).hexdigest()
        if stage1.CONTRACT.exists() else None,
        "inputs_and_code_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in paths
        },
        "production_flags_changed": False,
    }


async def _lineage_audit() -> dict:
    """Reapply fixed citation decisions to fresh LoRA embeddings; no semantic calls."""
    case = next(c for c in classification.load_cases() if c["aid"] == "2106.09685")
    original = case["fixture"]
    current_case = dict(case, fixture=OUTPUT / "2106.09685-embeddings.json.gz")
    # The source inventory must be the exact one used by the bounded S2 request.
    frozen = json.loads((stage1.INPUT / "2106.09685-result.json").read_text())
    if case["inventory"] != frozen["inventory"]:
        raise ValueError("lineage_inventory_mismatch")
    audit = next(c["audit"] for c in json.loads(
        (previous.OUTPUT / "classify-results.json").read_text()
    )["cases"] if c["aid"] == case["aid"])
    before = apply_context_classifications(await roadmap.replay(case), audit)
    after = apply_context_classifications(await roadmap.replay(current_case), audit)
    old = {r.candidate.paper_id: r for r in before.ranking.ranked_candidates}
    rows = []
    for row in after.ranking.ranked_candidates:
        pid = row.candidate.paper_id
        contexts = [c for c in audit["contexts"] if c["candidate_id"] == pid]
        rows.append({
            "candidate_id": pid, "title": row.candidate.title,
            "rank_before": old[pid].rank if pid in old else None, "rank_after": row.rank,
            "score_before": old[pid].score.model_dump() if pid in old else None,
            "score_after": row.score.model_dump(),
            "context_status_counts": {
                status: sum(c["status"] == status for c in contexts)
                for status in sorted({c["status"] for c in contexts})
            },
            "unprocessed_context_ids": [
                c["context_id"] for c in contexts if c["status"] == "unprocessed"
            ],
        })
    return {
        "status": "completed", "quality_gate": "held",
        "reason": "missing_value_rank_advantage_and_role_annotation_unresolved",
        "target": case["aid"], "classification_decisions_reused": True,
        "frozen_fixture_sha256": hashlib.sha256(original.read_bytes()).hexdigest(),
        "new_fixture_sha256": hashlib.sha256(current_case["fixture"].read_bytes()).hexdigest(),
        "scoring_version": after.ranking.scoring_version,
        "weights": after.ranking.weights, "top_k": after.ranking.shortlist_limit,
        "ranking": rows,
        "unranked_count": len(after.ranking.unranked_candidates),
        "top10_importance_missing": sum(
            r.score.citation_importance is None for r in after.ranking.ranked_candidates[:10]
        ),
        "new_llm_calls": 0, "graph_evaluated": False, "production_flags_changed": False,
    }


def evaluate(budget_cents: int) -> dict:
    if budget_cents != 160:
        return {"status": "blocked", "reason": "explicit_160_cent_authorization_required"}
    destination = OUTPUT / "results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_window_requires_review",
        }
    prepared = OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != plan():
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    contract = json.loads(stage1.CONTRACT.read_text())
    if not contract.get("contract_verified") or contract.get("status") != 200:
        return {"status": "blocked", "reason": "verified_contract_required"}
    if not (os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY")):
        return {"status": "blocked", "reason": "semantic_scholar_environment_missing"}
    if GATE_PATH.exists() and json.loads(GATE_PATH.read_text() or "{}").get("stopped"):
        return {"status": "blocked", "reason": "new_shared_gate_stop_requires_review"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        _write("results.json", {"status": "started", "max_http_requests": 2})
        _write("live-plan.json", plan())
        if stage1.RUNTIME_LIMIT.exists():
            (OUTPUT / "historical-stop.json").write_bytes(stage1.RUNTIME_LIMIT.read_bytes())
        try:
            # Same lock/deadline as normal clients. This new explicitly authorized
            # probe alone can ignore the archived historical marker (never a fresh 429).
            result = asyncio.run(stage1._evaluate(
                output=OUTPUT, targets=stage1.TARGETS,
                request_gate=SharedS2RequestGate(GATE_PATH, stop_path=None),
            ))
            _write("connection-results.json", result)
            if result["status"] != "completed":
                _write("results.json", result)
                return result
            lineage = asyncio.run(_lineage_audit())
            _write("lineage-audit.json", lineage)
            result = {
                "status": "completed", "reason": "connection_verified_lineage_quality_held",
                "http_requests": result["http_requests"], "cases": result["reports"],
                "new_llm_calls": 0, "production_flags_changed": False,
                "historical_stop_unchanged": plan()["historical_circuit_sha256"] == json.loads(
                    prepared.read_text()
                )["historical_circuit_sha256"],
            }
        except Exception:
            # The reserved window must not restart after uncertain send outcomes.
            result = {"status": "stopped", "reason": "runner_failed_review_reserved_window"}
        _write("results.json", result)
        return result
