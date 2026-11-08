"""Ground explanation in original evidence, not earlier generated explanations."""

from __future__ import annotations

import re
from collections import deque
from typing import Any

from paper_review.application.source_context import auxiliary_section, interleave, section_family
from paper_review.llm.gateway import adjacent_source_spans

EXPLANATION_RULES = """
Explain the mechanism connecting the supplied reasoning steps, not just their conclusion.
Preserve the source's subject, scope, conditions, direction and qualifications in Japanese.
Distinguish axes such as positions within one sequence versus independent examples,
training versus inference, and computational dependencies versus memory constraints.
Do not turn a restriction on one axis into a restriction on all parallel computation.
Every factual clause must be supported by supplied original sources. Generated step
summaries and concept labels are navigation hints, not authoritative evidence.
If the source does not establish a mechanism or an example, state that limitation;
do not supply an unsupported mechanism or invent results. Before answering, check
that the selected evidence supports the scope of each explanation, not merely its topic.
Put source references only in the structured evidence array, never as inline S/E codes.
Keep the explanation within what the selected evidence entries actually support;
if the allowed entries cannot support a long explanation, narrow it instead.
The input is selected excerpts, not an exhaustive paper. Missing support here does
not establish absence from the paper. Scope any limitation to the quotations selected
for this explanation; never say the paper lacks a mechanism or formula merely because
an excerpt is cut off. Generated navigation text is not a substitute for original evidence.
"""

SHORT_KNOWLEDGE_PROMPT = (
    "Extract prerequisites bridging the supplied reasoning edges for a reader with "
    "basic ML/Transformers. Return at most five distinct concepts, necessity 3 only; "
    "[] when none is required. Necessity: 0 unnecessary, 1 helpful, 2 important, "
    "3 the transition is difficult to follow without it. Select a supplied edge_id. "
    "Before selecting concepts, examine all transitions for distinct missing operations "
    "or assumptions. Allocate the limited items across those gaps before elaborating "
    "one mechanism. Two details of the same operation should normally share one item; "
    "do not spend separate items on an operation and its motivation while another "
    "supported transition remains unexplained. This is not a quota for each step. "
    "Consider the optimization objective and its constraints or penalties separately "
    "from parameter transformations, learning signals, data use, control decisions and "
    "measurement assumptions only where the original text makes them necessary. "
    "Naming a coefficient, reference distribution or constraint in a step does not "
    "teach what is optimized, what limits it or how that tradeoff acts. When needed, "
    "explain those roles even if another item already teaches a change of variables. "
    "Before merging items, check that each distinct necessary operation is still "
    "taught; sharing a symbol does not make an objective and a transformation the "
    "same prerequisite. Select no more than five items, without filling a quota. "
    "Scores and empirical trends alone are not prerequisite mechanisms. A mechanism "
    "mentioned in a step is not thereby explained, but omit already-taught operations. "
    "Use short concept names. In 2-4 concise Japanese sentences, teach the missing "
    "dependency or operation and apply it to the selected edge, rather than paraphrasing "
    "the steps or defining unrelated vocabulary. Combine related prerequisites only "
    "when the selected original quotations support every component. For equations, "
    "explain variable roles, the substitution and terms that cancel or remain invariant; "
    "select both the equation and its necessary definitions. For factorization, explain "
    "dimensions, the rank constraint, how the update is formed and which weights learn "
    "or stay fixed; preserve the distinction between a hypothesis and a guarantee. "
    "Do not introduce mathematics irrelevant to the actual transition. "
    "Select the edge and original evidence before composing the explanation. Check "
    "each factual clause, including the final sentence connecting it to the next "
    "step, against that item's own selected evidence. Another item's evidence or "
    "a step summary cannot supply a missing architecture, subject or condition. "
    "If a useful connection needs another original passage, explicitly select it "
    "in this item within its four-source limit. Otherwise omit or narrow the "
    "secondary connection, while retaining the supported mechanism. Do not repair "
    "missing support by adding a plausible connective sentence. "
    "Preserve the source's level of generality: a property established for one "
    "construction or distribution does not establish it for all related methods. "
    "Source ordering and step summaries are navigation, not necessity judgments or "
    "evidence. Do not infer that earlier excerpts deserve more output items. Never "
    "use evaluation reference lists or missing-source warnings as teaching material. "
    + EXPLANATION_RULES
)


DETAIL_PROMPT = (
    "Explain this prerequisite more deeply in Japanese, only as needed for the "
    "specified reasoning transition. Explain the mechanism and its assumptions; "
    "use an intuitive example only if supported. Admit missing explanatory sources. "
    "Write the actual mechanism in mechanism and the specific step connection in transition; "
    "do not write a preface, promise, generic summary or explanation of how you will answer. "
    "Select only sources supporting these sections; the application supplies verbatim quotes. "
    + EXPLANATION_RULES
)


def short_knowledge_inputs(
    reasoning: dict[str, Any],
    spans: list[dict[str, str]],
) -> tuple[list[dict[str, str]], dict[str, Any], dict[str, tuple[str, str]]]:
    """Prioritize original mechanism evidence, then its exact neighbours.

    Ordering is context selection only; it neither assigns scores nor appends
    evidence to a generated explanation. The model must explicitly select it.
    """
    by_id = {s["span_id"]: s for s in spans}
    theoretical_families = {
        section_family(s) for s in spans
        if re.search(r"theor|deriv|preliminar", s.get("section", ""), re.I)
        and not auxiliary_section(s)
    }
    mechanism_families = theoretical_families | {
        section_family(s) for s in spans
        if re.search(
            r"background|method|approach|optimi[sz]|finetun|architecture",
            s.get("section", ""), re.I,
        ) and not auxiliary_section(s)
    }
    primary_groups, measurement_groups, other = [], [], []
    for step in reasoning["steps"]:
        families = {
            section_family(by_id[e["span_id"]]) for e in step["evidence"]
            if e["span_id"] in by_id
        }
        theoretical = (step["kind"] in {"result", "observation"}
                       and bool(families) and families <= theoretical_families)
        analysis = step["kind"] == "result" and any(
            re.search(r"analysis|understanding|ablation|robustness|sensitivity",
                      by_id[e["span_id"]].get("section", ""), re.I)
            for e in step["evidence"] if e["span_id"] in by_id
        )
        ids = list(dict.fromkeys(e["span_id"] for e in step["evidence"]
                                if e["span_id"] in by_id))
        # A step may cite an abstract before its actual method/evaluation text.
        # Keep every original ID, but prefer the specific original passage.
        ids.sort(key=lambda sid: (
            auxiliary_section(by_id[sid]),
            bool(re.match(r"^(?:\d+\.?\s*)?(?:abstract|introduction)\b",
                          by_id[sid].get("section", "").strip(), re.I)),
        ))
        if step["kind"] in {"approach", "hypothesis"} or theoretical or analysis:
            ids.sort(key=lambda sid: section_family(by_id[sid]) not in mechanism_families)
            primary_groups.append(ids)
        elif step["kind"] in {"experiment", "observation", "interpretation"}:
            measurement_groups.append(ids)
            other.extend(ids)
        else:
            other.extend(ids)
    # A single long step/family must not spend the context before another
    # mechanism or a measurement assumption gets its first original excerpt.
    primary = list(dict.fromkeys(interleave(primary_groups)))
    primary.sort(key=lambda sid: section_family(by_id[sid]) not in mechanism_families)
    seeds = [group[0] for group in primary_groups if group]
    measurement_seeds = [group[0] for group in measurement_groups if group]
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(sid):
        if sid in by_id and sid not in seen:
            ordered.append(by_id[sid])
            seen.add(sid)

    for sid in [*seeds, *measurement_seeds, *primary]:
        add(sid)
    # Match exact original section intervals, never infer a continuation from
    # title similarity or from short aliases that change between requests.
    previous: dict[str, str] = {}
    following: dict[str, str] = {}
    neighbours: dict[str, list[str]] = {}
    for left, right in adjacent_source_spans(tuple(spans)):
        a, b = left["span_id"], right["span_id"]
        previous[b], following[a] = a, b
        neighbours.setdefault(a, []).append(b)
        neighbours.setdefault(b, []).append(a)
    # A definition may precede the selected mechanism. Require the same exact
    # section/revision and a unique touching interval; never infer across gaps.
    for sid in primary:
        if sid in previous:
            add(previous[sid])
    for sid in primary:
        if sid in following:
            add(following[sid])
    # Technical names in a generated method step are retrieval hints only.
    # Offer original passages mentioning each name, rather than spending the
    # budget on repeated introductions before reaching named mechanisms.
    families = {section_family(by_id[sid]) for sid in primary if sid in by_id}
    source_revisions = {family[:2] for family in families}
    mechanism_sources = [
        s for s in spans if not auxiliary_section(s)
        and (s.get("paper_id", ""), s.get("revision", "")) in source_revisions
        and (section_family(s) in families
             or re.search(r"background|preliminar", s.get("section", ""), re.I))
    ]
    for step in reasoning["steps"]:
        if step["kind"] not in {"approach", "hypothesis"}:
            continue
        terms = dict.fromkeys(re.findall(
            r"(?<![A-Za-z0-9-])(?:[A-Za-z][A-Za-z0-9-]{2,}|[A-Z]{2})(?![A-Za-z0-9-])",
            step["summary"],
        ))
        for term in terms:
            match = next((
                s for s in mechanism_sources if s["span_id"] not in seen
                and re.search(rf"\b{re.escape(term)}\b", s["text"], re.I)
            ), None)
            if match is not None:
                add(match["span_id"])
    for sid in other:
        add(sid)
    # One adjacent excerpt can still stop before the actual definition. Offer
    # the remaining original mechanism family and foundational sections fairly,
    # before unrelated fallback text. These are options, never selected evidence.
    primary_sections = {
        (by_id[sid].get("paper_id", ""), by_id[sid].get("revision", ""),
         by_id[sid]["section_id"]) for sid in primary if sid in by_id
    }
    connected = set(primary)
    frontier = deque(primary)
    while frontier:
        for sid in neighbours.get(frontier.popleft(), []):
            if sid not in connected:
                connected.add(sid)
                frontier.append(sid)
    mechanism_sections: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for s in spans:
        if auxiliary_section(s):
            continue
        origin = (s.get("paper_id", ""), s.get("revision", ""), s["section_id"])
        if (s["span_id"] in connected
                or (origin not in primary_sections and section_family(s) in families)
                or (origin[:2] in source_revisions and re.search(
                    r"background|preliminar", s.get("section", ""), re.I,
                ))):
            mechanism_sections.setdefault(origin, []).append(s)
    for s in interleave(list(mechanism_sections.values())):
        add(s["span_id"])
    for s in spans:
        add(s["span_id"])
    refs = {f"E{i}": (e["from_step_id"], e["to_step_id"]) for i, e in enumerate(reasoning["edges"])}
    extra = {
        "source_scope": "selected_original_excerpts_not_exhaustive_paper",
        "flow": {
            "steps": [
                {"id": s["id"], "kind": s["kind"], "summary": s["summary"]}
                for s in reasoning["steps"]
            ],
            "edges": [{"edge_id": f"E{i}", **e} for i, e in enumerate(reasoning["edges"])],
        },
    }
    return ordered, extra, refs


def detail_context(item: dict[str, Any], reasoning: dict[str, Any]) -> dict[str, Any]:
    """Exclude the earlier draft/why fields so a false explanation is not propagated."""
    ids = {item["from_step_id"], item["to_step_id"]}
    return {
        "concept": item["concept"],
        "from_step_id": item["from_step_id"],
        "to_step_id": item["to_step_id"],
        "steps": [
            {"id": s["id"], "kind": s["kind"], "summary": s["summary"]}
            for s in reasoning.get("steps", [])
            if s["id"] in ids
        ],
    }
