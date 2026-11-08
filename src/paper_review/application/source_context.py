"""Deterministic source scheduling using observed section headings and original IDs."""

import re
from collections.abc import Sequence


def interleave[T](groups: Sequence[Sequence[T]]) -> list[T]:
    return [
        group[index]
        for index in range(max(map(len, groups), default=0))
        for group in groups
        if index < len(group)
    ]


def section_family(span: dict[str, str]) -> tuple[str, str, str]:
    """A section number groups its observed subsections, within one source revision."""
    title = span.get("section", "").strip()
    number = re.match(r"^(\d+)(?:\.\d+)*(?:\.?\s|$)", title)
    family = f"number:{number[1]}" if number else f"section:{span['section_id']}"
    return span.get("paper_id", ""), span.get("revision", ""), family


def auxiliary_section(span: dict[str, str]) -> bool:
    title = span.get("section", "").strip()
    return bool(
        re.match(r"^(?:appendix\b|references\b|bibliography\b|acknowledg)", title, re.I)
        or re.match(r"^[A-Z](?:\.\d+(?:\.\d+)*\b|\s+[A-Za-z])", title)
    )
