"""Deterministic section windows for contribution extraction; no paper-specific terms."""

import re


def contribution_sources(spans: list[dict[str, str]]) -> list[dict[str, str]]:
    """Balance mechanism, context and experiments, including each section's continuation.

    Two original excerpts form a scheduling window, never a new concatenated quote.
    Numeric subsection headings inherit an observed parent heading's role. Unknown
    headings remain available after the prioritized roles; nothing is rewritten.
    """
    sections: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    roots: dict[tuple[str, str, str], int] = {}
    roles: dict[tuple[str, str, str], int] = {}
    for span in spans:
        origin = (span.get("paper_id", ""), span.get("revision", ""))
        key = (*origin, span["section_id"])
        sections.setdefault(key, []).append(span)
        title = span.get("section", "").lower()
        number = re.match(r"^\s*(\d+)(\.\d+)*\.?\s*", title)
        if re.search(r"method|approach|model\s*architecture|proposed\s*model", title):
            role = 0
        elif re.search(r"abstract|introduction|problem\s*statement", title):
            role = 1
        elif re.search(
            r"experiment|result|evaluation|ablation|model\s*variations|benchmark", title,
        ):
            role = 2
        else:
            role = 3
        if number and not number.group(2):
            roots[(*origin, number.group(1))] = role
        roles[key] = role
    # Parsers may omit an empty main heading. An explicitly labelled experiment
    # subsection can identify that otherwise missing parent family as experimental.
    for key, group in sections.items():
        number = re.match(r"^\s*(\d+)\.\d+", group[0].get("section", ""))
        if number and roles[key] == 2:
            roots.setdefault((*key[:2], number.group(1)), 2)
    # Second pass also handles source lists whose parent header appears later.
    for key, group in sections.items():
        number = re.match(r"^\s*(\d+)\.\d+", group[0].get("section", ""))
        if roles[key] == 3 and number:
            roles[key] = roots.get((*key[:2], number.group(1)), 3)
    buckets = [[group for key, group in sections.items() if roles[key] == role]
               for role in range(4)]
    ordered = []
    maximum = max((len(group) for group in sections.values()), default=0)
    for start in range(0, maximum, 2):
        # Interleave roles to avoid a long architecture starving all result evidence.
        for index in range(max((len(bucket) for bucket in buckets), default=0)):
            for bucket in buckets[:3]:
                if index < len(bucket):
                    ordered.extend(bucket[index][start:start + 2])
        for group in buckets[3]:
            ordered.extend(group[start:start + 2])
    return ordered


CONTRIBUTION_PROMPT = (
    "Extract at most three principal contributions and the research problem. "
    "Write the problem and every description in Japanese. Preserve proper names/formulas. "
    "The problem field must state only the research problem, not the solution or results. "
    "Select explicit problem_evidence supporting every clause of the problem. "
    "Cover the distinct principal findings, rather than spending all three slots on "
    "variants of the same mechanism. For an empirical paper, include the central measured "
    "result when present in the supplied evidence; do not replace it with minor design details. "
    "Each contribution must express one narrowly scoped, independently verifiable claim. "
    "Select all supplied original spans needed to support EVERY clause of its description, "
    "up to four spans. Read adjacent continuations when a mechanism, formula or condition "
    "crosses a span boundary. An overview saying we describe a method cannot establish its "
    "mechanism. Distinguish existing weights from updates, assumptions from findings, and "
    "experimental evidence from general guarantees. Never bundle named models, datasets, "
    "comparisons or benefits absent from the selected evidence. Narrow or omit a claim if "
    "its required support is unavailable. No paper memory or guessed continuation. "
    "For a named method, select a name-identifying quotation as well as any separate "
    "mechanism quotation. Do not assume the paper title supplies a missing identification. "
    "Do not claim the full paper lacks information just because selected inputs lack it."
)
