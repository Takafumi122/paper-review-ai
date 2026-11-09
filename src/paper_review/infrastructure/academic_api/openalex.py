"""Bounded OpenAlex metadata client. Keys stay in headers; no S2 fallback."""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date as calendar_date
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from paper_review.domain.models import ExternalIds
from paper_review.infrastructure.academic_api.arxiv import ArXivError, parse_arxiv_atom
from paper_review.infrastructure.academic_api.local_cache import LocalArtifactCache
from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter

BASE = "https://api.openalex.org"
FIELDS = ("id,title,doi,ids,publication_date,publication_year,authorships,"
          "abstract_inverted_index,locations,referenced_works,related_works,cited_by_count,type")


class OpenAlexError(Exception):
    """Sanitized error code only; never retain a request, header or response body."""


def work_id(value: str) -> str:
    match = re.fullmatch(r"(?:https://openalex.org/(?:works/)?)?(W\d+)", value, re.I)
    if not match:
        raise OpenAlexError("invalid_openalex_work_id")
    return match[1].upper()


def doi_id(value: str) -> str:
    value = re.sub(r"^https?://(?:dx\.)?doi.org/", "", value.strip(), flags=re.I).lower()
    if not re.fullmatch(r"10\.\d{4,9}/[^\s|,]+", value):
        raise OpenAlexError("invalid_doi")
    return value


def abstract_text(index: object) -> str | None:
    if not isinstance(index, dict) or not index:
        return None
    words: dict[int, str] = {}
    for word, positions in index.items():
        if not isinstance(word, str) or not isinstance(positions, list):
            return None
        for position in positions:
            if type(position) is not int or not 0 <= position < 20000 or position in words:
                return None
            words[position] = word
    if not words or sorted(words) != list(range(len(words))):
        return None
    return " ".join(words[i] for i in range(len(words)))


class OpenAlexWork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    abstract: str | None = None
    authors: list[str] = Field(default_factory=list)
    date: str | None = None
    year: int | None = None
    external_ids: ExternalIds
    doi_aliases: list[str] = Field(default_factory=list)
    arxiv_ids: list[str] = Field(default_factory=list)
    references: list[str] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)
    citation_count: int | None = None
    work_type: str | None = None
    retrieved_at: float
    content_hash: str
    first_public_date: str | None = None
    first_public_source: str | None = None
    first_public_hash: str | None = None


def parse_work(data: dict) -> OpenAlexWork:
    wid = work_id(data["id"])
    dois = set()
    arxiv = set()
    for value in [data.get("doi"), (data.get("ids") or {}).get("doi")]:
        if value:
            dois.add(doi_id(value))
    for location in data.get("locations") or []:
        if location.get("doi"):
            dois.add(doi_id(location["doi"]))
        for field in ("landing_page_url", "pdf_url"):
            url = location.get(field)
            if not isinstance(url, str):
                continue
            parsed = urlsplit(url)
            if parsed.hostname not in {"arxiv.org", "www.arxiv.org", "export.arxiv.org"}:
                continue
            match = re.fullmatch(
                r"/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[a-z][a-z.-]+/\d{7}))(?:v\d+)?(?:\.pdf)?",
                parsed.path, re.I,
            )
            if match:
                arxiv.add(match[1].lower())
    title = data.get("title") or data.get("display_name") or ""
    date = data.get("publication_date")
    # Never expand a year or month into a fabricated day.
    if date:
        try:
            if not isinstance(date, str) or not re.fullmatch(
                r"\d{4}(?:-\d{2})?(?:-\d{2})?", date,
            ):
                raise ValueError("invalid_date")
            calendar_date.fromisoformat(date + {4: "-01-01", 7: "-01", 10: ""}[len(date)])
        except ValueError:
            date = None
    normalized = {
        "id": wid, "title": title, "abstract": abstract_text(data.get("abstract_inverted_index")),
        "authors": [a["author"]["display_name"] for a in data.get("authorships", [])
                    if isinstance(a.get("author", {}).get("display_name"), str)],
        "date": date, "year": data.get("publication_year"),
        "external_ids": ExternalIds(
            openalex=wid, doi=doi_id(data["doi"]) if data.get("doi") else None,
            arxiv=next(iter(arxiv)) if len(arxiv) == 1 else None,
        ).model_dump(),
        "doi_aliases": sorted(dois), "arxiv_ids": sorted(arxiv),
        "references": list(dict.fromkeys(work_id(w) for w in data.get("referenced_works", []))),
        "related": list(dict.fromkeys(work_id(w) for w in data.get("related_works", []))),
        "citation_count": data.get("cited_by_count"), "work_type": data.get("type"),
    }
    return OpenAlexWork.model_validate({
        **normalized, "retrieved_at": time.time(),
        "content_hash": hashlib.sha256(json.dumps(normalized, sort_keys=True).encode()).hexdigest(),
    })


class OpenAlexClient:
    def __init__(self, *, api_key: str | None = None,
                 transport: httpx.AsyncBaseTransport | None = None,
                 cache: LocalArtifactCache | None = None, max_attempts: int = 40,
                 interval: float = 1.05, budget_key: str | None = None,
                 verify_arxiv_dates: bool = False,
                 arxiv_transport: httpx.AsyncBaseTransport | None = None):
        if not 0 <= max_attempts <= 40:
            raise ValueError("openalex_attempt_limit")
        self._key = api_key
        self._client = httpx.AsyncClient(
            transport=transport, timeout=15, follow_redirects=False, trust_env=False,
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        self.cache = cache
        self.budget_key = "openalex-request-window:" + budget_key if budget_key else None
        self.attempts = 0
        self.max_attempts = max_attempts
        self.stopped = False
        self.limiter = RateLimiter(interval)
        self.verify_arxiv_dates = verify_arxiv_dates
        self.arxiv_transport = arxiv_transport
        self.arxiv_limiter = RateLimiter(3)

    async def __aenter__(self) -> OpenAlexClient:
        return self

    async def __aexit__(self, *args) -> None:
        await self._client.aclose()

    async def reserve_request(self) -> None:
        if self.stopped or self.attempts >= self.max_attempts:
            raise OpenAlexError("openalex_stopped_or_attempt_budget_exhausted")
        await self.limiter.acquire()
        if (self.cache and self.budget_key
                and not self.cache.reserve_request(self.budget_key, self.max_attempts)):
            self.stopped = True
            raise OpenAlexError("openalex_stopped_or_attempt_budget_exhausted")
        self.attempts += 1

    def stop_requests(self) -> None:
        self.stopped = True
        if self.cache and self.budget_key:
            self.cache.stop_requests(self.budget_key)

    def cached_works(self) -> list[OpenAlexWork]:
        """Reuse normalized metadata across queries; the caller still verifies identity."""
        works: dict[str, OpenAlexWork] = {}
        for saved in self.cache.current_values("openalex-metadata-v1:") if self.cache else []:
            for raw in saved.get("works", []):
                try:
                    work = OpenAlexWork.model_validate(raw)
                except ValueError:
                    continue
                prior = works.get(work.id)
                if prior is None or (work.retrieved_at, work.content_hash) > (
                    prior.retrieved_at, prior.content_hash,
                ):
                    works[work.id] = work
        return self._cached_public_dates([works[wid] for wid in sorted(works)])

    def _cached_public_dates(self, works: list[OpenAlexWork]) -> list[OpenAlexWork]:
        """Discovery after the initial batch must still honor verified first dates."""
        if not self.verify_arxiv_dates or self.cache is None:
            return works
        enriched = []
        for work in works:
            aid = work.external_ids.arxiv
            saved = self.cache.get("arxiv-first-submission-v1:" + aid) if aid else None
            if saved:
                try:
                    value = saved["date"]
                    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                        raise ValueError("invalid_cached_first_date")
                    calendar_date.fromisoformat(value)
                    if (saved["source"] != f"https://arxiv.org/abs/{aid}"
                            or not re.fullmatch(r"[a-f0-9]{64}", saved["hash"])):
                        raise ValueError("invalid_cached_date_provenance")
                    work = work.model_copy(update={
                        "first_public_date": value, "first_public_source": saved["source"],
                        "first_public_hash": saved["hash"],
                    })
                except (KeyError, TypeError, ValueError):
                    pass
            enriched.append(work)
        return enriched

    async def _query(self, params: dict[str, str]) -> list[OpenAlexWork]:
        params = {"select": FIELDS, "per_page": "100", **params}
        key = "openalex-metadata-v1:" + hashlib.sha256(
            json.dumps(params, sort_keys=True).encode()
        ).hexdigest()
        cached = self.cache.get(key) if self.cache else None
        if cached is not None:
            return self._cached_public_dates([
                OpenAlexWork.model_validate(w) for w in cached["works"]
            ])
        await self.reserve_request()
        try:
            response = await self._client.get(f"{BASE}/works", params=params)
            if response.status_code != 200:
                raise OpenAlexError(f"openalex_http_{response.status_code}")
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
                raise OpenAlexError("openalex_invalid_response")
            if len(payload["results"]) > 100:
                raise OpenAlexError("openalex_response_limit")
            works = [parse_work(w) for w in payload["results"]]
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError,
                OpenAlexError) as exc:
            self.stopped = True
            if self.cache and self.budget_key:
                self.cache.stop_requests(self.budget_key)
            reason = (str(exc) if isinstance(exc, OpenAlexError)
                      else "openalex_invalid_or_unavailable")
            raise OpenAlexError(reason) from None
        if self.cache:
            self.cache.put(key, {"works": [w.model_dump() for w in works]},
                           ttl=86400 if works else 300)
        return self._cached_public_dates(works)

    async def get_works(self, ids: list[str]) -> list[OpenAlexWork]:
        if not ids:
            return []
        if len(ids) > 100:
            raise OpenAlexError("openalex_batch_limit")
        requested = {work_id(w) for w in ids}
        works = await self._query({"filter": "ids.openalex:" + "|".join(sorted(requested))})
        if any(w.id not in requested for w in works):
            self.stopped = True
            if self.cache and self.budget_key:
                self.cache.stop_requests(self.budget_key)
            raise OpenAlexError("openalex_identity_mismatch")
        return works

    async def find_doi(self, doi: str) -> list[OpenAlexWork]:
        doi = doi_id(doi)
        works = await self._query({"filter": "doi:" + doi})
        if any(doi not in w.doi_aliases for w in works):
            self.stopped = True
            if self.cache and self.budget_key:
                self.cache.stop_requests(self.budget_key)
            raise OpenAlexError("openalex_identity_mismatch")
        return works

    async def get_arxiv_works(self, ids: list[str]) -> list[OpenAlexWork]:
        if not ids:
            return []
        if len(ids) > 50 or any(not re.fullmatch(
            r"(?:\d{4}\.\d{4,5}|[a-z][a-z.-]+/\d{7})", aid, re.I,
        ) for aid in ids):
            raise OpenAlexError("invalid_arxiv_batch")
        values = [f"{scheme}://arxiv.org/abs/{aid}" for aid in sorted(set(ids))
                  for scheme in ("http", "https")]
        works = await self._query({"filter": "locations.landing_page_url:" + "|".join(values)})
        if any(not set(w.arxiv_ids).intersection(ids) for w in works):
            self.stop_requests()
            raise OpenAlexError("openalex_arxiv_identity_mismatch")
        return works

    async def enrich_public_dates(self, works: list[OpenAlexWork]) -> list[OpenAlexWork]:
        """Verify arXiv first submission in one batch inside the same request budget.

        The separate HTTP client never carries the OpenAlex authentication header.
        Date lookup does not replace bibliographic identity or publication-year aliases.
        """
        if not self.verify_arxiv_dates:
            return works
        dates: dict[str, dict] = {}
        ids = sorted({w.external_ids.arxiv for w in works if w.external_ids.arxiv})[:50]
        for aid in ids:
            cached = self.cache.get("arxiv-first-submission-v1:" + aid) if self.cache else None
            if cached:
                dates[aid] = cached
        pending = [aid for aid in ids if aid not in dates]
        if pending:
            await self.arxiv_limiter.acquire()
            await self.reserve_request()
            try:
                async with httpx.AsyncClient(
                    transport=self.arxiv_transport, timeout=20, trust_env=False,
                    follow_redirects=False, headers={"User-Agent": "paper-review-ai/0.1.0"},
                ) as client:
                    response = await client.get("https://export.arxiv.org/api/query", params={
                        "id_list": ",".join(pending), "max_results": str(len(pending)),
                    })
                if response.status_code != 200:
                    raise OpenAlexError(f"arxiv_date_http_{response.status_code}")
                for aid in pending:
                    try:
                        metadata = parse_arxiv_atom(response.text, aid)
                    except ArXivError:
                        continue
                    if metadata.arxiv_id != aid:
                        continue
                    value = {"date": metadata.first_submitted_date[:10],
                             "source": f"https://arxiv.org/abs/{aid}",
                             "hash": hashlib.sha256(response.content).hexdigest()}
                    calendar_date.fromisoformat(value["date"])
                    dates[aid] = value
                    if self.cache:
                        self.cache.put("arxiv-first-submission-v1:" + aid, value, ttl=86400)
            except (httpx.HTTPError, ValueError, OpenAlexError) as exc:
                self.stop_requests()
                raise OpenAlexError(str(exc) if isinstance(exc, OpenAlexError)
                                    else "arxiv_date_lookup_unavailable") from None
        return [w.model_copy(update={
            "first_public_date": dates[w.external_ids.arxiv]["date"],
            "first_public_source": dates[w.external_ids.arxiv]["source"],
            "first_public_hash": dates[w.external_ids.arxiv]["hash"],
        }) if w.external_ids.arxiv in dates else w for w in works]

    async def search(self, title: str) -> list[OpenAlexWork]:
        # Discovery only; the caller must corroborate identity independently.
        # Bibliographic titles are literal data, not wildcard/fuzzy search syntax.
        query = re.sub(r"[*?%~\[\]{}\\]", " ", title)[:500]
        if not query.strip():
            return []
        return await self._query({"search": query, "per_page": "10"})
