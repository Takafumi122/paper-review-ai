import re
from enum import StrEnum
from urllib.parse import urlsplit


class JobStatus(StrEnum):
    QUEUED = "queued"
    PARSING = "parsing"
    RETRIEVING = "retrieving_candidates"
    RANKING = "ranking_candidates"
    LINEAGE = "building_lineage"
    REASONING = "building_reasoning"
    KNOWLEDGE = "building_knowledge"
    COMPLETED = "completed"
    FAILED = "failed"
    PARTIAL = "partial_completed"


TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.PARTIAL}
ORDER = list(JobStatus)[:8]


def transition(current: JobStatus, target: JobStatus, *, fake: bool = False) -> JobStatus:
    if current in TERMINAL:
        raise ValueError("terminal job cannot transition")
    allowed = {ORDER[ORDER.index(current) + 1], JobStatus.FAILED}
    if current != JobStatus.QUEUED:
        allowed.add(JobStatus.PARTIAL)
    if fake and current == JobStatus.PARSING:
        allowed.add(JobStatus.COMPLETED)
    # A failed Lineage can be skipped while retaining independent Reasoning.
    if current in {JobStatus.RETRIEVING, JobStatus.RANKING}:
        allowed.add(JobStatus.REASONING)
    if target not in allowed:
        raise ValueError("invalid job transition")
    return target


def arxiv_identity(url: str) -> tuple[str, str]:
    """Validate without fetching. Keep revisions distinct in job fingerprints."""
    if any(c.isspace() for c in url):
        raise ValueError("invalid arXiv URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"https", "http"}
        or parsed.netloc != "arxiv.org"
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("arXiv URL required")
    match = re.fullmatch(
        r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v[1-9]\d*)?)(?:\.pdf)?",
        parsed.path,
    )
    if not match:
        raise ValueError("invalid arXiv identifier")
    revision = match[1]
    base = re.sub(r"v[1-9]\d*$", "", revision)
    return f"arxiv:{base}", revision
