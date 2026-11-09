"""Literal document headings, independent of paper content or ranking policy."""

import re


def is_bibliography_heading(title: str | None) -> bool:
    return bool(re.fullmatch(
        r"\s*(?:(?:\d+(?:\.\d+)*|[A-Z])[.)]?\s+)?"
        r"(?:references|bibliography)(?:\s+and\s+notes)?[.:]?\s*",
        title or "", re.I,
    ))
