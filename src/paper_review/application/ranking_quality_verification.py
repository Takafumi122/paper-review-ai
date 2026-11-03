"""Frozen-input classification/ranking and independent relevance-role comparison."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import citation_quality_evaluation as classification
from paper_review.application import explanation_verification as recent
from paper_review.application import identity_selection_verification as relation_before
from paper_review.application import roadmap_verification as parent
from paper_review.application.citation_contexts import (
    CITATION_PROMPT,
    CitationBatch,
    prepare_contexts,
    request_context,
)
from paper_review.config import ROOT
from paper_review.domain.models import CandidateSet
from paper_review.domain.stage1_scoring import DEFAULT_STAGE1_WEIGHTS, SCORING_VERSION
from paper_review.llm.gateway import ExecutionFailure, digest
from paper_review.llm.token_budget import estimate_input_tokens

OUTPUT = ROOT / "evaluation/reports/ranking-quality-v29-2026-11-02"
DATABASE = ROOT / ".work/ranking-quality-v29.db"
VERSION = "ranking-quality-v29"


def prior_cost() -> float:
    jid = json.loads((recent.OUTPUT / "accepted-job.json").read_text())["job_id"]
    with sqlite3.connect(f"file:{recent.previous.APP_DATABASE}?mode=ro", uri=True) as connection:
        return recent.prior_cost() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions "
                "where job_id=?", (jid,),
            )
        )


def frozen_batches() -> dict[str, list[list[str]]]:
    batches: dict[str, list[list[str]]] = {}
    with sqlite3.connect(f"file:{parent.DATABASE}?mode=ro", uri=True) as connection:
        for pid, metadata in connection.execute(
            "select paper_id,metadata_json from llm_executions where status='completed' "
            "and logical_call_id like 'citation-context-%' order by started_at"
        ):
            batches.setdefault(pid.removeprefix("arxiv:"), []).append(
                json.loads(metadata)["source_ids"]
            )
    return batches


def relevance_sources() -> list[str]:
    with sqlite3.connect(f"file:{relation_before.DATABASE}?mode=ro", uri=True) as connection:
        return json.loads(connection.execute(
            "select metadata_json from llm_executions where status='completed' "
            "and logical_call_id like '%contribution_relevance'"
        ).fetchone()[0])["source_ids"]


def plan() -> dict:
    cases = classification.load_cases()
    batches = frozen_batches()
    old = json.loads((parent.OUTPUT / "classify-results.json").read_text())
    checks = []
    for case in cases:
        contexts = {c["context_id"]: c for c in prepare_contexts(
            CandidateSet.model_validate(case["inventory"]), case["spans"],
        )}
        old_case = next(c for c in old["cases"] if c["aid"] == case["aid"])
        old_contexts = {c["context_id"]: c for c in old_case["audit"]["contexts"]}
        ids = [cid for batch in batches[case["aid"]] for cid in batch]
        assert set(ids) == {c["context_id"] for c in old_case["audit"]["contexts"]
                           if c.get("decision_origin") == "llm"}
        for cid in ids:
            assert hashlib.sha256(contexts[cid]["text"].encode()).hexdigest() == (
                old_contexts[cid]["text_sha256"]
            )
            assert contexts[cid]["candidate_id"] == old_contexts[cid]["candidate_id"]
        estimates = [estimate_input_tokens(
            CitationBatch.model_json_schema(), request_context([contexts[cid] for cid in batch]),
            CITATION_PROMPT,
        ) for batch in batches[case["aid"]]]
        assert max(estimates) <= 8000
        checks.append({"aid": case["aid"], "candidate_inventory_sha256": digest(case["inventory"]),
                       "original_spans_sha256": digest(case["spans"]),
                       "embedding_fixture_sha256": hashlib.sha256(
                           case["fixture"].read_bytes()
                       ).hexdigest(), "batch_context_ids": batches[case["aid"]],
                       "input_token_estimates": estimates,
                       "same_candidate_context_text_confirmed": True,
                       "quote_options_and_prompt_are_changed_components": True})
    return {
        "version": VERSION, "authorized_cumulative_usd": 1.6,
        "prior_used_or_reserved_usd": prior_cost(), "max_logical_calls": 9,
        "max_http_attempts": 27, "new_s2_requests": 0, "new_graph_requests": 0,
        "production_flags_changed": False, "classification_cases": checks,
        "scoring_contract": {"version": SCORING_VERSION, "weights": DEFAULT_STAGE1_WEIGHTS,
                             "top_k": 10, "aggregation": "maximum observed role",
                             "missing": "null with observed-weight renormalization",
                             "tie_break": "score desc, coverage desc, canonical ID"},
        "relevance_comparison": "original v27 Top10, contribution checkpoint and selected sources; "
                                "isolates rubric instruction, not a new Top10 graph evaluation",
        "frozen_relevance_source_ids": relevance_sources(),
        "source_code_sha256": {
            name: hashlib.sha256((ROOT / "src/paper_review" / name).read_bytes()).hexdigest()
            for name in ("application/ranking_quality_verification.py",
                         "application/citation_contexts.py", "application/full_analysis.py",
                         "application/citation_quality_evaluation.py",
                         "application/roadmap_verification.py", "llm/provider.py",
                         "application/stage1_shortlist.py", "domain/stage1_scoring.py",
                         "domain/understanding.py")
        },
        "parent_artifacts_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in [parent.OUTPUT / "classify-results.json",
                         parent.OUTPUT / "stage1-review.json",
                         relation_before.OUTPUT / "results.json"]
        },
        "limitations": ["agent provisional inspection, not independent human annotation",
                        "no formal quality threshold or readiness acceptance",
                        "fixed familiar papers used for development and comparison"],
    }


def evaluate(budget_cents: int = 50) -> dict:
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
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if prior_cost() >= 1.6:
        return {"status": "blocked", "reason": "shared_budget"}
    try:
        lock = (OUTPUT / "active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with lock:
        (OUTPUT / "live-plan.json").write_text(json.dumps(plan(), indent=2) + "\n")
        destination.write_text('{"status":"started"}\n')
        cases = classification.load_cases()
        try:
            classified = asyncio.run(classification._run(
                cases, OUTPUT / "classify-results.json", "sol", database=DATABASE,
                prior_cost=prior_cost, total_budget_usd=1.6, control_ids={},
                fixed_context_batches=frozen_batches(), version=VERSION,
            ))
            (OUTPUT / "classify-results.json").write_text(json.dumps(
                classified, ensure_ascii=False, indent=2,
            ) + "\n")
            accepted = json.loads((relation_before.OUTPUT / "results.json").read_text())["lineage"]
            related = asyncio.run(parent._lineage(
                cases, input_directory=parent.OUTPUT, database=DATABASE, prior_usage=prior_cost,
                version=f"{VERSION}-relevance", accepted_contributions=accepted,
                relevance_only=True, frozen_relevance_source_ids=relevance_sources(),
                total_budget_usd=1.6,
            ))
            (OUTPUT / "relevance-results.json").write_text(json.dumps(
                related, ensure_ascii=False, indent=2,
            ) + "\n")
            result = {"status": "completed", "cases": classified.get("cases", []),
                      "classification_status": classified["status"], "relevance": related,
                      "production_flags_changed": False}
        except Exception as exc:
            result = {"status": "stopped", "reason": (
                str(exc) if isinstance(exc, ExecutionFailure) else type(exc).__name__
            )}
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
