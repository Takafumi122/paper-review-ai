"""Persistent Phase 0 fake execution policy, with a single retry owner."""

import asyncio
import copy
import hashlib
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, select

from paper_review.config import Settings
from paper_review.infrastructure.database.models import (
    AnalysisJob,
    LLMExecution,
    PaperRevision,
    StageExecution,
)
from paper_review.infrastructure.database.session import transaction
from paper_review.infrastructure.observability import record
from paper_review.llm.pricing import EVALUATION_MODELS, RATES
from paper_review.llm.provider import (
    ROUTING,
    LLMProvider,
    LLMRequest,
    ProviderFailure,
    ProviderTimeout,
    TaskPolicy,
)
from paper_review.llm.token_budget import TOKEN_BUDGET_VERSION, estimate_input_tokens

SAFE_VALIDATION_REASONS = frozenset(
    {
        "unverified_evidence_unknown_span",
        "invalid_evidence_reference",
        "unverified_evidence_quote_mismatch",
        "duplicate_reasoning_step",
        "invalid_reasoning_edge",
        "orphan_reasoning_step",
        "result_without_predecessor",
        "reasoning_review_incomplete_coverage",
        "reasoning_review_unselected_evidence",
        "reasoning_review_missing_evidence",
        "reasoning_explanation_context_missing",
        "reasoning_unused_explanation_context",
        "unresolved_inline_reference",
        "invalid_stage2_component",
        "invalid_lineage_nodes",
        "ineligible_lineage_node",
        "invalid_lineage_edge",
        "lineage_chronology_unverified",
        "lineage_candidate_not_identified",
        "lineage_evidence_wrong_paper",
        "lineage_merge_limit",
        "lineage_merge_unjustified",
        "lineage_merge_support_without_merge",
        "lineage_merge_reason_without_merge",
        "lineage_merge_support_required",
        "lineage_merge_contributions_required",
        "lineage_merge_node_mismatch",
        "lineage_merge_branch_coverage_mismatch",
        "lineage_merge_unknown_contribution",
        "lineage_merge_requires_distinct_contributions",
        "lineage_merge_branch_not_identified",
        "lineage_merge_evidence_wrong_scope",
        "lineage_edge_review_coverage_mismatch",
        "lineage_merge_review_coverage_mismatch",
        "lineage_merge_review_unselected_evidence",
        "lineage_merge_review_missing_evidence",
        "lineage_cycle_depth_or_disconnection",
        "invalid_relevance_candidate",
        "unknown_contribution",
        "relevance_candidate_not_identified",
        "relevance_without_evidence",
        "relevance_evidence_wrong_candidate",
        "relevance_candidate_coverage_mismatch",
        "relevance_identity_source_required",
        "relevance_invalid_identity_source",
        "contribution_explanation_not_japanese",
        "problem_without_evidence",
        "duplicate_contribution_id",
        "contribution_review_incomplete_items",
        "knowledge_edge_missing",
        "citation_context_coverage_mismatch",
        "citation_candidate_mismatch",
        "citation_null_with_quote",
        "citation_role_without_anchored_quote",
        "citation_null_with_support",
        "citation_role_without_bound_support",
    }
)


class InputLimitExceeded(RuntimeError):
    pass


class ExecutionFailure(RuntimeError):
    pass


EVIDENCE_FIELDS = frozenset({
    "evidence", "reader_explanation_evidence", "problem_evidence", "identifying_evidence",
    "integration_evidence",
})


def validate_display_references(value: Any) -> None:
    """Reject citation-shaped transient labels; never alter an original quote."""
    if isinstance(value, str):
        if re.search(r"[\[(（](?:S|E)\d+(?:\s*[,、]\s*(?:S|E)\d+)*[\])）]", value) or re.search(
            r"(?:根拠|出典|(?i:source|evidence))\s*[:：]\s*(?:S|E)\d+", value
        ):
            raise ValueError("unresolved_inline_reference")
    elif isinstance(value, list):
        for item in value:
            validate_display_references(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key not in EVIDENCE_FIELDS:
                validate_display_references(item)


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def request_schema(
    output_schema: type[BaseModel],
    *,
    bind_evidence: bool,
    knowledge_edges: dict[str, tuple[str, str]] | None = None,
) -> dict[str, Any]:
    schema = output_schema.model_json_schema()
    if bind_evidence:
        evidence_schema = schema.get("$defs", {}).get("Evidence")
        if evidence_schema is None:
            raise ExecutionFailure("missing_evidence_schema")
        evidence_schema["properties"].pop("quote", None)
        evidence_schema["required"] = ["span_id"]
    if knowledge_edges is not None:
        item_schema = schema.get("$defs", {}).get("KnowledgeItem")
        if item_schema is None or not knowledge_edges:
            raise ExecutionFailure("missing_knowledge_edges")
        for key in ("from_step_id", "to_step_id"):
            item_schema["properties"].pop(key, None)
            item_schema["required"].remove(key)
        item_schema["properties"]["edge_id"] = {"type": "string", "enum": list(knowledge_edges)}
        item_schema["required"].append("edge_id")
    if bind_evidence:
        # Source selection precedes prose in generation only. Stored models and
        # the semantic reviewers keep their existing fields and contracts.
        for name, first in (
            ("KnowledgeItem", ("id", "concept", "edge_id", "evidence")),
            ("ReasoningStepDraft", ("id", "kind", "evidence", "summary",
                                    "reader_explanation_evidence")),
        ):
            definition = schema.get("$defs", {}).get(name)
            if definition is not None:
                properties = definition["properties"]
                order = list(dict.fromkeys(
                    [k for k in first if k in properties] + list(properties)
                ))
                definition["properties"] = {k: properties[k] for k in order}
                definition["required"] = [k for k in order if k in definition["required"]]
    return schema


def adjacent_source_spans(
    spans: tuple[dict[str, str], ...],
) -> list[tuple[dict[str, str], dict[str, str]]]:
    """Find unambiguous original boundaries within one paper, revision and section."""
    starts: dict[tuple[str, str, str, int], list[dict[str, str]]] = {}
    ends: dict[tuple[str, str, str, int], list[dict[str, str]]] = {}
    for span in spans:
        origin = (span.get("paper_id", ""), span.get("revision", ""), span.get("section_id", ""))
        if not all(origin):
            continue
        try:
            start, end = int(span["start_offset"]), int(span["end_offset"])
        except (KeyError, ValueError):
            continue
        if start < 0 or end <= start or end - start != len(span["text"]):
            continue
        starts.setdefault((*origin, start), []).append(span)
        ends.setdefault((*origin, end), []).append(span)
    return [
        (previous[0], following[0])
        for boundary, following in starts.items()
        if len(previous := ends.get(boundary, [])) == len(following) == 1
    ]


def prepare_source_request(
    schema: dict[str, Any], context: tuple[dict[str, str], ...]
) -> tuple[dict[str, Any], tuple[dict[str, str], ...], dict[str, tuple[str, str]]]:
    """Constrain transient short IDs; retain their original provenance outside the prompt."""
    if "Evidence" not in schema.get("$defs", {}):
        return schema, context, {}
    references: dict[str, tuple[str, str]] = {}
    transformed = []
    for span in context:
        if span["span_id"] == "task-data":
            transformed.append(span)
            continue
        alias = f"S{len(references)}"
        references[alias] = (span["span_id"], span["text"])
        transformed.append(
            {
                **{k: v for k, v in span.items() if k not in {"previous_span_id", "next_span_id"}},
                "span_id": alias,
            }
        )
    # Navigation only: connect exact contiguous excerpts actually present in
    # this request. Do not expand quotes or attach unselected evidence.
    for previous, following in adjacent_source_spans(tuple(transformed)):
        previous["next_span_id"] = following["span_id"]
        following["previous_span_id"] = previous["span_id"]
    # A code-owned index makes candidate-specific choices visible after aliasing.
    # Only sources actually included in this request can appear in these options.
    candidate_options: dict[str, dict[str, list[str]]] = {}
    for span in transformed:
        for field, key in (("identifies_candidate_ids", "identifying_source_ids"),
                           ("candidate_ids", "allowed_source_ids")):
            for cid in json.loads(span.get(field, "[]")):
                options = candidate_options.setdefault(cid, {
                    "identifying_source_ids": [], "allowed_source_ids": [],
                })
                options[key].append(span["span_id"])
    if candidate_options:
        for span in transformed:
            if span["span_id"] == "task-data":
                task = json.loads(span["text"])
                task["candidate_evidence_options"] = candidate_options
                span = {**span, "text": json.dumps(task, ensure_ascii=False)}
                transformed = [span if s["span_id"] == "task-data" else s for s in transformed]
                break
    result = copy.deepcopy(schema)
    result["$defs"]["Evidence"]["properties"]["span_id"]["enum"] = list(references)
    return result, tuple(transformed), references


def bind_knowledge_edges(value: Any, edges: dict[str, tuple[str, str]]) -> Any:
    if not isinstance(value, dict) or not isinstance(value.get("items"), list):
        return value
    items = []
    for item in value["items"]:
        if not isinstance(item, dict) or "from_step_id" in item or "to_step_id" in item:
            raise ValueError("knowledge_edge_missing")
        edge_id = item.get("edge_id")
        if not isinstance(edge_id, str) or edge_id not in edges:
            raise ValueError("knowledge_edge_missing")
        start, end = edges[edge_id]
        items.append(
            {
                **{k: v for k, v in item.items() if k != "edge_id"},
                "from_step_id": start,
                "to_step_id": end,
            }
        )
    return {**value, "items": items}


def bind_source_evidence(
    value: Any, sources: dict[str, str], *, original_ids: dict[str, str] | None = None
) -> Any:
    """Resolve model-selected source IDs to verbatim text; never accept generated quotes."""
    if isinstance(value, list):
        return [bind_source_evidence(item, sources, original_ids=original_ids) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key not in EVIDENCE_FIELDS or not isinstance(item, list):
            result[key] = bind_source_evidence(item, sources, original_ids=original_ids)
            continue
        bound = []
        for reference in item:
            if not isinstance(reference, dict) or set(reference) != {"span_id"}:
                raise ValueError("invalid_evidence_reference")
            source_id = reference["span_id"]
            if not isinstance(source_id, str) or source_id not in sources:
                raise ValueError("unverified_evidence_unknown_span")
            bound.append(
                {
                    "span_id": (original_ids or {}).get(source_id, source_id),
                    "quote": sources[source_id],
                }
            )
        result[key] = bound
    return result


class LLMGateway:
    def __init__(
        self,
        engine: Engine,
        settings: Settings,
        provider: LLMProvider,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        evaluation_model: str | None = None,
    ):
        self.engine = engine
        self.settings = settings
        self.provider = provider
        self.sleep = sleep
        if evaluation_model is not None and evaluation_model not in EVALUATION_MODELS:
            raise ValueError("unsupported_evaluation_model")
        self.routing = {
            task: replace(policy, model=evaluation_model) if evaluation_model else policy
            for task, policy in ROUTING.items()
        }
        self.pipeline_version = "model-evaluation-v1" if evaluation_model else "full-v44"

    async def execute[T: BaseModel](
        self,
        *,
        job_id: str,
        stage_id: str,
        logical_call_id: str,
        task_type: str,
        output_schema: type[T],
        owner: str,
        policy: TaskPolicy | None = None,
        context_spans: tuple[dict[str, str], ...] = (),
        instructions: str = "",
        validate_output: Callable[[T], None] | None = None,
        bind_evidence: bool = False,
        knowledge_edges: dict[str, tuple[str, str]] | None = None,
    ) -> T:
        configured = self.routing.get(task_type)
        if configured is None:
            raise ExecutionFailure("unknown_task")
        limits = policy or configured
        if limits.model != configured.model:
            raise ExecutionFailure("invalid_model")
        schema = request_schema(
            output_schema, bind_evidence=bind_evidence, knowledge_edges=knowledge_edges
        )
        provider_context = context_spans
        source_refs: dict[str, tuple[str, str]] = {}
        if bind_evidence:
            schema, provider_context, source_refs = prepare_source_request(schema, context_spans)
            if not source_refs:
                raise ExecutionFailure("insufficient_source_context")
        generation_settings: dict[str, Any] = (
            {"fake_deterministic": True}
            if self.settings.provider == "fake"
            else {"instructions": instructions, "reasoning_effort": "low"}
        )
        if bind_evidence:
            generation_settings["evidence_binding"] = "source-span-v2"
        if knowledge_edges is not None:
            generation_settings["edge_binding"] = "flow-edge-v1"
        # Estimate plain-text tokens with schema/framing headroom before reservation.
        input_estimate = estimate_input_tokens(schema, provider_context, instructions)
        if self.settings.provider != "fake" and input_estimate > limits.max_input_tokens:
            raise ExecutionFailure("context_budget_exceeded")
        # A previous owner may have lost a response. Commit recovery before guards
        # raise, so unknown cost remains visible even if no next attempt can start.
        with transaction(self.engine) as session:
            job = session.get(AnalysisJob, job_id)
            if (
                job is None
                or job.owner != owner
                or job.lease_until is None
                or job.lease_until <= time.time()
            ):
                raise ExecutionFailure("lease_lost")
            interrupted = session.scalars(
                select(LLMExecution).where(
                    LLMExecution.job_id == job_id, LLMExecution.status == "running"
                )
            )
            for previous in interrupted:
                previous.status = "interrupted"
                previous.cost_status = "unknown"
                previous.error_code = "response_lost"
                previous.finished_at = time.time()
        repair_error = None
        for _ in range(3):
            with transaction(self.engine) as session:
                job = session.get(AnalysisJob, job_id)
                if (
                    job is None
                    or job.owner != owner
                    or job.lease_until is None
                    or job.lease_until <= time.time()
                ):
                    raise ExecutionFailure("lease_lost")
                stage = session.get(StageExecution, stage_id)
                if stage is None or stage.job_id != job_id:
                    raise ExecutionFailure("invalid_stage")
                parsing = session.scalar(
                    select(StageExecution).where(
                        StageExecution.job_id == job_id, StageExecution.name == "parsing"
                    )
                )
                revision_id = (parsing.checkpoint or {}).get("revision_id") if parsing else None
                revision = session.get(PaperRevision, revision_id) if revision_id else None
                parser_version = revision.parser_version if revision else None
                content_hash = revision.content_hash if revision else None
                scoring_version = (
                    "unresolved" if self.settings.provider == "fake" else "stage1-v2/identity-v1"
                )
                input_hash = digest(
                    {
                        "revision": job.arxiv_revision,
                        "stage_input": stage.input_hash,
                        "context_spans": context_spans,
                        "knowledge_edges": knowledge_edges,
                        "content_hash": content_hash,
                    }
                )
                prompt_hash = digest(
                    instructions or "Phase 0 fake readiness check; no paper analysis"
                )
                fingerprint = digest(
                    {
                        "input": input_hash,
                        "schema": schema,
                        "task": task_type,
                        "provider": self.settings.provider,
                        "model": limits.model,
                        "settings": generation_settings,
                        "prompt": prompt_hash,
                        "prompt_version": "1",
                        "schema_version": "1",
                        "pipeline_version": "fake-pipeline-v1"
                        if self.settings.provider == "fake"
                        else self.pipeline_version,
                        "parser_version": "fake-v1"
                        if self.settings.provider == "fake"
                        else parser_version,
                        "scoring_version": scoring_version,
                        "language": "ja",
                        "policy": vars(limits),
                    }
                )
                attempts = list(
                    session.scalars(
                        select(LLMExecution)
                        .where(
                            LLMExecution.job_id == job_id,
                            LLMExecution.logical_call_id == logical_call_id,
                        )
                        .order_by(LLMExecution.attempt)
                    )
                )
                for previous in attempts:
                    if previous.metadata_json["fingerprint"] != fingerprint:
                        raise ExecutionFailure("logical_call_input_changed")
                    if previous.status == "completed" and previous.output is not None:
                        metadata = dict(previous.metadata_json)
                        metadata["cache_hit"] = True
                        previous.metadata_json = metadata
                        cached = output_schema.model_validate(previous.output)
                        if validate_output is not None:
                            validate_output(cached)
                        return cached
                if attempts and attempts[-1].error_code in {
                    "refusal",
                    "configuration",
                    "invalid_input",
                }:
                    raise ExecutionFailure(attempts[-1].error_code)
                if attempts and attempts[-1].metadata_json.get("retryable") is False:
                    raise ExecutionFailure(attempts[-1].error_code or "provider_error")
                deadline_at = (
                    attempts[0].started_at if attempts else time.time()
                ) + limits.deadline
                if len(attempts) >= 3:
                    raise ExecutionFailure("attempt_limit")
                if time.time() >= deadline_at:
                    raise ExecutionFailure("task_deadline")
                all_attempts = list(
                    session.scalars(select(LLMExecution).where(LLMExecution.job_id == job_id))
                )
                category = (
                    "jit_detail" if task_type.startswith("knowledge_detail") else "initial_analysis"
                )
                cap = 3 if category == "jit_detail" else 30
                if len(all_attempts) >= cap:
                    raise ExecutionFailure("job_attempt_limit")
                budget = (
                    self.settings.detail_budget_usd
                    if category == "jit_detail"
                    else self.settings.initial_budget_usd
                )
                used = sum(
                    e.reserved_usd
                    if e.cost_status in {"reserved", "unknown"}
                    else e.estimated_cost_usd or 0
                    for e in all_attempts
                )
                # Synthetic USD-per-token rates. No claim about real provider pricing.
                input_rate, output_rate = (
                    (0.000001, 0.000002) if limits.model == "gpt-5-nano" else (0.000002, 0.000004)
                )
                if self.settings.provider == "openai":
                    if limits.model not in RATES:
                        raise ExecutionFailure("unknown_model_price")
                    input_rate, output_rate = RATES[limits.model]
                reservation = (
                    limits.max_input_tokens * input_rate + limits.max_output_tokens * output_rate
                )
                if used + reservation > budget:
                    raise ExecutionFailure("budget_exceeded")
                metadata = {
                    "provider": self.settings.provider,
                    "requested_model": limits.model,
                    "returned_model": None,
                    "provider_request_id": None,
                    "generation_settings": {
                        k: v for k, v in generation_settings.items() if k != "instructions"
                    },
                    "prompt_version": "1",
                    "prompt_hash": prompt_hash,
                    "schema_version": "1",
                    "pipeline_version": "fake-pipeline-v1"
                    if self.settings.provider == "fake"
                    else self.pipeline_version,
                    "scoring_version": scoring_version,
                    "input_hash": input_hash,
                    "source_ids": [
                        s["span_id"] for s in context_spans if s.get("span_id") != "task-data"
                    ],
                    "parser_version": parser_version,
                    "content_hash": content_hash,
                    "fingerprint": fingerprint,
                    "task_type": task_type,
                    "source_alias_map": {alias: ref[0] for alias, ref in source_refs.items()},
                    "input_token_estimate": input_estimate,
                    "token_budget_version": TOKEN_BUDGET_VERSION,
                    "cache_hit": False,
                    "currency": "USD",
                    "price_table_version": "fake-v1"
                    if self.settings.provider == "fake"
                    else "openai-2026-10-30",
                    "simulated": self.settings.provider == "fake",
                    "input_tokens": None,
                    "cached_input_tokens": None,
                    "output_tokens": None,
                    "reasoning_tokens": None,
                    "latency_seconds": None,
                    "retry_reason": attempts[-1].error_code if attempts else None,
                }
                execution = LLMExecution(
                    job_id=job_id,
                    paper_id=job.paper_id,
                    stage_id=stage_id,
                    logical_call_id=logical_call_id,
                    attempt=len(attempts) + 1,
                    reserved_usd=reservation,
                    metadata_json=metadata,
                    cost_category=category,
                )
                session.add(execution)
                session.flush()
                execution_id = execution.id
                execution.metadata_json = {
                    **execution.metadata_json, "provider_client_request_id": execution_id,
                    "timing_version": "attempt-timing-v1",
                }
                request = LLMRequest(
                    job_id=job_id,
                    paper_id=job.paper_id,
                    stage_id=stage_id,
                    logical_call_id=logical_call_id,
                    task_type=task_type,
                    model=limits.model,
                    prompt_hash=prompt_hash,
                    input_hash=input_hash,
                    output_schema=schema,
                    max_input_tokens=limits.max_input_tokens,
                    max_output_tokens=limits.max_output_tokens,
                    attempt_timeout=limits.timeout,
                    task_deadline=deadline_at,
                    cost_category=category,
                    remaining_budget=budget - used - reservation,
                    repair_error=repair_error,
                    context_spans=provider_context,
                    generation_settings=generation_settings,
                    client_request_id=execution_id,
                )
            started = time.monotonic()
            wall_started = time.time()
            effective_timeout = min(limits.timeout, max(0, deadline_at - wall_started))
            attempt_timer = asyncio.timeout(effective_timeout)
            response = None
            validated = None
            error = None
            retryable = True
            retry_after = None
            http_status = None
            validation_reason = None
            timeout_origin = None
            timeout_phase = None
            try:
                async with attempt_timer:
                    response = await self.provider.generate(request)
                if response.input_tokens > limits.max_input_tokens:
                    raise InputLimitExceeded
                provider_output = response.output
                if knowledge_edges is not None:
                    provider_output = bind_knowledge_edges(provider_output, knowledge_edges)
                if bind_evidence:
                    provider_output = bind_source_evidence(
                        provider_output,
                        {alias: ref[1] for alias, ref in source_refs.items()},
                        original_ids={alias: ref[0] for alias, ref in source_refs.items()},
                    )
                validated = output_schema.model_validate(provider_output)
                if bind_evidence:
                    validate_display_references(provider_output)
                if validate_output is not None:
                    validate_output(validated)
            except InputLimitExceeded:
                error = "input_limit_exceeded"
                retryable = False
            except ProviderTimeout as exc:
                error = "timeout"
                timeout_origin = "transport"
                timeout_phase = exc.phase
            except TimeoutError:
                error = "timeout"
                timeout_origin = "gateway" if attempt_timer.expired() else "provider"
            except ValidationError:
                validated = None
                error = "schema_validation"
            except ValueError as exc:
                validated = None
                error = "schema_validation"
                candidate = exc.args[0] if exc.args else None
                if isinstance(candidate, str) and candidate in SAFE_VALIDATION_REASONS:
                    validation_reason = candidate
            except ProviderFailure as exc:
                retry_after = exc.retry_after
                http_status = exc.http_status
                error = (
                    exc.code
                    if exc.code
                    in {
                        "refusal",
                        "configuration",
                        "invalid_input",
                        "rate_limit",
                        "unavailable",
                        "output_limit",
                        "incomplete",
                        "invalid_response",
                        "response_not_completed",
                    }
                    else "provider_error"
                )
                retryable = exc.retryable and error not in {
                    "refusal",
                    "configuration",
                    "invalid_input",
                }
            except Exception:
                # Provider exception text may contain source content; never persist it.
                error = "provider_error"
                retryable = False
            with transaction(self.engine) as session:
                saved = session.get(LLMExecution, execution_id)
                assert saved is not None
                finished_at = time.time()
                saved.finished_at = finished_at
                saved.error_code = error
                saved.status = "completed" if validated is not None else "failed"
                saved.cost_status = "estimated" if response else "unknown"
                metadata = dict(saved.metadata_json)
                metadata["latency_seconds"] = time.monotonic() - started
                metadata["wall_elapsed_seconds"] = finished_at - wall_started
                metadata["attempt_timeout_seconds"] = limits.timeout
                metadata["effective_timeout_seconds"] = effective_timeout
                metadata["task_deadline_at"] = deadline_at
                metadata["timeout_origin"] = timeout_origin
                metadata["timeout_phase"] = timeout_phase
                metadata["retryable"] = retryable
                metadata["provider_http_status"] = http_status
                metadata["validation_reason"] = validation_reason
                metadata["validation_status"] = "valid" if validated is not None else error
                if response is not None:
                    metadata.update(
                        {
                            "input_tokens": response.input_tokens,
                            "cached_input_tokens": response.cached_input_tokens,
                            "output_tokens": response.output_tokens,
                            "reasoning_tokens": response.reasoning_tokens,
                            "returned_model": response.returned_model,
                            "provider_request_id": response.request_id,
                        }
                    )
                    saved.estimated_cost_usd = (
                        response.input_tokens * input_rate
                        + response.output_tokens * output_rate
                        - (
                            min(response.cached_input_tokens or 0, response.input_tokens)
                            * input_rate
                            * 0.9
                            if self.settings.provider == "openai"
                            else 0
                        )
                    )
                saved.metadata_json = metadata
                saved.output = validated.model_dump(mode="json") if validated is not None else None
            record(
                "llm_finished",
                job_id=job_id,
                stage_id=stage_id,
                attempt_id=execution_id,
                status="completed" if validated is not None else "failed",
                error_code=error,
                input_tokens=response.input_tokens if response else None,
                output_tokens=response.output_tokens if response else None,
                estimated_cost_usd=saved.estimated_cost_usd,
                retry_count=len(attempts),
            )
            if validated is not None:
                return validated
            if not retryable:
                raise ExecutionFailure(error or "provider_error")
            repair_error = validation_reason or error
            # No sleep after final attempt; durable count remains authoritative on restart.
            if len(attempts) < 2:
                delay = (
                    retry_after
                    if retry_after is not None
                    else 2 ** len(attempts) + random.uniform(0, 0.5)
                )
                await self.sleep(min(delay, max(0, deadline_at - time.time())))
        raise ExecutionFailure("attempt_limit")
