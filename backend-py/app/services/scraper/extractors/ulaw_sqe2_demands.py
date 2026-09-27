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
REVIEW_KEY = "duration_review_status"
BLOG_URL = "https://www.law.ac.uk/resources/blog/sqe-prep-courses/"
_WEEKLY_UNITS = re.compile(
    r"There are six units per week throughout the course "
    r"\(two units per week in the part-time courses\)", re.I,
)
_CONTENT_ONLY = re.compile(
    r"(?:full-time )?SQE2 Preparation Course has 5 weeks of content "
    r"but the actual completion time is slightly longer", re.I,
)
_TOTAL_DURATION = re.compile(
    r"\b(?:(?:course|total)\s+)?duration\s*:\s*(\d+(?:\.\d+)?)\s*"
    r"(weeks?|months?|years?)\b", re.I,
)
_OTHER_COURSE_CONTAINER = re.compile(r"related|similar|recommended|card|carousel", re.I)


def _course_total_duration(soup: BeautifulSoup, url: str):
    """Accept a duration label only in this course's detail content, not cards."""
    root = soup.select_one("main") or soup
    for tag in root.select("p, li, dt, dd, div"):
        if tag.name == "div" and tag.select_one("p, li, dt, dd"):
            continue
        parent = tag
        excluded = False
        while parent is not None and parent != root:
            identity = " ".join([str(parent.get("id") or ""), *parent.get("class", [])])
            if _OTHER_COURSE_CONTAINER.search(identity):
                excluded = True
                break
            if parent.name == "a" and parent.get("href"):
                linked = urlparse(urljoin(url, parent["href"]))
                if linked.path.rstrip("/") != urlparse(url).path.rstrip("/"):
                    excluded = True
                    break
            parent = parent.parent
        if excluded:
            continue
        match = _TOTAL_DURATION.search(" ".join(tag.get_text(" ", strip=True).split()))
        if match and float(match[1]) > 0:
            return match
    return None


def review_sqe2_duration(
    url: str, course_html: str, blog_html: str = "",
    demands_evidence: list[dict] | None = None,
) -> tuple[dict | None, dict | None]:
    """Review only a fetched, titled course-owned page, never an absent source.

    The content-only statement is not a course duration. An explicit total
    duration on that same course page takes precedence if published later.
    """
    if not is_sqe2_course(url) or not course_html:
        return None, None
    soup = BeautifulSoup(course_html, "html.parser")
    if not re.search(r"\bSQE2 Preparation Course\b", soup.get_text(" ", strip=True)[:1500], re.I):
        return None, None
    for tag in soup(["script", "style", "nav", "footer"]):
        tag.decompose()
    text = " ".join(soup.get_text(" ", strip=True).split())
    total = _course_total_duration(soup, url)
    if total and float(total[1]) > 0:
        unit = total[2].lower().rstrip("s")
        return None, {
            "duration": float(total[1]), "duration_term": unit,
            "evidence": {"field_key": "duration", "value": float(total[1]),
                         "source_url": url, "snippet": total[0],
                         "method": "ulaw_sqe2:course_total_duration", "confidence": 0.90},
        }
    weekly = _WEEKLY_UNITS.search(text)
    blog = BeautifulSoup(blog_html, "html.parser")
    blog_text = " ".join(blog.get_text(" ", strip=True).split())
    match = _CONTENT_ONLY.search(blog_text)
    demand = next((item for item in (demands_evidence or [])
                   if item.get("field_key") == "ielts_overall"
                   and item.get("method") == METHOD
                   and item.get("source_url") and item.get("snippet")), None)
    if weekly and match and demand:
        return {
            "status": "confirmed_unpublished",
            "reason": "The course page gives weekly units, and the official SQE2 article says five weeks is content only; no total course duration is established.",
            "sources": [
                {"url": url, "snippet": weekly[0]},
                {"url": demand["source_url"], "snippet": demand["snippet"]},
                {"url": BLOG_URL, "snippet": match[0]},
            ],
        }, None
    return None, None


async def fetch_sqe2_blog() -> str:
    """Read only the exact official article; an unavailable article is not proof."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(BLOG_URL)
            response.raise_for_status()
            if len(response.content) > 2_000_000:
                raise ValueError("SQE2 article exceeds size limit")
            return response.text
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("ULaw SQE2 duration article unavailable: %s", exc)
        return ""


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