"""Ground explanation in original evidence, not earlier generated explanations."""

from __future__ import annotations

from typing import Any

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
    "Extract prerequisite concepts bridging existing reasoning edges for a reader "
    "with basic ML/Transformers. Use short concept names, never full paper claims. "
    "Identify the precise causal link the reader cannot follow without each concept. "
    "In short_explanation, explain the dependency or operation and why it leads to "
    "the next step in 2-4 concise Japanese sentences when supported. "
    "Do not paraphrase steps or explain unrelated vocabulary. "
    "Prefer transferable prerequisite mechanisms over names of experimental trends. "
    "A table's scores or rank sensitivity are observations, not a prerequisite by "
    "themselves. Teach the underlying operation needed to understand the transition. "
    "For matrix-update/factorization transitions, when supported, explain what the "
    "factor dimensions and intermediate rank parameter control, how the factors "
    "produce the update, and which original weights are fixed versus learned. "
    "Do not conflate a low-dimensional adaptation hypothesis with a theorem about "
    "optimal update rank. Combine overlapping prerequisites rather than making a "
    "glossary of every term. Do not force matrix concepts into unrelated papers. "
    "The short explanation must teach the mechanism and apply it to the edge; "
    "numbers from a result table or a warning about missing text cannot replace "
    "that teaching. Do not invent a formal definition absent from the sources. "
    "Necessity: 0 unnecessary, 1 helpful, 2 important, "
    "3 causal transition difficult without it. Select a provided edge_id. "
    "At most 5 items; [] if none is required. " + EXPLANATION_RULES
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
    """Prioritize original mechanism evidence, then its exact continuations.

    Ordering is context selection only; it neither assigns scores nor appends
    evidence to a generated explanation. The model must explicitly select it.
    """
    by_id = {s["span_id"]: s for s in spans}
    primary, other = [], []
    for step in reasoning["steps"]:
        target = other if step["kind"] in {"experiment", "result", "conclusion"} else primary
        target.extend(e["span_id"] for e in step["evidence"])
    ordered: list[dict[str, str]] = []
    seen: set[str] = set()

    def add(sid):
        if sid in by_id and sid not in seen:
            ordered.append(by_id[sid])
            seen.add(sid)

    for sid in primary:
        add(sid)
    # Match exact original section intervals, never infer a continuation from
    # title similarity or from short aliases that change between requests.
    starts: dict[tuple[str, str, str, str], list[str]] = {}
    ends: dict[str, tuple[str, str, str, str]] = {}
    for s in spans:
        origin = (s.get("paper_id", ""), s.get("revision", ""), s.get("section_id", ""))
        if not all(origin):
            continue
        try:
            start, end = int(s["start_offset"]), int(s["end_offset"])
        except (KeyError, ValueError):
            continue
        if start < 0 or end <= start or end - start != len(s["text"]):
            continue
        starts.setdefault((*origin, str(start)), []).append(s["span_id"])
        ends[s["span_id"]] = (*origin, str(end))
    for sid in primary:
        if sid in ends:
            matches = starts.get(ends[sid], [])
            if len(matches) == 1:
                add(matches[0])
    for sid in other:
        add(sid)
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
