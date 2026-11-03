"""HTTPX-based arXiv client for metadata and safe temporary PDF retrieval."""

from __future__ import annotations

import asyncio
import logging
import tempfile
import uuid
import xml.etree.ElementTree as ET
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

from paper_review.domain.jobs import normalize_arxiv_id
from paper_review.domain.models import ArXivMetadata
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter

logger = logging.getLogger("paper_review")

ALLOWED_HOSTS: frozenset[str] = frozenset({"arxiv.org", "export.arxiv.org", "www.arxiv.org"})
ALLOWED_SCHEMES: frozenset[str] = frozenset({"https", "http"})

DEFAULT_API_URL = "https://export.arxiv.org/api/query"
DEFAULT_USER_AGENT = (
    "paper-review-ai/0.1.0 (https://github.com/Takafumi122/paper-reading-assistant)"
)
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_RATE_LIMIT_DELAY = 3.0
DEFAULT_MAX_PDF_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB
MAX_REDIRECTS = 5
PDF_MAGIC_BYTES = b"%PDF"

ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


class ArXivError(Exception):
    """Base exception for all arXiv client errors."""


class ArXivSecurityError(ArXivError):
    """Raised when an untrusted URL, host, or redirect target is encountered."""


class ArXivHttpError(ArXivError):
    """Raised on non-success HTTP status codes."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ArXivNotFoundError(ArXivHttpError):
    """Raised when a paper is not found on arXiv."""


class ArXivRateLimitError(ArXivHttpError):
    """Raised when arXiv returns HTTP 429 Too Many Requests."""


class ArXivParseError(ArXivError):
    """Raised when arXiv API returns malformed or unparseable XML."""


class ArXivDownloadError(ArXivError):
    """Raised when PDF download fails, exceeds limit, or is not a valid PDF."""


def validate_arxiv_url(url: str) -> str:
    """Ensure the URL belongs strictly to allowed arXiv hosts and schemes."""
    if not isinstance(url, str):
        raise ArXivSecurityError("URL must be a string")
    parsed = urlsplit(url)
    if parsed.scheme not in ALLOWED_SCHEMES:
        raise ArXivSecurityError(f"forbidden scheme: {parsed.scheme!r}")
    if parsed.netloc not in ALLOWED_HOSTS:
        raise ArXivSecurityError(f"forbidden host: {parsed.netloc!r}")
    return url


def parse_arxiv_atom(xml_text: str, target_id: str) -> ArXivMetadata:
    """Parse arXiv Atom XML response into ArXivMetadata domain model."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ArXivParseError(f"Malformed XML from arXiv API: {exc}") from exc

    entries = root.findall("atom:entry", ATOM_NS)
    if not entries:
        raise ArXivNotFoundError(f"Paper {target_id} not found in arXiv")

    entry = entries[0]

    # Check for arXiv error entry (arXiv returns an entry with title "Error" on invalid query)
    title_el = entry.find("atom:title", ATOM_NS)
    title = " ".join((title_el.text or "").split()) if title_el is not None else ""
    if title.lower() == "error":
        summary_el = entry.find("atom:summary", ATOM_NS)
        err_msg = (
            summary_el.text.strip()
            if summary_el is not None and summary_el.text
            else "arXiv API error"
        )
        raise ArXivNotFoundError(f"arXiv paper not found: {err_msg}")

    # Extract ID and version from entry ID URL
    id_el = entry.find("atom:id", ATOM_NS)
    entry_id_url = (id_el.text or "").strip() if id_el is not None else ""
    parsed_id = normalize_arxiv_id(entry_id_url)

    # Authors
    authors: list[str] = []
    for author_el in entry.findall("atom:author", ATOM_NS):
        name_el = author_el.find("atom:name", ATOM_NS)
        if name_el is not None and name_el.text:
            cleaned_name = " ".join(name_el.text.split())
            if cleaned_name:
                authors.append(cleaned_name)

    # Summary (abstract)
    summary_el = entry.find("atom:summary", ATOM_NS)
    abstract = summary_el.text.strip() if summary_el is not None and summary_el.text else ""

    # Published and updated dates
    published_el = entry.find("atom:published", ATOM_NS)
    first_submitted = (published_el.text or "").strip() if published_el is not None else ""

    updated_el = entry.find("atom:updated", ATOM_NS)
    last_updated = (updated_el.text or "").strip() if updated_el is not None else ""

    # Categories
    primary_el = entry.find("arxiv:primary_category", ATOM_NS)
    primary_cat = primary_el.attrib.get("term", "") if primary_el is not None else ""

    categories: list[str] = []
    for cat_el in entry.findall("atom:category", ATOM_NS):
        term = cat_el.attrib.get("term")
        if term and term not in categories:
            categories.append(term)
    if primary_cat and primary_cat not in categories:
        categories.insert(0, primary_cat)

    # Optional metadata
    doi_el = entry.find("arxiv:doi", ATOM_NS)
    doi = doi_el.text.strip() if doi_el is not None and doi_el.text else None

    comment_el = entry.find("arxiv:comment", ATOM_NS)
    comment = comment_el.text.strip() if comment_el is not None and comment_el.text else None

    jref_el = entry.find("arxiv:journal_ref", ATOM_NS)
    journal_ref = jref_el.text.strip() if jref_el is not None and jref_el.text else None

    # Version tracking:
    # In arXiv Atom API:
    #   <published> = first submission date (version 1)
    #   <updated> = retrieved/current version date
    # A single Atom response does NOT provide intermediate version dates (e.g. v2..v(N-1)).
    # We explicitly record first_submitted_date, current_version, and current_version_date.
    # Complete version history is not guessable from a single response and remains None.
    current_version = parsed_id.version or "v1"
    current_version_date = last_updated if last_updated else first_submitted

    # PDF URL
    pdf_url = f"https://arxiv.org/pdf/{parsed_id.raw_revision}.pdf"
    for link_el in entry.findall("atom:link", ATOM_NS):
        if link_el.attrib.get("title") == "pdf" or (
            link_el.attrib.get("type") == "application/pdf"
        ):
            href = link_el.attrib.get("href")
            if href:
                pdf_url = href.replace("http://", "https://")
                if not pdf_url.endswith(".pdf"):
                    pdf_url = f"{pdf_url}.pdf"
                break

    return ArXivMetadata(
        arxiv_id=parsed_id.arxiv_id,
        title=title,
        authors=authors,
        abstract=abstract,
        primary_category=primary_cat or (categories[0] if categories else "cs.CL"),
        categories=categories,
        first_submitted_date=first_submitted,
        current_version=current_version,
        current_version_date=current_version_date,
        version_history=None,
        pdf_url=pdf_url,
        doi=doi,
        comment=comment,
        journal_ref=journal_ref,
    )


class ArXivClient:
    """Client for fetching arXiv metadata and downloading PDFs to temporary files."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        api_base_url: str = DEFAULT_API_URL,
        user_agent: str = DEFAULT_USER_AGENT,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        rate_limiter: RateLimiter | None = None,
        max_pdf_size_bytes: int = DEFAULT_MAX_PDF_SIZE_BYTES,
        max_attempts: int = 3,
        backoff_factor: float = 1.0,
    ) -> None:
        self.api_base_url = api_base_url
        self.user_agent = user_agent
        self.timeout = timeout_seconds
        self.rate_limiter = rate_limiter or RateLimiter(DEFAULT_RATE_LIMIT_DELAY)
        self.max_pdf_size_bytes = max_pdf_size_bytes
        self.max_attempts = max_attempts
        self.backoff_factor = backoff_factor
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
            self._owned_client = httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(self.timeout),
                headers={"User-Agent": self.user_agent},
            )
            self._owned_loop = current_loop
        return self._owned_client

    async def close(self) -> None:
        if self._owned_client is not None and not self._owned_client.is_closed:
            await self._owned_client.aclose()
        self._owned_client = None
        self._owned_loop = None

    async def __aenter__(self) -> ArXivClient:
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.close()

    async def _safe_request(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        """Execute request and follow redirects safely while verifying host at each step."""
        current_url = validate_arxiv_url(url)
        for hop in range(MAX_REDIRECTS + 1):
            response = await client.request(method, current_url, follow_redirects=False, **kwargs)
            if response.is_redirect:
                if hop == MAX_REDIRECTS:
                    raise ArXivHttpError(
                        "Too many redirects from arXiv", status_code=response.status_code
                    )
                location = response.headers.get("Location")
                if not location:
                    raise ArXivHttpError(
                        "Redirect response missing Location header",
                        status_code=response.status_code,
                    )
                next_url = urljoin(current_url, location)
                current_url = validate_arxiv_url(next_url)
                continue
            return response
        raise ArXivHttpError("Redirect loop exceeded", status_code=500)

    async def get_metadata(self, arxiv_id_or_url: str) -> ArXivMetadata:
        """Fetch metadata for an arXiv paper and return ArXivMetadata domain model."""
        parsed_id = normalize_arxiv_id(arxiv_id_or_url)
        query_url = f"{self.api_base_url}?id_list={parsed_id.raw_revision}&start=0&max_results=1"

        client = self._get_client()
        last_error: Exception | None = None

        for attempt in range(1, self.max_attempts + 1):
            await self.rate_limiter.acquire()
            try:
                response = await self._safe_request(client, "GET", query_url)
                if response.status_code == 429:
                    retry_after = None
                    if "Retry-After" in response.headers:
                        try:
                            retry_after = float(response.headers["Retry-After"])
                        except ValueError:
                            pass
                    raise ArXivRateLimitError(
                        "arXiv API rate limit exceeded (429)",
                        status_code=429,
                        retry_after=retry_after,
                    )
                if response.status_code == 404:
                    raise ArXivNotFoundError(
                        f"arXiv paper {parsed_id.raw_revision} not found (404)",
                        status_code=404,
                    )
                if response.status_code >= 500:
                    raise ArXivHttpError(
                        f"arXiv API server error ({response.status_code})",
                        status_code=response.status_code,
                    )
                if response.status_code >= 400:
                    raise ArXivHttpError(
                        f"arXiv API client error ({response.status_code})",
                        status_code=response.status_code,
                    )

                return parse_arxiv_atom(response.text, parsed_id.raw_revision)

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                last_error = exc
                if attempt == self.max_attempts:
                    break
                await asyncio.sleep(self.backoff_factor * attempt)
            except (ArXivRateLimitError, ArXivHttpError) as exc:
                last_error = exc
                if exc.status_code in {429, 500, 502, 503, 504} and attempt < self.max_attempts:
                    wait = getattr(exc, "retry_after", None) or (self.backoff_factor * attempt)
                    await asyncio.sleep(wait)
                    continue
                raise

        raise ArXivHttpError(
            f"Failed to fetch metadata for {parsed_id.raw_revision} "
            f"after {self.max_attempts} attempts: {last_error}"
        )

    @asynccontextmanager
    async def download_pdf_temp(
        self,
        arxiv_id_or_url: str,
        *,
        max_size_bytes: int | None = None,
    ) -> AsyncIterator[Path]:
        """Download an arXiv paper PDF to a secure temporary file.

        The temporary file is yielded as a Path and automatically unlinked
        when exiting the context, whether on success or on exception.
        """
        parsed_id = normalize_arxiv_id(arxiv_id_or_url)
        pdf_url = parsed_id.canonical_pdf_url
        max_bytes = max_size_bytes or self.max_pdf_size_bytes

        temp_dir = tempfile.gettempdir()
        unique_token = uuid.uuid4().hex
        partial_path = Path(temp_dir) / f"arxiv_{parsed_id.raw_revision}_{unique_token}.tmp"
        final_path = Path(temp_dir) / f"arxiv_{parsed_id.raw_revision}_{unique_token}.pdf"

        client = self._get_client()

        try:
            for attempt in range(1, self.max_attempts + 1):
                await self.rate_limiter.acquire()
                try:
                    # Request with streaming response
                    current_url = validate_arxiv_url(pdf_url)
                    for hop in range(MAX_REDIRECTS + 1):
                        async with client.stream(
                            "GET", current_url, follow_redirects=False
                        ) as response:
                            if response.is_redirect:
                                if hop == MAX_REDIRECTS:
                                    raise ArXivHttpError(
                                        "Too many redirects downloading PDF",
                                        status_code=response.status_code,
                                    )
                                location = response.headers.get("Location")
                                if not location:
                                    raise ArXivHttpError(
                                        "PDF redirect missing Location header",
                                        status_code=response.status_code,
                                    )
                                next_url = urljoin(current_url, location)
                                current_url = validate_arxiv_url(next_url)
                                continue

                            if response.status_code == 404:
                                raise ArXivNotFoundError(
                                    f"PDF for {parsed_id.raw_revision} not found (404)",
                                    status_code=404,
                                )
                            if response.status_code == 429:
                                raise ArXivRateLimitError(
                                    "arXiv PDF rate limit exceeded (429)",
                                    status_code=429,
                                )
                            if response.status_code >= 500:
                                raise ArXivHttpError(
                                    f"arXiv server error downloading PDF ({response.status_code})",
                                    status_code=response.status_code,
                                )
                            if response.status_code >= 400:
                                raise ArXivHttpError(
                                    f"arXiv client error downloading PDF ({response.status_code})",
                                    status_code=response.status_code,
                                )

                            # Check Content-Length header if present
                            content_length_header = response.headers.get("Content-Length")
                            if content_length_header:
                                try:
                                    if int(content_length_header) > max_bytes:
                                        raise ArXivDownloadError(
                                            f"PDF size exceeds limit: {content_length_header} "
                                            f"> {max_bytes} bytes"
                                        )
                                except ValueError:
                                    pass

                            # Stream to partial file
                            total_downloaded = 0
                            with open(partial_path, "wb") as file_out:
                                async for chunk in response.aiter_bytes(chunk_size=65536):
                                    total_downloaded += len(chunk)
                                    if total_downloaded > max_bytes:
                                        raise ArXivDownloadError(
                                            f"PDF download exceeded limit of {max_bytes} bytes "
                                            f"({total_downloaded} bytes received)"
                                        )
                                    file_out.write(chunk)

                            if total_downloaded == 0:
                                raise ArXivDownloadError("Downloaded PDF file is empty")

                            # Verify PDF magic bytes (%PDF)
                            with open(partial_path, "rb") as verify_file:
                                magic = verify_file.read(4)
                                if magic != PDF_MAGIC_BYTES:
                                    raise ArXivDownloadError(
                                        "Downloaded file is not a valid PDF "
                                        f"(magic bytes {magic!r} != {PDF_MAGIC_BYTES!r})"
                                    )

                            # Atomically rename partial to final
                            partial_path.rename(final_path)
                            break  # Success
                    break  # Success

                except (httpx.TimeoutException, httpx.NetworkError) as exc:
                    if partial_path.exists():
                        partial_path.unlink()
                    if attempt == self.max_attempts:
                        raise ArXivDownloadError(
                            f"Network error downloading PDF for {parsed_id.raw_revision}: {exc}"
                        ) from exc
                    await asyncio.sleep(self.backoff_factor * attempt)
                except ArXivHttpError as exc:
                    if partial_path.exists():
                        partial_path.unlink()
                    if exc.status_code in {429, 500, 502, 503, 504} and attempt < self.max_attempts:
                        wait = getattr(exc, "retry_after", None) or (self.backoff_factor * attempt)
                        await asyncio.sleep(wait)
                        continue
                    raise

            if not final_path.exists():
                raise ArXivDownloadError(f"PDF download failed for {parsed_id.raw_revision}")

            yield final_path

        finally:
            # Absolute guarantee that temporary files are deleted
            for path in (partial_path, final_path):
                if path.exists():
                    try:
                        path.unlink()
                    except OSError:
                        pass

    def get_metadata_sync(self, arxiv_id_or_url: str) -> ArXivMetadata:
        """Synchronous wrapper for get_metadata."""
        return asyncio.run(self.get_metadata(arxiv_id_or_url))

    @contextmanager
    def download_pdf_temp_sync(
        self,
        arxiv_id_or_url: str,
        *,
        max_size_bytes: int | None = None,
    ) -> Iterator[Path]:
        """Synchronous context manager wrapper for download_pdf_temp."""

        async def _run() -> AsyncIterator[Path]:
            async with self.download_pdf_temp(
                arxiv_id_or_url, max_size_bytes=max_size_bytes
            ) as path:
                yield path

        # For synchronous usage, run the async generator step-by-step
        gen = _run()
        loop = asyncio.new_event_loop()
        try:
            path = loop.run_until_complete(gen.__anext__())
            try:
                yield path
            finally:
                try:
                    loop.run_until_complete(gen.__anext__())
                except StopAsyncIteration:
                    pass
        finally:
            loop.close()
