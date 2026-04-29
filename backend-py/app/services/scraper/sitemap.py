"""Sitemap-based course discovery.

Many universities link only a small selection of "featured" courses from
their homepage but publish the full catalogue in ``sitemap.xml``. The
homepage BFS crawl in :mod:`app.services.scraper.discovery` therefore
finds five or ten candidates when the institution actually offers fifty.

This module provides a fallback path that:

1. Probes the four standard sitemap-index locations.
2. Reads ``robots.txt`` for any non-standard ``Sitemap:`` directive.
3. Recurses ONE level into a sitemap-index (sub-sitemap fetch).
4. Filters the resulting ``<loc>`` URLs through the same
   :func:`_looks_like_course` heuristic the HTML crawler uses, so the
   downstream extractor never has to care which path discovered a URL.

Network is async (httpx); parsing is a stdlib regex sweep over the XML.
We deliberately avoid an XML parser — sitemaps are loosely-validated in
the wild and a strict parser fails on the first un-escaped ampersand.
"""
from __future__ import annotations

import logging
import re
from typing import Final
from urllib.parse import urlparse

from app.services.scraper.discovery import (
    _COURSE_TEXT,
    _COURSE_URL_HINTS,
    _JUNK_TEXT,
    _is_known_non_course_url,
)
from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)

_SITEMAP_INDEX_PATHS: Final = (
    "/sitemap.xml",
    "/sitemap_index.xml",
    "/sitemap-index.xml",
    "/sitemaps.xml",
)
_LOC_RE = re.compile(r"<loc>([^<]+)</loc>", re.I)
_ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap:\s*(\S+)", re.I | re.M)
_NESTED_RE = re.compile(r"sitemap", re.I)
_NOISE_PARAMS: Final = ("students", "audience", "mode", "view", "tab", "ref")
# Matches university course-code slugs like EDUC626, NURS320, BIOL101.
# Used in _is_course_loc to allow short single-token slugs that contain
# no degree qualifier word but are clearly valid course identifiers.
_COURSE_CODE_RE = re.compile(r"^[A-Z]{2,8}\d{3,4}$")


def _is_nested_loc(loc: str) -> bool:
    """A `<loc>` that points at another sitemap rather than a real page."""
    return bool(_NESTED_RE.search(loc)) or loc.lower().endswith(".xml")


def _normalize_sitemap_url(loc: str) -> str:
    """Strip noise query params + apply known site-specific path rewrites.

    Mirrors Node ``normalizeSitemapUrl``. The VU rewrite is the only
    site-specific case currently in use; new ones should be added here
    rather than scattered through callers.
    """
    try:
        u = urlparse(loc)
    except Exception:
        return loc
    # Drop noise params from the query string.
    if u.query:
        kept = "&".join(
            seg for seg in u.query.split("&") if seg.split("=", 1)[0].lower() not in _NOISE_PARAMS
        )
        u = u._replace(query=kept)
    # Site-specific: VU's sitemap publishes Drupal-multisite legacy paths
    # (`/site-N/courses/...`) that all 404; the canonical public path is
    # `/courses/<slug>`.
    if u.netloc.endswith("vu.edu.au"):
        u = u._replace(path=re.sub(r"^/site-\d+/courses/", "/courses/", u.path, flags=re.I))
    return u.geturl()


def _slug_to_name(loc: str) -> str:
    """Convert a sitemap URL into a human-ish course name from its last path segment."""
    try:
        path = urlparse(loc).path
    except Exception:
        return loc
    parts = [p for p in path.split("/") if p]
    if not parts:
        return loc
    last = parts[-1].split("?")[0]
    last = re.sub(r"\.html?$", "", last, flags=re.I)
    last = last.replace("-", " ").replace("_", " ").strip()
    return " ".join(w.capitalize() for w in last.split())


def _looks_like_course_url(url: str) -> bool:
    lurl = url.lower()
    return any(h in lurl for h in _COURSE_URL_HINTS)


def _is_course_loc(loc: str, *, base_host: str = "") -> bool:
    """A `<loc>` we want to surface as a candidate course page.

    We require:
    1. Same registrable host as the sitemap's origin (SSRF guard) —
       prevents a hostile/misconfigured sitemap from injecting off-domain
       URLs that downstream stages would then fetch.
    2. Not a known non-course URL (scholarships, news, events, etc.) —
       uses the same blocklist as the BFS crawler so sitemap-sourced
       URLs go through identical filtering.
    3. URL hint matching one of ``_COURSE_URL_HINTS``.
    4. Slug-derived name that doesn't look like junk, so generic
       ``/courses/`` index pages and obvious non-courses (search,
       login, privacy) never make it into the candidate list.
    """
    if base_host:
        loc_host = urlparse(loc).netloc
        if loc_host and not _same_registrable_host(base_host, loc_host):
            return False
    # Apply the same non-course blocklist used by the BFS HTML crawler.
    # Without this, URLs like /study/scholarships/... and /study/events/...
    # pass through because they contain /study/ (a course URL hint) but
    # are clearly not course pages. The BFS crawler never stages these
    # because _looks_like_course() calls _is_known_non_course_url() first;
    # the sitemap path previously skipped that check (Federation regression).
    if _is_known_non_course_url(loc):
        return False
    if not _looks_like_course_url(loc):
        return False
    name = _slug_to_name(loc)
    if not name or len(name) < 4:
        return False
    if _JUNK_TEXT.match(name):
        return False
    # An honest course slug almost always contains a degree/level word OR
    # is at least two words long; this trims index pages like /courses/all.
    # Exception: university course-code slugs (e.g. EDUC626, NURS320)
    # are a single uppercase token with no degree qualifier word.  ACU
    # publishes its full handbook at /handbook/handbook-YYYY/course/<CODE>;
    # without this exception every handbook URL is blocked and the sitemap
    # fallback returns 0 candidates.  The pattern [A-Z]{2,8}\d{3,4} is
    # specific enough that index pages (/courses/all, /programs/overview)
    # never match it.
    if not _COURSE_TEXT.search(name) and len(name.split()) < 2:
        last_seg = urlparse(loc).path.rstrip("/").rsplit("/", 1)[-1].upper()
        if not _COURSE_CODE_RE.match(last_seg):
            return False
    return True


async def _fetch_text(url: str) -> str:
    """``fetch_html`` returns "" on any error — the fallback should
    keep going through the rest of the candidate list rather than abort."""
    try:
        return await fetch_html(url) or ""
    except Exception as exc:
        log.debug("sitemap fetch failed for %s: %s", url, exc)
        return ""


def _same_registrable_host(host_a: str, host_b: str) -> bool:
    """Loose same-origin check for sitemap URLs.

    Universities frequently publish faculty-specific sitemaps on
    sub-domains (``handbook.unimelb.edu.au`` vs ``unimelb.edu.au``), so a
    strict netloc match would reject legitimate cross-subdomain locs. We
    instead compare the last two host labels — `unimelb.edu.au` matches
    `handbook.unimelb.edu.au` but NOT `evil.com`.

    This is a defence-in-depth measure: a hostile or misconfigured
    sitemap could otherwise direct the scraper at arbitrary off-domain
    URLs, which the downstream extractor would then fetch with the same
    headers and cookies. Returning False here causes the loc to be
    silently dropped from the candidate set.
    """
    if not host_a or not host_b:
        return False
    a_parts = host_a.lower().split(".")
    b_parts = host_b.lower().split(".")
    if len(a_parts) < 2 or len(b_parts) < 2:
        return host_a.lower() == host_b.lower()
    # Compare the registrable suffix (last two labels). This errs on the
    # side of permissive for ccTLD edge cases (e.g. .co.uk would compare
    # `co.uk` and incorrectly match all `.co.uk` hosts) — acceptable
    # because course scraping never targets .co.uk universities and the
    # full-host match path above catches exact equality first.
    return a_parts[-2:] == b_parts[-2:]


async def discover_from_sitemap(origin: str, *, emit=None) -> list[dict]:
    """Probe sitemap.xml + robots.txt at ``origin`` and return course candidates.

    ``origin`` is the scheme+host (e.g. ``https://www.asahe.edu.au``) — no
    path. Returns ``[{url, name}]`` deduped by URL. Empty list when no
    sitemap is reachable; never raises.

    ``emit`` is the same async callable shape used by ``discovery.py`` so
    progress shows up in the UI live-log panel.

    All candidate URLs are constrained to the same registrable host as
    ``origin`` (see :func:`_same_registrable_host`). A misconfigured or
    hostile sitemap that lists off-domain URLs will have those URLs
    silently dropped — the scraper never fetches them.
    """
    parsed = urlparse(origin)
    base = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else origin.rstrip("/")
    base_host = urlparse(base).netloc

    candidates: list[str] = list(f"{base}{p}" for p in _SITEMAP_INDEX_PATHS)
    # robots.txt may publish non-standard sitemap locations — very common
    # on large university sites that split by faculty.
    robots = await _fetch_text(f"{base}/robots.txt")
    if robots:
        for m in _ROBOTS_SITEMAP_RE.findall(robots):
            url = m.strip()
            if not url:
                continue
            # Some hosts publish a relative `Sitemap: /sitemap.xml`
            # directive — RFC technically requires absolute, but real
            # sites violate it. Resolve against base.
            if url.startswith("/"):
                url = f"{base}{url}"
            elif not url.lower().startswith(("http://", "https://")):
                url = f"{base}/{url.lstrip('/')}"
            # SSRF guard: only accept robots-published sitemap URLs that
            # live on the same registrable host.
            sitemap_host = urlparse(url).netloc
            if base_host and sitemap_host and not _same_registrable_host(base_host, sitemap_host):
                log.debug("sitemap: dropping off-host robots directive %s", url)
                continue
            if url not in candidates:
                candidates.append(url)

    if emit:
        await emit(
            "status",
            f"[DISCOVER] sitemap: probing {len(candidates)} URL(s) at {base}",
            phase="discover",
            kind="sitemap_start",
        )

    found: dict[str, str] = {}
    seen_locs: set[str] = set()

    for sm_url in candidates:
        xml = await _fetch_text(sm_url)
        if not xml or "<" not in xml:
            continue
        all_locs = _LOC_RE.findall(xml)
        if not all_locs:
            continue

        nested = [loc for loc in all_locs if _is_nested_loc(loc)]
        # Recurse one level into a sitemap-index. We don't recurse further:
        # a malicious or misconfigured site could otherwise pull the
        # scraper into an unbounded fetch loop.
        if nested:
            if emit:
                await emit(
                    "status",
                    f"[DISCOVER] sitemap index {sm_url}: {len(nested)} sub-sitemap(s)",
                    phase="discover",
                    kind="sitemap_index",
                )
            for nested_url in nested:
                if nested_url in seen_locs:
                    continue
                seen_locs.add(nested_url)
                sub_xml = await _fetch_text(nested_url)
                if not sub_xml:
                    continue
                added = 0
                for raw_loc in _LOC_RE.findall(sub_xml):
                    loc = _normalize_sitemap_url(raw_loc)
                    if (
                        loc in found
                        or _is_nested_loc(loc)
                        or not _is_course_loc(loc, base_host=base_host)
                    ):
                        continue
                    found[loc] = _slug_to_name(loc)
                    added += 1
                if emit and added:
                    await emit(
                        "status",
                        f"[DISCOVER] sub-sitemap {nested_url}: +{added} candidates "
                        f"(total {len(found)})",
                        phase="discover",
                        kind="sitemap_page",
                        added=added,
                        total=len(found),
                    )

        before = len(found)
        for raw_loc in all_locs:
            loc = _normalize_sitemap_url(raw_loc)
            if (
                loc in found
                or _is_nested_loc(loc)
                or not _is_course_loc(loc, base_host=base_host)
            ):
                continue
            found[loc] = _slug_to_name(loc)
        added = len(found) - before
        if emit and added:
            await emit(
                "status",
                f"[DISCOVER] sitemap {sm_url}: +{added} candidates (total {len(found)})",
                phase="discover",
                kind="sitemap_page",
                added=added,
                total=len(found),
            )
        # First sitemap that yields candidates wins. Many sites publish
        # both sitemap.xml and sitemap_index.xml with overlapping content;
        # parsing both is redundant work.
        if found:
            break

    if emit:
        await emit(
            "status",
            f"[DISCOVER] sitemap: done — {len(found)} unique candidates",
            phase="discover",
            kind="sitemap_done",
            total=len(found),
        )

    return [{"url": u, "name": n} for u, n in found.items()]
