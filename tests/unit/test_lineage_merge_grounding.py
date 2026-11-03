"""Synthetic exact-source fixtures test constraints, not scientific/live readiness."""

import asyncio
import json

import pytest

from paper_review.application import lora_stage2_verification as recorded
from paper_review.application.full_analysis import generate_lineage
from paper_review.application.lineage_merge_review import review_lineage_merge
from paper_review.application.stage1_shortlist import Stage1Analysis
from paper_review.domain.models import CandidatePaper, CandidateSet, DateProvenance, Paper
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.domain.understanding import (
    Contributions,
    LineageEdge,
    LineageProposal,
    LineageProposalDraft,
    validate_lineage,
)


def fixture():
    texts = {
        "a": "Alpha (2017) supplies retrieval used for our retrieval contribution.",
        "b": "Beta (2018) supplies generation used for our generation contribution.",
        "ca": "Our first contribution improves retrieval.",
        "cb": "Our second contribution improves generation.",
        "m": "Our method explicitly integrates the retrieval and generation contributions.",
    }
    sources = {sid: {"span_id": sid, "paper_id": "target", "revision": "r",
                     "section_id": sid, "section": "Method", "text": text,
                     "start_offset": "0", "end_offset": str(len(text))}
               for sid, text in texts.items()}

    def evidence(sid):
        return [{"span_id": sid, "quote": texts[sid]}]

    candidates = {pid: CandidatePaper(
        paper_id=pid, title=pid, authors=[f"A {author}"], year=year,
        publication_date=str(year), embedding=[1, 0], embedding_model="unit",
    ) for pid, author, year in [("a", "Alpha", 2017), ("b", "Beta", 2018)]}
    contributions = Contributions.model_validate({
        "problem": "検索と生成に制約がある", "problem_evidence": evidence("ca"),
        "contributions": [
            {"id": "C1", "description": "検索を改善する", "evidence": evidence("ca")},
            {"id": "C2", "description": "生成を改善する", "evidence": evidence("cb")},
        ],
    })
    proposal = LineageProposalDraft.model_validate({
        "predecessor_ids": ["a", "b"], "merge_reason": "検索と生成という異なる貢献を統合",
        "missing_reason": None, "edges": [
            {"from_paper_id": pid, "to_paper_id": "target", "relationship": "手法を利用",
             "relationship_score": 4, "evidence_confidence": 1, "evidence": evidence(pid)}
            for pid in candidates
        ], "merge_support": {
            "node_id": "target", "branches": [
                {"from_paper_id": pid, "contribution_id": cid, "evidence": evidence(pid)}
                for pid, cid in [("a", "C1"), ("b", "C2")]
            ], "integration_evidence": evidence("m"),
        },
    })
    target = Paper(id="target", title="Target", published_at="2022-01-01",
                   embedding=[1, 0], embedding_model="unit")
    return target, candidates, sources, contributions, proposal


def verdict(schema, extra, decision="supported"):
    assert extra is not None
    return schema.model_validate({
        "node_id": extra["node_id"], "branches": [
            {"from_paper_id": b["from_paper_id"], "contribution_id": b["contribution_id"],
             "decision": "supported", "branch_evidence_ids": b["branch_source_ids"],
             "contribution_evidence_ids": b["contribution_source_ids"], "reason": "原文で支持"}
            for b in extra["branches"]
        ], "distinct_lineages_decision": "supported",
        "distinct_lineage_evidence_ids": list(dict.fromkeys(
            q for b in extra["branches"] for q in b["branch_source_ids"]
        )), "integration_decision": decision,
        "integration_evidence_ids": extra["integration_source_ids"] if decision == "supported"
        else [], "reason": "原文で支持" if decision == "supported" else "統合を示す根拠不足",
    })


def test_complete_grounding_accepts_structurally_valid_target_merge():
    target, candidates, sources, contributions, proposal = fixture()
    edges = validate_lineage(proposal, target, candidates, sources, contributions=contributions)
    assert len(edges) == 2 and all(e["status"] == "supported" for e in edges)


@pytest.mark.parametrize("change,reason", [
    ("missing", "support_required"),
    ("unknown_contribution", "unknown_contribution"), ("wrong_node", "node_mismatch"),
    ("duplicate_branch", "coverage_mismatch"), ("foreign_source", "wrong_scope"),
    ("invented_quote", "quote_mismatch"), ("no_contributions", "contributions_required"),
])
def test_merge_reason_alone_ids_and_unbound_evidence_fail_closed(change, reason):
    target, candidates, sources, contributions, proposal = fixture()
    support = proposal.merge_support
    assert support is not None
    if change == "missing":
        proposal.merge_support = None
    elif change == "unknown_contribution":
        support.branches[1].contribution_id = "C99"
    elif change == "wrong_node":
        support.node_id = "a"
    elif change == "duplicate_branch":
        support.branches[1].from_paper_id = "a"
    elif change == "foreign_source":
        sources["m"]["paper_id"] = "unrelated"
    elif change == "invented_quote":
        support.integration_evidence[0].quote = "Invented integration."
    elif change == "no_contributions":
        contributions = None
    with pytest.raises(ValueError, match=reason):
        validate_lineage(proposal, target, candidates, sources, contributions=contributions)


def test_intermediate_merge_uses_actual_incoming_branches():
    target, candidates, sources, contributions, proposal = fixture()
    candidates["j"] = CandidatePaper(paper_id="j", title="Intermediate", authors=["A Joint"],
                                      year=2020, publication_date="2020", date_provenance=(
        DateProvenance(kind="publication_year", value="2020", precision="year", source_id="unit")
    ))
    sources["j"] = {"paper_id": "target", "section": "Method",
                     "text": "We extend Joint (2020)."}
    for edge in proposal.edges:
        edge.to_paper_id = "j"
    proposal.predecessor_ids.append("j")
    proposal.edges.append(LineageEdge.model_validate(proposal.edges[0].model_dump() | {
        "from_paper_id": "j", "to_paper_id": "target",
        "evidence": [{"span_id": "j", "quote": "We extend Joint (2020)."}],
    }))
    assert proposal.merge_support is not None
    proposal.merge_support.node_id = "j"
    assert len(validate_lineage(proposal, target, candidates, sources,
                                contributions=contributions)) == 3


def test_old_proposal_is_readable_but_an_old_unproven_merge_is_not_accepted():
    target, candidates, sources, contributions, proposal = fixture()
    raw = proposal.model_dump(exclude={"merge_support"})
    old = LineageProposal.model_validate(raw)
    assert old.merge_support is None
    with pytest.raises(ValueError, match="support_required"):
        validate_lineage(old, target, candidates, sources, contributions=contributions)


@pytest.mark.parametrize("decision", ["supported", "unsupported", "uncertain", "wrong_scope"])
def test_review_is_bound_to_each_items_selected_quotations(decision):
    _, _, _, contributions, proposal = fixture()

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        assert validator is not None
        assert task == "lineage_merge_review" and kw["require_all_sources"]
        assert kw["bind_evidence"] is False
        value = verdict(schema, extra, decision)
        validator(value)
        return value

    result, bindings = asyncio.run(review_lineage_merge(proposal, contributions, "s", call))
    assert result.supported == (decision == "supported")
    assert len(bindings) == 5


def test_supported_review_cannot_borrow_another_branchs_quote():
    _, _, _, contributions, proposal = fixture()

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        assert validator is not None
        value = verdict(schema, extra)
        value.branches[0].branch_evidence_ids = value.branches[1].branch_evidence_ids
        validator(value)
        return value

    with pytest.raises(ValueError, match="unselected_evidence"):
        asyncio.run(review_lineage_merge(proposal, contributions, "s", call))


@pytest.mark.parametrize("change", ["one_branch_only", "contribution_quote", "unknown_quote"])
def test_distinct_lineages_need_selected_branch_evidence_for_both_lines(change):
    _, _, _, contributions, proposal = fixture()

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        assert validator is not None and extra is not None
        value = verdict(schema, extra)
        value.distinct_lineage_evidence_ids = (
            extra["branches"][0]["branch_source_ids"] if change == "one_branch_only" else
            extra["branches"][0]["contribution_source_ids"] if change == "contribution_quote"
            else ["unknown"]
        )
        validator(value)
        return value

    with pytest.raises(ValueError, match="missing_evidence|unselected_evidence"):
        asyncio.run(review_lineage_merge(proposal, contributions, "s", call))


@pytest.mark.parametrize("decision", ["unsupported", "uncertain", "wrong_scope"])
def test_supported_integration_cannot_override_unverified_distinct_lines(decision):
    _, _, _, contributions, proposal = fixture()

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        assert validator is not None
        value = verdict(schema, extra)
        value.distinct_lineages_decision = decision
        value.distinct_lineage_evidence_ids = []
        validator(value)
        return value

    value, _ = asyncio.run(review_lineage_merge(proposal, contributions, "s", call))
    assert not value.supported


def test_two_selected_quotes_in_one_original_span_remain_separate():
    _, _, sources, contributions, proposal = fixture()
    sources["a"]["text"] += " " + sources["ca"]["text"]
    contributions.contributions[0].evidence[0].span_id = "a"

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        assert validator is not None
        value = verdict(schema, extra)
        validator(value)
        return value

    _, bindings = asyncio.run(review_lineage_merge(proposal, contributions, "s", call))
    assert len([q for q in bindings.values() if q["original_span_id"] == "a"]) == 2


def test_recorded_lora_merge_requires_grounding_then_semantic_review_even_with_same_contribution():
    case, analysis, checkpoint = asyncio.run(recorded.inputs())
    raw = json.loads((recorded.OUTPUT / "graph-results.json").read_text())["lineage"]
    data = {"predecessor_ids": [n["id"] for n in raw["nodes"]],
            "merge_reason": raw["merge_reason"], "missing_reason": raw["missing_reason"],
            "edges": [{k: v for k, v in e.items() if k not in {"confidence", "status"}}
                      for e in raw["edges"]]}
    proposal = LineageProposal.model_validate(data)
    candidates = {r.candidate.paper_id: r.candidate for r in analysis.ranking.top_k()}
    sources = {s["span_id"]: s for s in case["spans"]}
    contributions = Contributions.model_validate(checkpoint["contributions"])
    with pytest.raises(ValueError, match="support_required"):
        validate_lineage(proposal, analysis.target, candidates, sources,
                         contributions=contributions)
    data["merge_support"] = {
        "node_id": analysis.target.id,
        "branches": [{"from_paper_id": e["from_paper_id"], "contribution_id": "C1",
                      "evidence": e["evidence"]} for e in data["edges"]],
        "integration_evidence": data["edges"][0]["evidence"],
    }
    # ID equality no longer rejects a proposal mechanically. It still needs the
    # independent semantic integration review; this is not a positive verdict.
    assert len(validate_lineage(
        LineageProposal.model_validate(data), analysis.target, candidates, sources,
        contributions=contributions,
    )) == 2


@pytest.mark.parametrize("decision", ["supported", "unsupported", "uncertain"])
@pytest.mark.parametrize("shared_contribution", [False, True])
def test_shared_generator_accepts_supported_review_or_abstains_without_regenerating(
    decision, shared_contribution,
):
    target, candidates, sources, contributions, proposal = fixture()
    if shared_contribution:
        contributions.contributions = contributions.contributions[:1]
        contributions.contributions[0].description = "検索と生成を明示的に統合する"
        contributions.contributions[0].evidence = [
            type(contributions.contributions[0].evidence[0])(
                span_id="m", quote=sources["m"]["text"],
            ),
        ]
        assert proposal.merge_support is not None
        for branch in proposal.merge_support.branches:
            branch.contribution_id = "C1"
    inventory = CandidateSet(target_paper_id="target", candidates=list(candidates.values()))
    analysis = Stage1Analysis(target=target, inventory=inventory, ranking=rank_stage1_candidates(
        target, inventory, citation_context_classifications={"a": "core_foundation",
                                                            "b": "core_foundation"},
    ))
    calls = []

    async def call(schema, stage_id, task, instruction, spans, extra=None, validator=None, **kw):
        assert validator is not None
        assert extra is not None or task == "contribution_extraction"
        support = proposal.merge_support
        assert support is not None
        calls.append(task)
        if task == "contribution_extraction":
            value = contributions
        elif task == "contribution_review":
            assert extra is not None
            value = schema.model_validate({"items": [
                {"item_id": item["item_id"], "claims": [{
                    "text": item["summary"], "decision": "supported", "reason": "原文で支持",
                    "evidence_span_ids": item["selected_source_ids"],
                }]} for item in extra["items"]
            ]})
        elif task == "contribution_relevance":
            value = schema.model_validate({"evaluations": [
                {"candidate_id": pid, "score": 4, "contribution_ids": [cid], "reason": "基盤",
                 "identifying_evidence": support.branches[i].evidence,
                 "evidence": support.branches[i].evidence}
                for i, (pid, cid) in enumerate(
                    (b.from_paper_id, b.contribution_id) for b in support.branches
                )
            ]})
        elif task == "lineage":
            value = schema.model_validate(proposal.model_dump())
        else:
            assert task == "lineage_merge_review"
            value = verdict(schema, extra, decision)
        validator(value)
        return value

    result = asyncio.run(generate_lineage(target, analysis, list(sources.values()), "s", call,
                                          adjustment_policy="identity-v1"))
    assert calls.count("lineage") == calls.count("lineage_merge_review") == 1
    if decision == "supported":
        assert result["status"] == "completed" and len(result["edges"]) == 2
        assert result["merge_review"]["supported"]
    else:
        assert result["status"] == "insufficient_evidence" and not result["edges"]
        assert len(result["unverified_proposal"]["edges"]) == 2
