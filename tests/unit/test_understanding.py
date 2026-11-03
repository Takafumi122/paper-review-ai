"""Semantic-output validation is independent of the model/provider."""

import pytest

from paper_review.domain.models import CandidatePaper, DateProvenance, Paper, Reference, Stage1Score
from paper_review.domain.understanding import (
    Evidence,
    KnowledgeDetailDraft,
    LineageProposal,
    ReasoningFlow,
    ReasoningFlowDraft,
    ReasoningStep,
    ReasoningStepDraft,
    ReasoningStepReview,
    Relevance,
    candidate_is_identified,
    final_score,
    validate_evidence,
    validate_lineage,
    validate_reasoning,
    validate_reasoning_review,
)

SOURCES = {"s": {"paper_id": "target", "text": "Prior work by Author (2020) has a limitation."}}


def test_lineage_identity_requires_actual_labels_and_bibliography_author_year():
    candidate = CandidatePaper(
        paper_id="candidate", title="Measuring the Intrinsic Dimension", year=2018,
        authors=["Chunyuan Li", "Heerad Farkhoor"], source_references=[Reference(
            id="r", paper_id="target", revision_id="rev", ref_order=2,
            raw_text="Chunyuan Li et al. Intrinsic Dimension. 2018a.",
        )],
    )
    for text in ["Li et al. (2018b)", "Li & Liang (2018)", "[2]", candidate.title]:
        assert not candidate_is_identified(candidate, [Evidence(span_id="s", quote=text)], SOURCES)
    assert candidate_is_identified(
        candidate, [Evidence(span_id="s", quote="Li et al. (2018a)")], SOURCES,
    )
    candidate.source_references[0].citation_key = "[2]"
    assert candidate_is_identified(
        candidate, [Evidence(span_id="s", quote="Earlier methods [1,2].")], SOURCES,
    )


def test_reasoning_draft_keeps_reported_facts_and_explanation_in_the_reviewed_text():
    from pydantic import ValidationError

    original = {
        "id": "a",
        "kind": "observation",
        "summary": "層あたりの計算量が示される。",
        "evidence": [{"span_id": "s", "quote": "Prior work"}],
    }
    with pytest.raises(ValidationError):
        ReasoningFlowDraft.model_validate({"steps": [original], "edges": [], "limitations": []})
    draft = ReasoningFlowDraft.model_validate(
        {
            "steps": [
                {
                    **original,
                    "reader_explanation": "逐次段数と計算量は異なる指標として整理できる。",
                    "reader_explanation_evidence": original["evidence"],
                }
            ],
            "edges": [],
            "limitations": [],
        }
    )
    flow = draft.to_flow()
    assert flow.steps[0].summary.startswith(original["summary"])
    assert "補足（引用に基づく解釈）" in flow.steps[0].summary
    assert "異なる指標" in flow.steps[0].summary
    assert flow.steps[0].evidence == draft.steps[0].evidence
    assert "reader_explanation" not in flow.steps[0].model_dump()
    draft.steps[0].reader_explanation = None
    draft.steps[0].reader_explanation_evidence = []
    assert draft.to_flow().steps[0].summary == original["summary"]
    # Historical public results remain readable without the new generation-only field.
    assert ReasoningFlow.model_validate({"steps": [original], "edges": [], "limitations": []})


def test_part_sources_require_explicit_support_and_a_shared_four_source_limit():
    from pydantic import ValidationError

    def refs(ids):
        return [{"span_id": i, "quote": f"Original {i}"} for i in ids]

    base = {
        "id": "a",
        "kind": "observation",
        "summary": "原文の事実。",
        "evidence": refs("ab"),
        "reader_explanation": "根拠に基づく整理。",
        "reader_explanation_evidence": refs("bcd"),
    }
    step = ReasoningStepDraft.model_validate(base)
    assert [e.span_id for e in step.combined_evidence()] == list("abcd")
    for change, error in [
        ({"reader_explanation_evidence": []}, "missing_or_unused"),
        ({"reader_explanation": None}, "missing_or_unused"),
        ({"reader_explanation": None, "reader_explanation_evidence": [],
          "reader_explanation_context": "未使用の対象名"}, "unused_explanation_context"),
        ({"reader_explanation_evidence": refs("cde")}, "combined_evidence_limit"),
        (
            {"reader_explanation_evidence": [{"span_id": "a", "quote": "Different text"}]},
            "conflicting_quote",
        ),
    ]:
        with pytest.raises(ValidationError, match=error):
            ReasoningStepDraft.model_validate({**base, **change})
    without = ReasoningStepDraft.model_validate(
        {**base, "reader_explanation": None, "reader_explanation_evidence": []}
    )
    assert [e.span_id for e in without.combined_evidence()] == list("ab")
    repeated = ReasoningStepDraft.model_validate(
        {**base, "evidence": refs("aa"), "reader_explanation_evidence": refs("bb")}
    )
    assert [e.span_id for e in repeated.combined_evidence()] == list("ab")


def test_claim_review_requires_complete_summary_and_selected_support():
    step = ReasoningStep.model_validate(
        {
            "id": "a",
            "kind": "approach",
            "summary": "注意を並列計算する。生成もすべて並列化する。",
            "evidence": [{"span_id": "s", "quote": "We compute attention simultaneously."}],
        }
    )
    claims = [
        {
            "text": "注意を並列計算する。",
            "decision": "supported",
            "evidence_span_ids": ["s"],
            "reason": "注意の計算を支持",
        },
        {
            "text": "生成もすべて並列化する。",
            "decision": "wrong_scope",
            "evidence_span_ids": [],
            "reason": "生成全体の根拠はない",
        },
    ]
    review = ReasoningStepReview.model_validate({"claims": claims})
    validate_reasoning_review(review, step)
    assert not review.supported
    for invalid, error in [
        (claims[:1], "incomplete_coverage"),
        ([claims[1], claims[0]], "incomplete_coverage"),
        ([{**claims[0], "evidence_span_ids": ["neighbor-step"]}, claims[1]], "unselected_evidence"),
        ([{**claims[0], "evidence_span_ids": []}, claims[1]], "missing_evidence"),
        ([{**claims[0], "evidence_span_ids": ["s", "s"]}, claims[1]], "unselected_evidence"),
    ]:
        with pytest.raises(ValueError, match=error):
            validate_reasoning_review(ReasoningStepReview.model_validate({"claims": invalid}), step)


def test_detail_draft_requires_mechanism_and_transition_not_old_preface_contract():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        KnowledgeDetailDraft.model_validate(
            {
                "explanation": "以下は論文の主旨を要約したものです。",
                "evidence": [{"span_id": "s", "quote": "Prior work"}],
            }
        )


def test_evidence_must_match_known_source():
    validate_evidence([Evidence(span_id="s", quote="Author (2020) has a limitation.")], SOURCES)
    for eid, quote in [("unknown", "Author"), ("s", "Invented result")]:
        with pytest.raises(ValueError, match="unverified_evidence"):
            validate_evidence([Evidence(span_id=eid, quote=quote)], SOURCES)


def test_result_requires_a_supported_predecessor_without_inventing_edges():
    steps = [
        {
            "id": str(i),
            "kind": kind,
            "summary": kind,
            "evidence": [{"span_id": "s", "quote": "Prior work"}],
        }
        for i, kind in enumerate(["problem", "approach", "result", "conclusion"])
    ]
    edges = [
        {"from_step_id": "0", "to_step_id": "1", "relation": "leads_to"},
        {"from_step_id": "1", "to_step_id": "3", "relation": "supports"},
        {"from_step_id": "2", "to_step_id": "3", "relation": "supports"},
    ]
    flow = ReasoningFlow.model_validate({"steps": steps, "edges": edges, "limitations": []})
    with pytest.raises(ValueError, match="result_without_predecessor"):
        validate_reasoning(flow, SOURCES)
    flow.edges.append(type(flow.edges[0])(from_step_id="1", to_step_id="2", relation="leads_to"))
    validate_reasoning(flow, SOURCES)


def test_disconnected_interpretation_is_not_accepted_as_a_flow():
    flow = ReasoningFlow.model_validate(
        {
            "steps": [
                {
                    "id": str(i),
                    "kind": kind,
                    "summary": kind,
                    "evidence": [{"span_id": "s", "quote": "Prior work"}],
                }
                for i, kind in enumerate(["problem", "approach", "interpretation"])
            ],
            "edges": [{"from_step_id": "0", "to_step_id": "1", "relation": "leads_to"}],
            "limitations": [],
        }
    )
    with pytest.raises(ValueError, match="orphan_reasoning_step"):
        validate_reasoning(flow, SOURCES)
    validate_reasoning(ReasoningFlow(steps=flow.steps[:1], edges=[], limitations=[]), SOURCES)


def test_identity_adjustment_and_missing_values():
    stage1 = Stage1Score(semantic_similarity=0.8, citation_relation=1, citation_importance=None)
    relevance = Relevance(
        candidate_id="a",
        score=4,
        contribution_ids=["c"],
        reason="test",
        evidence=[Evidence(span_id="s", quote="Prior work")],
    )
    final = final_score(stage1, relevance, adjustment_policy="identity-v1")
    assert final["value"] == pytest.approx((0.4 + 0.25 * 0.8 + 0.2) / 0.85)
    assert final["coverage"] == pytest.approx(0.85) and final["status"] == "partial"
    assert final_score(stage1, relevance, adjustment_policy="unknown")["value"] is None
    assert (
        final_score(
            stage1, relevance.model_copy(update={"score": None}), adjustment_policy="identity-v1"
        )["value"]
        is None
    )
    zero = final_score(
        Stage1Score(), relevance.model_copy(update={"score": 0}), adjustment_policy="identity-v1"
    )
    assert zero["value"] == 0 and zero["coverage"] == 0.4


def graph(**changes):
    data = {
        "predecessor_ids": ["a"],
        "merge_reason": None,
        "missing_reason": None,
        "edges": [
            {
                "from_paper_id": "a",
                "to_paper_id": "target",
                "relationship": "limitation",
                "relationship_score": 4,
                "evidence_confidence": 1.0,
                "evidence": [{"span_id": "s", "quote": "Prior work by Author (2020)"}],
            }
        ],
    }
    data.update(changes)
    return LineageProposal.model_validate(data)


def candidates():
    return {
        "a": CandidatePaper(
            paper_id="a",
            title="Prior",
            authors=["A Author"],
            year=2020,
            date_provenance=DateProvenance(
                kind="publication_year", value="2020", precision="year", source_id="s2"
            ),
        )
    }


def test_lineage_confidence_is_computed_and_no_evidence_is_hard_zero():
    target = Paper(id="target", published_at="2022-01-01")
    edges = validate_lineage(graph(), target, candidates(), SOURCES)
    assert edges[0]["status"] == "supported" and edges[0]["confidence"] == 1
    proposal = graph()
    proposal.edges[0].evidence = []
    assert (
        validate_lineage(proposal, target, candidates(), SOURCES)[0]["status"]
        == "insufficient_evidence"
    )


def test_lineage_date_overlap_and_ineligible_candidates_fail_closed():
    with pytest.raises(ValueError, match="chronology"):
        validate_lineage(
            graph(), Paper(id="target", published_at="2020-06-01"), candidates(), SOURCES
        )
    prior = candidates()
    prior["a"].eligibility_status = "uncertain"
    with pytest.raises(ValueError, match="ineligible"):
        validate_lineage(graph(), Paper(id="target", published_at="2022-01-01"), prior, SOURCES)


def test_reasoning_edges_cannot_invent_steps_or_form_cycles():
    data = {
        "steps": [
            {
                "id": "a",
                "kind": "problem",
                "summary": "問題",
                "evidence": [{"span_id": "s", "quote": "a limitation"}],
            },
            {
                "id": "b",
                "kind": "approach",
                "summary": "方法",
                "evidence": [{"span_id": "s", "quote": "Prior work"}],
            },
        ],
        "edges": [{"from_step_id": "a", "to_step_id": "b", "relation": "leads_to"}],
        "limitations": [],
    }
    flow = ReasoningFlow.model_validate(data)
    validate_reasoning(flow, SOURCES)
    flow.edges[0].to_step_id = "a"
    with pytest.raises(ValueError, match="invalid_reasoning_edge"):
        validate_reasoning(flow, SOURCES)
