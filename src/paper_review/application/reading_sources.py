"""Selectable original reading windows, prepared before generation and evidence binding."""

import json
import re

from paper_review.llm.gateway import adjacent_source_spans

WINDOW_LIMIT = 1200
SENTENCE_END = re.compile(r"""[.!?]["')\]]*\s+(?=[A-Z(\[])""")


def reading_source_windows(spans: list[dict[str, str]]) -> list[dict[str, str]]:
    """Repartition only uniquely contiguous text, retaining original offsets and provenance."""
    groups: dict[tuple[str, str, str], list[dict[str, str]]] = {}
    for span in spans:
        start, end = int(span["start_offset"]), int(span["end_offset"])
        if start < 0 or end - start != len(span["text"]):
            raise ValueError("invalid_reading_source_interval")
        origin = (span["paper_id"], span["revision"], span["section_id"])
        groups.setdefault(origin, []).append(span)
    links = {
        (a["span_id"], b["span_id"]) for a, b in adjacent_source_spans(tuple(spans))
    }
    components: list[list[dict[str, str]]] = []
    for group in groups.values():
        current: list[dict[str, str]] = []
        for span in sorted(group, key=lambda s: int(s["start_offset"])):
            if current and (current[-1]["span_id"], span["span_id"]) not in links:
                components.append(current)
                current = []
            current.append(span)
        if current:
            components.append(current)
    windows = []
    for component in components:
        first = component[0]
        base = int(first["start_offset"])
        text = "".join(s["text"] for s in component)
        cursor = 0
        boundary = "section_start" if base == 0 else "source_component_start"
        while cursor < len(text):
            end = min(cursor + WINDOW_LIMIT, len(text))
            next_boundary = "source_component_end"
            if end < len(text):
                ends = [m.end() for m in SENTENCE_END.finditer(text, cursor, end)
                        if m.end() > cursor + WINDOW_LIMIT // 2]
                if ends:
                    end = ends[-1]
                    next_boundary = "sentence_boundary"
                else:
                    next_boundary = "length_limit"
            start_offset, end_offset = base + cursor, base + end
            touched = [
                s for s in component
                if int(s["start_offset"]) < end_offset and int(s["end_offset"]) > start_offset
            ]
            unchanged = (
                len(touched) == 1
                and int(touched[0]["start_offset"]) == start_offset
                and int(touched[0]["end_offset"]) == end_offset
            )
            pages = {s["page"] for s in touched if s["text"].strip()}
            windows.append({
                **(touched[0] if unchanged else first),
                "span_id": (touched[0]["span_id"] if unchanged else
                            f"{first['span_id']}:reading:{start_offset}-{end_offset}"),
                "start_offset": str(start_offset),
                "end_offset": str(end_offset),
                "text": text[cursor:end],
                "page": next(iter(pages)) if len(pages) == 1 else "unknown",
                "source_kind": "stored_reading_window",
                "source_span_ids": json.dumps([s["span_id"] for s in touched]),
                "start_boundary": boundary,
                "end_boundary": next_boundary,
            })
            cursor = end
            boundary = next_boundary
    return windows
