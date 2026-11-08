"""Candidate evidence, deterministic missing values and language checks; no network."""

import asyncio
import json

import pytest

from paper_review.application.full_analysis import generate_lineage
from paper_review.application.lineage_evidence import (
    RelevanceSelectionBatch,
    prepare_candidate_evidence,
    selected_relevance,
    symbolic_citation_owners,
    validate_contribution_language,
)
from paper_review.application.stage1_shortlist import Stage1Analysis
from paper_review.domain.models import (
    CandidatePaper,
    CandidateSet,
    Paper,
    Reference,
    ReferenceResolution,
    Stage1RankedCandidate,
    Stage1RankingResult,
    Stage1Score,
)
from paper_review.domain.understanding import Contributions, LineageProposal


def candidate(cid, label):
    return CandidatePaper(paper_id=cid, title=f"Prior {cid}", year=2010,
        source_references=[Reference(id=cid, paper_id="target", revision_id="r", ref_order=label,
                                     citation_key=f"[{label}]", raw_text="Prior reference")])


def span(sid, text, *, section="section", start=0):
    return {"span_id": sid, "text": text, "paper_id": "target", "revision": "r",
            "section_id": section, "section": "Method", "start_offset": str(start),
            "end_offset": str(start+len(text))}


def test_candidate_direct_spans_are_fairly_prioritized_and_neighbours_preserve_offsets():
    a, b, missing = candidate("a", 2), candidate("b", 3), candidate("missing", 4)
    first = span("a", "Prior [2] is used.")
    neighbour = span("context", "We extend its method.", start=len(first["text"]))
    second = span("b", "Other method [3].", section="other")
    gap = span("gap", "Unrelated missing interval.", start=1000)
    result, allowed = prepare_candidate_evidence(
        {"a": a, "b": b, "missing": missing}, [first, neighbour, gap, second],
    )
    assert [s["span_id"] for s in result] == ["a", "b", "context"]
    assert allowed == {"a": {"a", "context"}, "b": {"b"}, "missing": set()}
    assert json.loads(result[-1]["candidate_ids"]) == ["a"]
    assert json.loads(result[-1]["identifies_candidate_ids"]) == []
    assert json.loads(result[0]["identifies_candidate_ids"]) == ["a"]
    assert result[-1]["text"] == neighbour["text"]
    assert result[-1]["start_offset"] == neighbour["start_offset"]


def test_symbolic_citation_requires_unique_resolution_and_matching_body_revision():
    ref = candidate("a", 2).source_references[0].model_copy(update={"citation_key": "[LPP+20]"})
    resolution = ReferenceResolution(
        reference_id=ref.id, reference_order=ref.ref_order, candidate_id="a",
        status="resolved", method="exact_title_author_year",
    )
    bibliography = {**span("bibliography", "[LPP+20] Prior paper"), "section": "References"}
    earlier = {**span("earlier", "Prior [LPP+20]."), "revision": "older"}
    spans = [
        span("actual", "We compare against [LPP+20].", section="results"),
        span("other", "See [LPP+200] or [lpp+20]."), bibliography, earlier,
    ]
    candidates = {cid: candidate(cid, label) for cid, label in [("a", 2), ("b", 3)]}
    _, allowed = prepare_candidate_evidence(
        candidates, spans, symbolic_owners=symbolic_citation_owners([ref], [resolution])
    )
    assert allowed == {"a": {"actual"}, "b": set()}
    assert not symbolic_citation_owners([ref, ref.model_copy(update={"id": "duplicate"})],
                                        [resolution])
    assert not symbolic_citation_owners([ref], [resolution, resolution])
    assert not symbolic_citation_owners([ref], [resolution.model_copy(
        update={"status": "unresolved"}
    )])


def test_candidate_source_options_use_only_bound_request_aliases():
    from paper_review.llm.gateway import prepare_source_request, request_schema

    first = span("original-a", "Prior [2] is used.")
    second = span("original-b", "Other [3] is used.", section="other")
    prepared, _ = prepare_candidate_evidence({"a": candidate("a", 2), "b": candidate("b", 3)},
                                              [first, second])
    task = {"span_id": "task-data", "text": json.dumps({"candidates": []})}
    _, context, aliases = prepare_source_request(
        request_schema(RelevanceSelectionBatch, bind_evidence=True), (prepared[0], task),
    )
    options = json.loads(context[-1]["text"])["candidate_evidence_options"]
    assert options == {"a": {"identifying_source_ids": ["S0"], "allowed_source_ids": ["S0"]}}
    assert aliases["S0"][0] == "original-a"
    assert "candidate_evidence_options" not in json.loads(task["text"])


@pytest.mark.parametrize("identity", [None, "context", "a"])
def test_identity_is_explicitly_selected_and_neighbour_alone_cannot_identify(identity):
    anchor = span("a", "Prior [2] is used.")
    neighbour = span("context", "Its limitation motivates our method.", start=len(anchor["text"]))
    prepared, _ = prepare_candidate_evidence({"a": candidate("a", 2)}, [anchor, neighbour])
    sources = {s["span_id"]: s for s in prepared}
    draft = RelevanceSelectionBatch.model_validate({"evaluations": [{
        "candidate_id": "a", "score": 3, "contribution_ids": ["c"], "reason": "制約",
        "identifying_evidence": [] if identity is None else [
            {"span_id": identity, "quote": sources[identity]["text"]}],
        "evidence": [{"span_id": "context", "quote": neighbour["text"]}],
    }]})
    if identity == "a":
        value = selected_relevance(draft, prepared)
        assert [e.span_id for e in value.evaluations[0].evidence] == ["a", "context"]
    else:
        with pytest.raises(
            ValueError, match="relevance_(identity_source_required|invalid_identity_source)",
        ):
            selected_relevance(draft, prepared)


def test_english_only_contributions_rejected_without_translating_evidence():
    value = Contributions.model_validate({"problem": "計算負荷", "contributions": [{
        "id": "c", "description": "LoRAによる低ランク更新。",
        "evidence": [{"span_id": "s", "quote": "Original English evidence."}],
    }]})
    validate_contribution_language(value)
    value.contributions[0].description = "We introduce low-rank updates."
    with pytest.raises(ValueError, match="contribution_explanation_not_japanese"):
        validate_contribution_language(value)
    assert value.contributions[0].evidence[0].quote == "Original English evidence."


def analysis():
    target = Paper(id="target", title="Target", published_at="2020-01-01", embedding=[1., 0.])
    candidates = [candidate("a", 2), candidate("b", 3)]
    return Stage1Analysis(target=target, inventory=CandidateSet(
        target_paper_id="target", candidates=candidates,
    ), ranking=Stage1RankingResult(target_paper_id="target", ranked_candidates=[
        Stage1RankedCandidate(candidate=c, rank=i+1, score=Stage1Score(
            semantic_similarity=0.9, citation_relation=1, initial_score=0.9, coverage=0.75,
        )) for i, c in enumerate(candidates)
    ]))


@pytest.mark.parametrize("missing_all", [False, True])
def test_missing_prepared_evidence_is_null_and_never_sent_for_semantic_scoring(missing_all):
    stage1 = analysis()
    sources = [span("s", "We introduce a new method." if missing_all else "We extend method [2].")]
    evidence = [{"span_id": "s", "quote": sources[0]["text"]}]
    calls = []

    async def call(
        schema, stage_id, task, instruction, spans, extra=None, validator=None, **kwargs,
    ):
        assert validator is not None
        calls.append(task)
        if task == "contribution_extraction":
            value = Contributions.model_validate({"problem": "制約", "problem_evidence": evidence,
                "contributions": [{
                "id": "c", "description": "手法の拡張", "evidence": evidence,
            }]})
        elif task == "contribution_review":
            assert extra is not None
            value = schema.model_validate({"items": [{"item_id": item["item_id"], "claims": [{
                "text": item["summary"], "decision": "supported",
                "evidence_span_ids": item["selected_source_ids"], "reason": "引用で支持",
            }]} for item in extra["items"]]})
        elif task == "contribution_relevance":
            assert extra is not None
            assert [c["candidate_id"] for c in extra["candidates"]] == ["a"]
            assert kwargs["preserve_source_order"] is True
            assert json.loads(spans[0]["candidate_ids"]) == ["a"]
            assert "evidence" not in extra["contributions"][0]
            value = schema.model_validate({"evaluations": [{
                "candidate_id": "a", "score": 3, "contribution_ids": ["c"],
                "reason": "方法を拡張", "identifying_evidence": evidence, "evidence": evidence,
            }]})
        elif task == "lineage_edge_review":
            assert extra is not None
            value = schema.model_validate({"edges": [
                {"edge_id": item["edge_id"], "claims": [{
                    "text": item["relationship"], "decision": "supported",
                    "evidence_span_ids": item["selected_source_ids"], "reason": "原文で支持",
                }]} for item in extra["edges"]
            ]})
        else:
            assert task == "lineage"
            value = LineageProposal.model_validate({"predecessor_ids": ["a"],
                "merge_reason": None, "missing_reason": None, "edges": [{
                    "from_paper_id": "a", "to_paper_id": "target", "relationship": "方法を拡張",
                    "relationship_score": 3, "evidence_confidence": 1., "evidence": evidence,
                }]})
        validator(value)
        return value

    result = asyncio.run(generate_lineage(stage1.target, stage1, sources, "stage", call,
                                         adjustment_policy="identity-v1"))
    assert stage1.ranking.ranked_candidates[1].score.initial_score == 0.9
    rows = {r["candidate_id"]: r for r in result["relevance"]["evaluations"]}
    assert rows["b"]["score"] is None and rows["b"]["evidence"] == []
    assert result["evidence_preparation"]["b"]["status"] == "not_prepared"
    if missing_all:
        assert calls == ["contribution_extraction", "contribution_review"]
        assert result["status"] == "insufficient_evidence"
        assert all(r["score"] is None for r in rows.values())
    else:
        assert calls == [
            "contribution_extraction", "contribution_review", "contribution_relevance", "lineage",
            "lineage_edge_review",
        ]
        assert result["status"] == "completed"
        assert result["edges"][0]["confidence"] == pytest.approx(0.85)


def test_candidate_evidence_cannot_be_borrowed_from_another_candidate():
    stage1 = analysis()
    sources = [span("a", "Method [2]."), span("b", "Other method [3].", section="other")]

    async def call(
        schema, stage_id, task, instruction, spans, extra=None, validator=None, **kwargs,
    ):
        assert validator is not None
        if task == "contribution_extraction":
            value = Contributions.model_validate({"problem": "制約",
                "problem_evidence": [{"span_id": "a", "quote": "Method [2]."}],
                "contributions": [{
                "id": "c", "description": "方法", "evidence": [{"span_id": "a",
                                                                "quote": "Method [2]."}],
            }]})
        elif task == "contribution_review":
            assert extra is not None
            value = schema.model_validate({"items": [{"item_id": item["item_id"], "claims": [{
                "text": item["summary"], "decision": "supported",
                "evidence_span_ids": item["selected_source_ids"], "reason": "引用で支持",
            }]} for item in extra["items"]]})
        else:
            assert task == "contribution_relevance"
            value = schema.model_validate({"evaluations": [{
                "candidate_id": "a", "score": 3, "contribution_ids": ["c"], "reason": "方法",
                "identifying_evidence": [{"span_id": "a", "quote": "Method [2]."}],
                "evidence": [{"span_id": "b", "quote": "Other method [3]."}],
            }]})
        validator(value)
        return value

    with pytest.raises(ValueError, match="relevance_evidence_wrong_candidate"):
        asyncio.run(generate_lineage(stage1.target, stage1, sources, "stage", call,
                                     adjustment_policy="identity-v1"))


@pytest.mark.parametrize("decision", ["unsupported", "uncertain"])
def test_semantic_failure_stops_before_relevance_and_graph_without_retry(decision):
    stage1 = analysis()
    sources = [span("s", "We extend method [2].")]
    evidence = [{"span_id": "s", "quote": sources[0]["text"]}]
    calls = []

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        calls.append(task)
        assert validator is not None
        if task == "contribution_extraction":
            value = schema.model_validate({"problem": "制約", "problem_evidence": evidence,
                "contributions": [{"id": "c", "description": "方法", "evidence": evidence}]})
        else:
            assert task == "contribution_review" and extra is not None
            assert kw["require_all_sources"] is True and kw["bind_evidence"] is False
            value = schema.model_validate({"items": [{"item_id": item["item_id"], "claims": [{
                "text": item["summary"], "decision": (
                    "supported" if item["item_id"] == "problem" else decision
                ), "evidence_span_ids": ["s"] if item["item_id"] == "problem" else [],
                "reason": "根拠対応を点検",
            }]} for item in extra["items"]]})
        validator(value)
        return value

    result = asyncio.run(generate_lineage(stage1.target, stage1, sources, "stage", call,
                                         adjustment_policy="identity-v1"))
    assert calls == ["contribution_extraction", "contribution_review"]
    assert result["status"] == "insufficient_evidence" and result["edges"] == []
    assert result["contribution_review"]["items"][1]["claims"][0]["decision"] == decision
