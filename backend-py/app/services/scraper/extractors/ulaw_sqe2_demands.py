"""Course-owned English requirement in ULaw's linked SQE2 Course Demands PDF."""
from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.scraper.pdf_fetcher import download_pdf_text

log = logging.getLogger(__name__)
COURSE_PATH = "/study/postgraduate/law/sqe-2-preparation-course/"
HUB_PATH = "/study/course-demands/"
PDF_NAME = "pdf_students_programme-demands-sqe2-preparation-course-ft-pt.pdf"
METHOD = "ulaw_sqe2:course_demands_pdf"
ENGLISH_FIELDS = ("ielts_overall", "ielts_listening", "ielts_reading",
                  "ielts_writing", "ielts_speaking")


def is_sqe2_course(url: str) -> bool:
    parsed = urlparse(url)
    return (parsed.scheme == "https" and parsed.hostname == "www.law.ac.uk"
            and parsed.path.rstrip("/") + "/" == COURSE_PATH)


_REQUIREMENT = re.compile(
    r"(A sufficient command of English to follow the course to a successful conclusion"
    r"\s*\(an English\s+Language level equivalent to IELTS\s*"
    r"(?P<overall>\d(?:\.\d)?)\s+with a minimum of\s*"
    r"(?P<component>\d(?:\.\d)?)\s+in each component\))",
    re.I,
)


def _owned_link(html: str, base: str, path: str) -> str | None:
    for a in BeautifulSoup(html, "html.parser").select("a[href]"):
        url = urljoin(base, a["href"])
        parsed = urlparse(url)
        if (parsed.scheme == "https" and parsed.hostname == "www.law.ac.uk"
                and parsed.path.lower() == path and not parsed.query
                and not parsed.fragment):
            return url
    return None


def parse_demands(text: str, pdf_url: str) -> list[dict]:
    """Reject a wrong/changed document; do not use a campus-wide IELTS policy."""
    if not re.search(r"\bSQE2 Preparation Course\b", text[:1500], re.I):
        return []
    match = _REQUIREMENT.search(" ".join(text.split()))
    if not match:
        return []
    overall, component = float(match["overall"]), float(match["component"])
    if not (0 < component <= overall <= 9):
        return []
    snippet = match[1]
    values = {"ielts_overall": overall, **{
        f"ielts_{part}": component
        for part in ("listening", "reading", "writing", "speaking")
    }}
    return [
        {"field_key": field, "value": value, "source_url": pdf_url,
         "snippet": f"SQE2 Preparation Course — {snippet}",
         "method": METHOD, "confidence": 0.90}
        for field, value in values.items()
    ]


async def recover_sqe2_english(url: str, course_html: str) -> list[dict]:
    """Follow only the course's own demands link, then its named PDF on ULaw."""
    if not is_sqe2_course(url) or not course_html:
        return []
    hub = _owned_link(course_html, url, HUB_PATH)
    if not hub:
        log.warning("ULaw SQE2 demands recovery: course page has no demands link: %s", url)
        return []
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(hub)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("course demands index exceeds size limit")
        pdf_url = _owned_link(
            response.text, hub,
            "/globalassets/13.-media--doc-repo/04.-students/programme-demands/" + PDF_NAME,
        )
        if not pdf_url:
            raise ValueError("SQE2 Preparation Course demands PDF not linked from official index")
        text = await asyncio.wait_for(download_pdf_text(pdf_url), timeout=20)
        evidence = parse_demands(text, pdf_url)
        if not evidence:
            raise ValueError("SQE2 demands PDF lacks course title or exact English requirement")
        return evidence
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as exc:
        log.warning("ULaw SQE2 demands recovery failed for %s: %s", url, exc)
        return []