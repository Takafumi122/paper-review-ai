"""Unit tests for ArXivClient using HTTPX mock transport."""

import asyncio
from pathlib import Path

import httpx
import pytest

from paper_review.infrastructure.academic_api.arxiv import (
    ArXivClient,
    ArXivDownloadError,
    ArXivNotFoundError,
    ArXivParseError,
    ArXivSecurityError,
    RateLimiter,
)

SAMPLE_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>http://arxiv.org/api/query?id_list=1706.03762</id>
  <updated>2023-08-02T00:00:00Z</updated>
  <entry>
    <id>http://arxiv.org/abs/1706.03762v7</id>
    <updated>2023-08-02T01:09:47Z</updated>
    <published>2017-06-12T17:57:34Z</published>
    <title> Attention Is All You Need
    </title>
    <summary> The dominant sequence transduction models are based on complex recurrent or
convolutional neural networks in an encoder-decoder configuration. </summary>
    <author>
      <name>Ashish Vaswani</name>
    </author>
    <author>
      <name>Noam Shazeer</name>
    </author>
    <arxiv:doi>10.48550/arXiv.1706.03762</arxiv:doi>
    <arxiv:comment>15 pages, 5 figures</arxiv:comment>
    <arxiv:journal_ref>NeurIPS 2017</arxiv:journal_ref>
    <arxiv:primary_category term="cs.CL"/>
    <category term="cs.CL"/>
    <category term="cs.LG"/>
    <link title="pdf" href="http://arxiv.org/pdf/1706.03762v7"
          rel="related" type="application/pdf"/>
  </entry>
</feed>"""

EMPTY_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <id>http://arxiv.org/api/query?id_list=9999.99999</id>
</feed>"""

ERROR_ENTRY_ATOM_XML = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Error</title>
    <summary>incorrect id format for 9999.99999v99</summary>
  </entry>
</feed>"""

VALID_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


def test_get_metadata_success():
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert "export.arxiv.org" in str(request.url)
        assert "id_list=1706.03762" in str(request.url)
        assert "paper-review-ai" in request.headers.get("User-Agent", "")
        return httpx.Response(200, text=SAMPLE_ATOM_XML)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            return await client.get_metadata("1706.03762")

    metadata = asyncio.run(_run())

    assert metadata.arxiv_id == "1706.03762"
    assert metadata.title == "Attention Is All You Need"
    assert metadata.authors == ["Ashish Vaswani", "Noam Shazeer"]
    assert metadata.abstract.startswith("The dominant sequence")
    assert metadata.primary_category == "cs.CL"
    assert metadata.categories == ["cs.CL", "cs.LG"]
    assert metadata.first_submitted_date == "2017-06-12T17:57:34Z"
    assert metadata.current_version == "v7"
    assert metadata.current_version_date == "2023-08-02T01:09:47Z"
    assert metadata.version_history is None
    assert metadata.pdf_url == "https://arxiv.org/pdf/1706.03762v7.pdf"
    assert metadata.doi == "10.48550/arXiv.1706.03762"
    assert metadata.comment == "15 pages, 5 figures"
    assert metadata.journal_ref == "NeurIPS 2017"

    # Test conversion to domain entities
    ext_ids = metadata.to_external_ids()
    assert ext_ids.arxiv == "1706.03762"
    assert ext_ids.doi == "10.48550/arXiv.1706.03762"

    paper = metadata.to_paper()
    assert paper.id == "arxiv:1706.03762"
    assert paper.published_at == "2017-06-12"

    rev = metadata.to_paper_revision(page_count=15)
    assert rev.arxiv_version == "v7"
    assert rev.page_count == 15


def test_get_metadata_not_found_404():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="Not Found")

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            await client.get_metadata("9999.99999")

    with pytest.raises(ArXivNotFoundError, match="not found"):
        asyncio.run(_run())


def test_get_metadata_empty_feed():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=EMPTY_ATOM_XML)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            await client.get_metadata("9999.99999")

    with pytest.raises(ArXivNotFoundError, match="not found"):
        asyncio.run(_run())


def test_get_metadata_arxiv_error_entry():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=ERROR_ENTRY_ATOM_XML)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            await client.get_metadata("9999.99999")

    with pytest.raises(ArXivNotFoundError, match="arXiv paper not found"):
        asyncio.run(_run())


def test_get_metadata_malformed_xml():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<feed>malformed unclosed tag")

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            await client.get_metadata("1706.03762")

    with pytest.raises(ArXivParseError, match="Malformed XML"):
        asyncio.run(_run())


def test_get_metadata_transient_retry():
    calls = 0

    def handle_request(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, text=SAMPLE_ATOM_XML)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
        backoff_factor=0.001,
    )

    async def _run():
        async with client:
            return await client.get_metadata("1706.03762")

    meta = asyncio.run(_run())
    assert meta.title == "Attention Is All You Need"
    assert calls == 2


def test_download_pdf_success_and_cleanup():
    def handle_request(request: httpx.Request) -> httpx.Response:
        assert "arxiv.org/pdf/1706.03762" in str(request.url)
        return httpx.Response(
            200,
            content=VALID_PDF_BYTES,
            headers={"Content-Type": "application/pdf"},
        )

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )
    temp_path: Path | None = None

    async def _run():
        nonlocal temp_path
        async with client:
            async with client.download_pdf_temp("1706.03762") as path:
                temp_path = path
                assert path.exists()
                assert path.is_file()
                assert path.suffix == ".pdf"
                assert path.read_bytes().startswith(b"%PDF")

    asyncio.run(_run())

    # Outside the context manager, the temporary file MUST be cleaned up
    assert temp_path is not None
    assert not temp_path.exists()


def test_download_pdf_cleanup_on_exception():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=VALID_PDF_BYTES)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )
    captured_path: Path | None = None

    async def _run():
        nonlocal captured_path
        async with client:
            async with client.download_pdf_temp("1706.03762") as path:
                captured_path = path
                assert path.exists()
                raise RuntimeError("simulated parsing failure")

    with pytest.raises(RuntimeError, match="simulated parsing failure"):
        asyncio.run(_run())

    # Cleanup must occur even when an exception is raised inside the context
    assert captured_path is not None
    assert not captured_path.exists()


def test_download_pdf_invalid_magic_bytes():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>HTML Error Page</html>")

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            async with client.download_pdf_temp("1706.03762"):
                pass

    with pytest.raises(ArXivDownloadError, match="not a valid PDF"):
        asyncio.run(_run())


def test_download_pdf_size_exceeded():
    large_pdf = b"%PDF" + b"X" * 1000

    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=large_pdf)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            async with client.download_pdf_temp("1706.03762", max_size_bytes=500):
                pass

    with pytest.raises(ArXivDownloadError, match="exceeds limit"):
        asyncio.run(_run())


def test_security_rejection_forbidden_host():
    from paper_review.infrastructure.academic_api.arxiv import validate_arxiv_url

    with pytest.raises(ArXivSecurityError, match="forbidden host"):
        validate_arxiv_url("https://attacker.com/evil.pdf")


def test_security_rejection_redirect_to_evil_host():
    def handle_request(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"Location": "http://169.254.169.254/latest/meta-data"},
        )

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )

    async def _run():
        async with client:
            await client.get_metadata("1706.03762")

    with pytest.raises(ArXivSecurityError, match="forbidden host"):
        asyncio.run(_run())


def test_sync_wrappers():
    def handle_request(request: httpx.Request) -> httpx.Response:
        if "api/query" in str(request.url):
            return httpx.Response(200, text=SAMPLE_ATOM_XML)
        return httpx.Response(200, content=VALID_PDF_BYTES)

    client = ArXivClient(
        transport=httpx.MockTransport(handle_request),
        rate_limiter=RateLimiter(min_interval_seconds=0),
    )
    meta = client.get_metadata_sync("1706.03762")
    assert meta.title == "Attention Is All You Need"

    captured: Path | None = None
    with client.download_pdf_temp_sync("1706.03762") as path:
        assert path.exists()
        captured = path
    assert captured is not None
    assert not captured.exists()
