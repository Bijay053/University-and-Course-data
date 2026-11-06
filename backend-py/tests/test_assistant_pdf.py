import asyncio
import socket

import httpx
import pytest

from app.services import scrape_assistant_tools as tools
from tests.helpers.assistant_pdf_fixture import make_pdf

HOME = "https://example.edu"
FEE = [
    "International tuition fees 2026",
    "Course                 Annual tuition      Full Course tuition",
    "Bachelor of Arts       GBP 18,900           GBP 56,700",
    "Domestic tuition       GBP 9,790 per year",
]
ENGLISH = [
    "International postgraduate English language entry requirements",
    "Master of Arts IELTS overall 6.5, no band below 6.0.",
    "Pre-sessional English programme: IELTS overall 5.0.",
]


@pytest.mark.asyncio
async def test_real_text_pdf_page_specific_immutable_quotes_and_no_numeric_facts(monkeypatch):
    raw = make_pdf(FEE, ENGLISH)
    async def fetch(url):
        return raw
    async def forbidden(*args, **kwargs):
        pytest.fail("PDF tables must not run through HTML numeric extractors")
    from app.services.scraper.extractors import fee, english_test
    monkeypatch.setattr(fee, "extract", forbidden)
    monkeypatch.setattr(english_test, "extract", forbidden)
    monkeypatch.setattr(tools, "public_request", fetch)
    result = await tools.inspect_source(HOME + "/guidance", HOME)  # magic, not suffix
    assert result["verified"] and result["sourceType"] == "pdf"
    assert result["pageCount"] == result["pagesRead"] == 2
    assert result["facts"] == [] and not result["nonTuition"]
    first = [quote for quote in result["excerpts"] if quote["page"] == 1]
    second = [quote for quote in result["excerpts"] if quote["page"] == 2]
    assert "Annual tuition" in first[0]["text"] and "Full Course tuition" in first[0]["text"]
    assert "Domestic tuition" in first[0]["text"]
    assert "\n" in first[0]["text"] and "    " in first[0]["text"]
    assert "6.5" in second[0]["text"] and "Pre-sessional" in second[0]["text"]
    assert all(tools.quote_in_source(quote["text"], result) for quote in result["excerpts"])
    assert all(f"-p{quote['page']}-" in quote["id"] for quote in result["excerpts"])
    again = await tools.inspect_source(HOME + "/guidance", HOME)
    assert again["excerpts"] == result["excerpts"]
    raw = make_pdf(FEE, [line.replace("6.5", "7.0") for line in ENGLISH])
    changed = await tools.inspect_source(HOME + "/guidance", HOME)
    assert not ({q["id"] for q in changed["excerpts"]} & {q["id"] for q in result["excerpts"]})


@pytest.mark.asyncio
@pytest.mark.parametrize("raw, message", [
    (make_pdf(None), "no readable text"),
    (make_pdf(FEE, password="secret"), "password protected"),
    (b"%PDF-1.7\nbroken", "malformed"),
    (make_pdf(*([FEE] * 13)), "12-page"),
    (b"%PDF" + b"x" * tools.MAX_BYTES, "size limit"),
    (b"<html>Challenge page</html>", "did not return a PDF"),
], ids=["image-only", "encrypted", "malformed", "page-budget", "byte-budget", "not-pdf"])
async def test_pdf_failures_are_explicit_not_verified(monkeypatch, raw, message):
    async def fetch(url):
        return raw
    monkeypatch.setattr(tools, "public_request", fetch)
    result = await tools.inspect_source(HOME + "/fees.pdf", HOME)
    assert not result["verified"] and result["sourceType"] == "pdf"
    assert message in result["error"]
    assert not result.get("facts") and not result.get("excerpts")


@pytest.mark.asyncio
async def test_mixed_readable_and_unreadable_pages_are_not_silently_verified(monkeypatch):
    async def fetch(url):
        return make_pdf(FEE, None, ENGLISH)
    monkeypatch.setattr(tools, "public_request", fetch)
    result = await tools.inspect_source(HOME + "/fees.pdf", HOME)
    assert result["verified"] and result["unreadablePages"] == [2]
    assert result["pagesRead"] == 2 and result["pageCount"] == 3
    assert {quote["page"] for quote in result["excerpts"]} == {1, 3}
    assert "Unreadable pages [2]" in result["note"]


@pytest.mark.asyncio
@pytest.mark.parametrize("title", [
    "Incidental Fees Schedule", "Ancillary Fees", "Non-tuition fees",
    "Materials and Equipment Fees", "Application Fees",
])
async def test_non_tuition_tables_cannot_become_international_fee_facts(monkeypatch, title):
    async def fetch(url):
        return make_pdf([title, "International Bachelor of Arts GBP 10,000 per year"])
    monkeypatch.setattr(tools, "public_request", fetch)
    result = await tools.inspect_source(HOME + "/schedule.pdf", HOME)
    assert result["verified"] and result["nonTuition"]
    assert result["facts"] == [] and "cannot establish international tuition" in result["note"]


@pytest.mark.asyncio
async def test_parser_text_budget_is_enforced(monkeypatch):
    async def fetch(url):
        return make_pdf(["International tuition " + "amounts " * 2000])
    monkeypatch.setattr(tools, "public_request", fetch)
    result = await tools.inspect_source(HOME + "/fees.pdf", HOME)
    assert not result["verified"] and "text research limit" in result["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_parser_timeout_and_cancellation_kill_and_reap_child(monkeypatch, cancel):
    class Process:
        returncode = None
        killed = False
        waited = False
        started = asyncio.Event()
        async def communicate(self, raw):
            self.started.set()
            await asyncio.Event().wait()
        def kill(self):
            self.killed = True
            self.returncode = -9
        async def wait(self):
            self.waited = True
    process = Process()
    async def spawn(*args, **kwargs):
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(tools, "PDF_PARSE_SECONDS", 0.02)
    task = asyncio.create_task(tools.pdf_pages(b"%PDF"))
    await process.started.wait()
    if cancel:
        task.cancel()
    with pytest.raises(asyncio.CancelledError if cancel else tools.SourceReadError):
        await task
    assert process.killed and process.waited


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [
    "https://evil.test/fees.pdf", "http://127.0.0.1/fees.pdf",
    "https://example.edu/fees-final.pdf",
])
async def test_redirect_is_never_followed_even_to_an_official_pdf(monkeypatch, target):
    calls = []
    real_transport = tools.httpx.AsyncHTTPTransport
    def transport(**kwargs):
        instance = real_transport(**kwargs)
        async def handle(request):
            calls.append(str(request.url))
            if request.url.path == "/robots.txt":
                return httpx.Response(200, text="User-agent: *\nAllow: /\n", request=request)
            return httpx.Response(302, headers={"location": target}, request=request)
        instance.handle_async_request = handle
        return instance
    monkeypatch.setattr(tools.httpx, "AsyncHTTPTransport", transport)
    result = await tools.inspect_source(HOME + "/fees.pdf", HOME)
    assert not result["verified"] and calls == [HOME + "/robots.txt", HOME + "/fees.pdf"]


@pytest.mark.asyncio
async def test_pdf_transport_blocks_private_dns_before_tcp(monkeypatch):
    async def dns(*args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
    result = await tools.inspect_source(HOME + "/fees.pdf", HOME)
    assert not result["verified"]


@pytest.mark.asyncio
async def test_only_official_fee_pdf_links_are_exposed(monkeypatch):
    async def fetch(url):
        return (
            "<html><title>Fees</title><body>" + "Official fee guidance. " * 10 +
            '<a href="/fees.pdf?year=2026">International tuition</a>'
            '<a href="https://evil.test/fees.pdf">Tuition PDF</a>'
            '<a href="/download?id=2">Tuition PDF</a>'
            '<a href="http://127.0.0.1/fees.pdf">Fees</a></body></html>'
        ).encode()
    async def no_facts(*args):
        return []
    from app.services.scraper.extractors import fee, english_test
    monkeypatch.setattr(fee, "extract", no_facts)
    monkeypatch.setattr(english_test, "extract", no_facts)
    monkeypatch.setattr(tools, "public_request", fetch)
    result = await tools.inspect_source(HOME, HOME)
    assert result["feeLinks"] == [HOME + "/fees.pdf?year=2026", HOME + "/download?id=2"]
