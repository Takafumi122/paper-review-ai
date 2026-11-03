import pytest

from paper_review.domain.jobs import ORDER, TERMINAL, JobStatus, arxiv_identity, transition


@pytest.mark.parametrize(("current", "target"), list(zip(ORDER[:-1], ORDER[1:], strict=True)))
def test_normal_transitions(current, target):
    assert transition(current, target) == target


@pytest.mark.parametrize("current", list(TERMINAL))
@pytest.mark.parametrize("target", list(JobStatus))
def test_terminal_is_immutable(current, target):
    with pytest.raises(ValueError):
        transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (JobStatus.QUEUED, JobStatus.COMPLETED),
        (JobStatus.QUEUED, JobStatus.PARTIAL),
        (JobStatus.PARSING, JobStatus.QUEUED),
        (JobStatus.PARSING, JobStatus.PARSING),
        (JobStatus.PARSING, JobStatus.COMPLETED),
        (JobStatus.LINEAGE, JobStatus.RANKING),
    ],
)
def test_invalid_transitions(current, target):
    with pytest.raises(ValueError):
        transition(current, target)


def test_fake_shortcut_and_failure_paths():
    assert transition(JobStatus.PARSING, JobStatus.COMPLETED, fake=True) == JobStatus.COMPLETED
    assert transition(JobStatus.RANKING, JobStatus.REASONING) == JobStatus.REASONING
    assert transition(JobStatus.KNOWLEDGE, JobStatus.PARTIAL) == JobStatus.PARTIAL
    assert transition(JobStatus.PARSING, JobStatus.FAILED) == JobStatus.FAILED


@pytest.mark.parametrize(
    "url",
    [
        "https://arxiv.org/abs/1706.03762?x=1",
        "https://user@arxiv.org/abs/1706.03762",
        "https://arxiv.org:443/abs/1706.03762",
        "https://arxiv.org/abs/1706.03762v0",
        "https://arxiv.org/abs/1706.03762\n",
        "https://arxiv.org/abs/../1706.03762",
    ],
)
def test_invalid_arxiv(url):
    with pytest.raises(ValueError):
        arxiv_identity(url)


def test_arxiv_normalization():
    assert arxiv_identity("http://arxiv.org/pdf/hep-th/9901001v2.pdf") == (
        "arxiv:hep-th/9901001",
        "hep-th/9901001v2",
    )
