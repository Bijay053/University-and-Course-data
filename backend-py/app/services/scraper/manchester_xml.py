"""University of Manchester XML course catalogue discovery provider.

Manchester publishes three machine-readable XML course lists that enumerate
every taught programme and research degree:

  UG  — https://www.manchester.ac.uk/study/undergraduate/courses/{year}/xml/
  PGT — https://www.manchester.ac.uk/study/masters/courses/list/xml/
  PGR — https://www.manchester.ac.uk/study/postgraduate-research/programmes/list/xml/

Each XML list contains ``<li id="id{N}">`` entries with:
  - ``<a href="{id}/{slug}/">`` — relative path; full URL = base_url + href
  - ``<div class="degree">``   — degree type (BSc, MSc, PhD, MBA, …)
  - ``<div class="duration">`` — duration string ("3 years", "1 year", …)
  - ``<div class="ucas">``     — UCAS code (UG only)

This provider is **discovery-only** (links_only): it parses the XML feeds and
returns ``{name, url}`` link dicts for normal per-course HTML extraction.
Per-course pages are Cloudflare-protected and must be fetched via scrape.do
(set ``extraction.scrape_do_per_course: true`` in the YAML).

Total catalogue size: ~366 UG + ~321 PGT + ~247 PGR = ~934 courses.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Callable, Coroutine, Optional

import httpx

log = logging.getLogger("scraper.manchester_xml")

# ── XML feed base URLs ────────────────────────────────────────────────────────
_FEEDS: list[tuple[str, str, str]] = [
    # (level_label, academic_level, xml_base_url)
    (
        "UG",
        "Undergraduate",
        "https://www.manchester.ac.uk/study/undergraduate/courses/{year}/xml/",
    ),
    (
        "PGT",
        "Postgraduate",
        "https://www.manchester.ac.uk/study/masters/courses/list/xml/",
    ),
    (
        "PGR",
        "Postgraduate",
        "https://www.manchester.ac.uk/study/postgraduate-research/programmes/list/xml/",
    ),
]

# ── XML parsing patterns ──────────────────────────────────────────────────────
_LI_RE = re.compile(r'<li\s+id="id(\d+)"(.*?)</li>', re.DOTALL)
_HREF_RE = re.compile(r'<a\s+href="([^"]+)"', re.IGNORECASE)
_TITLE_RE = re.compile(r'<a\b[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)
_SCREENREADER_RE = re.compile(r'<span[^>]*class="screenreader"[^>]*>.*?</span>', re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')
_DIV_RE = re.compile(r'<div\s+class="([^"]+)"[^>]*>(.*?)</div>', re.DOTALL | re.IGNORECASE)

# ── Duration parsing ─────────────────────────────────────────────────────────
_DUR_RE = re.compile(
    r"""
    (?P<lo>[\d.]+)          # leading number (or range start)
    (?:\s+or\s+(?P<hi>[\d.]+))?  # optional "or N" (range)
    \s+
    (?P<unit>year|month|week)s?
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _parse_duration(dur_str: str) -> tuple[float | None, str | None]:
    """Return (value, term) from a duration string like '3 years' or '3 or 4 years'."""
    if not dur_str:
        return None, None
    m = _DUR_RE.search(dur_str)
    if not m:
        return None, None
    # For ranges ("3 or 4 years"), take the minimum
    val = float(m.group("lo"))
    unit = m.group("unit").capitalize()
    term = "Years" if unit == "Year" else (unit + "s")
    return val, term


def _academic_level(level_label: str, degree: str) -> str | None:
    """Derive academic_level from the feed label and degree string."""
    lc = degree.lower()
    if any(x in lc for x in ("phd", "mphil", "dphil", "edd", "dba", "md ", "dmd")):
        return "Doctorate"
    if level_label == "UG":
        return "Undergraduate"
    return "Postgraduate"


def _parse_entry(li_body: str, base_url: str, level_label: str, academic_level: str) -> dict | None:
    """Parse a single <li …>…</li> fragment and return a link dict."""
    href_m = _HREF_RE.search(li_body)
    if not href_m:
        return None
    href = href_m.group(1).strip()
    # The XML feed URL ends in …/xml/ but per-course pages drop that segment.
    # e.g. feed base: …/courses/2026/xml/  →  course base: …/courses/2026/
    course_base = re.sub(r"/xml/?$", "/", base_url.rstrip("/") + "/")
    full_url = course_base.rstrip("/") + "/" + href.lstrip("/")

    # Extract title: content of <a>, strip <span class="screenreader"> and tags
    title_m = _TITLE_RE.search(li_body)
    if not title_m:
        return None
    raw_title = title_m.group(1)
    raw_title = _SCREENREADER_RE.sub("", raw_title)
    title = _TAG_RE.sub("", raw_title).strip()
    if not title:
        return None

    # Extract <div class="..."> fields
    div_vals: dict[str, str] = {}
    for dm in _DIV_RE.finditer(li_body):
        div_vals[dm.group(1).strip()] = _TAG_RE.sub("", dm.group(2)).strip()

    degree   = div_vals.get("degree", "")
    duration_str = div_vals.get("duration", "")
    ucas_code    = div_vals.get("ucas", "")

    dur_val, dur_term = _parse_duration(duration_str)
    acad_level = _academic_level(level_label, degree)

    return {
        "name":           title,
        "url":            full_url,
        # Extra metadata — carried in the link dict for observability;
        # the per-course extractor may override with page-level values.
        "_xml_degree":    degree,
        "_xml_duration":  duration_str,
        "_xml_dur_val":   dur_val,
        "_xml_dur_term":  dur_term,
        "_xml_ucas":      ucas_code,
        "_xml_level":     level_label,
        "_xml_acad_level": acad_level,
    }


async def _fetch_feed(
    client: httpx.AsyncClient,
    label: str,
    base_url: str,
) -> list[dict]:
    """Fetch one XML feed and return parsed link dicts."""
    log.info("[MANCHESTER-XML] fetching %s feed: %s", label, base_url)
    try:
        resp = await client.get(base_url, timeout=30)
        resp.raise_for_status()
    except Exception as exc:
        log.error("[MANCHESTER-XML] failed to fetch %s feed: %s", label, exc)
        return []

    xml = resp.text
    log.info(
        "[MANCHESTER-XML] %s feed: %d chars, parsing …",
        label, len(xml),
    )

    academic_level = (
        "Undergraduate" if label == "UG" else "Postgraduate"
    )
    links: list[dict] = []
    for li_m in _LI_RE.finditer(xml):
        li_body = li_m.group(2)
        entry = _parse_entry(li_body, base_url, label, academic_level)
        if entry is not None:
            links.append(entry)

    log.info("[MANCHESTER-XML] %s feed: parsed %d course link(s)", label, len(links))
    return links


# ── Public entry point ────────────────────────────────────────────────────────
EmitFn = Callable[..., Coroutine]


async def fetch_manchester_xml_links(
    cfg: Any,
    emit: Optional[EmitFn] = None,
) -> list[dict]:
    """Fetch all Manchester XML course feeds and return discovery link dicts.

    Args:
        cfg:  A ``ManchesterXmlConfig`` instance from the uni YAML.
        emit: Optional coroutine for streaming status messages.

    Returns:
        List of ``{name, url, …}`` dicts consumed by the scrape orchestrator.
        No ``searchstax_result`` / ``swiftype_result`` key — the orchestrator
        performs a normal per-course HTML extraction for each link.
    """

    async def _emit(msg: str) -> None:
        if emit:
            try:
                await emit("status", msg, phase="discover")
            except Exception:  # noqa: BLE001
                pass

    ug_year = str(getattr(cfg, "ug_year", "2026"))

    feeds_to_fetch = []
    for label, acad_level, base_tmpl in _FEEDS:
        base_url = base_tmpl.format(year=ug_year)
        enabled_key = f"include_{label.lower()}"
        # Default: include all three feeds unless explicitly disabled
        if getattr(cfg, enabled_key, True):
            feeds_to_fetch.append((label, acad_level, base_url))

    await _emit(
        f"[MANCHESTER-XML] fetching {len(feeds_to_fetch)} XML feed(s) "
        f"(UG year={ug_year}) …"
    )

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xml,*/*;q=0.9",
    }

    async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
        # Fetch feeds concurrently
        results = await asyncio.gather(
            *[_fetch_feed(client, label, base_url) for label, _, base_url in feeds_to_fetch],
            return_exceptions=True,
        )

    all_links: list[dict] = []
    for (label, _, _), result in zip(feeds_to_fetch, results):
        if isinstance(result, Exception):
            log.error("[MANCHESTER-XML] feed %s raised: %s", label, result)
        else:
            all_links.extend(result)

    # Deduplicate by URL (same course can appear in multiple feeds rarely)
    seen: set[str] = set()
    deduped: list[dict] = []
    for lnk in all_links:
        if lnk["url"] not in seen:
            seen.add(lnk["url"])
            deduped.append(lnk)

    dup_count = len(all_links) - len(deduped)
    log.info(
        "[MANCHESTER-XML] total=%d unique=%d duplicates=%d",
        len(all_links), len(deduped), dup_count,
    )
    await _emit(
        f"[MANCHESTER-XML] {len(deduped)} unique course link(s) "
        f"({dup_count} duplicates removed)."
    )

    return deduped
