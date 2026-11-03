"""Fixed diagnostic cases for the unchanged production evidence reviewer.

Provisional source labels are not human acceptance or calibrated accuracy.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from paper_review.application.reasoning_review import REASONING_REVIEW_PROMPT, review_reasoning_step
from paper_review.config import ROOT, Settings
from paper_review.domain.understanding import Evidence, ReasoningFlow, ReasoningStep
from paper_review.infrastructure.database.models import AnalysisJob, Base, LLMExecution, Paper
from paper_review.infrastructure.database.session import make_engine, transaction
from paper_review.llm.gateway import LLMGateway, digest
from paper_review.llm.openai_provider import OpenAIProvider
from paper_review.llm.provider import ROUTING
from paper_review.worker import Worker

OUTPUT = ROOT / "evaluation/reports/reasoning-review-diagnostic-v1-2026-10-31"
DATABASE = ROOT / ".work/reasoning-review-diagnostic-v1.db"
SOURCE_DATABASE = ROOT / ".work/app.db"
SOURCE_JOB = "f07bfd5e-df08-438f-a853-8b8a8f01ace7"
TOTAL_BUDGET_USD = 0.50


@dataclass
class ReviewCase:
    id: str
    step: ReasoningStep
    expected: Literal["accept", "reject", "adjudication_required"]
    rationale: str
    provenance: str = "synthetic_fixture"


def historical_step() -> ReasoningStep:
    if not SOURCE_DATABASE.is_file():
        raise ValueError("historical_database_missing")
    engine = make_engine(Settings(database_path=SOURCE_DATABASE))
    try:
        with Session(engine) as session:
            executions = session.scalars(
                select(LLMExecution).where(LLMExecution.job_id == SOURCE_JOB)
            )
            for execution in executions:
                if execution.metadata_json.get("task_type") == "reasoning" and execution.output:
                    flow = ReasoningFlow.model_validate(execution.output)
                    return next(s for s in flow.steps if s.id == "5")
        raise ValueError("historical_step_missing")
    finally:
        engine.dispose()


def cases() -> list[ReviewCase]:
    """Freeze expectations before calling the reviewer; labels never enter its input."""
    rows: list[ReviewCase] = []

    def add(cid, summary, quotes, expected, rationale):
        rows.append(
            ReviewCase(
                cid,
                ReasoningStep(
                    id=cid,
                    kind="observation",
                    summary=summary,
                    evidence=[
                        Evidence(span_id=f"fixture:{cid}:{i}", quote=q)
                        for i, q in enumerate(quotes)
                    ],
                ),
                expected,
                rationale,
            )
        )

    training = "We trained on Dataset A using eight GPUs for 100,000 training steps."
    table = "Self-attention: complexity per layer O(n^2 d); sequential operations O(1)."
    decoding = "The decoder generates one symbol at a time, using previously generated symbols."
    add(
        "training_correct",
        "データセットAで、GPU8基を用いて10万ステップ学習した。",
        [training],
        "accept",
        "数値と対象が明記される。",
    )
    add("training_wrong_number", "GPU16基で学習した。", [training], "reject", "8基を16基へ変更。")
    add(
        "per_layer_correct",
        "自己注意の層あたり計算量はO(n²d)である。",
        [table],
        "accept",
        "層単位の範囲を保持。",
    )
    add(
        "constant_total",
        "自己注意の総計算量は系列長によらず一定である。",
        [table],
        "reject",
        "逐次段数と計算量の混同。",
    )
    add(
        "decoding_correct",
        "デコーダは過去に生成した記号を使い、一度に1記号ずつ生成する。",
        [decoding],
        "accept",
        "逐次生成が明記される。",
    )
    add(
        "parallel_decoding",
        "デコーダはすべての位置を同時に生成する。",
        [decoding],
        "reject",
        "逐次生成と矛盾。",
    )
    cut = "Table 2 compares our translation quality and training costs with"
    tail = "previously published models."
    add(
        "comparison_truncated",
        "表2は既存モデルとの翻訳品質と学習コストを比較する。",
        [cut],
        "reject",
        "比較相手の続きが選択されていない。",
    )
    add(
        "comparison_continued",
        "表2は既存モデルとの翻訳品質と学習コストを比較する。",
        [cut, tail],
        "accept",
        "選択済みの続きが比較相手を特定。",
    )
    add(
        "table_intent",
        "著者は計算量を小さくする目的で自己注意を設計した。",
        [table],
        "reject",
        "比較表だけでは設計意図を帰属できない。",
    )
    add(
        "qualified_inference",
        "この表から、逐次演算段数が一定でも層あたり計算量が一定とは限らないと読める。",
        [table],
        "accept",
        "示された式からの限定的で明示的な解釈。",
    )
    original = historical_step()
    rows.append(
        ReviewCase(
            "historical_purpose",
            original,
            "adjudication_required",
            "目的の帰属と文脈上の補足の境界。正解を先取りしない。",
            SOURCE_JOB,
        )
    )
    qualified = original.model_copy(
        update={
            "summary": (
                "引用に示されたデータセットと学習手順は、翻訳実験の学習条件として整理できる。"
            )
        }
    )
    rows.append(
        ReviewCase(
            "qualified_purpose",
            qualified,
            "adjudication_required",
            "限定的な整理への書き換え。採用を強制せず挙動を比較。",
            SOURCE_JOB,
        )
    )
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, int]:
    scored = [
        r for r in rows if r["expected"] != "adjudication_required" and r["status"] == "reviewed"
    ]
    return {
        "scored_cases": len(scored),
        "expected_accept_rejected": sum(
            r["expected"] == "accept" and not r["accepted"] for r in scored
        ),
        "expected_reject_accepted": sum(
            r["expected"] == "reject" and r["accepted"] for r in scored
        ),
        "unscored_borderline_cases": sum(r["expected"] == "adjudication_required" for r in rows),
    }


def state(status: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    attempts = [a for row in rows for a in row.get("attempts", [])]
    return {
        "status": status,
        "cases": rows,
        "summary": summarize(rows),
        "cost": {
            "estimated_usd": sum(a["estimated_usd"] or 0 for a in attempts),
            "unsettled_reserved_usd": sum(
                a["reserved_usd"] for a in attempts if a["cost_status"] in {"reserved", "unknown"}
            ),
            "has_unknown_cost": any(a["cost_status"] == "unknown" for a in attempts),
        },
    }


def evaluate() -> dict[str, Any]:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    destination = OUTPUT / "results.json"
    if destination.exists():
        previous = json.loads(destination.read_text())
        return (
            previous
            if previous.get("status") == "completed"
            else {
                "status": "blocked",
                "reason": "previous_run_requires_review",
            }
        )
    if not os.environ.get("OPENAI_API_KEY"):
        return {"status": "blocked", "reason": "OPENAI_API_KEY_missing"}
    lock = OUTPUT / "active.lock"
    try:
        handle = lock.open("x")
    except FileExistsError:
        return {"status": "blocked", "reason": "active_or_interrupted"}
    try:
        handle.close()
        suite = cases()
        plan = {
            "version": "reasoning-review-diagnostic-v1",
            "budget_usd": TOTAL_BUDGET_USD,
            "max_http_attempts": 3 * len(suite),
            "review_prompt_sha256": digest(REASONING_REVIEW_PROMPT),
            "model": ROUTING["reasoning_review"].model,
            "task_policy": vars(ROUTING["reasoning_review"]),
            "labels": "agent_provisional_not_human_ground_truth",
            "cases": [
                {
                    "id": c.id,
                    "expected": c.expected,
                    "rationale": c.rationale,
                    "provenance": c.provenance,
                    "input_sha256": digest(c.step.model_dump()),
                }
                for c in suite
            ],
        }
        frozen_path = OUTPUT / "prepared-plan.json"
        if frozen_path.exists():
            frozen = json.loads(frozen_path.read_text())
            if any(
                frozen.get(k) != plan[k]
                for k in ("version", "budget_usd", "review_prompt_sha256", "cases")
            ):
                return {"status": "blocked", "reason": "frozen_plan_changed"}
        (OUTPUT / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
        destination.write_text(json.dumps({"status": "started", "cases": []}) + "\n")
        result = asyncio.run(_run(suite, destination))
        destination.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    finally:
        lock.unlink()


async def _run(suite, destination) -> dict[str, Any]:
    config = Settings(database_path=DATABASE, provider="openai")
    engine = make_engine(config)
    Base.metadata.create_all(engine)
    rows: list[dict[str, Any]] = []
    try:
        for case in suite:
            with Session(engine) as session:
                attempts = list(session.scalars(select(LLMExecution)))
                used = sum(
                    a.reserved_usd
                    if a.cost_status in {"reserved", "unknown"}
                    else a.estimated_cost_usd or 0
                    for a in attempts
                )
            if used >= TOTAL_BUDGET_USD:
                return {**state("stopped", rows), "reason": "global_budget"}
            limits = config.model_copy(update={"initial_budget_usd": TOTAL_BUDGET_USD - used})
            worker = Worker(engine, limits, gateway=LLMGateway(engine, limits, OpenAIProvider()))
            with transaction(engine) as session:
                pid = f"diagnostic:{case.id}"
                session.add(
                    Paper(id=pid, title="Evidence reviewer diagnostic", authors=[], external_ids={})
                )
                session.flush()
                job = AnalysisJob(
                    paper_id=pid,
                    arxiv_revision="fixed-diagnostic-v1",
                    fingerprint=digest(case.step.model_dump()),
                    owner=worker.owner,
                    lease_until=time.time() + 600,
                    status="building_reasoning",
                )
                session.add(job)
                session.flush()
                jid = job.id
            row: dict[str, Any] = {"id": case.id, "expected": case.expected}
            try:
                review = await review_reasoning_step(worker, jid, case.step, 0)
                row.update(status="reviewed", accepted=review.supported, review=review.model_dump())
            except Exception as exc:
                row.update(status="failed", error_type=type(exc).__name__)
            with transaction(engine) as session:
                job = session.get(AnalysisJob, jid)
                assert job is not None
                job.status = "completed" if row["status"] == "reviewed" else "failed"
                job.owner = None
                row["attempts"] = [
                    {
                        "status": a.status,
                        "cost_status": a.cost_status,
                        "estimated_usd": a.estimated_cost_usd,
                        "reserved_usd": a.reserved_usd,
                        "requested_model": a.metadata_json.get("requested_model"),
                        "returned_model": a.metadata_json.get("returned_model"),
                        "input_tokens": a.metadata_json.get("input_tokens"),
                        "output_tokens": a.metadata_json.get("output_tokens"),
                    }
                    for a in session.scalars(select(LLMExecution).where(LLMExecution.job_id == jid))
                ]
            rows.append(row)
            destination.write_text(
                json.dumps(state("running", rows), ensure_ascii=False, indent=2) + "\n"
            )
            if row["status"] == "failed":
                return state("stopped", rows)
        return state("completed", rows)
    finally:
        engine.dispose()
