import asyncio

import httpx
import pytest

from paper_review.application.openalex_inventory import resolve_openalex_inventory
from paper_review.application.stage1_shortlist import Stage1Analysis, apply_context_classifications
from paper_review.domain.models import ExternalIds, Paper, Reference
from paper_review.domain.stage1_scoring import rank_stage1_candidates
from paper_review.infrastructure.academic_api.local_cache import LocalArtifactCache
from paper_review.infrastructure.academic_api.openalex import (
    OpenAlexClient,
    OpenAlexError,
    abstract_text,
    parse_work,
)


@pytest.mark.parametrize("case", ["match", "expired", "conflicting_id", "ambiguous"])
def test_cross_query_cached_identity_is_strict_and_ttl_bounded(settings, case):
    cache = LocalArtifactCache(settings.database_path.parent / "metadata-cache.db")
    calls = []

    def handler(request):
        calls.append(request)
        records = [work(title="A useful prior method")]
        if case == "ambiguous":
            records.append(work(wid="W2", title="A useful prior method", arxiv="1602.00001"))
        return httpx.Response(200, json={"results": records})

    async def run():
        async with OpenAlexClient(cache=cache, transport=httpx.MockTransport(handler),
                                  interval=0) as seed:
            await seed.search("different discovery query")
        if case == "expired":
            with cache.connect() as connection:
                connection.execute("UPDATE artifacts SET expires=0")
        raw = "A Author. A useful prior method. NeurIPS, 2016."
        if case == "conflicting_id":
            raw += " arXiv:1609.99999"
        reference = Reference(id="r", revision_id="v1", paper_id="target", ref_order=1,
                              raw_text=raw)
        async with OpenAlexClient(cache=cache, max_attempts=0, interval=0,
                                  transport=httpx.MockTransport(handler)) as client:
            result = await resolve_openalex_inventory(
                Paper(id="target", published_at="2020-01-01"), [reference], client,
            )
            assert client.attempts == 0
        return result

    inventory = asyncio.run(run())
    assert len(calls) == 1  # Only the cache seed; no retrieval HTTP calls.
    resolution = inventory.reference_resolutions[0]
    if case == "match":
        assert resolution.status == "resolved"
        provenance = inventory.candidates[0].metadata_provenance
        assert provenance["identity_lookup"] == "cached_openalex_metadata"
        assert inventory.candidates[0].metadata_provenance["source"] == "openalex"
    else:
        assert resolution.status == "unresolved"
        assert not inventory.eligible_candidates
        if case == "ambiguous":
            assert resolution.reason == "ambiguous_identity"


def test_discovery_query_omits_venue_but_still_checks_full_reference():
    queries = []

    def handler(request):
        queries.append(request.url.params.get("search"))
        return httpx.Response(200, json={"results": [work(title="A useful prior method")]})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            return await resolve_openalex_inventory(
                Paper(id="target", published_at="2020-01-01"),
                [Reference(id="r", revision_id="v1", paper_id="target", ref_order=1,
                           raw_text="A Author. A useful prior method. NeurIPS, 2016.")], client,
            )

    result = asyncio.run(run())
    assert queries == ["A useful prior method"]
    assert result.reference_resolutions[0].status == "resolved"


def test_discovery_query_omits_trailing_bibliographic_year():
    from paper_review.application.openalex_inventory import discovery_title

    reference = Reference(id="r", revision_id="v", paper_id="t", ref_order=1,
                          raw_text="A Author. A useful prior method, 2016.")
    assert discovery_title(reference) == "A useful prior method"
    assert discovery_title(reference.model_copy(update={
        "raw_text": "A Author. Learning in 2016, revisited. NeurIPS, 2017.",
    })) == "Learning in 2016, revisited"


def test_identifier_reference_survives_full_pool_without_reordering_bibliography(settings):
    references = [Reference(id=str(i), revision_id="v", paper_id="target", ref_order=i,
                            raw_text=f"A Author. Prior method number {i} design. 2016.")
                  for i in range(1, 51)]
    references.append(Reference(
        id="51", revision_id="v", paper_id="target", ref_order=51,
        raw_text="A Author. Explicit prior method. arXiv:1609.00001, 2016.",
    ))
    calls = []
    cache = LocalArtifactCache(settings.database_path.parent / "metadata-cache.db")

    def handler(request):
        calls.append(request)
        if "filter" in request.url.params:
            rows = [work(wid="W999", title="Explicit prior method", arxiv="1609.00001")]
        else:
            assert request.url.params["search"] == "seed"
            rows = [work(wid=f"W{i}", title=f"Prior method number {i} design",
                         arxiv=f"1601.{i:05d}") for i in range(1, 51)]
        return httpx.Response(200, json={"results": rows})

    async def run():
        async with OpenAlexClient(cache=cache, transport=httpx.MockTransport(handler),
                                  interval=0) as seed:
            await seed.search("seed")
        calls.clear()
        async with OpenAlexClient(cache=cache, transport=httpx.MockTransport(handler), interval=0,
                                  max_attempts=40) as client:
            result = await resolve_openalex_inventory(
                Paper(id="target", published_at="2020-01-01"), references, client,
            )
            assert client.attempts <= 40
            return result

    inventory = asyncio.run(run())
    assert len(inventory.candidates) == 50
    assert [r.reference_order for r in inventory.reference_resolutions] == list(range(1, 52))
    explicit = inventory.reference_resolutions[-1]
    assert explicit.candidate_id == "openalex:W999"
    assert explicit.status == "resolved"
    assert calls[0].url.params.get("filter")


@pytest.mark.parametrize("variant", [
    "idless", "different_author", "different_date", "different_doi",
])
def test_identifierless_duplicate_requires_complete_agreement_and_is_not_readded(variant):
    authors = [{"author": {"display_name": name}} for name in
               ["A Author", "B Writer", "C Researcher"]]
    primary = work(title="A useful prior method", authorships=authors)
    alias = work(wid="W2", title="A useful prior method", locations=[], authorships=authors)
    if variant == "different_author":
        alias["authorships"] = authors[:2] + [{"author": {"display_name": "D Other"}}]
    elif variant == "different_date":
        alias["publication_date"] = "2016-06-01"
    elif variant == "different_doi":
        alias["doi"] = "https://doi.org/10.1234/distinct"

    def handler(request):
        if "W10" in request.url.params.get("filter", ""):
            return httpx.Response(200, json={"results": [work(wid="W10", arxiv="1901.00001",
                year=2019, referenced_works=["https://openalex.org/W1", "https://openalex.org/W2"])]})
        return httpx.Response(200, json={"results": [primary, alias]})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            return await resolve_openalex_inventory(
                Paper(id="target", published_at="2019-01-01",
                      external_ids=ExternalIds(openalex="W10")),
                [Reference(id="r", revision_id="v1", paper_id="target", ref_order=1,
                    raw_text="A Author, B Writer, C Researcher. A useful prior method. 2016.")],
                client,
            )

    result = asyncio.run(run())
    if variant == "idless":
        assert result.reference_resolutions[0].candidate_id == "openalex:W1"
        assert result.reference_resolutions[0].status == "resolved"
        assert len(result.candidates) == 1  # The indexed alias is not added a second time.
        provenance = result.candidates[0].metadata_provenance
        assert '"W2"' in provenance["openalex_alias_hashes"]
    else:
        assert result.reference_resolutions[0].status == "unresolved"
        assert result.reference_resolutions[0].reason == "ambiguous_identity"


@pytest.mark.parametrize("cached", [False, True])
def test_later_discovery_honors_verified_first_date(settings, cached):
    from paper_review.application.openalex_inventory import to_candidate

    cache = LocalArtifactCache(settings.database_path.parent / "verified-dates.db")
    cache.put("arxiv-first-submission-v1:2206.00001", {
        "date": "2022-06-02", "source": "https://arxiv.org/abs/2206.00001", "hash": "a" * 64,
    }, ttl=86400)
    raw = work(arxiv="2206.00001", year=2022, publication_date="2022-01-01")
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [raw]})

    async def run():
        async with OpenAlexClient(cache=cache, verify_arxiv_dates=True, interval=0,
                                  transport=httpx.MockTransport(handler)) as client:
            initial = await client.search("later discovery")
        if cached:
            async with OpenAlexClient(cache=cache, max_attempts=0, verify_arxiv_dates=True,
                                      transport=httpx.MockTransport(handler)) as client:
                return client.cached_works()
        return initial

    found = asyncio.run(run())[0]
    assert found.date == "2022-01-01"  # Preserve original metadata independently.
    assert found.first_public_date == "2022-06-02"
    candidate = to_candidate(Paper(id="target", published_at="2022-05-29"), found, cited=True)
    assert candidate.eligibility_status == "excluded"
    assert candidate.exclusion_reason == "published_after_target"
    assert candidate.date_provenance and candidate.date_provenance.source_id.endswith("2206.00001")
    assert len(calls) == 1  # Date reuse itself never adds a request.


def work(wid="W1", *, title="Prior method", arxiv="1601.00001", year=2016, **extra):
    return {
        "id": f"https://openalex.org/{wid}", "title": title, "publication_year": year,
        "publication_date": str(year), "authorships": [{"author": {"display_name": "A Author"}}],
        "locations": [{"landing_page_url": f"https://arxiv.org/abs/{arxiv}"}],
        "abstract_inverted_index": {"A": [0], "method": [1]},
        "referenced_works": [], "related_works": [], **extra,
    }


@pytest.mark.parametrize("status", [301, 401, 403, 429, 503])
def test_error_stops_without_retry_or_redirect(settings, status):
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["Authorization"] == "Bearer dummy-key"
        assert "dummy-key" not in str(request.url)
        return httpx.Response(status, json={"secret": "must-not-be-stored"})

    cache = LocalArtifactCache(settings.database_path.parent / "cache.db")

    async def run():
        for _ in range(2):
            async with OpenAlexClient(api_key="dummy-key", transport=httpx.MockTransport(handler),
                                      interval=0, cache=cache, budget_key="same-job") as client:
                with pytest.raises(OpenAlexError):
                    await client.search("Prior")

    asyncio.run(run())
    assert len(calls) == 1  # Persistent stop also blocks a restarted client.
    assert b"dummy-key" not in cache.path.read_bytes()
    assert b"must-not-be-stored" not in cache.path.read_bytes()


def test_cache_preserves_original_provenance_and_budget_survives_restart(settings):
    calls = []
    cache = LocalArtifactCache(settings.database_path.parent / "cache.db")

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [work()]})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0,
                                  cache=cache, budget_key="job", max_attempts=1) as client:
            first = await client.get_works(["W1"])
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0,
                                  cache=cache, budget_key="job", max_attempts=1) as client:
            assert await client.get_works(["W1"]) == first
            with pytest.raises(OpenAlexError, match="budget_exhausted"):
                await client.search("uncached")

    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("index", [None, {}, {"A": [1]}, {"A": [0], "B": [0]}, {"A": [True]}])
def test_bad_or_missing_abstract_is_not_filled(index):
    assert abstract_text(index) is None


def test_abstract_reconstruction_precision_and_location_identity():
    data = work(publication_date="2016-03", doi="https://doi.org/10.1234/example")
    data["locations"].append({"landing_page_url": "https://arxiv.org.attacker.test/abs/9999.99999"})
    parsed = parse_work(data)
    assert parsed.abstract == "A method"
    assert parsed.date == "2016-03"
    assert parsed.arxiv_ids == ["1601.00001"]
    assert parsed.external_ids.doi == "10.1234/example"
    assert parse_work(work(publication_date="2016-13-99")).date is None


def test_inventory_identity_related_nulls_and_provenance():
    target = Paper(id="target", title="Target", published_at="2017-06-12",
                   external_ids=ExternalIds(arxiv="1706.03762"))
    refs = [Reference(id="ref", revision_id="v1", paper_id="target", ref_order=1,
                      raw_text="Author. Prior method. arXiv:1601.00001. 2016.", citation_key="[1]")]
    target_record = work("W10", title="Target", arxiv="1706.03762", year=2017,
                         referenced_works=["https://openalex.org/W1"],
                         related_works=["https://openalex.org/W2"])

    def handler(request):
        if "1706.03762" in request.url.params.get("filter", ""):
            result = [target_record]
        elif "W2" in request.url.params.get("filter", ""):
            result = [work("W2", title="Related method", arxiv="1501.00001", year=2015)]
        else:
            result = [work()]
        return httpx.Response(200, json={"results": result})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            return await resolve_openalex_inventory(target, refs, client)

    inventory = asyncio.run(run())
    assert inventory.references_complete is False
    assert inventory.reference_resolutions[0].status == "resolved"
    candidates = {c.paper_id: c for c in inventory.candidates}
    assert set(candidates) == {"openalex:W1", "openalex:W2"}
    cited, related = candidates["openalex:W1"], candidates["openalex:W2"]
    assert cited.source_references[0].citation_key == "[1]"
    assert cited.is_influential_citation is None
    assert cited.date_provenance and cited.date_provenance.precision == "year"
    assert "openalex" in cited.date_provenance.source_id
    assert related.is_target_reference is False
    initial = rank_stage1_candidates(target, inventory, target_references_fully_resolved=False)
    analysis = apply_context_classifications(
        Stage1Analysis(target=target, inventory=inventory, ranking=initial), {"contexts": []},
    )
    scores = {r.candidate.paper_id: r.score for r in analysis.ranking.ranked_candidates}
    assert scores["openalex:W1"].citation_relation == 1
    assert scores["openalex:W1"].citation_importance is None
    # Related work with no observed citation does not acquire a false zero.
    unranked = analysis.ranking.unranked_candidates
    assert unranked[0].candidate.paper_id == "openalex:W2"
    assert unranked[0].score.citation_relation is None


def test_title_alone_cannot_resolve_and_conflicting_ids_are_retained():
    target = Paper(id="target", published_at="2020-01-01")
    refs = [Reference(id=str(i), revision_id="v", paper_id="target", ref_order=i, raw_text=text)
            for i, text in enumerate([
                "Someone. Prior method. 2016.",
                "Author. Prior method. arXiv:1601.00001 DOI:10.1234/other. 2016.",
                "Author. Prior method. arXiv:1601.00001 arXiv:1602.00001. 2016.",
            ])]

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": [
                work(doi="https://doi.org/10.1234/example"),
            ]})), interval=0) as client:
            return await resolve_openalex_inventory(target, refs, client)

    inventory = asyncio.run(run())
    assert len(inventory.reference_resolutions) == 3
    assert all(r.status == "unresolved" for r in inventory.reference_resolutions)
    assert inventory.reference_resolutions[1].reason == "identifier_conflict"
    assert inventory.reference_resolutions[2].reason == (
        "multiple_identifiers_possible_merged_reference"
    )
    assert not inventory.eligible_candidates


def test_invalid_payload_is_sanitized_and_no_second_request():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"results": [{"raw": "private-body"}]})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            for _ in range(2):
                with pytest.raises(OpenAlexError) as exc:
                    await client.search("query")
                assert "private-body" not in str(exc.value)

    asyncio.run(run())
    assert len(calls) == 1


def test_candidate_limit_retains_every_reference_outcome():
    target = Paper(id="target", published_at="2020-01-01")
    refs = [Reference(id=str(i), revision_id="v", paper_id="target", ref_order=i,
                      raw_text=f"Unknown paper {i}.") for i in range(55)]

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"results": []})),
            interval=0, max_attempts=2,
        ) as client:
            result = await resolve_openalex_inventory(target, refs, client)
            assert client.attempts == 2
            return result

    inventory = asyncio.run(run())
    assert len(inventory.candidates) == 50
    assert len(inventory.reference_resolutions) == 55
    assert not inventory.eligible_candidates


def test_distinct_records_with_shared_doi_are_ambiguous():
    target = Paper(id="target", title="Target", published_at="2020-01-01",
                   external_ids=ExternalIds(arxiv="1901.00001"))
    records = [work("W1", doi="https://doi.org/10.1234/shared"),
               work("W2", doi="https://doi.org/10.1234/shared", arxiv="1602.00001")]

    def handler(request):
        return httpx.Response(200, json={"results": [
            work("W10", title="Target", arxiv="1901.00001", year=2019,
                 referenced_works=["https://openalex.org/W1", "https://openalex.org/W2"]),
        ] if "1901.00001" in request.url.params.get("filter", "") else records})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            return await resolve_openalex_inventory(target, [], client)

    inventory = asyncio.run(run())
    assert len(inventory.candidates) == 2
    assert all(c.resolution_status == "ambiguous" for c in inventory.candidates)
    assert not inventory.eligible_candidates


def test_unresolved_placeholders_do_not_evict_identified_references():
    target = Paper(id="target", title="Target", published_at="2020-01-01",
                   external_ids=ExternalIds(arxiv="1901.00001"))
    references = [Reference(id=str(i), revision_id="v", paper_id="target", ref_order=i,
                            raw_text=f"Unresolved entry {i}.") for i in range(50)]

    def handler(request):
        if "1901.00001" in request.url.params.get("filter", ""):
            result = [work("W10", title="Target", arxiv="1901.00001", year=2019,
                           referenced_works=["https://openalex.org/W1"])]
        elif "filter" in request.url.params:
            result = [work()]
        else:
            result = []
        return httpx.Response(200, json={"results": result})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            return await resolve_openalex_inventory(target, references, client)

    inventory = asyncio.run(run())
    assert len(inventory.candidates) == 50
    assert len(inventory.reference_resolutions) == 50
    assert any(c.paper_id == "openalex:W1" and c.resolution_status == "resolved"
               for c in inventory.candidates)


def test_bibliographic_title_is_not_wildcard_search_syntax():
    queries = []

    def handler(request):
        queries.append(request.url.params["search"])
        return httpx.Response(200, json={"results": []})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            await client.search("Vicuna: 90%* ChatGPT quality?")
            await client.search("***")

    asyncio.run(run())
    assert queries == ["Vicuna: 90   ChatGPT quality "]


def test_arxiv_location_batch_includes_both_schemes_and_rejects_other_identity():
    filters = []

    def handler(request):
        filters.append(request.url.params["filter"])
        return httpx.Response(200, json={"results": [work(arxiv="1601.00001")]})

    async def run():
        async with OpenAlexClient(transport=httpx.MockTransport(handler), interval=0) as client:
            assert (await client.get_arxiv_works(["1601.00001"]))[0].id == "W1"
            with pytest.raises(OpenAlexError, match="arxiv_identity_mismatch"):
                await client.get_arxiv_works(["1602.00001"])

    asyncio.run(run())
    assert "http://arxiv.org/abs/1601.00001" in filters[0]
    assert "https://arxiv.org/abs/1601.00001" in filters[0]


def test_verified_first_date_uses_shared_budget_and_never_forwards_key(settings):
    from paper_review.application.openalex_inventory import to_candidate
    from paper_review.infrastructure.academic_api.rate_limiter import RateLimiter

    atom = '''<feed xmlns="http://www.w3.org/2005/Atom"
        xmlns:arxiv="http://arxiv.org/schemas/atom"><entry>
        <id>http://arxiv.org/abs/1601.00001v2</id><updated>2018-01-01T00:00:00Z</updated>
        <published>2016-01-03T00:00:00Z</published><title>Prior method</title>
        <summary>A method.</summary><author><name>A Author</name></author>
        <arxiv:primary_category term="cs.CL"/><category term="cs.CL"/>
        <link title="pdf" href="https://arxiv.org/pdf/1601.00001v2"/></entry></feed>'''
    calls = []

    def arxiv(request):
        calls.append(request)
        assert "Authorization" not in request.headers
        assert "dummy-key" not in str(request.url)
        return httpx.Response(200, text=atom)

    cache = LocalArtifactCache(settings.database_path.parent / "cache.db")

    async def run():
        async with OpenAlexClient(
            api_key="dummy-key", verify_arxiv_dates=True, interval=0, max_attempts=1,
            cache=cache, arxiv_transport=httpx.MockTransport(arxiv),
        ) as client:
            client.arxiv_limiter = RateLimiter(0)
            dated = await client.enrich_public_dates([parse_work(work(publication_date="2017"))])
            assert client.attempts == 1
            assert await client.enrich_public_dates(dated) == dated  # cache, no extra request
            with pytest.raises(OpenAlexError, match="budget_exhausted"):
                await client.search("uncached")
            return dated[0]

    result = asyncio.run(run())
    candidate = to_candidate(Paper(id="target", published_at="2016-06-01"), result, cited=True)
    assert result.date == "2017"  # Publication metadata is preserved separately.
    assert candidate.publication_date == "2016-01-03"
    assert candidate.temporal_status == "eligible"
    assert candidate.date_provenance and candidate.date_provenance.kind == "arxiv_first_submitted"
    assert len(calls) == 1
