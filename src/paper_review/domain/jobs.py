import re
from dataclasses import dataclass
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


def transition(
    current: JobStatus, target: JobStatus, *, fake: bool = False, detail: bool = False
) -> JobStatus:
    if current in TERMINAL:
        raise ValueError("terminal job cannot transition")
    allowed = {ORDER[ORDER.index(current) + 1], JobStatus.FAILED}
    if current != JobStatus.QUEUED:
        allowed.add(JobStatus.PARTIAL)
    if detail and current == JobStatus.PARSING:
        allowed.add(JobStatus.KNOWLEDGE)
    if fake and current == JobStatus.PARSING:
        allowed.add(JobStatus.COMPLETED)
    # A failed Lineage can be skipped while retaining independent Reasoning.
    if current in {JobStatus.RETRIEVING, JobStatus.RANKING}:
        allowed.add(JobStatus.REASONING)
    if target not in allowed:
        raise ValueError("invalid job transition")
    return target


@dataclass(frozen=True)
class ParsedArXivId:
    paper_id: str  # e.g. "arxiv:1706.03762"
    arxiv_id: str  # e.g. "1706.03762"
    version: str | None  # e.g. "v7" or None
    raw_revision: str  # e.g. "1706.03762v7" or "1706.03762"

    @property
    def canonical_abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self.raw_revision}"

    @property
    def canonical_pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.raw_revision}.pdf"


def normalize_arxiv_id(raw_input: str) -> ParsedArXivId:
    """Normalize any arXiv identifier (URL, raw ID, prefixed ID) into a unified identity.

    Supports:
      - 1706.03762
      - 1706.03762v7
      - arxiv:1706.03762
      - https://arxiv.org/abs/1706.03762
      - https://arxiv.org/pdf/1706.03762
      - https://arxiv.org/pdf/1706.03762.pdf
      - legacy formats e.g. cs/0101001 or math.PR/0501001v2
    """
    if not isinstance(raw_input, str):
        raise ValueError("arXiv identifier must be a string")
    trimmed = raw_input.strip()
    if not trimmed or any(c.isspace() for c in trimmed):
        raise ValueError("invalid arXiv identifier")

    if "://" in trimmed:
        parsed = urlsplit(trimmed)
        if parsed.scheme not in {"https", "http"}:
            raise ValueError(f"unsupported scheme: {parsed.scheme}")
        if parsed.netloc not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
            raise ValueError(f"unsupported host: {parsed.netloc}")
        if parsed.query or parsed.fragment:
            raise ValueError("query and fragment not allowed in arXiv paper URL")
        match = re.fullmatch(
            r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v[1-9]\d*)?)(?:\.pdf)?",
            parsed.path,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError("invalid arXiv URL path")
        raw_revision = match[1]
    else:
        candidate = trimmed
        if candidate.lower().startswith("arxiv:"):
            candidate = candidate[6:]
        match = re.fullmatch(
            r"((?:\d{4}\.\d{4,5}|[a-z][a-z.\-]+/\d{7})(?:v[1-9]\d*)?)(?:\.pdf)?",
            candidate,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError("invalid arXiv identifier")
        raw_revision = match[1]

    base = re.sub(r"v[1-9]\d*$", "", raw_revision)
    v_match = re.search(r"(v[1-9]\d*)$", raw_revision)
    version = v_match.group(1) if v_match else None
    return ParsedArXivId(
        paper_id=f"arxiv:{base}",
        arxiv_id=base,
        version=version,
        raw_revision=raw_revision,
    )


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
    parsed_id = normalize_arxiv_id(url)
    return parsed_id.paper_id, parsed_id.raw_revision
