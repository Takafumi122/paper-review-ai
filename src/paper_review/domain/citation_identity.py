"""Exact target-bibliography author/year aliases, shared by classification and edges."""

import re
from collections.abc import Iterator

from paper_review.domain.models import CandidatePaper


def author_year_aliases(candidate: CandidatePaper) -> set[tuple[str, int, str]]:
    if not candidate.authors or not candidate.authors[0].split():
        return set()
    # The target may cite the conference year, whereas S2 reports the preprint
    # year. This is a citation alias, never a change to publication chronology.
    cited_years = set()
    ambiguous = False
    for ref in candidate.source_references:
        text = re.sub(r"https?://\S+", "", ref.raw_text, flags=re.I)
        years = {
            (int(m[1]), m[2].lower()) for m in re.finditer(
                r"(?<![\w/.-])((?:19|20)\d{2})([a-z]?)(?![\w]|\.\s*\d)", text, re.I,
            )
        }
        if len(years) > 1:
            ambiguous = True
        else:
            cited_years.update(years)
    if ambiguous:
        # Multiple years/suffixes in one entry cannot safely establish an alias.
        return set()
    if not cited_years:
        if candidate.year is None:
            return set()
        cited_years = {(candidate.year, "")}
    return {(candidate.authors[0].split()[-1].lower(), year, suffix)
            for year, suffix in cited_years}


def author_year_matches(
    candidate: CandidatePaper, text: str, *, suffix: str | None = None,
) -> Iterator[re.Match[str]]:
    for surname, year, alias_suffix in sorted(author_year_aliases(candidate)):
        if suffix is not None and alias_suffix != suffix:
            continue
        pattern = (
            rf"(?<![A-Za-z]){re.escape(surname)}"
            rf"(?:\s+et\s+al\.?|\s*(?:&|and)\s+(?P<second>[A-Za-z-]+))?"
            rf"\s*[, (]\s*{year}{re.escape(alias_suffix)}(?![A-Za-z\d])"
        )
        for match in re.finditer(pattern, text, re.I):
            if re.search(r"(?<![A-Za-z])[A-Z][A-Za-z-]+\s*(?:&|and)\s*$", text[:match.start()]):
                continue
            names = candidate.authors or []
            if match["second"] and (
                len(names) != 2 or not names[1].split()
                or names[1].split()[-1].lower() != match["second"].lower()
            ):
                continue
            yield match
