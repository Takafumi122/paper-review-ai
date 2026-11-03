"""Identity, original-source provenance, bounded calls and null-preserving scoring."""

import asyncio
from types import SimpleNamespace
from typing import cast

import pytest

from paper_review.application.citation_contexts import (
    CitationBatch,
    classify_contexts,
    context_batches,
    prepare_contexts,
    sentence_intervals,
    validate_decisions,
)
from paper_review.application.full_analysis import source_spans
from paper_review.application.stage1_shortlist import Stage1Analysis, apply_context_classifications
from paper_review.domain.models import CandidatePaper, CandidateSet, Paper, PaperSection, Reference
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.llm.gateway import ExecutionFailure
from paper_review.worker import Worker


def candidate(pid="a", *, key: str | None = "[2]", authors=None):
    return CandidatePaper(
        paper_id=pid, title=f"Prior method {pid}", authors=authors or ["Alice Smith"], year=2020,
        resolution_status="resolved", eligibility_status="eligible",
        source_references=[Reference(
            id=f"ref-{pid}", revision_id="r", paper_id="target", ref_order=2,
            raw_text="Reference", citation_key=key,
        )],
    )


def spans(text):
    return source_spans([PaperSection(
        id="section", paper_id="target", revision_id="r", section_index=0,
        title="Method", text=text, page_start=1, page_end=1,
    )])


def inventory(*candidates):
    return CandidateSet(target_paper_id="target", candidates=list(candidates))


def original_quote(option, pool):
    originals = {s["span_id"]: s for s in pool}
    return "".join(
        originals[r["span_id"]]["text"][
            r["start_offset"] - int(originals[r["span_id"]]["start_offset"]):
            r["end_offset"] - int(originals[r["span_id"]]["start_offset"])
        ] for r in option["source_ranges"]
    )


def test_numeric_group_exact_identity_not_bibliographic_order_guess():
    pool = spans("We use the attention method [2,19]. Compare [12].")
    contexts = prepare_contexts(inventory(candidate()), pool)
    assert len(contexts) == 1 and contexts[0]["source_ids"] == ["section:p1:c0"]
    assert contexts[0]["text"] == pool[0]["text"]
    assert prepare_contexts(inventory(candidate(key=None)), pool) == []
    assert prepare_contexts(inventory(candidate()), spans("Only [12] is cited.")) == []
    assert prepare_contexts(inventory(candidate(), candidate("b")), pool) == []


def test_author_year_split_continuation_and_ambiguity():
    text = "x " * 195 + "Smith et al. (2020) motivates our approach."
    pool = spans(text)
    contexts = prepare_contexts(inventory(candidate(key=None)), pool)
    assert len(contexts) == 1 and len(contexts[0]["source_ids"]) == 2
    assert contexts[0]["text"] == text
    for option in contexts[0]["support_options"]:
        quote = text[option["start"]:option["end"]]
        assert "Smith et al. (2020" in quote
        assert quote == original_quote(option, pool)
    assert prepare_contexts(
        inventory(candidate(key=None), candidate("b", key=None)), pool
    ) == []
    # Removing the continuation cannot complete a citation from memory.
    assert prepare_contexts(inventory(candidate(key=None)), pool[:1]) == []
    assert prepare_contexts(inventory(candidate(key=None)), spans("Smith et al. (2021)")) == []


def test_bibliography_year_suffix_prevents_a_b_candidate_confusion():
    li = candidate(key=None, authors=["Chunyuan Li", "Heerad Farkhoor"])
    li.year = 2018
    li.source_references[0].raw_text = "Chunyuan Li et al. Intrinsic Dimension. April 2018a."
    pool = spans("Li et al. (2018b) describes rank. Li & Liang (2018) describes kernels. "
                 "Smith (2017) and Li et al. (2018a) motivates our hypothesis.")
    contexts = prepare_contexts(inventory(li), pool)
    assert len(contexts) == 1
    assert contexts[0]["citation_anchors"] == ["Li et al. (2018a"]
    assert prepare_contexts(inventory(li), spans("Li et al. (2018) is ambiguous.")) == []
    # Suffix information must come from the target bibliography, never be invented.
    li.source_references[0].raw_text = "Chunyuan Li et al. Intrinsic Dimension. 2018."
    assert prepare_contexts(inventory(li), spans("Li et al. (2018b) describes rank.")) == []


@pytest.mark.parametrize("joiner", ["&", "and"])
def test_two_author_citation_checks_the_actual_second_author(joiner):
    li = candidate(key=None, authors=["Xiang Lisa Li", "Percy Liang"])
    li.year = 2021
    pool = spans(f"Li {joiner} Jones (2021) differs. Li {joiner} Liang (2021) is used.")
    contexts = prepare_contexts(inventory(li), pool)
    assert len(contexts) == 1
    assert contexts[0]["citation_anchors"] == [f"Li {joiner} Liang (2021"]
    liang = candidate("b", key=None, authors=["Percy Liang"])
    liang.year = 2021
    contexts = prepare_contexts(inventory(li, liang), pool)
    assert {c["candidate_id"] for c in contexts} == {"a"}


def test_target_bibliography_year_is_an_alias_not_a_publication_date_change():
    prior = candidate(key=None, authors=["Greg Yang", "Edward Hu"])
    prior.year = 2020
    prior.source_references[0].raw_text = (
        "Greg Yang and Edward Hu. Feature Learning. arXiv:2011.14522, May 2021. "
        "URL https://arxiv.org/abs/2011.14522."
    )
    pool = spans("We use scaling (Yang & Hu, 2021).")
    contexts = prepare_contexts(inventory(prior), pool)
    assert len(contexts) == 1 and contexts[0]["citation_anchors"] == ["Yang & Hu, 2021"]
    assert prior.year == 2020
    assert prepare_contexts(inventory(prior), spans("Yang & Hu (2020) is not this alias.")) == []
    assert all(original_quote(option, pool) == contexts[0]["text"][option["start"]:option["end"]]
               for option in contexts[0]["support_options"])


def test_two_distinct_bibliography_entries_preserve_same_paper_year_suffixes():
    prior = candidate(key=None, authors=["Jacob Devlin"])
    prior.year = 2018
    prior.source_references[0].raw_text = "Jacob Devlin. BERT, 2019a."
    prior.source_references.append(prior.source_references[0].model_copy(update={
        "id": "second", "raw_text": "Jacob Devlin. BERT, 2019b.",
    }))
    contexts = prepare_contexts(inventory(prior), spans(
        "Devlin (2019a) motivates this. Devlin (2019b) is compared."
    ))
    assert contexts[0]["citation_anchors"] == ["Devlin (2019a", "Devlin (2019b"]
    assert prepare_contexts(inventory(prior), spans("Devlin (2019) is ambiguous.")) == []


def test_competing_years_within_one_reference_are_not_guessed():
    prior = candidate(key=None)
    prior.source_references[0].raw_text = "Smith. Trends in 2019, published 2020."
    assert prepare_contexts(inventory(prior), spans("Smith (2020) uses a method.")) == []


def test_same_author_year_requires_actual_unambiguous_author_form():
    pair = candidate(key=None, authors=["Zeyuan Allen-Zhu", "Yuanzhi Li"])
    triple = candidate("b", key=None, authors=["Zeyuan Allen-Zhu", "Yuanzhi Li", "Zhao Song"])
    for prior in [pair, triple]:
        prior.year = 2018
        prior.source_references[0].raw_text = "Prior paper, 2019."
    contexts = prepare_contexts(inventory(pair, triple), spans(
        "Allen-Zhu et al. (2019) is ambiguous. Allen-Zhu & Li (2019) is identified."
    ))
    assert len(contexts) == 1
    assert contexts[0]["candidate_id"] == "a"
    assert contexts[0]["citation_anchors"] == ["Allen-Zhu & Li (2019"]


def test_doi_and_url_digits_do_not_become_bibliography_years():
    prior = candidate(key=None)
    prior.source_references[0].raw_text = (
        "Smith. SemEval-2017. Workshop, 2017. doi:10.18653/ v1/s17-2001. "
        "URL https://example.org/2020/item."
    )
    contexts = prepare_contexts(inventory(prior), spans("Smith (2017) is compared."))
    assert len(contexts) == 1


def test_same_first_author_year_can_be_distinguished_by_explicit_suffixes():
    a = candidate(key=None)
    b = candidate("b", key=None)
    a.source_references[0].raw_text = "Alice Smith. First paper. 2020a."
    b.source_references[0].raw_text = "Alice Smith. Second paper. 2020b."
    contexts = prepare_contexts(inventory(a, b), spans(
        "Smith et al. (2020a) is background. Smith et al. (2020b) is a comparison."
    ))
    assert {c["candidate_id"]: c["citation_anchors"] for c in contexts} == {
        "a": ["Smith et al. (2020a"], "b": ["Smith et al. (2020b"],
    }


def test_batches_bound_cost_and_validate_exact_coverage():
    context = {"candidate_id": "a", "candidate_title": "Prior", "source_ids": ["s"],
               "text": "x", "target_paper_id": "target", "citation_anchors": ["[2]"],
               "support_options": []}
    contexts = [{**context, "context_id": f"C{i}"} for i in range(80)]
    batches = context_batches(contexts)
    assert len(batches) == 4 and sum(map(len, batches)) == 48
    result = CitationBatch.model_validate({"decisions": [{
        "context_id": "C0", "candidate_id": "a", "classification": None,
        "support_id": None, "reason": "曖昧",
    }]})
    validate_decisions(result, contexts[:1])
    with pytest.raises(ValueError, match="coverage"):
        validate_decisions(result, contexts[:2])


@pytest.mark.parametrize("change", [
    {"candidate_id": "other"},
    {"support_id": "other-context-P0"},
    {"support_id": "C0-P999"},
    {"support_id": None},
    {"classification": None},
    {"supporting_quote": "fabricated"},
])
def test_candidate_binding_and_original_support_required(change):
    context = prepare_contexts(
        inventory(candidate()),
        spans("Prior work [2] has a limitation. We introduce a new architecture."),
    )
    value = {
        "context_id": "C0", "candidate_id": "a", "classification": "motivating_prior_work",
        "support_id": "C0-P0", "reason": "限界が動機となる",
    }
    validate_decisions(CitationBatch.model_validate({"decisions": [value]}), context)
    for option in context[0]["support_options"]:
        text = context[0]["text"][option["start"]:option["end"]]
        assert "[2]" in text and text == original_quote(option, spans(context[0]["text"]))
    with pytest.raises(ValueError):
        result = CitationBatch.model_validate({"decisions": [{**value, **change}]})
        validate_decisions(result, context)


def test_sentence_options_preserve_the_whole_inspiration_sentence_across_chunks():
    text = ("Preceding context. " * 20 + "We take inspiration from Smith et al. (2020), "
            "who show a low intrinsic dimension. We hypothesize a low intrinsic rank. "
            "Unfinished trailing text")
    pool = spans(text)
    contexts = prepare_contexts(inventory(candidate(key=None)), pool)
    assert len(contexts) == 1
    quotes = []
    for option in contexts[0]["support_options"]:
        quote = contexts[0]["text"][option["start"]:option["end"]]
        assert quote == original_quote(option, pool)
        assert "We take inspiration from Smith et al. (2020)" in quote
        assert not quote.endswith("Unfinished trailing text")
        assert option["boundary_basis"] == "sentence_punctuation"
        quotes.append(quote)
    assert quotes and any("We hypothesize a low intrinsic rank." in q for q in quotes)


def test_sentence_detection_handles_pdf_spacing_abbreviations_and_decimals():
    text = "See Fig. 2 and Smith et al. (2020) [2], with 3.14 units.Next sentence. Truncated"
    sentences = [text[a:b] for a, b in sentence_intervals(text)]
    assert sentences == [
        "See Fig. 2 and Smith et al. (2020) [2], with 3.14 units.", "Next sentence.",
    ]


def test_incomplete_sentence_or_source_gap_never_produces_completed_quote():
    text = "x" * 400 + "Mid-sentence Smith et al. (2020) without a period"
    assert prepare_contexts(inventory(candidate(key=None)), spans(text))[0]["support_options"] == []
    pool = spans("Start Smith et al. (2020) " + "x" * 800 + ".")
    # A gap must not let a later period complete a citation in a different chain.
    pool = [pool[0], *pool[2:]]
    assert prepare_contexts(inventory(candidate(key=None)), pool)[0]["support_options"] == []


@pytest.mark.parametrize("long", [False, True])
def test_page_separator_can_complete_stored_sentence_only_within_bounded_window(long):
    first = "We list Smith (2020), " + "x " * (850 if long else 210) + "\n17"
    text = first + "\n" + "and another dataset. The list is used for evaluation."
    section = PaperSection(
        id="s", paper_id="target", revision_id="r", section_index=0, title="Datasets",
        text=text, page_start=17, page_end=18, span_info={"source_spans": [
            {"start_offset": 0, "end_offset": len(first), "page": 17},
            {"start_offset": len(first) + 1, "end_offset": len(text), "page": 18},
        ]},
    )
    pool = source_spans([section])
    contexts = prepare_contexts(inventory(candidate(key=None)), pool)
    assert len(contexts) == 1
    if long:
        assert contexts[0]["support_options"] == []
    else:
        assert contexts[0]["support_options"]
        for option in contexts[0]["support_options"]:
            quote = contexts[0]["text"][option["start"]:option["end"]]
            assert quote == original_quote(option, pool)
            assert "\n17\nand another dataset." in quote
        separator = next(s for s in pool if s.get("source_kind") == "stored_separator")
        assert separator["page"] == "unknown"


def test_no_complete_quote_preserves_null_without_a_provider_call():
    class Gateway:
        async def execute(self, **kwargs):
            raise AssertionError("No semantic judgment is needed without an allowed quote")

    worker = SimpleNamespace(gateway=Gateway(), owner="test")
    result = asyncio.run(classify_contexts(
        cast(Worker, worker), "job", "stage", inventory(candidate()), spans("Unfinished [2] text")
    ))
    assert result["errors"] == [] and result["contexts_without_complete_options"] == 1
    context = result["contexts"][0]
    assert context["classification"] is None and context["supporting_quote"] is None
    assert context["decision_origin"] == "deterministic_no_support"
    assert context["evidence_source_ranges"] == []


def test_table_options_always_retain_header_and_cited_row():
    text = ("Table4: Parsing results.\nParser Training WSJ23F1\n"
            "Transformer semi-supervised 92.7\nSmith et al. (2020) [2] multi-task 93.0\n"
            "Jones generative 93.3\nWe discuss the results here.")
    pool = spans(text)
    contexts = prepare_contexts(inventory(candidate()), pool)
    assert contexts and contexts[0]["support_options"]
    for option in contexts[0]["support_options"]:
        quote = contexts[0]["text"][option["start"]:option["end"]]
        assert quote == original_quote(option, pool)
        assert option["support_kind"] == "table_context"
        assert "Table4: Parsing results." in quote and "WSJ23F1" in quote
        assert "[2] multi-task 93.0" in quote
        assert "We discuss" not in quote
    # Without the visible header there is no offered evidence for a metric claim.
    missing = prepare_contexts(inventory(candidate()), spans("Smith [2] multi-task 93.0\n"))
    assert missing[0]["support_options"] == []


def test_maximum_role_provenance_and_unknown_are_not_zero():
    inv = inventory(candidate(), candidate("b", key="[3]"))
    target = Paper(id="target", title="Target")
    analysis = Stage1Analysis(target=target, inventory=inv, ranking=rank_stage1_candidates(
        target, inv, target_references_fully_resolved=False,
    ))
    records = [
        {"candidate_id": "a", "status": "completed", "classification": role,
         "source_ids": [source]}
        for role, source in [("general_background", "s1"), ("core_foundation", "s2")]
    ] + [{"candidate_id": "b", "status": "completed", "classification": None,
          "source_ids": ["s3"]}]
    applied = apply_context_classifications(analysis, {"contexts": records})
    rows = {r.candidate.paper_id: r for r in applied.ranking.ranked_candidates}
    assert rows["a"].score.citation_importance == 1
    assert rows["a"].score.component_details["citation_importance"].source_ids == ["s2"]
    assert rows["b"].score.citation_importance is None
    assert rows["b"].score.missing_reasons["citation_importance"] == "citation_context_uncertain"
    assert analysis.ranking.ranked_candidates[0].score.citation_importance is None
    assert applied.ranking.weights == analysis.ranking.weights


@pytest.mark.parametrize("fail", [False, True])
def test_classifier_retains_valid_uncertainty_without_retry(fail):
    calls = []

    class Gateway:
        async def execute(self, **kwargs):
            calls.append(kwargs)
            assert kwargs["task_type"] == "citation_classification"
            if fail:
                raise ExecutionFailure("budget_exceeded")
            result = CitationBatch.model_validate({"decisions": [{
                "context_id": s["span_id"], "candidate_id": s["candidate_id"],
                "classification": None, "support_id": None, "reason": "文脈不足",
            } for s in kwargs["context_spans"]]})
            kwargs["validate_output"](result)
            return result

    worker = SimpleNamespace(gateway=Gateway(), owner="test")
    result = asyncio.run(classify_contexts(
        cast(Worker, worker), "job", "stage", inventory(candidate()), spans("We cite [2].")
    ))
    assert len(calls) == 1
    assert result["contexts_completed"] == (0 if fail else 1)
    assert result["contexts"][0]["classification"] is None
    assert result["contexts"][0]["source_ids"] == ["section:p1:c0"]


def test_prose_after_numeric_table_does_not_inherit_unrelated_table_as_a_sentence():
    text = ("Table 2: Scores.\nModel BLEU\nA 27.3\nB 28.4\n"
            "Residual Dropout We apply dropout [2] to every sub-layer. "
            "We also apply it to embeddings.")
    pool = spans(text)
    context = prepare_contexts(inventory(candidate()), pool)[0]
    assert context["support_options"]
    for option in context["support_options"]:
        quote = context["text"][option["start"]:option["end"]]
        assert "dropout [2]" in quote and "BLEU" not in quote and "Table 2" not in quote
        assert quote == original_quote(option, pool)
        assert option["support_kind"] == "prose"
        assert option["boundary_basis"] == "table_to_prose"


@pytest.mark.parametrize("batches", [[["C0", "C0"]], [["C999"]], [[]], [["C0"]] * 5])
def test_fixed_batches_cannot_expand_or_repeat_paid_work(batches):
    class NoCalls:
        async def execute(self, **kwargs):
            raise AssertionError("invalid frozen batch must be rejected before an LLM call")

    worker = cast(Worker, SimpleNamespace(gateway=NoCalls(), owner="test"))
    with pytest.raises(ValueError, match="invalid_frozen_citation_batches"):
        asyncio.run(classify_contexts(
            worker, "job", "stage", inventory(candidate()), spans("We use method [2]."),
            fixed_context_batches=batches,
        ))
