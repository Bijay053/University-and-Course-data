"""Bounded research tools. Model text never becomes SQL, code or a data update."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import sys
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup
from httpcore._backends.auto import AutoBackend

MAX_BYTES = 2_000_000
PDF_PARSE_SECONDS = 4


class SourceReadError(ValueError):
    """A safe, user-facing failure (never expose raw transport/parser errors)."""


async def pdf_pages(raw: bytes) -> dict:
    """Kill and reap the parser on timeout or cancellation, not just its await."""
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(Path(__file__).with_name("scrape_assistant_pdf.py")),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(raw), PDF_PARSE_SECONDS)
        if process.returncode:
            raise SourceReadError("PDF parsing failed within the memory or CPU budget.")
        result = json.loads(output)
        if result.get("error"):
            raise SourceReadError(result["error"])
        return result
    except TimeoutError:
        raise SourceReadError("PDF parsing reached its time budget.") from None
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()


async def inspect_pdf(raw: bytes, url: str) -> dict:
    from app.services.scraper.pdf_classifier import is_non_tuition_fee_pdf

    document = await pdf_pages(raw)
    digest = hashlib.sha256(raw).hexdigest()[:20]
    excerpts = []
    for page in document["pages"]:
        # IDs bind the immutable document bytes, page and exact excerpt.
        for excerpt in evidence_excerpts(page["text"], [], preserve_layout=True)[:4]:
            excerpts.append({
                **excerpt, "id": f"pdf-{digest}-p{page['page']}-{excerpt['id']}",
                "page": page["page"],
            })
    text = "\n\n".join(page["text"] for page in document["pages"])
    non_tuition = is_non_tuition_fee_pdf(url, document["pages"][0]["text"])
    return {
        "url": url, "title": urlsplit(url).path.rsplit("/", 1)[-1] or "Official PDF guidance",
        "sourceType": "pdf", "verified": True, "text": text[:24000], "_fullText": text,
        "facts": [], "excerpts": excerpts, "englishLinks": [],
        "pageCount": document["pageCount"], "pagesRead": len(document["pages"]),
        "unreadablePages": document["unreadablePages"], "nonTuition": non_tuition,
        "note": (
            "Text-only PDF research; no fee or IELTS values assigned. "
            "Keep page/table headers, course, international/domestic audience, year and Annual/Full Course "
            "period together; missing or ambiguous scope is unverified. Never infer annual tuition from a total. "
            + ("This is non-tuition guidance: its amounts cannot establish international tuition. " if non_tuition else "")
            + (f"Unreadable pages {document['unreadablePages']} remain unverified." if document["unreadablePages"] else "")
        ),
    }


def evidence_excerpts(text: str, facts: list[dict], *, preserve_layout=False) -> list[dict]:
    """Give the model immutable quote IDs instead of asking it to copy prose."""
    if not preserve_layout:
        text = " ".join(text.split())
    starts = {0}
    for fact in facts:
        value = fact.get("value")
        if isinstance(value, (float, int)):
            variants = {str(value), f"{value:g}", f"{value:,.0f}"}
            pattern = r"(?<![\w.])(?:" + "|".join(re.escape(v) for v in variants) + r")(?![\w.])"
            starts.update(max(0, match.start() - 230) for match in list(re.finditer(pattern, text))[:3])
    for cue in ("IELTS", "international", "tuition", "fees", "award", "duration", "location"):
        starts.update(max(0, match.start() - 160) for match in list(re.finditer(re.escape(cue), text, re.I))[:2])
    if preserve_layout:
        starts = {text.rfind("\n", 0, start) + 1 for start in starts}
    selected = sorted(starts)[:24]
    return [{"id": f"q{index}", "text": text[start:start + 800]} for index, start in enumerate(selected)]


class PublicNetworkBackend(AutoBackend):
    """Pin each TCP connection to a checked public IP, retaining HTTP/TLS host."""

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        host = host.decode("ascii") if isinstance(host, bytes) else host
        answers = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM), 4,
        )
        addresses = [ipaddress.ip_address(item[4][0]) for item in answers]
        if not addresses or any(not address.is_global for address in addresses):
            raise ValueError("Source does not resolve exclusively to public addresses.")
        chosen = next((address for address in addresses if address.version == 4), addresses[0])
        return await super().connect_tcp(
            str(chosen), port, timeout=timeout,
            local_address=local_address, socket_options=socket_options,
        )


def official_url(url: str, official_home: str) -> str:
    value = urlsplit(url)
    host = (value.hostname or "").lower().removeprefix("www.")
    owner = (urlsplit(official_home).hostname or "").lower().removeprefix("www.")
    if (
        not owner or host != owner or value.scheme != "https"
        or value.username or value.password or value.port not in (None, 443)
        or len(url) > 1800
    ):
        raise ValueError("Use an HTTPS page on this university's official website.")
    return value._replace(fragment="").geturl()


async def public_request(url: str, *, method="GET", body=None, params=None, user_agent=None) -> bytes:
    if method not in {"GET", "POST"}:
        raise ValueError("Unsupported research method.")
    value = urlsplit(url)
    if value.scheme != "https" or value.username or value.password or value.port not in (None, 443):
        raise ValueError("Unsafe source URL.")
    transport = httpx.AsyncHTTPTransport(retries=0)
    transport._pool._network_backend = PublicNetworkBackend()
    async with httpx.AsyncClient(
        transport=transport, follow_redirects=False, trust_env=False, timeout=httpx.Timeout(9, connect=4),
        headers={"User-Agent": user_agent or "StudyInfoCentre-SourceVerifier/1.0"},
    ) as client:
        async with client.stream(method, url, json=body if method == "POST" else None, params=params or None) as response:
            if response.is_redirect:
                target = official_url(urljoin(url, response.headers.get("location", "")), url)
                # Revalidate every hop, limit redirects and never forward credentials.
                if target == url:
                    raise ValueError("Source redirect could not be verified.")
                raise SourceReadError("Source redirects; provide its final official URL.")
            response.raise_for_status()
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                chunks.extend(chunk)
                if len(chunks) > MAX_BYTES:
                    raise SourceReadError("Source exceeds the safe research size limit.")
            return bytes(chunks)


async def inspect_source(url: str, home: str, *, raw_body: bytes | None = None) -> dict:
    source_type = "pdf" if urlsplit(url).path.lower().endswith(".pdf") else "html"
    try:
        url = official_url(url, home)
        raw = raw_body if raw_body is not None else await asyncio.wait_for(public_request(url), timeout=12)
        if len(raw) > MAX_BYTES:
            raise SourceReadError("Source exceeds the safe research size limit.")
        if raw.lstrip().startswith(b"%PDF"):
            source_type = "pdf"
            return await inspect_pdf(raw, url)
        if source_type == "pdf":
            raise SourceReadError("Official PDF URL did not return a PDF document.")
        if b"\x00" in raw[:500]:
            raise ValueError("This research tool needs a readable HTML page, not a binary document.")
        html = raw.decode("utf-8", errors="replace")
        from app.services.scraper.extractors.fee import extract as extract_fee
        from app.services.scraper.extractors.english_test import extract as extract_english
        soup = BeautifulSoup(html, "html.parser")
        # Keep source extraction on the original HTML, but remove website
        # chrome and forms from the immutable text cited in chat.
        for node in soup.select("script,style,noscript"):
            node.decompose()
        evidence_soup = BeautifulSoup(str(soup), "html.parser")
        for node in evidence_soup.select("nav,footer,header,aside,form,select,[class*='related'],[role='navigation'],.elementor-location-header,.elementor-location-footer,.mobile-menu-bar-div,.elementor-nav-menu"):
            node.decompose()
        evidence_root = evidence_soup.find("main") or evidence_soup.find("article") or evidence_soup
        text = evidence_root.get_text(" ", strip=True)
        if len(text) < 150 or re.search(r"^(?:Just a moment|Access denied|Attention Required)", text, re.I):
            raise ValueError("The source returned a challenge or an empty page.")
        facts = []
        for extractor in (extract_fee, extract_english):
            for result in (await extractor(html, url))[:8]:
                facts.append({
                    "field": result.field_key, "value": result.value,
                    "normalized": result.normalized, "quote": result.snippet,
                    "method": result.method,
                })
        title = soup.title.get_text(" ", strip=True) if soup.title else urlsplit(url).path
        english_links = []
        fee_links = []
        for anchor in soup.select("a[href]"):
            label = anchor.get_text(" ", strip=True) + " " + anchor["href"]
            english = bool(re.search(r"english|language.requirements", label, re.I))
            fee_pdf = bool(
                re.search(r"fee|tuition", label, re.I)
                and (urlsplit(anchor["href"]).path.lower().endswith(".pdf")
                     or re.search(r"\bPDF\b", anchor.get_text(" ", strip=True), re.I))
            )
            if not english and not fee_pdf:
                continue
            try:
                link = official_url(urljoin(url, anchor["href"]), home)
            except (ValueError, TypeError):
                continue
            if english and link not in english_links:
                english_links.append(link)
            if fee_pdf and link not in fee_links:
                fee_links.append(link)
        from app.services.assistant_eligibility import domestic_evidence
        headings = evidence_soup.find_all("h1")
        eligibility_name = headings[0].get_text(" ", strip=True) if len(headings) == 1 else ""
        from app.services.scraper.excelsia_source import facts as excelsia_facts
        intake_facts = excelsia_facts(html, url)
        return {
            "url": url, "title": title[:200], "text": text[:24000], "_fullText": text,
            "_eligibilityName": eligibility_name,
            "_excelsiaIntake": {"name": intake_facts.get("course_name"), "months": intake_facts.get("intake_months"),
                               "quote": intake_facts.get("_intake_snippet")},
            "_eligibilityQuote": domestic_evidence(html, eligibility_name) if eligibility_name else None,
            "facts": facts, "excerpts": evidence_excerpts(text, facts), "verified": True,
            "englishLinks": english_links[:8], "feeLinks": fee_links[:8],
        }
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return {
            "url": url, "verified": False, "sourceType": source_type,
            "error": str(exc) if isinstance(exc, SourceReadError) else
            f"Official {source_type.upper()} source could not be verified within the research limit.",
        }


def quote_in_source(quote: str, source: dict) -> bool:
    normal = lambda text: " ".join(str(text).split()).casefold()
    # Extractor snippets can be synthesized summaries, not verbatim quotations.
    haystacks = [source.get("_fullText") or source.get("text", "")]
    return len(quote.strip()) >= 12 and any(normal(quote) in normal(text) for text in haystacks)


def read_path(value, path: str):
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


async def inspect_catalogue(config) -> dict:
    api = config.discovery.generic_search_api
    if not api or not api.enabled:
        return {"available": False, "note": "No configured catalogue API; use the guarded discovery repair for a complete scan."}
    try:
        raw = await public_request(api.url, method=api.method.upper(), body=api.body, params=api.params)
        document = json.loads(raw)
        items = read_path(document, api.root_path) if api.root_path else document
        if not isinstance(items, list):
            raise ValueError("Catalogue response did not contain a course list.")
        totals = [
            read_path(document, key) for key in (
                "TotalResults", "totalResults", "total",
                "response.resultPacket.resultsSummary.totalMatching",
            )
        ]
        total = next((n for n in totals if isinstance(n, int) and not isinstance(n, bool)), None)
        return {
            "available": True, "observed": len(items), "publishedTotal": total,
            "complete": total is not None and total == len(items),
            "note": "A bounded first-page check; not a complete scan unless its published total matches.",
        }
    except Exception:
        return {"available": False, "note": "Catalogue API could not be verified. No completeness claim is possible."}
