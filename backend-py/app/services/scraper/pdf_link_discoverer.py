"""Phase 6: Autonomous PDF link discovery.

Scans the university main page (and optional extra pages) for PDF links
and ranks them by signal strength — URL keywords, anchor text, page
context, and surrounding headings.

Entry points
------------
discover_pdf_links(html, base_url) -> list[PdfLink]
    Parse one HTML page for PDF links and score them.

discover_pdf_links_for_university(uni_url, extra_html, emit) -> list[PdfLink]
    Fetch the university homepage (and a few known sub-pages) and return
    scored PDF links suitable for classification.

The returned links are sorted by ``total_score`` descending.  The caller
(orchestrator) picks the top ``fee_schedule`` and ``entry_requirements``
candidates and feeds their URLs into ``load_university_pdf_data``.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

# ── Keyword sets ──────────────────────────────────────────────────────────────

_FEE_URL_RE = re.compile(
    r"fee|tuition|cost|price|schedule|rate|charges|international|finance",
    re.I,
)
_REQ_URL_RE = re.compile(
    r"entry.?req|admission|eligibility|english.?req|ielts|prerequisite|academic.?req|criteria",
    re.I,
)
_HANDBOOK_URL_RE = re.compile(
    r"handbook|course.?guide|programme.?guide|subject.?outline|unit.?outline",
    re.I,
)
_PROSPECTUS_URL_RE = re.compile(
    r"prospectus|viewbook|undergraduate.?guide|postgraduate.?guide|future.?student",
    re.I,
)
_INTAKE_URL_RE = re.compile(
    r"intake|calendar|key.?date|semester.?date|trimester.?date|start.?date",
    re.I,
)
_SCHOLARSHIP_URL_RE = re.compile(
    r"scholarship|bursary|financial.?aid|grant|award",
    re.I,
)

_FEE_ANCHOR_TERMS = frozenset([
    "fee", "fees", "tuition", "tuition fee", "tuition fees",
    "international fee", "cost", "costs", "schedule", "rate",
    "charges", "pricing", "fee schedule",
])
_REQ_ANCHOR_TERMS = frozenset([
    "entry requirement", "entry requirements", "admission", "admissions",
    "admission requirement", "eligibility", "english requirement",
    "english requirements", "ielts", "language requirement",
    "academic requirement", "academic requirements", "prerequisite",
    "how to apply",
])
_HANDBOOK_ANCHOR_TERMS = frozenset([
    "handbook", "course guide", "programme guide", "subject guide",
    "unit outline", "subject outline",
])
_PROSPECTUS_ANCHOR_TERMS = frozenset([
    "prospectus", "viewbook", "course brochure",
    "undergraduate prospectus", "postgraduate prospectus",
])
_INTAKE_ANCHOR_TERMS = frozenset([
    "intake", "calendar", "key dates", "semester dates",
    "trimester dates", "start dates", "academic calendar",
])
_SCHOLARSHIP_ANCHOR_TERMS = frozenset([
    "scholarship", "scholarships", "bursary", "financial aid",
    "grant", "award", "funding",
])

# Sub-pages to probe if not found in the main page
_PROBE_PATHS: list[str] = [
    "/international/fees",
    "/international/tuition-fees",
    "/fees",
    "/fees-and-costs",
    "/study/fees",
    "/admissions/entry-requirements",
    "/study/entry-requirements",
    "/international/entry-requirements",
    "/admission",
    "/admissions",
    "/future-students",
]

_HTTP_TIMEOUT = 10.0
_MAX_PROBE_PAGES = 3
_MIN_SCORE_THRESHOLD = 0.15


# ── Data class ────────────────────────────────────────────────────────────────

@dataclass
class PdfLink:
    """A PDF link discovered on a university page with relevance scores."""

    url: str
    anchor_text: str = ""
    context_text: str = ""         # surrounding paragraph / list item text
    source_page_url: str = ""
    fee_score: float = 0.0
    req_score: float = 0.0
    handbook_score: float = 0.0
    prospectus_score: float = 0.0
    intake_score: float = 0.0
    scholarship_score: float = 0.0

    @property
    def total_score(self) -> float:
        return max(
            self.fee_score,
            self.req_score,
            self.handbook_score,
            self.prospectus_score,
            self.intake_score,
            self.scholarship_score,
        )

    @property
    def best_category(self) -> str:
        scores = {
            "fee_schedule": self.fee_score,
            "entry_requirements": self.req_score,
            "handbook": self.handbook_score,
            "prospectus": self.prospectus_score,
            "intake_calendar": self.intake_score,
            "scholarship": self.scholarship_score,
        }
        return max(scores, key=scores.__getitem__)

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "anchor_text": self.anchor_text,
            "source_page_url": self.source_page_url,
            "best_category": self.best_category,
            "total_score": round(self.total_score, 3),
            "fee_score": round(self.fee_score, 3),
            "req_score": round(self.req_score, 3),
        }


# ── Scoring helpers ───────────────────────────────────────────────────────────

def _score_text_against(text: str, term_set: frozenset[str], url_re: re.Pattern) -> float:
    """Score a text snippet by keyword containment."""
    if not text:
        return 0.0
    tl = text.lower()
    # Exact term hit in the text
    exact = any(term in tl for term in term_set)
    # Regex match (broader)
    regex = bool(url_re.search(text))
    return (0.60 if exact else 0.0) + (0.40 if regex else 0.0)


def _score_link(
    href: str,
    anchor: str,
    context: str,
) -> PdfLink:
    """Build a PdfLink with scores derived from URL, anchor text, and context."""
    link = PdfLink(url=href, anchor_text=anchor, context_text=context)
    combined = f"{href} {anchor} {context}"

    def _s(terms: frozenset, url_re: re.Pattern) -> float:
        return min(
            1.0,
            _score_text_against(href, terms, url_re) * 0.5
            + _score_text_against(anchor, terms, url_re) * 0.35
            + _score_text_against(context, terms, url_re) * 0.15,
        )

    link.fee_score = _s(_FEE_ANCHOR_TERMS, _FEE_URL_RE)
    link.req_score = _s(_REQ_ANCHOR_TERMS, _REQ_URL_RE)
    link.handbook_score = _s(_HANDBOOK_ANCHOR_TERMS, _HANDBOOK_URL_RE)
    link.prospectus_score = _s(_PROSPECTUS_ANCHOR_TERMS, _PROSPECTUS_URL_RE)
    link.intake_score = _s(_INTAKE_ANCHOR_TERMS, _INTAKE_URL_RE)
    link.scholarship_score = _s(_SCHOLARSHIP_ANCHOR_TERMS, _SCHOLARSHIP_URL_RE)
    return link


# ── HTML parsing ──────────────────────────────────────────────────────────────

_PDF_HREF_RE = re.compile(r'href=["\']([^"\']*\.pdf(?:\?[^"\']*)?)["\']', re.I)
_ANCHOR_RE = re.compile(r'<a\s[^>]*href=["\']([^"\']*\.pdf[^"\']*)["\'][^>]*>(.*?)</a>', re.I | re.S)
_TAG_STRIP_RE = re.compile(r'<[^>]+>')
_WS_NORM_RE = re.compile(r'\s+')


def _strip_tags(html_fragment: str) -> str:
    text = _TAG_STRIP_RE.sub(" ", html_fragment)
    return _WS_NORM_RE.sub(" ", text).strip()


def _context_around(html: str, pos: int, window: int = 300) -> str:
    """Extract plain text surrounding a match position in the HTML."""
    start = max(0, pos - window)
    end = min(len(html), pos + window)
    snippet = html[start:end]
    return _strip_tags(snippet)[:200]


def discover_pdf_links(html: str, base_url: str, source_page_url: str = "") -> list[PdfLink]:
    """Parse *html* for PDF links and return scored ``PdfLink`` objects.

    Parameters
    ----------
    html:
        Raw HTML of a page already fetched.
    base_url:
        Base URL for resolving relative hrefs.
    source_page_url:
        The URL of the page being parsed (stored on each PdfLink for debugging).
    """
    seen: set[str] = set()
    results: list[PdfLink] = []

    for m in _ANCHOR_RE.finditer(html):
        href_raw = m.group(1).strip()
        anchor_raw = _strip_tags(m.group(2)).strip()

        # Resolve relative URL
        try:
            href = urljoin(base_url, href_raw)
        except Exception:
            continue

        # Normalise (drop fragment)
        href = href.split("#")[0].strip()
        if not href.lower().startswith("http"):
            continue
        if href in seen:
            continue
        seen.add(href)

        context = _context_around(html, m.start())
        link = _score_link(href, anchor_raw, context)
        link.source_page_url = source_page_url or base_url

        if link.total_score >= _MIN_SCORE_THRESHOLD:
            results.append(link)

    # Fallback: bare href matches (no enclosing anchor text captured)
    for m in _PDF_HREF_RE.finditer(html):
        href_raw = m.group(1).strip()
        try:
            href = urljoin(base_url, href_raw)
        except Exception:
            continue
        href = href.split("#")[0].strip()
        if href in seen or not href.lower().startswith("http"):
            continue
        seen.add(href)
        context = _context_around(html, m.start())
        link = _score_link(href, "", context)
        link.source_page_url = source_page_url or base_url
        if link.total_score >= _MIN_SCORE_THRESHOLD:
            results.append(link)

    return sorted(results, key=lambda l: l.total_score, reverse=True)


# ── HTTP helpers ──────────────────────────────────────────────────────────────

async def _fetch_html(url: str, client: httpx.AsyncClient) -> str:
    """Fetch *url* and return the HTML body. Returns "" on any failure."""
    try:
        resp = await client.get(url, timeout=_HTTP_TIMEOUT, follow_redirects=True)
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "")
            if "html" in ct or not ct:
                return resp.text
    except Exception as exc:  # noqa: BLE001
        log.debug("[PDF_DISC] fetch failed %s: %s", url[:80], exc)
    return ""


# ── Public entry point ────────────────────────────────────────────────────────

async def discover_pdf_links_for_university(
    uni_url: str,
    *,
    extra_html: str | None = None,
    emit: Callable[..., Any] | None = None,
) -> list[PdfLink]:
    """Discover and score PDF links from a university's public pages.

    Strategy:
    1. Parse ``extra_html`` if provided (already-fetched main page HTML).
    2. Fetch the main ``uni_url`` page.
    3. Probe up to ``_MAX_PROBE_PAGES`` high-value sub-paths
       (``/fees``, ``/admissions/entry-requirements``, etc.) that commonly
       host fee schedules and admissions documents.

    Returns
    -------
    list[PdfLink]
        All discovered PDF links with total_score ≥ _MIN_SCORE_THRESHOLD,
        sorted by score descending.  Duplicates (same URL) are deduplicated.
    """
    if not uni_url:
        return []

    parsed = urlparse(uni_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    all_links: dict[str, PdfLink] = {}

    async with httpx.AsyncClient(
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; UniversityScraper/1.0)",
            "Accept": "text/html,application/xhtml+xml,*/*",
        },
        follow_redirects=True,
        timeout=_HTTP_TIMEOUT,
        verify=False,
    ) as client:

        # Stage 1: parse provided extra_html
        if extra_html:
            for lnk in discover_pdf_links(extra_html, uni_url, source_page_url=uni_url):
                all_links.setdefault(lnk.url, lnk)

        # Stage 2: fetch the main page
        main_html = await _fetch_html(uni_url, client)
        if main_html:
            for lnk in discover_pdf_links(main_html, uni_url, source_page_url=uni_url):
                all_links.setdefault(lnk.url, lnk)

        # Stage 3: probe high-value sub-paths
        probed = 0
        for path in _PROBE_PATHS:
            if probed >= _MAX_PROBE_PAGES:
                break
            probe_url = origin + path
            page_html = await _fetch_html(probe_url, client)
            if not page_html:
                continue
            probed += 1
            for lnk in discover_pdf_links(page_html, probe_url, source_page_url=probe_url):
                if lnk.url not in all_links:
                    all_links[lnk.url] = lnk
                else:
                    # Merge scores — take max per dimension
                    existing = all_links[lnk.url]
                    existing.fee_score = max(existing.fee_score, lnk.fee_score)
                    existing.req_score = max(existing.req_score, lnk.req_score)
                    existing.handbook_score = max(existing.handbook_score, lnk.handbook_score)
                    existing.intake_score = max(existing.intake_score, lnk.intake_score)
                    existing.scholarship_score = max(existing.scholarship_score, lnk.scholarship_score)

    results = sorted(all_links.values(), key=lambda l: l.total_score, reverse=True)

    if emit and results:
        top = results[:3]
        top_summary = "; ".join(
            f"{l.best_category}({l.total_score:.0%}) {l.url.split('/')[-1][:40]}"
            for l in top
        )
        await emit(
            "log",
            f"[PDF_DISC] found {len(results)} PDF candidates: {top_summary}",
        )
    else:
        log.info("[PDF_DISC] %s → %d PDF candidates", uni_url[:60], len(results))

    return results
