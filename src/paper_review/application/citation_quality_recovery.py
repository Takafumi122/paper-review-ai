"""One new LoRA window, carrying immutable Attention evidence and all old costs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3

from paper_review.application import citation_quality_evaluation as baseline
from paper_review.config import ROOT
from paper_review.llm.pricing import RATES
from paper_review.llm.provider import ROUTING

OUTPUT = ROOT / "evaluation/reports/citation-context-lora-recovery-v1-2026-11-02"
DATABASE = ROOT / ".work/citation-context-lora-recovery-v1.db"
SENTENCE_OUTPUT = ROOT / "evaluation/reports/citation-sentence-options-v19-2026-11-02"
SENTENCE_DATABASE = ROOT / ".work/citation-sentence-v19.db"
EXPANDED_OUTPUT = ROOT / "evaluation/reports/citation-expanded-v20-2026-11-02"
EXPANDED_DATABASE = ROOT / ".work/citation-expanded-v20.db"
EXPANDED_IDS = {
    "1706.03762": {"C11", "C25", "C31", "C43", "C55", "C58"},
    "2106.09685": {"C10", "C19", "C24", "C28", "C34", "C39"},
}


def prior_cost() -> float:
    # The old failed window stays closed. Unknown costs never become zero.
    with sqlite3.connect(f"file:{baseline.DATABASE}?mode=ro", uri=True) as connection:
        return baseline.prior_reservation() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def sentence_prior_cost() -> float:
    with sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True) as connection:
        return prior_cost() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def expanded_prior_cost() -> float:
    with sqlite3.connect(f"file:{SENTENCE_DATABASE}?mode=ro", uri=True) as connection:
        return sentence_prior_cost() + sum(
            reserved if status in {"reserved", "unknown"} else estimated or 0
            for status, reserved, estimated in connection.execute(
                "select cost_status,reserved_usd,estimated_cost_usd from llm_executions"
            )
        )


def expanded_plan(cases: list[dict]) -> dict:
    prepared = baseline.plan(cases)
    policy = ROUTING["citation_classification"]
    input_rate, output_rate = RATES[policy.model]
    # Parent hashes lock the original cases; freeze the new selected contexts separately.
    batches = []
    for case in cases:
        contexts = baseline.prepare_contexts(
            baseline.CandidateSet.model_validate(case["inventory"]), case["spans"]
        )
        selected = [c for c in contexts if c["context_id"] in EXPANDED_IDS[case["aid"]]]
        if len(selected) != len(EXPANDED_IDS[case["aid"]]) or (
            len(baseline.context_batches(selected)) != 1
        ):
            raise ValueError("expanded_controls_not_one_batch")
        batches.append({"aid": case["aid"], "batch_sha256": baseline.digest(
            baseline.request_context(selected)
        )})
    return {
        **prepared, "version": "citation-expanded-v20", "budget_usd": 0.15,
        "model_order": ["gpt-6.1-sol"], "max_logical_calls": 2, "max_http_attempts": 6,
        "allowed_total_budget_cents": [10, 15], "automatic_execution": False,
        "reservation_per_attempt_usd": (
            policy.max_input_tokens * input_rate + policy.max_output_tokens * output_rate
        ),
        "cases": [{k: v for k, v in c.items() if k != "batches_sha256"}
                  for c in prepared["cases"]],
        "controls": {k: sorted(v) for k, v in EXPANDED_IDS.items()},
        "selected_batches": batches, "prior_reserved_or_used_usd": expanded_prior_cost(),
        "pre_model_source_review_sha256": hashlib.sha256(
            (EXPANDED_OUTPUT / "pre-model-source-review.json").read_bytes()
        ).hexdigest(),
    }


def evaluate_expanded(budget_cents: int = 10) -> dict:
    if budget_cents != 15:
        return {"status": "blocked", "reason": "explicit_shared_ceiling_required"}
    destination = EXPANDED_OUTPUT / "sol-results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_run_requires_review"
        }
    cases = baseline.load_cases()
    frozen = expanded_plan(cases)
    prepared = EXPANDED_OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != frozen:
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if expanded_prior_cost() + frozen["reservation_per_attempt_usd"] > 0.15:
        return {"status": "blocked", "reason": "insufficient_shared_budget"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    try:
        reservation = (EXPANDED_OUTPUT / "sol-active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with reservation:
        (EXPANDED_OUTPUT / "live-plan.json").write_text(json.dumps(frozen, indent=2) + "\n")
        destination.write_text('{"status":"started","cases":[]}\n')
        result = asyncio.run(baseline._run(
            cases, destination, "sol", database=EXPANDED_DATABASE,
            prior_cost=expanded_prior_cost, total_budget_usd=0.15, control_ids=EXPANDED_IDS,
        ))
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result


def sentence_plan(cases: list[dict]) -> dict:
    policy = ROUTING["citation_classification"]
    input_rate, output_rate = RATES[policy.model]
    return {
        **baseline.plan(cases), "version": "citation-sentence-sol-v19",
        "model_order": ["gpt-6.1-sol"], "max_logical_calls": 2, "max_http_attempts": 6,
        "prior_reserved_or_used_usd": sentence_prior_cost(),
        "allowed_total_budget_cents": [10, 15], "automatic_execution": False,
        "reservation_per_attempt_usd": (
            policy.max_input_tokens * input_rate + policy.max_output_tokens * output_rate
        ),
    }


def evaluate_sentences(budget_cents: int = 10) -> dict:
    """Operator must supply the authorized ceiling explicitly; default never increases it."""
    if budget_cents not in {10, 15}:
        return {"status": "blocked", "reason": "unsupported_budget"}
    destination = SENTENCE_OUTPUT / "sol-results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_run_requires_review"
        }
    cases = baseline.load_cases()
    frozen = sentence_plan(cases)
    prepared = SENTENCE_OUTPUT / "prepared-plan.json"
    if not prepared.exists() or json.loads(prepared.read_text()) != frozen:
        return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
    if sentence_prior_cost() + frozen["reservation_per_attempt_usd"] > budget_cents / 100:
        return {"status": "blocked", "reason": "insufficient_shared_budget"}
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    try:
        reservation = (SENTENCE_OUTPUT / "sol-active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with reservation:
        (SENTENCE_OUTPUT / "live-plan.json").write_text(json.dumps({
            **frozen, "authorized_total_budget_usd": budget_cents / 100,
        }, indent=2) + "\n")
        destination.write_text('{"status":"started","cases":[]}\n')
        result = asyncio.run(baseline._run(
            cases, destination, "sol", database=SENTENCE_DATABASE,
            prior_cost=sentence_prior_cost, total_budget_usd=budget_cents / 100,
        ))
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result


def parent(cases: list[dict]) -> tuple[dict, str]:
    """Reuse only the exact successful Attention controls under unchanged source/schema."""
    if json.loads((baseline.OUTPUT / "prepared-plan.json").read_text()) != baseline.plan(cases):
        raise ValueError("parent_plan_changed")
    path = baseline.OUTPUT / "sol-results.json"
    result = json.loads(path.read_text())
    if result.get("status") != "stopped" or result.get("reason") != "classification_unavailable":
        raise ValueError("parent_not_stopped")
    rows = {row["aid"]: row for row in result["cases"]}
    attention = rows["1706.03762"]
    lora = rows["2106.09685"]
    controls = attention["audit"]["contexts"]
    if (
        attention["audit"]["errors"]
        or {c["context_id"] for c in controls} != baseline.CONTROL_IDS["1706.03762"]
        or not all(c["status"] == "completed" for c in controls)
        or lora["audit"]["contexts_completed"] != 0
        or not lora["audit"]["errors"]
    ):
        raise ValueError("parent_controls_changed")
    return attention, hashlib.sha256(path.read_bytes()).hexdigest()


def plan(cases: list[dict]) -> dict:
    _, parent_hash = parent(cases)
    return {
        **baseline.plan(cases), "version": "citation-context-lora-recovery-v1",
        "parent_sol_result_sha256": parent_hash,
        "prior_reserved_or_used_usd": prior_cost(),
        "sol_new_cases": ["2106.09685"], "sol_carried_cases": ["1706.03762"],
        "max_logical_calls": 3, "max_http_attempts": 9,
        "required_diagnostics_version": "attempt-timing-v1",
    }


def evaluate(phase: str = "sol") -> dict:
    if phase not in {"sol", "nano"}:
        return {"status": "blocked", "reason": "unknown_phase"}
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / f"{phase}-results.json"
    if destination.exists():
        old = json.loads(destination.read_text())
        return old if old.get("status") == "completed" else {
            "status": "blocked", "reason": "previous_run_requires_review"
        }
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    if phase == "nano":
        result_path, review_path = OUTPUT / "sol-results.json", OUTPUT / "sol-review.json"
        if not result_path.exists() or not review_path.exists():
            return {"status": "blocked", "reason": "source_reviewed_sol_baseline_required"}
        assessment = json.loads(review_path.read_text())
        if (
            json.loads(result_path.read_text()).get("status") != "completed"
            or assessment.get("accepted_for_comparison") is not True
            or assessment.get("sol_result_sha256")
            != hashlib.sha256(result_path.read_bytes()).hexdigest()
        ):
            return {"status": "blocked", "reason": "source_reviewed_sol_baseline_required"}
    try:
        reservation = (OUTPUT / f"{phase}-active.lock").open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    with reservation:
        cases = baseline.load_cases()
        try:
            frozen = plan(cases)
            carried, parent_hash = parent(cases)
        except (OSError, ValueError, KeyError, TypeError):
            return {"status": "blocked", "reason": "parent_unavailable_or_changed"}
        prepared = OUTPUT / "prepared-plan.json"
        if not prepared.exists() or json.loads(prepared.read_text()) != frozen:
            return {"status": "blocked", "reason": "prepared_plan_required_or_changed"}
        (OUTPUT / "plan.json").write_text(json.dumps(frozen, indent=2) + "\n")
        destination.write_text('{"status":"started","cases":[]}\n')
        selected = [c for c in cases if c["aid"] == "2106.09685"] if phase == "sol" else cases
        result = asyncio.run(baseline._run(
            selected, destination, phase, database=DATABASE, prior_cost=prior_cost,
        ))
        if phase == "sol":
            result["cases"] = [carried, *result["cases"]]
            result["carried_attention_sha256"] = parent_hash
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
