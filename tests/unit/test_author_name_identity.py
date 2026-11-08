import pytest

from paper_review.domain.citation_identity import author_surname, author_year_matches
from paper_review.domain.models import CandidatePaper, Reference


@pytest.mark.parametrize("name,expected", [
    ("Jason Wei", "wei"), ("Wei, Jason", "wei"), ("Ahn,Michael", "ahn"),
    ("John Smith, Jr.", "smith"), ("de la Cruz, Juan", "cruz"), ("", ""),
])
def test_explicit_name_order(name, expected):
    assert author_surname(name) == expected


def test_comma_format_does_not_turn_given_name_into_citation_identity():
    candidate = CandidatePaper(paper_id="cot", title="CoT", authors=["Wei, Jason"], year=2023,
                               source_references=[Reference(
                                   id="ref",paper_id="target",revision_id="v",ref_order=1,
                                   raw_text="Jason Wei. Chain of thought. 2022.",
                               )])
    assert list(author_year_matches(candidate, "Wei et al. (2022)"))
    assert not list(author_year_matches(candidate, "Jason et al. (2022)"))
    assert not list(author_year_matches(candidate, "Wei et al. (2023)"))
