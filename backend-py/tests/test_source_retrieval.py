import httpx
import pytest

from app.services import scrape_assistant_tools as tools
from app.services import source_retrieval as retrieval

HOME = "https://www.example.edu"
URL = HOME + "/requirements.pdf.pdf?year=2027"


@pytest.mark.asyncio
@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream"])
async def test_binary_pdf_is_not_decoded_or_url_rewritten(monkeypatch, content_type):
    raw = b"%PDF-1.7\n\xff\x00binary"
    seen = []
    original = httpx.AsyncClient
    def reply(request):
        seen.append(str(request.url))
        return httpx.Response(200, content=(
            b"User-agent: *\nAllow: /\n" if request.url.path == "/robots.txt" else raw
        ), headers={"content-type": content_type})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(reply)))
    assert await tools.public_request(URL) == raw
    assert seen == [HOME + "/robots.txt", URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("robots_status,robots_body,source_status,headers", [
    (403, b"", 200, {}),
    (200, b"User-agent: *\nDisallow: /\n", 200, {}),
    (200, b"User-agent: *\nAllow: /\n", 403, {}),
    (200, b"User-agent: *\nAllow: /\n", 503, {"cf-mitigated": "challenge"}),
    (200, b"User-agent: *\nAllow: /\n", 429, {}),
])
async def test_blocked_sources_never_use_provider(monkeypatch, robots_status, robots_body, source_status, headers):
    original = httpx.AsyncClient
    def reply(request):
        return (httpx.Response(robots_status, content=robots_body) if request.url.path == "/robots.txt"
                else httpx.Response(source_status, content=b"not permitted", headers=headers))
    async def forbidden(url, **kwargs):
        pytest.fail("A restriction must not trigger a proxy")
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-provider-token")
    monkeypatch.setattr(retrieval, "_scrape_do_bytes", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(reply)))
    with pytest.raises(tools.SourceReadError):
        await tools.public_request(URL)


@pytest.mark.asyncio
async def test_permitted_temporary_failure_uses_binary_provider(monkeypatch):
    original = httpx.AsyncClient
    seen = []
    def reply(request):
        return (httpx.Response(200, text="User-agent: *\nAllow: /\n")
                if request.url.path == "/robots.txt" else httpx.Response(502, text="origin temporarily unavailable"))
    async def proxy(url, **kwargs):
        seen.append(url)
        return b"%PDF-1.7\n\xffbinary"
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-provider-token")
    monkeypatch.setattr(retrieval, "_scrape_do_bytes", proxy)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(reply)))
    assert (await tools.public_request(URL)).startswith(b"%PDF-")
    assert seen == [URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("status,headers,raw", [
    (200, {"content-type": "application/pdf"}, b"<html>not pdf</html>"),
    (302, {"location": "https://other.test/private.pdf"}, b""),
    (200, {"Scrape.do-Target-Redirected-Location": "https://other.test/private.pdf"}, b"%PDF-1.7"),
    (200, {}, b"x" * (retrieval.MAX_BYTES + 1)),
])
async def test_invalid_pdf_redirects_and_size_are_rejected(status, headers, raw):
    with pytest.raises(tools.SourceReadError):
        await retrieval._read_response(httpx.Response(status, headers=headers, content=raw), URL)


@pytest.mark.asyncio
async def test_pdf_parser_failure_is_not_verified(monkeypatch):
    async def broken(raw, url):
        raise tools.SourceReadError("PDF parsing failed within the memory or CPU budget.")
    monkeypatch.setattr(tools, "inspect_pdf", broken)
    result = await tools.inspect_source(URL, HOME, raw_body=b"%PDF-1.7\nbroken")
    assert result["verified"] is False
    assert result["failureStage"] == "pdf_parse"


@pytest.mark.asyncio
async def test_compressed_response_is_decoded_once():
    import gzip
    raw = b"%PDF-1.7\nbinary"
    response = httpx.Response(200, headers={"content-encoding": "gzip"},
                              content=gzip.compress(raw))
    # httpx already decoded content; retaining Content-Encoding must not
    # cause a second decompression while the source is validated.
    assert await retrieval._read_response(response, URL, raw=response.content) == raw


@pytest.mark.asyncio
async def test_html_shares_the_binary_safe_reader():
    raw = b"<html><main>Official course requirements</main></html>"
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=raw)
    )) as client:
        assert await retrieval.read_permitted_source(client, HOME + "/course") == raw


@pytest.mark.asyncio
async def test_provider_preserves_bytes_disables_redirects_and_redacts_token(monkeypatch, caplog):
    import logging
    from contextlib import asynccontextmanager
    from app.services.scraper import http_fetcher, rate_limiter, scrape_do_semaphore
    original = httpx.AsyncClient
    seen = []
    token = "test-secret-never-in-logs"
    raw = b"%PDF-1.7\n\xff\x00data"
    def reply(request):
        seen.append(request)
        return httpx.Response(200, content=raw)
    @asynccontextmanager
    async def slot(*args, **kwargs):
        yield
    async def acquire():
        return True
    monkeypatch.setenv("SCRAPE_DO_TOKEN", token)
    monkeypatch.setattr(http_fetcher, "_get_scrape_do_sem", slot)
    monkeypatch.setattr(scrape_do_semaphore, "account_slot", slot)
    monkeypatch.setattr(rate_limiter, "acquire_scrape_do", acquire)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(reply)))
    with caplog.at_level(logging.INFO, logger="httpx"):
        assert await retrieval._scrape_do_bytes(URL) == raw
    assert len(seen) == 1
    assert seen[0].url.params["url"] == URL
    assert seen[0].url.params["disableRedirection"] == "true"
    assert seen[0].url.params["render"] == "false"
    assert seen[0].headers["User-Agent"] == "StudyInfoCentre-SourceVerifier/1.0"
    assert token not in caplog.text


@pytest.mark.asyncio
async def test_downloaded_pdf_uses_real_isolated_text_parser(monkeypatch):
    import io
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})
    })
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 40 700 Td (Postgraduate taught: IELTS overall 6.5, each band at least 6.0. Health and research excluded.) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    raw = output.getvalue()
    original = httpx.AsyncClient
    def reply(request):
        return (httpx.Response(200, text="User-agent: *\nAllow: /\n")
                if request.url.path == "/robots.txt"
                else httpx.Response(200, content=raw, headers={"content-type": "application/pdf"}))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(reply)))
    result = await tools.inspect_source(URL, HOME)
    assert result["verified"] and result["sourceType"] == "pdf"
    assert "IELTS overall 6.5" in result["text"]
    assert result["pagesRead"] == 1
    assert result["excerpts"][0]["page"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 403, 503])
async def test_incomplete_response_never_triggers_proxy(monkeypatch, status):
    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"<html>"
            raise httpx.ReadTimeout("Incomplete body may conceal a challenge")
    async def forbidden(*args, **kwargs):
        pytest.fail("Unknown partial response must not trigger proxy fallback")
    monkeypatch.setenv("SCRAPE_DO_TOKEN", "test-token")
    monkeypatch.setattr(retrieval, "_scrape_do_bytes", forbidden)
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(status, stream=BrokenStream())
    )) as client:
        with pytest.raises(tools.SourceReadError):
            await retrieval.read_permitted_source(client, URL, robots_allowed=True)
