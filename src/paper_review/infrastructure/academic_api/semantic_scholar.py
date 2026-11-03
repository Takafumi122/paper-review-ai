"""Semantic Scholar API client for Paper Review AI.

Provides HTTPX-based access to Semantic Scholar Academic Graph API:
- Target paper identity resolution (S2 ID, ArXiv ID, DOI)
- Target paper metadata retrieval
- References retrieval with pagination, citation contexts, and influence signals
- Decouples raw Semantic Scholar JSON responses from the domain layer.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from contextlib import nullcontext
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field

from paper_review.domain.models import ExternalIds
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter
from paper_review.infrastructure.academic_api.s2_request_gate import (
    S2GateStopped,
    SharedS2RequestGate,
    default_s2_request_gate,
)

logger = logging.getLogger("paper_review")

DEFAULT_S2_API_URL = "https://api.semanticscholar.org/graph/v1"
DEFAULT_USER_AGENT = (
    "paper-review-ai/0.1.0 (https://github.com/Takafumi122/paper-reading-assistant)"
)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RATE_LIMIT_DELAY = 1.0  # All endpoints share the workspace-wide send gate too.

SPECTER2_FIELD = "embedding.specter_v2"
SPECTER2_MODEL = "semantic-scholar:specter_v2"
SPECTER2_FIELDS = f"paperId,title,externalIds,{SPECTER2_FIELD}"

PAPER_FIELDS = (
    "paperId,externalIds,title,abstract,year,publicationDate,"
    "citationCount,influentialCitationCount,referenceCount,"
    "fieldsOfStudy,publicationVenue,authors"
)

REFERENCE_FIELDS = (
    "citedPaper.paperId,citedPaper.externalIds,citedPaper.title,"
    "citedPaper.abstract,citedPaper.year,citedPaper.publicationDate,"
    "citedPaper.citationCount,citedPaper.influentialCitationCount,"
    "citedPaper.referenceCount,citedPaper.fieldsOfStudy,"
    "citedPaper.publicationVenue,citedPaper.authors,"
    "isInfluential,contexts,intents"
)

CITATION_FIELDS = (
    "citingPaper.paperId,citingPaper.externalIds,citingPaper.title,"
    "citingPaper.abstract,citingPaper.year,citingPaper.publicationDate,"
    "citingPaper.citationCount,citingPaper.influentialCitationCount,"
    "citingPaper.referenceCount,citingPaper.fieldsOfStudy,"
    "citingPaper.publicationVenue,citingPaper.authors,"
    "isInfluential,contexts,intents"
)


class SemanticScholarError(Exception):
    """Base exception for all Semantic Scholar API client errors."""


class SemanticScholarNotFoundError(SemanticScholarError):
    """Raised when a paper or reference list is not found in Semantic Scholar."""

    def __init__(self, message: str, status_code: int = 404) -> None:
        super().__init__(message)
        self.status_code = status_code


class SemanticScholarRateLimitError(SemanticScholarError):
    """Raised when Semantic Scholar returns 429 Too Many Requests."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 429,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class SemanticScholarHttpError(SemanticScholarError):
    """Raised on HTTP error status codes from Semantic Scholar."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class SemanticScholarParseError(SemanticScholarError):
    """Raised when the response JSON cannot be parsed or lacks expected structure."""


# ---------------------------------------------------------------------------
# Infrastructure Data Transfer Objects (Decoupled from Domain Layer)
# ---------------------------------------------------------------------------


class S2Paper(BaseModel):
    """Normalized paper metadata from Semantic Scholar."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    paper_id: str
    title: str
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    year: int | None = None
    publication_date: str | None = None
    citation_count: int | None = None
    influential_citation_count: int | None = None
    reference_count: int | None = None
    fields_of_study: list[str] = Field(default_factory=list)
    venue: str | None = None
    external_ids: ExternalIds = Field(default_factory=ExternalIds)
    embedding: list[float] | None = None
    embedding_model: str | None = None
    embedding_response_model: str | None = None
    embedding_missing_reason: str | None = "embedding_not_requested"


class S2Reference(BaseModel):
    """Reference connection from target paper to a cited paper."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cited_paper: S2Paper
    is_influential: bool = False
    contexts: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


class S2Citation(BaseModel):
    """Citation connection from a citing paper to target paper."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    citing_paper: S2Paper
    is_influential: bool = False
    contexts: list[str] = Field(default_factory=list)
    intents: list[str] = Field(default_factory=list)


def parse_s2_external_ids(raw_ids: dict[str, Any] | None) -> ExternalIds:
    """Safely parse Semantic Scholar externalIds dictionary into domain ExternalIds."""
    if not isinstance(raw_ids, dict):
        return ExternalIds()
    arxiv = raw_ids.get("ArXiv") or raw_ids.get("arxiv")
    doi = raw_ids.get("DOI") or raw_ids.get("doi")
    corpus_id = raw_ids.get("CorpusId") or raw_ids.get("corpusId")
    s2_id = str(corpus_id) if corpus_id else None

    return ExternalIds(
        arxiv=str(arxiv) if arxiv else None,
        doi=str(doi) if doi else None,
        semantic_scholar=s2_id,
    )


def parse_specter2_embedding(
    raw: Any, *, explicitly_requested: bool
) -> tuple[list[float] | None, str | None, str | None]:
    """Validate documented {model, vector}; never promote default v1 to v2.

    Return vector, concise failure reason, and provider model label. Unknown labels
    fail closed: a future provider contract change requires explicit verification.
    """
    response_model = raw.get("model") if isinstance(raw, dict) else None
    response_model = response_model if isinstance(response_model, str) else None
    if not explicitly_requested:
        return None, "embedding_not_requested", response_model
    if raw is None:
        return None, "embedding_missing", response_model
    if not isinstance(raw, dict):
        return None, "embedding_malformed", response_model
    if response_model != "specter_v2":
        return None, "embedding_model_mismatch", response_model
    vector = raw.get("vector")
    if not isinstance(vector, list):
        return None, "embedding_malformed", response_model
    if not vector:
        return None, "embedding_empty", response_model
    if any(type(value) not in (int, float) for value in vector):
        return None, "embedding_non_numeric", response_model
    try:
        values = [float(value) for value in vector]
    except (OverflowError, ValueError):
        return None, "embedding_non_finite", response_model
    if not all(math.isfinite(value) for value in values):
        return None, "embedding_non_finite", response_model
    if not any(values):
        return None, "embedding_zero_norm", response_model
    return values, None, response_model


def parse_s2_paper_json(data: dict[str, Any], *, specter2_requested: bool = False) -> S2Paper:
    """Parse raw S2 paper JSON into S2Paper DTO with strict validation."""
    if not isinstance(data, dict):
        raise SemanticScholarParseError("Invalid paper data: expected dict")

    paper_id = data.get("paperId")
    if not paper_id or not isinstance(paper_id, str):
        raise SemanticScholarParseError("Paper missing paperId field")

    title = (data.get("title") or "").strip()
    if not title:
        raise SemanticScholarParseError(f"Paper {paper_id} missing title field")

    raw_authors = data.get("authors") or []
    authors: list[str] = []
    if isinstance(raw_authors, list):
        for a in raw_authors:
            if isinstance(a, dict) and a.get("name"):
                authors.append(str(a["name"]).strip())
            elif isinstance(a, str):
                authors.append(a.strip())

    venue = None
    pub_venue = data.get("publicationVenue")
    if isinstance(pub_venue, dict) and pub_venue.get("name"):
        venue = str(pub_venue["name"]).strip()
    elif isinstance(data.get("venue"), str):
        venue = str(data["venue"]).strip()

    raw_fields = data.get("fieldsOfStudy") or []
    fields: list[str] = []
    if isinstance(raw_fields, list):
        for f in raw_fields:
            if isinstance(f, str) and f.strip():
                fields.append(f.strip())

    ext_ids = parse_s2_external_ids(data.get("externalIds"))
    # Graph paperId is the canonical S2 lookup identity; CorpusId is a different namespace.
    ext_ids = ExternalIds(
        arxiv=ext_ids.arxiv,
        doi=ext_ids.doi,
        semantic_scholar=paper_id,
    )

    vector, reason, response_model = parse_specter2_embedding(
        data.get("embedding"), explicitly_requested=specter2_requested
    )
    return S2Paper(
        embedding=vector,
        embedding_model=SPECTER2_MODEL if vector is not None else None,
        embedding_response_model=response_model,
        embedding_missing_reason=reason,
        paper_id=paper_id,
        title=title,
        abstract=data.get("abstract") or None,
        authors=authors,
        year=data.get("year") if isinstance(data.get("year"), int) else None,
        publication_date=data.get("publicationDate") or None,
        citation_count=(
            data.get("citationCount") if isinstance(data.get("citationCount"), int) else None
        ),
        influential_citation_count=(
            data.get("influentialCitationCount")
            if isinstance(data.get("influentialCitationCount"), int)
            else None
        ),
        reference_count=(
            data.get("referenceCount") if isinstance(data.get("referenceCount"), int) else None
        ),
        fields_of_study=fields,
        venue=venue,
        external_ids=ext_ids,
    )


class SemanticScholarClient:
    """Async HTTP client for interacting with the Semantic Scholar Academic Graph API."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base_url: str = DEFAULT_S2_API_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        api_key: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        rate_limiter: RateLimiter | None = None,
        max_attempts: int = 3,
        backoff_factor: float = 1.0,
        max_http_attempts: int = 40,
        request_gate: SharedS2RequestGate | None = None,
    ) -> None:
        self.api_base_url = api_base_url.rstrip("/")
        self.user_agent = user_agent
        self.api_key = (
            api_key or os.environ.get("SEMANTIC_SCHOLAR_API_KEY") or os.environ.get("S2_API_KEY")
        )
        self.timeout = timeout_seconds
        self.rate_limiter = rate_limiter or RateLimiter(DEFAULT_RATE_LIMIT_DELAY)
        # Direct MockTransport injection is an offline replay, never live HTTP.
        # A local limiter (even RateLimiter(0)) cannot bypass the live shared gate.
        self.request_gate = request_gate or (
            None if client is None and isinstance(transport, httpx.MockTransport)
            else default_s2_request_gate()
        )
        self.max_attempts = max_attempts
        self.backoff_factor = backoff_factor
        self.max_http_attempts = max_http_attempts
        self.http_attempts = 0

        self._transport = transport
        self._external_client = client
        self._owned_client: httpx.AsyncClient | None = None
        self._owned_loop: asyncio.AbstractEventLoop | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._external_client is not None:
            return self._external_client
        try:
            current_loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            current_loop = None
        if (
            self._owned_client is None
            or self._owned_client.is_closed
            or self._owned_loop is not current_loop
        ):
            headers = {"User-Agent": self.user_agent}
            self._owned_client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self.timeout),
                headers=headers,
            )
            self._owned_loop = current_loop
        return self._owned_client

    async def close(self) -> None:
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()
        self._owned_client = None
        self._owned_loop = None

    async def __aenter__(self) -> SemanticScholarClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    @staticmethod
    def resolve_target_id(external_ids: ExternalIds) -> str | None:
        """Resolve Target Paper identity into Semantic Scholar lookup key.

        Priority order:
          1. semantic_scholar ID if known
          2. arXiv ID (prefixed with 'ARXIV:')
          3. DOI (prefixed with 'DOI:')
        Title-based fuzzy search is strictly forbidden for automatic identity resolution.
        """
        if external_ids.semantic_scholar:
            s2_id = external_ids.semantic_scholar.strip()
            if s2_id:
                return s2_id
        if external_ids.arxiv:
            arxiv_id = external_ids.arxiv.strip()
            if arxiv_id:
                return f"ARXIV:{arxiv_id}"
        if external_ids.doi:
            doi = external_ids.doi.strip()
            if doi:
                return f"DOI:{doi}"
        return None

    async def _send_request(
        self,
        url: str,
        *,
        body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        """Execute request with rate limiting and retry handling for 429/5xx."""
        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            if self.http_attempts >= self.max_http_attempts:
                raise SemanticScholarHttpError("http_attempt_budget_exhausted")
            # Merge client defaults without mutating caller-owned headers. A
            # conflicting caller key is an error, including when auth is disabled.
            request = client.build_request("POST" if body is not None else "GET", url, json=body)
            caller_key = request.headers.get("x-api-key")
            if caller_key is not None and caller_key != self.api_key:
                raise SemanticScholarError("conflicting_semantic_scholar_authentication")
            if self.api_key:
                request.headers["x-api-key"] = self.api_key
            await self.rate_limiter.acquire()
            try:
                gate = self.request_gate
                async with gate.request() if gate is not None else nullcontext() as handle:
                    self.http_attempts += 1
                    # Redirects are extra HTTP requests and cannot bypass the gate.
                    response = await client.send(request, follow_redirects=False)
                    if response.status_code == 429 and gate is not None:
                        assert handle is not None
                        gate.stop(handle)
                if 300 <= response.status_code < 400:
                    raise SemanticScholarHttpError(
                        "Semantic Scholar redirect refused", status_code=response.status_code
                    )
                if response.status_code == 200:
                    return response

                if response.status_code == 404:
                    raise SemanticScholarNotFoundError(
                        f"Resource not found in Semantic Scholar (404): {url}",
                        status_code=404,
                    )

                if response.status_code == 429:
                    retry_after = None
                    if "Retry-After" in response.headers:
                        try:
                            retry_after = float(response.headers["Retry-After"])
                        except ValueError:
                            pass
                    err = SemanticScholarRateLimitError(
                        "Semantic Scholar API rate limit exceeded (429)",
                        status_code=429,
                        retry_after=retry_after,
                    )
                    # A 429 stops this run. Retry-After remains diagnostic metadata;
                    # it never automatically reopens the operator circuit.
                    raise err

                if response.status_code >= 500:
                    if attempt < self.max_attempts:
                        await asyncio.sleep(self.backoff_factor * attempt)
                        continue
                    raise SemanticScholarHttpError(
                        f"Semantic Scholar server error ({response.status_code})",
                        status_code=response.status_code,
                    )

                if response.status_code >= 400:
                    raise SemanticScholarHttpError(
                        f"Semantic Scholar client error ({response.status_code})",
                        status_code=response.status_code,
                    )

            except S2GateStopped:
                raise SemanticScholarRateLimitError(
                    "Semantic Scholar circuit open; no HTTP request sent", status_code=429
                ) from None
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    raise SemanticScholarHttpError(
                        "Network error communicating with Semantic Scholar"
                    ) from None
                await asyncio.sleep(self.backoff_factor * attempt)

        if last_error:
            raise last_error
        raise SemanticScholarHttpError(f"Request failed after {self.max_attempts} attempts: {url}")

    async def get_paper(
        self,
        paper_id: str,
        *,
        fields: str = PAPER_FIELDS,
    ) -> S2Paper:
        """Fetch metadata for a single paper by S2 ID, arXiv ID, or DOI."""
        encoded_id = quote(paper_id.strip(), safe=":")
        url = f"{self.api_base_url}/paper/{encoded_id}?fields={fields}"
        response = await self._send_request(url)

        try:
            data = response.json()
        except Exception as exc:
            raise SemanticScholarParseError(f"Malformed JSON from Semantic Scholar: {exc}") from exc

        return parse_s2_paper_json(data, specter2_requested=SPECTER2_FIELD in fields.split(","))

    async def get_papers(
        self, paper_ids: list[str], *, fields: str = PAPER_FIELDS
    ) -> dict[str, S2Paper | None]:
        """Exact identifier batch lookup; preserve nulls and positional correspondence."""
        if not paper_ids:
            return {}
        if len(paper_ids) > 500:
            raise ValueError("S2 batch lookup supports at most 500 identifiers")
        response = await self._send_request(
            f"{self.api_base_url}/paper/batch?fields={fields}",
            body={"ids": paper_ids},
        )
        try:
            data = response.json()
        except ValueError as exc:
            raise SemanticScholarParseError("Invalid batch JSON") from exc
        if not isinstance(data, list) or len(data) != len(paper_ids):
            raise SemanticScholarParseError("Invalid batch response length/shape")
        return {
            key: (
                parse_s2_paper_json(item, specter2_requested=SPECTER2_FIELD in fields.split(","))
                if item is not None
                else None
            )
            for key, item in zip(paper_ids, data, strict=True)
        }

    async def get_specter2_papers(self, paper_ids: list[str]) -> dict[str, S2Paper | None]:
        """One explicit v2 batch through the existing limiter/retry/request budget."""
        return await self.get_papers(paper_ids, fields=SPECTER2_FIELDS)

    async def search_papers(self, title: str) -> list[S2Paper]:
        """Bounded title search, *not* identity confirmation; callers must corroborate."""
        query = urlencode({"query": title, "fields": PAPER_FIELDS, "limit": 10})
        response = await self._send_request(f"{self.api_base_url}/paper/search?{query}")
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise SemanticScholarParseError("Invalid search response")
        # Do not accept a unique visible hit from a truncated ambiguous result set.
        if payload.get("next") is not None or payload.get("total", 0) > 10:
            raise SemanticScholarParseError("title_search_truncated")
        return [parse_s2_paper_json(item) for item in payload["data"]]

    def get_paper_sync(
        self,
        paper_id: str,
        *,
        fields: str = PAPER_FIELDS,
    ) -> S2Paper:
        """Synchronous wrapper for get_paper."""
        return asyncio.run(self.get_paper(paper_id, fields=fields))

    async def get_references(
        self,
        paper_id: str,
        *,
        limit: int = 100,
        max_results: int | None = None,
        fields: str = REFERENCE_FIELDS,
    ) -> list[S2Reference]:
        """Fetch references cited by a paper with pagination support."""
        encoded_id = quote(paper_id.strip(), safe=":")
        results: list[S2Reference] = []
        offset = 0
        fetch_limit = min(limit, 100)  # S2 max page size is 100 on graph api

        while True:
            url = (
                f"{self.api_base_url}/paper/{encoded_id}/references"
                f"?fields={fields}&limit={fetch_limit}&offset={offset}"
            )
            response = await self._send_request(url)
            try:
                payload = response.json()
            except Exception as exc:
                msg = f"Malformed JSON in references response: {exc}"
                raise SemanticScholarParseError(msg) from exc

            raw_data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw_data, list) or not raw_data:
                break

            for item in raw_data:
                if not isinstance(item, dict):
                    continue
                cited_json = item.get("citedPaper")
                if not isinstance(cited_json, dict) or not cited_json.get("title"):
                    # Skip cited items without metadata
                    continue
                try:
                    cited_paper = parse_s2_paper_json(cited_json)
                except SemanticScholarParseError:
                    continue

                raw_contexts = item.get("contexts") or []
                contexts = [c.strip() for c in raw_contexts if isinstance(c, str) and c.strip()]
                raw_intents = item.get("intents") or []
                intents = [i.strip() for i in raw_intents if isinstance(i, str) and i.strip()]

                results.append(
                    S2Reference(
                        cited_paper=cited_paper,
                        is_influential=bool(item.get("isInfluential")),
                        contexts=contexts,
                        intents=intents,
                    )
                )
                if max_results is not None and len(results) >= max_results:
                    return results[:max_results]

            offset += len(raw_data)
            # If fewer items than limit returned, no more pages exist
            if len(raw_data) < fetch_limit:
                break

        return results

    def get_references_sync(
        self,
        paper_id: str,
        *,
        limit: int = 100,
        max_results: int | None = None,
        fields: str = REFERENCE_FIELDS,
    ) -> list[S2Reference]:
        """Synchronous wrapper for get_references."""
        return asyncio.run(
            self.get_references(
                paper_id,
                limit=limit,
                max_results=max_results,
                fields=fields,
            )
        )

    async def get_citations(
        self,
        paper_id: str,
        *,
        limit: int = 100,
        max_results: int | None = None,
        fields: str = CITATION_FIELDS,
    ) -> list[S2Citation]:
        """Fetch papers citing the target paper with pagination support."""
        encoded_id = quote(paper_id.strip(), safe=":")
        results: list[S2Citation] = []
        offset = 0
        fetch_limit = min(limit, 100)

        while True:
            url = (
                f"{self.api_base_url}/paper/{encoded_id}/citations"
                f"?fields={fields}&limit={fetch_limit}&offset={offset}"
            )
            response = await self._send_request(url)
            try:
                payload = response.json()
            except Exception as exc:
                msg = f"Malformed JSON in citations response: {exc}"
                raise SemanticScholarParseError(msg) from exc

            raw_data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(raw_data, list) or not raw_data:
                break

            for item in raw_data:
                if not isinstance(item, dict):
                    continue
                citing_json = item.get("citingPaper")
                if not isinstance(citing_json, dict) or not citing_json.get("title"):
                    continue
                try:
                    citing_paper = parse_s2_paper_json(citing_json)
                except SemanticScholarParseError:
                    continue

                raw_contexts = item.get("contexts") or []
                contexts = [c.strip() for c in raw_contexts if isinstance(c, str) and c.strip()]
                raw_intents = item.get("intents") or []
                intents = [i.strip() for i in raw_intents if isinstance(i, str) and i.strip()]

                results.append(
                    S2Citation(
                        citing_paper=citing_paper,
                        is_influential=bool(item.get("isInfluential")),
                        contexts=contexts,
                        intents=intents,
                    )
                )
                if max_results is not None and len(results) >= max_results:
                    return results[:max_results]

            offset += len(raw_data)
            if len(raw_data) < fetch_limit:
                break

        return results

    def get_citations_sync(
        self,
        paper_id: str,
        *,
        limit: int = 100,
        max_results: int | None = None,
        fields: str = CITATION_FIELDS,
    ) -> list[S2Citation]:
        """Synchronous wrapper for get_citations."""
        return asyncio.run(
            self.get_citations(
                paper_id,
                limit=limit,
                max_results=max_results,
                fields=fields,
            )
        )
