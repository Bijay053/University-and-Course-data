"""Browser-based discovery for Macquarie University's find-a-course catalogue.

Macquarie's catalogue at https://www.mq.edu.au/study/find-a-course is a
Svelte-based SPA served behind Cloudflare. Two compounding problems break
the default discovery path:

1. **Cloudflare 403 on plain HTTP** — ``curl https://www.mq.edu.au/`` returns
   HTTP 403 with ``cf-mitigated: challenge``. The BFS HTTP crawler therefore
   yields zero candidates. (mq.yaml sets ``always_browser_discover: true`` to
   bypass.)

2. **URL shape mismatch** — every other Australian university we scrape
   exposes course detail pages at ``/courses/<slug>``, ``/course/<slug>``,
   ``/degrees/<slug>`` or ``/programs/<slug>``. Macquarie does NOT: real
   course URLs live at::

       /study/find-a-course/undergraduate/<slug>
       /study/find-a-course/postgraduate/<slug>
       /study/find-a-course/undergraduate/<faculty>/<slug>  (combined / co-op)

   ``browser_discover_generic._NAV_LINK_SELECTOR`` and ``_looks_like_course``
   require ``/courses/`` or sibling tokens to be present in the path — so the
   generic browser pass harvests only the 6 nav links from the homepage and
   stages them as junk courses (the user-reported "Undergraduate", "Browse
   all degrees" etc. that the guards now block).

This module is a Macquarie-specific browser sweep modelled on
``csu_browser_discover.py``:

* Visits the three catalogue seed pages
  (find-a-course, /undergraduate, /postgraduate).
* For each page: waits for the Svelte course grid to hydrate, scrolls to the
  bottom in small steps to trigger any lazy-load, and harvests every anchor
  whose path matches the MQ course-URL regex.
* Dedupes by URL, drops listing roots and major / specialisation sub-pages.
* Returns ``[{"url": str, "name": str}, ...]`` or ``[]`` on failure (caller
  falls back to ``browser_discover_generic`` then Wayback CDX).

Discovery floor / defence-in-depth
----------------------------------
A successful sweep should return at least ~150 course URLs (Macquarie's
catalogue is ~300 UG+PG). When the count falls below
``_DISCOVERY_FLOOR``, this module emits a loud ``[DISCOVER] MQ: WARNING``
status so the operator notices the regression in the live job log — but it
still returns whatever it found so partial discovery is better than zero.

Live verification
-----------------
The module cannot be exercised from the Replit dev sandbox because the
Cloudflare layer challenges headless Chromium with our outbound IP range.
Set ``MQ_LIVE_TEST=1`` and run the smoke test from a network that MQ
accepts (the user's local machine, the prod droplet, etc.)::

    cd /root/University-and-Course-data && \\
        cd backend-py && PYTHONPATH=. MQ_LIVE_TEST=1 \\
        python -m pytest tests/test_mq_browser_discover.py -k live -v -s
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# Faculty subpages — the 4 MQ faculties each publish a per-faculty course
# index.  Tried FIRST because they render course anchors in plain HTML
# without needing filter UI interaction (the catalogue landing pages are
# pure-SPA search shells that show filters + faculty cards only, no
# course links until the user clicks something).
_FACULTY_SEED_URLS: tuple[str, ...] = (
    "https://www.mq.edu.au/study/find-a-course/arts",
    "https://www.mq.edu.au/study/find-a-course/business",
    "https://www.mq.edu.au/study/find-a-course/medicine-and-health-sciences",
    "https://www.mq.edu.au/study/find-a-course/science-and-engineering",
)

# Catalogue landing seeds — visited AFTER the faculty pages.  These are
# the SPA search shells; they require filter UI interaction to populate
# course anchors (see ``_interactive_filter_harvest``).  Mirrors the
# entries in ``_HOST_EXTRA_SEEDS`` so the two discovery paths agree on
# which roots to enumerate.
_CATALOGUE_SEED_URLS: tuple[str, ...] = (
    "https://www.mq.edu.au/study/find-a-course",
    "https://www.mq.edu.au/study/find-a-course/undergraduate",
    "https://www.mq.edu.au/study/find-a-course/postgraduate",
)

_SEED_URLS: tuple[str, ...] = _FACULTY_SEED_URLS + _CATALOGUE_SEED_URLS

_MQ_ORIGIN = "https://www.mq.edu.au"

# Course-detail URL regex.  Allows one OPTIONAL faculty segment between the
# level token and the course slug to cover MQ's combined-degree and co-op
# listings (e.g. ``/undergraduate/combined-bachelor-master-degrees/
# bachelor-of-laws-master-of-laws`` and ``/undergraduate/employability-
# initiatives/cooperative-education-program-in-actuarial-studies``).
#
# Listing roots — ``/undergraduate``, ``/postgraduate``, ``/research``,
# ``/undergraduate/combined-bachelor-master-degrees`` (path ends here) —
# deliberately do NOT match because they have no trailing slug segment.
#
# ``courses`` is the generic sub-path used by the coursehandbook resolver
# when it constructs admissions URLs (``/study/find-a-course/courses/<slug>``).
# ``research`` was omitted from the original regex, silently dropping all
# research degrees (Doctor of Philosophy, Professional Doctorates, etc.)
# from the browser-sweep harvest tier.
_COURSE_PATH_RE = re.compile(
    r"^/study/find-a-course/"
    r"(?:undergraduate|postgraduate|research|courses)"
    r"(?:/[^/]+){1,2}/?$"
)

# Search-result link regex — broader than _COURSE_PATH_RE because the
# /search page links directly to the admissions URL for each course, which
# may use any of the four level tokens above.  Does NOT require a trailing
# slug segment count (the search page guarantees it's a real course page,
# not a listing root).
_SEARCH_COURSE_LINK_RE = re.compile(
    r"^/study/find-a-course/"
    r"(?:undergraduate|postgraduate|research|courses)"
    r"/[^/?#]+",
    re.IGNORECASE,
)

# Last-segment slugs that look like a course URL but are category /
# wizard / builder pages.  Belt-and-suspenders alongside ``mq.yaml``'s
# ``block_url_patterns`` and ``guards.is_blocked_page``.
_LISTING_LAST_SEGMENTS: frozenset[str] = frozenset({
    "combined-bachelor-master-degrees",
    "double-degree-builder",
    "browse-all-degrees",
    "view-degrees",
    "view-all-degrees",
})

# Path substrings that always indicate a sub-degree page (a major or
# specialisation), not a real course.
_BLOCKED_PATH_SUBSTRINGS: tuple[str, ...] = (
    "/find-a-course/courses/major/",
    "/find-a-course/courses/specialisation/",
    "/find-a-course/courses/specialization/",
)

# Selector that waits for the catalogue/faculty page to hydrate.  Faculty
# subpages render plain ``/study/find-a-course/<slug>`` anchors, while the
# catalogue landing pages only show course-shape anchors AFTER filter
# interaction (handled by ``_interactive_filter_harvest``).  Match the
# broadest catalogue-relative shape so faculty pages don't time out
# waiting for the narrower UG/PG-anchored variant that never appears
# there.
_HYDRATE_WAIT_SELECTOR = "a[href*='/study/find-a-course/']"
_HYDRATE_WAIT_MS = 12_000

# Filter buttons / chips we try on catalogue landing pages to coax the
# SPA into rendering course results.  Tried in order; the FIRST one that
# resolves to a visible, clickable element fires.  Defensive — every
# click is wrapped in try/except so a missing selector never aborts the
# sweep.  Sourced from common SPA patterns; the live MQ filter UI uses
# accessible labels so role+name selectors are the most resilient.
_FILTER_CLICK_SELECTORS: tuple[str, ...] = (
    "button:has-text('Undergraduate')",
    "button:has-text('Postgraduate')",
    "label:has-text('Undergraduate')",
    "label:has-text('Postgraduate')",
    "a:has-text('All courses')",
    "a:has-text('View all')",
    "button:has-text('Search')",
    "button:has-text('Apply filters')",
)

# Scroll loop bounds
_MAX_SCROLL_ITERS = 25
_SCROLL_SETTLE_S = 1.5
_INITIAL_SETTLE_S = 4.0

# Discovery floor: when total deduped URLs across all seeds falls below
# this we emit a [DISCOVER] MQ: WARNING.  MQ publishes 367 international
# courses as of August 2026; 300 is a reasonable two-thirds cushion.
_DISCOVERY_FLOOR = 300

# Hard cap mirrors the CSU module — guard against runaway harvests if MQ
# ever exposes a duplicated link grid.  Capped well above _DISCOVERY_FLOOR
# so the warning fires before the cap.
_HARD_MAX_LINKS = 1_500

# Extract every ``<a href>`` from the DOM and resolve it against the page
# origin.  Returns ``[{href, text}, ...]`` so the Python caller can apply
# the canonical URL filter.
_EXTRACT_ANCHORS_JS = r"""
() => {
  const ORIGIN = 'https://www.mq.edu.au';
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    if (!raw || raw.startsWith('mailto:') || raw.startsWith('tel:')
        || raw.startsWith('#') || raw.startsWith('javascript:')) {
      return;
    }
    let url;
    try { url = new URL(raw, ORIGIN).href; } catch (_) { return; }
    const text = (a.innerText || a.textContent || '')
      .replace(/\s+/g, ' ').trim();
    out.push({ href: url, text });
  });
  return out;
}
"""


# ── Funnelback JSON API discovery (Tier 0) ────────────────────────────────
# MQ exposes its full international course catalogue as a Funnelback/Squiz
# search JSON endpoint.  A single HTTP GET returns all 338+ courses as a
# structured JSON payload — no browser, no DOM parsing, no Cloudflare
# challenge page (the JSON API endpoint passes Scrape.do's residential proxy
# cleanly).  Each result includes:
#   • ``liveUrl``            — the real admissions URL (correct path prefix)
#   • ``title``              — course name
#   • ``metaData.studyLevel``       — "Undergraduate" / "Postgraduate" / …
#   • ``metaData.courseDuration``   — "1 year", "2 years", …
#
# After collecting the URL list we concurrently fetch each course's
# Gatsby page-data.json endpoint, which is a structured JSON blob
# containing fees, IELTS scores, description, and entry requirements.
# The URL transform is:
#   https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts
#   → https://www.mq.edu.au/study/page-data/find-a-course/undergraduate/bachelor-of-arts/page-data.json
#
# Both Funnelback and page-data.json results are combined into a
# ``scrapy_result``-shaped dict per course (matching the searchstax
# short-circuit in orchestrator._extract_only) so the normal
# per-course HTML fetch + extraction is bypassed entirely.
#
# Source reference: MU_AU Scrapy spider (provided by operator 2026-08-12).
_FUNNELBACK_API_URL = (
    "https://mqu-search.funnelback.squiz.cloud/s/search.json"
    "?collection=mqu~sp-courses&profile=international"
    "&query=!padrenull&start_rank=1&num_ranks=500"
)
# Minimum result count to trust the Funnelback API response (avoids
# partial/error responses being treated as a real catalogue).
_FUNNELBACK_MIN_RESULTS = 50

# Concurrent page-data.json fetches — 20 parallel keeps a 338-course
# fetch under ~30s at ~200ms/request.
_PAGE_DATA_PARALLEL = 20
_PAGE_DATA_TIMEOUT_S = 20.0

# Scrape.do fetch UA — same as used by the resolver pass.
_SCRAPE_DO_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _page_data_url(live_url: str) -> str:
    """Convert an MQ admissions URL to its Gatsby page-data.json endpoint.

    ``https://www.mq.edu.au/study/find-a-course/undergraduate/bachelor-of-arts``
    →
    ``https://www.mq.edu.au/study/page-data/find-a-course/undergraduate/bachelor-of-arts/page-data.json``
    """
    # Strip query/fragment, normalise trailing slash.
    base = live_url.split("?")[0].split("#")[0].rstrip("/")
    # The transform inserts "page-data/" after the ".au/study" segment.
    return base.replace(
        "www.mq.edu.au/study/",
        "www.mq.edu.au/study/page-data/",
    ) + "/page-data.json"


def _parse_study_level(raw: str) -> str:
    """Map Funnelback studyLevel to a canonical degree_level prefix."""
    lower = (raw or "").lower().strip()
    if "undergraduate" in lower:
        return "Undergraduate"
    if "postgraduate" in lower or "postgrad" in lower:
        return "Postgraduate"
    if "research" in lower or "doctorate" in lower or "phd" in lower:
        return "Doctorate"
    return raw.strip() if raw else ""


def _parse_duration_funnelback(raw: str) -> tuple[float | None, str]:
    """Parse '2 years', '18 months', etc. → (value, term).

    Returns (None, '') when the string is empty or unparseable.
    """
    raw = (raw or "").strip().lower()
    if not raw:
        return None, ""
    m = re.search(r"(\d+(?:\.\d+)?)\s*(year|years|month|months|semester|semesters)", raw)
    if not m:
        return None, ""
    val = float(m.group(1))
    unit = m.group(2).rstrip("s")   # "year" / "month" / "semester"
    return val, unit


def _extract_program_from_page_data(body: str | None) -> dict:
    """Extract ``program`` dict from a Gatsby page-data.json response body.

    Returns ``{}`` on any parse failure.
    """
    if not body:
        return {}
    try:
        outer = json.loads(body)
        program_json = (
            outer.get("result", {})
                 .get("data", {})
                 .get("current", {})
                 .get("fields", {})
                 .get("json")
        )
        if not program_json:
            return {}
        return json.loads(program_json) if isinstance(program_json, str) else program_json
    except Exception:  # noqa: BLE001
        return {}


def _build_scrapy_result(
    name: str,
    url: str,
    funnelback_meta: dict,
    program: dict,
) -> dict:
    """Combine Funnelback metadata + page-data.json program dict into a
    ``scrapy_result``-compatible payload + evidence list.

    Shape mirrors ``searchstax_hud._item_to_link`` so the orchestrator's
    ``_extract_only`` short-circuit accepts it verbatim.
    """
    page_data_url_str = _page_data_url(url)

    def _ev(field_key, value, method, source, page_type, snippet, confidence):
        return {
            "field_key": field_key,
            "value": value,
            "normalized": value,
            "source_url": source,
            "page_type": page_type,
            "method": method,
            "snippet": snippet,
            "confidence": confidence,
            "decision_status": "selected",
        }

    payload: dict = {
        "course_name": name,
        "course_website": url,
        "has_central_fee_page": False,
    }
    evidence: list[dict] = []

    evidence.append(_ev(
        "course_name", name, "funnelback:title", url, "course",
        f"Funnelback result title: {name}", 0.95,
    ))

    # ── Degree level from Funnelback studyLevel ────────────────────────
    study_level_raw = funnelback_meta.get("studyLevel", "")
    if study_level_raw:
        payload["degree_level"] = study_level_raw
        # academic_level mapping
        sl_lower = study_level_raw.lower()
        if "undergraduate" in sl_lower:
            payload["academic_level"] = "Undergraduate"
        elif "postgraduate" in sl_lower or "postgrad" in sl_lower:
            payload["academic_level"] = "Postgraduate"
        elif "research" in sl_lower or "doctorate" in sl_lower:
            payload["academic_level"] = "Doctorate"
        evidence.append(_ev(
            "degree_level", study_level_raw, "funnelback:studyLevel", url, "course",
            f"Funnelback metaData.studyLevel: {study_level_raw}", 0.85,
        ))

    # ── Duration from Funnelback courseDuration ────────────────────────
    dur_raw = funnelback_meta.get("courseDuration", "")
    dur_val, dur_term = _parse_duration_funnelback(dur_raw)
    if dur_val is not None:
        payload["duration"] = dur_val
        payload["duration_term"] = dur_term
        evidence.append(_ev(
            "duration", dur_val, "funnelback:courseDuration", url, "course",
            f"Funnelback metaData.courseDuration: {dur_raw}", 0.75,
        ))

    # ── Below: extract from program (Gatsby page-data.json) ───────────
    if not program:
        return {"name": name, "url": url, "payload": payload, "evidence": evidence}

    # International fee
    fees = program.get("fees") or []
    for fee_item in fees:
        fee_type_label = (
            (fee_item.get("fee_type") or {}).get("label") or ""
        ).lower()
        if "international" in fee_type_label:
            raw_fee = fee_item.get("estimated_annual_fee")
            try:
                fee_val = float(raw_fee)
                payload["international_fee"] = fee_val
                payload["fee_term"] = "Year"
                payload["fee_year"] = "2026"
                payload["currency"] = "AUD"
                evidence.append(_ev(
                    "international_fee", fee_val, "page_data:fees",
                    page_data_url_str, "course",
                    f"page-data.json fees[].estimated_annual_fee (international): {raw_fee}",
                    0.90,
                ))
            except (TypeError, ValueError):
                pass
            break  # take the first matching international fee

    # IELTS scores
    ielts_fields = {
        "ielts_overall_score": "ielts_overall",
        "ielts_reading_score": "ielts_reading",
        "ielts_writing_score": "ielts_writing",
        "ielts_listening_score": "ielts_listening",
        "ielts_speaking_score": "ielts_speaking",
    }
    for src_key, dst_key in ielts_fields.items():
        raw_val = program.get(src_key)
        if raw_val is not None:
            try:
                score = float(raw_val)
                payload[dst_key] = score
                evidence.append(_ev(
                    dst_key, score, "page_data:ielts",
                    page_data_url_str, "course",
                    f"page-data.json {src_key}: {raw_val}", 0.90,
                ))
            except (TypeError, ValueError):
                pass

    # Description from marketing_items.descriptions
    try:
        descs = (program.get("marketing_items") or {}).get("descriptions") or []
        desc_parts = [
            d["long_description"]
            for d in descs
            if d.get("long_description")
        ]
        if desc_parts:
            payload["description"] = "\n".join(desc_parts)
            evidence.append(_ev(
                "description", payload["description"][:80] + "…",
                "page_data:marketing_descriptions",
                page_data_url_str, "course",
                "page-data.json marketing_items.descriptions", 0.80,
            ))
    except Exception:  # noqa: BLE001
        pass

    # Admission requirements / other_requirement
    admission_req = program.get("admission_requirements")
    if admission_req:
        payload["other_requirement"] = admission_req
        evidence.append(_ev(
            "other_requirement", str(admission_req)[:80],
            "page_data:admission_requirements",
            page_data_url_str, "course",
            "page-data.json admission_requirements", 0.80,
        ))

    # Study mode + location from offering[]
    try:
        offerings = program.get("offering") or []
        locations: set[str] = set()
        for of in offerings:
            loc = (of.get("location") or "").strip()
            if loc:
                locations.add(loc)
        if locations:
            # Derive study_mode: "Off-campus" = online delivery
            if "Off-campus" in locations and len(locations) == 1:
                study_mode = "Online"
            elif "Off-campus" not in locations:
                study_mode = "On-Campus"
            else:
                study_mode = "Hybrid"
            payload["study_mode"] = study_mode
            evidence.append(_ev(
                "study_mode", study_mode, "page_data:offering.location",
                page_data_url_str, "course",
                f"offering locations: {sorted(locations)}", 0.75,
            ))
            # Campus location (first non-Off-campus location)
            on_campus = sorted(l for l in locations if l != "Off-campus")
            if on_campus:
                payload["course_location"] = on_campus[0]
                evidence.append(_ev(
                    "course_location", on_campus[0], "page_data:offering.location",
                    page_data_url_str, "course",
                    f"Offering campus location: {on_campus[0]}", 0.75,
                ))
    except Exception:  # noqa: BLE001
        pass

    # Study load from enrolment_patterns
    try:
        patterns = program.get("enrolment_patterns") or []
        if "Full Time" in patterns and "Part Time" in patterns:
            payload["study_load"] = "Full Time"
        elif "Part Time" in patterns:
            payload["study_load"] = "Part Time"
        elif "Full Time" in patterns:
            payload["study_load"] = "Full Time"
    except Exception:  # noqa: BLE001
        pass

    return {"name": name, "url": url, "payload": payload, "evidence": evidence}


async def _discover_from_funnelback_api(
    emit_fn,
    *,
    max_courses: int = 500,
) -> list[dict]:
    """Fetch the MQ Funnelback search API and return fully-extracted link dicts.

    Each returned link dict has the shape::

        {
            "name": str,
            "url": str,
            "scrapy_result": {name, url, payload, evidence},
        }

    The ``scrapy_result`` key causes ``orchestrator._extract_only`` to
    return the pre-built payload verbatim without making any further HTTP
    requests or running the HTML extraction pipeline.

    Steps
    -----
    1. Fetch the Funnelback JSON API endpoint via Scrape.do (render=False,
       residential proxy bypasses Cloudflare on the squiz.cloud host).
       Falls back to plain httpx with a browser UA when Scrape.do is
       unavailable.
    2. Parse ``response.resultPacket.results`` → list of courses with
       ``liveUrl``, ``title``, ``metaData``.
    3. Concurrently fetch the Gatsby ``page-data.json`` endpoint for each
       course (plain httpx, 20 parallel).  The page-data.json host is the
       same www.mq.edu.au and may be CF-blocked from the Replit sandbox;
       if a batch of plain-httpx fetches returns CF challenges, the
       function retries via Scrape.do for the failed courses.
    4. Build a ``scrapy_result`` payload per course from Funnelback metadata
       + page-data.json program dict.

    Returns ``[]`` on any unrecoverable error (caller falls through to
    the coursehandbook sitemap + browser-sweep tiers).
    """
    import httpx as _httpx

    await emit_fn("[DISCOVER] MQ: Tier 0 — Funnelback API fetch")

    # ── Step 1: Fetch Funnelback search JSON ────────────────────────────
    fb_body: str | None = None

    # Try scrape.do first (residential proxy bypasses CF from any IP).
    try:
        from app.services.scraper.http_fetcher import fetch_html_scrape_do
        fb_body = await fetch_html_scrape_do(
            _FUNNELBACK_API_URL,
            render=False,
            rate_limit=False,  # discovery phase — exempt from fleet limiter
            max_retries=1,
        )
    except Exception as _exc:  # noqa: BLE001
        log.debug("mq funnelback: scrape.do fetch failed: %s", _exc)

    # Fallback: plain httpx (works from prod server where IP isn't blocked).
    if not fb_body:
        try:
            async with _httpx.AsyncClient(
                headers={"User-Agent": _SCRAPE_DO_UA},
                follow_redirects=True,
                timeout=30.0,
            ) as client:
                r = await client.get(_FUNNELBACK_API_URL)
                if r.status_code == 200:
                    fb_body = r.text
                else:
                    log.warning(
                        "mq funnelback: httpx fallback → HTTP %s", r.status_code
                    )
        except Exception as _exc:  # noqa: BLE001
            log.warning("mq funnelback: httpx fallback failed: %s", _exc)

    if not fb_body:
        await emit_fn(
            "[DISCOVER] MQ: Tier 0 — Funnelback API unreachable; "
            "falling through to coursehandbook sitemap"
        )
        return []

    # ── Step 2: Parse the JSON response ─────────────────────────────────
    try:
        fb_data = json.loads(fb_body)
        results = (
            fb_data
            .get("response", {})
            .get("resultPacket", {})
            .get("results", [])
        )
    except Exception as _exc:  # noqa: BLE001
        await emit_fn(
            f"[DISCOVER] MQ: Tier 0 — Funnelback JSON parse error: {_exc}; "
            "falling through"
        )
        return []

    if len(results) < _FUNNELBACK_MIN_RESULTS:
        await emit_fn(
            f"[DISCOVER] MQ: Tier 0 — only {len(results)} results "
            f"(expected ≥{_FUNNELBACK_MIN_RESULTS}); possible API error; "
            "falling through"
        )
        return []

    await emit_fn(
        f"[DISCOVER] MQ: Tier 0 — Funnelback returned {len(results)} courses; "
        "fetching page-data.json for each…"
    )

    # Build the (url, name, metaData) triples.
    course_triples: list[tuple[str, str, dict]] = []
    for r in results:
        live_url = (r.get("liveUrl") or "").strip().rstrip("/")
        title = (r.get("title") or "").strip()
        meta = r.get("metaData") or {}
        if not live_url or not title:
            continue
        # Only keep URLs that are real admissions course pages.
        parsed_path = live_url.replace("https://www.mq.edu.au", "")
        if not _SEARCH_COURSE_LINK_RE.match(parsed_path):
            continue
        course_triples.append((live_url, title, meta))

    await emit_fn(
        f"[DISCOVER] MQ: Tier 0 — {len(course_triples)} valid course URLs "
        f"(filtered from {len(results)} Funnelback results)"
    )

    # ── Step 3: Concurrently fetch page-data.json ────────────────────────
    sem = asyncio.Semaphore(_PAGE_DATA_PARALLEL)
    programs: dict[str, dict] = {}  # url → program dict

    async def _fetch_page_data(client: "_httpx.AsyncClient", url: str) -> None:
        pd_url = _page_data_url(url)
        async with sem:
            try:
                resp = await client.get(pd_url, timeout=_PAGE_DATA_TIMEOUT_S)
                if resp.status_code == 200:
                    prog = _extract_program_from_page_data(resp.text)
                    if prog:
                        programs[url] = prog
                # Non-200: CF challenge or 404 — handled by scrape.do retry below.
            except Exception:  # noqa: BLE001
                pass

    async with _httpx.AsyncClient(
        headers={"User-Agent": _SCRAPE_DO_UA},
        follow_redirects=True,
        timeout=_PAGE_DATA_TIMEOUT_S,
    ) as client:
        await asyncio.gather(*[
            _fetch_page_data(client, url)
            for url, _, _ in course_triples
        ])

    plain_ok = len(programs)
    await emit_fn(
        f"[DISCOVER] MQ: Tier 0 — page-data.json via plain httpx: "
        f"{plain_ok}/{len(course_triples)} succeeded"
    )

    # Retry CF-blocked courses via Scrape.do (render=False).
    missing_urls = [
        url for url, _, _ in course_triples if url not in programs
    ]
    if missing_urls:
        try:
            from app.services.scraper.http_fetcher import fetch_html_scrape_do
            scrape_do_ok = 0
            # Sequential to avoid burning Scrape.do credits in a parallel burst.
            for url in missing_urls:
                pd_url = _page_data_url(url)
                try:
                    body = await fetch_html_scrape_do(
                        pd_url, render=False, rate_limit=False, max_retries=1,
                    )
                    if body:
                        prog = _extract_program_from_page_data(body)
                        if prog:
                            programs[url] = prog
                            scrape_do_ok += 1
                except Exception:  # noqa: BLE001
                    pass
            if scrape_do_ok:
                await emit_fn(
                    f"[DISCOVER] MQ: Tier 0 — page-data.json via scrape.do retry: "
                    f"+{scrape_do_ok} (total now {len(programs)}/{len(course_triples)})"
                )
        except Exception as _exc:  # noqa: BLE001
            log.debug("mq funnelback: scrape.do page-data retry unavailable: %s", _exc)

    # ── Step 4: Build scrapy_result links ───────────────────────────────
    links: list[dict] = []
    for url, name, meta in course_triples[:max_courses]:
        program = programs.get(url, {})
        scrapy_result = _build_scrapy_result(name, url, meta, program)
        links.append({
            "name": name,
            "url": url,
            "scrapy_result": scrapy_result,
        })

    await emit_fn(
        f"[DISCOVER] MQ: Tier 0 — Funnelback API complete: "
        f"{len(links)} course links "
        f"({len(programs)} with full page-data.json, "
        f"{len(links) - len(programs)} metadata-only)"
    )
    return links


# ── Coursehandbook sitemap discovery ──────────────────────────────────────
# The real, complete MQ course catalogue lives at
# ``coursehandbook.mq.edu.au`` (a Squiz-fronted handbook host, NOT the
# Svelte SPA at ``www.mq.edu.au/study/find-a-course``).  Its sitemap
# index at ``/sitemap.xml`` lists 14 child sitemaps containing ~28K URLs
# across years 2020-2027 in three shapes:
#
#     /YYYY/courses/CXXXXXX       — actual course detail pages (the target)
#     /YYYY/units/<UNITCODE>      — individual subjects (NOT courses)
#     /YYYY/aos/NXXXXXX           — areas-of-study / majors (NOT courses)
#     /YYYY/doubledegree/DXXXXXX  — combined degrees (NOT individual courses)
#
# We harvest ONLY ``/YYYY/courses/CXXXXXX`` for the current year + next
# year (the user-facing UI defaults to the current academic year and
# offers the next year as a tab; older years are still served but
# represent expired offerings we do not want to stage).
#
# Probed 2026-05-25 from Replit sandbox via stealth: the index returns
# 200 + 14 child sitemap URLs, child sitemap-1 contains
# ``/2026/courses/C000001`` -> "Bachelor of Biodiversity and Conservation",
# which proves the host is reachable and the URLs render real course HTML.
_COURSEHANDBOOK_SITEMAP_INDEX = (
    "https://coursehandbook.mq.edu.au/sitemap.xml"
)
_COURSEHANDBOOK_COURSE_RE = re.compile(
    r"^https://coursehandbook\.mq\.edu\.au/(\d{4})/courses/C\d+/?$"
)
_COURSEHANDBOOK_SITEMAP_TIMEOUT_S = 30.0

# After harvesting coursehandbook URLs (which point at the academic catalogue —
# descriptions, learning outcomes, credit points, but NO fees / IELTS /
# session / campus data), we resolve each to its equivalent admissions page
# at www.mq.edu.au/study/find-a-course/courses/<slug>. The admissions pages
# DO have fee, IELTS, session, campus, study-mode data (verified live
# 2026-05-25 on bachelor-of-arts, bachelor-of-biodiversity-and-conservation,
# bachelor-of-environment, bachelor-of-chiropractic-science, master-of-
# business-administration — 9/10 sample courses returned 200 with
# "Estimated annual fee AUD $XX,XXX", "Session 1 (23 February 2026)",
# "North Ryde", "International student" toggle). Coursehandbook is the
# academic-staff handbook, not the prospective-student admissions site.
_STUDY_URL_BASE = "https://www.mq.edu.au/study/find-a-course/courses/"
# Root of the MQ admissions find-a-course tree.  The resolver now
# constructs level-specific sub-paths (undergraduate/postgraduate/
# research/courses) rather than always using /courses/.
_STUDY_URL_ROOT = "https://www.mq.edu.au/study/find-a-course/"

# Regex to find a direct link to the admissions page embedded in the
# coursehandbook HTML.  The handbook often carries an "Apply" or "View
# this course" anchor pointing at www.mq.edu.au — this is the most
# reliable way to recover the correct path prefix (undergraduate /
# postgraduate / research) without making extra network requests.
_ADMISSIONS_URL_IN_PAGE_RE = re.compile(
    r"https://www\.mq\.edu\.au/study/find-a-course/"
    r"(?:undergraduate|postgraduate|research|courses)"
    r"/[^\"\' <>&#\s]+",
    re.IGNORECASE,
)

# Title-prefix map for inferring which URL path segment a course belongs
# to.  Listed most-specific first so shorter substrings don't shadow
# longer ones.  Matching is done on the lowercased, delivery-suffix-
# stripped title so "(OUA)"/"(NMJI)" variants are already removed.
_TITLE_LEVEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("doctor of",              "research"),
    ("doctor in",              "research"),
    ("phd",                    "research"),
    ("professional doctorate", "research"),
    ("master of",              "postgraduate"),
    ("master in",              "postgraduate"),
    ("master by",              "postgraduate"),
    ("executive master",       "postgraduate"),
    ("graduate certificate",   "postgraduate"),
    ("graduate diploma",       "postgraduate"),
    ("bachelor of",            "undergraduate"),
    ("bachelor in",            "undergraduate"),
    ("bachelor ",              "undergraduate"),   # "Bachelor Honours" etc.
    ("associate degree",       "undergraduate"),
    ("diploma of",             "undergraduate"),
    ("diploma in",             "undergraduate"),
)


def _infer_url_prefix(title: str) -> str:
    """Return the URL path segment most likely to host *title* on MQ's site.

    Matches the lowercased title against ``_TITLE_LEVEL_PREFIXES`` (most-
    specific patterns first) and returns the corresponding segment string
    (``"undergraduate"``, ``"postgraduate"``, ``"research"``, or the
    fallback ``"courses"``).

    Examples verified against live 2026 MQ admissions URLs::

        "Bachelor of Arts"                  → "undergraduate"
        "Master of Business Administration" → "postgraduate"
        "Doctor of Philosophy"              → "research"
        "Graduate Certificate in Business"  → "postgraduate"
        "Combined Degree Programme"         → "courses"  (fallback)
    """
    lower = title.lower().strip()
    for pattern, segment in _TITLE_LEVEL_PREFIXES:
        if lower.startswith(pattern):
            return segment
    return "courses"   # safe fallback — was always the previous behaviour


# Parallel batch size for the resolver pass — each stealth goto takes
# ~2-3s, so 6 parallel keeps a 350-course resolve under ~3 minutes.
_RESOLVE_PARALLEL = 6
# Per-page timeout for the title-only resolve goto (no body wait required;
# <title> is in the SPA shell static HTML).
_RESOLVE_GOTO_TIMEOUT_MS = 15_000
# Strip the "| Macquarie University" or " - Macquarie University" suffix
# that some coursehandbook titles carry. The bare course name is what
# slugifies to the admissions URL.
_TITLE_SUFFIX_RE = re.compile(
    r"\s*(?:\||\-|–|—)\s*Macquarie\s+University\s*$", re.I,
)
# Strip delivery-mode parenthetical suffixes that the coursehandbook
# appends but that are ABSENT from the admissions URL slug on
# www.mq.edu.au.  Without stripping, these produce dead slugs that
# 404 on the admissions site and count as fetch_failed:
#
#   "Bachelor of Arts (OUA)"  → slug "bachelor-of-arts-oua"  → 404
#   "Bachelor of Science (NMJI)" → slug "bachelor-of-science-nmji" → 404
#
# Stripping yields the real slug:
#   "Bachelor of Arts"  → slug "bachelor-of-arts"  → valid admissions page
#
# OUA  = Open Universities Australia delivery mode
# NMJI = Ningbo Nottingham JI / NMJ Institute partner program
# These two account for 25 of the 104 persistent fetch_failed courses
# observed in the August 2026 production run.
_DELIVERY_SUFFIX_RE = re.compile(
    r"\s*\(\s*(?:OUA|NMJI)\s*\)\s*$", re.IGNORECASE,
)
# Slug character whitelist: lowercase letters, digits, hyphen. Anything
# else (parens, slashes, ampersands, apostrophes, commas) is replaced by
# a hyphen, and runs of hyphens are collapsed. Matches the canonical
# www.mq.edu.au URL shape (e.g. "Bachelor of Game Design and Development"
# → "bachelor-of-game-design-and-development").
_SLUG_NON_WORD_RE = re.compile(r"[^a-z0-9]+")
# Restrict to recent academic years.  The handbook keeps prior-year
# offerings live (2020+) which would explode the harvest to ~2K stale
# URLs; only the previous + current + next year are real catalogue.
#
# Including the previous year (2025) is intentional: the coursehandbook
# sitemap has 205 entries for 2025 vs 177 for 2026 — many are the same
# programs with different year prefixes, but ~50-80 are unique courses
# only in 2025 (discontinued in 2026 or not yet re-published).  Since
# all handbook URLs resolve to year-agnostic admissions URLs at
# www.mq.edu.au/study/find-a-course/courses/<slug>, duplicates are
# deduplicated by the resolver so stale-year entries never double-count.
# Courses genuinely discontinued will either 404 (skipped) or show
# "not currently available" (rejected by guards).  Net effect: ~+50-80
# additional unique admissions URLs discovered per run.
import datetime as _dt
_THIS_YEAR = _dt.date.today().year
_COURSEHANDBOOK_YEARS: frozenset[str] = frozenset({
    str(_THIS_YEAR - 1), str(_THIS_YEAR), str(_THIS_YEAR + 1),
})


async def _discover_from_coursehandbook_sitemap(
    emit_fn,
    *,
    max_courses: int,
) -> list[dict]:
    """Harvest MQ course URLs from coursehandbook.mq.edu.au sitemaps.

    Uses the stealth context (patchright + xvfb) to bypass the
    Cloudflare challenge that fronts the handbook host for the sitemap
    XML files.  The per-course title resolver that follows uses plain
    httpx (coursehandbook.mq.edu.au course-detail pages are accessible
    without a browser; the sitemap index/child URLs are not reliably so).

    Returns ``[{"url": admissions_url, "name": course_name}, ...]``
    deduped, capped at *max_courses*, or ``[]`` on any failure (caller
    falls back to the widget sweep).
    """
    try:
        from app.services.scraper.stealth_browser import stealth_context
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: stealth_browser unavailable for "
            "coursehandbook sitemap — %s", exc,
        )
        return []

    await emit_fn(
        f"[DISCOVER] MQ: trying coursehandbook sitemap "
        f"(years={sorted(_COURSEHANDBOOK_YEARS)})"
    )

    course_urls: set[str] = set()
    try:
        async with stealth_context() as ctx:
            page = await ctx.new_page()

            # Step 1: fetch the sitemap index → list of child sitemap URLs
            try:
                await page.goto(
                    _COURSEHANDBOOK_SITEMAP_INDEX,
                    wait_until="domcontentloaded",
                    timeout=int(_COURSEHANDBOOK_SITEMAP_TIMEOUT_S * 1000),
                )
                index_body = await page.content()
            except Exception as exc:  # noqa: BLE001
                await emit_fn(
                    f"[DISCOVER] MQ: coursehandbook index unreachable "
                    f"({exc!r}); falling back to widget sweep"
                )
                return []

            child_sitemaps = re.findall(
                r"<loc>([^<]+\.xml)</loc>", index_body
            )
            if not child_sitemaps:
                await emit_fn(
                    "[DISCOVER] MQ: coursehandbook index returned no "
                    "child sitemaps; falling back to widget sweep"
                )
                return []

            await emit_fn(
                f"[DISCOVER] MQ: coursehandbook index → "
                f"{len(child_sitemaps)} child sitemap(s)"
            )

            # Step 2: walk each child sitemap, filter to target-year
            # /courses/CXXXX URLs.  Sitemap-1 + sitemap-3 each hold ~10K
            # URLs (units + aos + doubledegree dominate), so the filter
            # is strict and we exit early on max_courses.
            for child in child_sitemaps:
                if len(course_urls) >= max_courses:
                    break
                try:
                    await page.goto(
                        child,
                        wait_until="domcontentloaded",
                        timeout=int(_COURSEHANDBOOK_SITEMAP_TIMEOUT_S * 1000),
                    )
                    body = await page.content()
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_browser_discover: child sitemap %s failed: %s",
                        child, exc,
                    )
                    continue

                for loc in re.findall(r"<loc>([^<]+)</loc>", body):
                    m = _COURSEHANDBOOK_COURSE_RE.match(loc)
                    if m and m.group(1) in _COURSEHANDBOOK_YEARS:
                        course_urls.add(loc.rstrip("/"))
                        if len(course_urls) >= max_courses:
                            break
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: coursehandbook sitemap pass failed: %s",
            exc,
        )
        return []

    handbook_urls = sorted(course_urls)
    await emit_fn(
        f"[DISCOVER] MQ: coursehandbook sitemap harvested "
        f"{len(handbook_urls)} course URL(s); resolving to admissions URLs…"
    )

    # ── Resolve coursehandbook URLs → www.mq.edu.au admissions URLs ─────
    # Coursehandbook is the ACADEMIC catalogue (descriptions, learning
    # outcomes, credit points) and contains NO fee / IELTS / session /
    # campus data. The admissions pages at
    # www.mq.edu.au/study/find-a-course/courses/<slug> are where all the
    # student-facing data lives. We render each coursehandbook URL just
    # long enough to read its <title> tag (present in the SPA shell
    # static HTML — no body wait needed), slugify the name, and emit
    # the equivalent admissions URL. Verified live 2026-05-25.
    study_courses = await _resolve_to_study_urls(handbook_urls, emit_fn)
    await emit_fn(
        f"[DISCOVER] MQ: resolved {len(study_courses)}/{len(handbook_urls)} "
        f"coursehandbook URLs to admissions URLs"
    )
    return study_courses[:max_courses]


def _slugify_course_name(name: str) -> str:
    """Convert a course name to its www.mq.edu.au URL slug.

    Examples (verified against live admissions URLs):
      "Bachelor of Arts" → "bachelor-of-arts"
      "Bachelor of Game Design and Development"
        → "bachelor-of-game-design-and-development"
      "Master of Business Administration"
        → "master-of-business-administration"

    Strips the "| Macquarie University" page-title suffix first when
    present (some pages carry it, others don't), lowercases, replaces
    any non-alphanumeric run with a single hyphen, and trims leading/
    trailing hyphens.
    """
    if not name:
        return ""
    cleaned = _TITLE_SUFFIX_RE.sub("", name.strip())
    slug = _SLUG_NON_WORD_RE.sub("-", cleaned.lower()).strip("-")
    return slug


async def _resolve_to_study_urls(
    handbook_urls: list[str],
    emit_fn,
) -> list[dict]:
    """For each coursehandbook URL, extract the course name from <title>
    and construct the equivalent www.mq.edu.au admissions URL.

    coursehandbook.mq.edu.au is NOT Cloudflare-protected — plain httpx
    returns 200 OK with the correct per-course <title> in the static HTML
    (verified 2026-07-24: C000001 → "Bachelor of Biodiversity and
    Conservation", 208 KB SSR response, no CF challenge).  Using plain
    httpx instead of patchright is therefore both faster (~200 ms/request
    vs 10-15 s browser goto) and more reliable — the old patchright-based
    resolver timed out on ~256/383 courses because the 15 s per-page limit
    was exhausted by browser startup + 208 KB page load, silently dropping
    two-thirds of the catalogue.

    Runs 20 concurrent httpx requests (no rate-limit risk; coursehandbook
    is a lightweight CDN-backed SSR app).  Returns
    ``[{"url": admissions_url, "name": title}, ...]`` deduped on
    admissions URL.

    URLs whose <title> can't be parsed (empty / "Handbook" site-nav title
    / fetch error) are skipped with a warning rather than emitted with a
    bad slug.
    """
    if not handbook_urls:
        return []

    import httpx as _httpx

    _HTTPX_PARALLEL = 20
    _HTTPX_TIMEOUT = 20.0  # per-request; 208 KB SSR page ~200 ms normally

    out_by_url: dict[str, dict] = {}
    skipped = 0
    sem = asyncio.Semaphore(_HTTPX_PARALLEL)

    _HEADERS = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
    }

    # Track failure reasons for diagnostics.
    _fail_reasons: list[str] = []

    async def _resolve_one(client: "_httpx.AsyncClient", handbook_url: str) -> tuple[str, str] | None:
        async with sem:
            try:
                r = await client.get(handbook_url, timeout=_HTTPX_TIMEOUT)
            except Exception as exc:  # noqa: BLE001
                log.warning("mq resolver: httpx GET %s failed: %s", handbook_url, exc)
                _fail_reasons.append(f"network_error:{handbook_url.rsplit('/', 1)[-1]}")
                return None
            if r.status_code >= 400:
                log.warning("mq resolver: %s → HTTP %s", handbook_url, r.status_code)
                _fail_reasons.append(f"http_{r.status_code}:{handbook_url.rsplit('/', 1)[-1]}")
                return None
            body = r.text
        m = re.search(r"<title>([^<]+)</title>", body, re.I)
        if not m:
            _fail_reasons.append(f"no_title:{handbook_url.rsplit('/', 1)[-1]}")
            return None
        raw_title = m.group(1).strip()
        # "Handbook" is the fallback site-nav title when the SSR didn't
        # inject a per-course override; skip rather than emit
        # /courses/handbook as a garbage admissions URL.
        if not raw_title or raw_title.lower() in ("handbook", "macquarie university handbook"):
            _fail_reasons.append(f"generic_title:{handbook_url.rsplit('/', 1)[-1]}")
            return None
        # Strip delivery-mode suffixes (OUA, NMJI) before slugification.
        # The coursehandbook appends "(OUA)" / "(NMJI)" to course titles but
        # these are absent from the www.mq.edu.au admissions URL slug.
        # Without stripping, "Bachelor of Arts (OUA)" → dead slug
        # "bachelor-of-arts-oua" (404).  With stripping: "bachelor-of-arts".
        display_title = raw_title  # keep original for the "name" field
        slug_title = _DELIVERY_SUFFIX_RE.sub("", raw_title).strip()
        slug = _slugify_course_name(slug_title)
        if not slug or len(slug) < 3:
            _fail_reasons.append(f"bad_slug({raw_title[:30]}):{handbook_url.rsplit('/', 1)[-1]}")
            return None

        # ── Admissions URL construction (two strategies) ─────────────
        # Primary: look for a direct link to the admissions page embedded
        # in the coursehandbook HTML.  The handbook commonly carries an
        # "Apply" or "View this course" anchor pointing at www.mq.edu.au
        # with the correct path prefix (undergraduate/postgraduate/
        # research).  This is more reliable than title inference because
        # it handles combined degrees, specialist programmes, and any
        # course whose prefix is counter-intuitive.
        direct_match = _ADMISSIONS_URL_IN_PAGE_RE.search(body)
        if direct_match:
            raw = direct_match.group(0).split("?")[0].split("#")[0].rstrip("/")
            # Validate: must have at least one slug segment after the
            # level token so listing roots don't slip through.
            raw_path = raw[len("https://www.mq.edu.au"):]
            if _SEARCH_COURSE_LINK_RE.match(raw_path):
                return (raw, display_title)

        # Secondary: infer the URL path prefix from the course title.
        # Title inference is correct for 95 %+ of MQ courses:
        #   "Bachelor of *"  → /undergraduate/
        #   "Master of *"    → /postgraduate/
        #   "Doctor of *"    → /research/
        # and falls back to /courses/ for anything ambiguous.
        # This replaces the old always-/courses/ construction that caused
        # 79 / 198 (40 %) fetch_failed in the August 2026 production run.
        prefix = _infer_url_prefix(slug_title)
        admissions_url = f"{_STUDY_URL_ROOT}{prefix}/{slug}"
        return (admissions_url, display_title)

    try:
        async with _httpx.AsyncClient(
            headers=_HEADERS,
            follow_redirects=True,
        ) as client:
            tasks = [
                asyncio.create_task(_resolve_one(client, url))
                for url in handbook_urls
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                skipped += 1
            elif result is None:
                skipped += 1
            else:
                admissions_url, name = result
                if admissions_url not in out_by_url:
                    out_by_url[admissions_url] = {"url": admissions_url, "name": name}
    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: httpx resolver pass raised: %s", exc)

    if skipped:
        # Summarise failure reasons so operators can diagnose resolver gaps
        # without reading raw logs.  Group by reason prefix, show counts.
        reason_summary: dict[str, int] = {}
        for r in _fail_reasons:
            key = r.split(":")[0]
            reason_summary[key] = reason_summary.get(key, 0) + 1
        sample = "; ".join(f"{k}×{v}" for k, v in sorted(reason_summary.items()))
        await emit_fn(
            f"[DISCOVER] MQ: resolver skipped {skipped} URL(s) — "
            f"reasons: {sample or 'unknown'}"
        )
    return sorted(out_by_url.values(), key=lambda d: d["url"])


async def _discover_from_search_page(
    emit_fn,
    *,
    max_courses: int,
) -> list[dict]:
    """Harvest MQ course URLs from the /search page via stealth browser.

    MQ's search page (https://www.mq.edu.au/search?query=&category=courses)
    is backed by Squiz Matrix / Funnelback and shows every course in the
    catalogue.  As of August 2026 this includes 367 international courses
    — including research degrees and combined degrees that are absent from
    the coursehandbook sitemap.

    The page is Cloudflare Enterprise-protected (same as the rest of
    www.mq.edu.au); the stealth browser (patchright + Xvfb) passes it
    cleanly.

    Pagination strategy
    -------------------
    Squiz Matrix supports ``start_rank`` (1-based offset) and ``num_ranks``
    (results per page) as URL parameters.  We load pages of 100 at a time
    until either the page returns zero *new* course anchors (pagination
    exhausted or params ignored + wrap-around detected) or ``max_courses``
    is reached.  Hard cap: 10 pages × 100 = 1 000 >> 367, so runaway is
    impossible.

    Fallback: if URL-param pagination stalls after page 1 (same 10 results
    on every page), we attempt click-based "next page" navigation as a
    secondary approach before giving up.

    Returns ``[{"url": str, "name": str}, ...]`` deduped, or ``[]`` on
    any failure (caller's handbook + browser-sweep tiers still run).
    """
    try:
        from app.services.scraper.stealth_browser import stealth_context
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "mq_search_discover: stealth_browser unavailable — %s", exc,
        )
        return []

    await emit_fn(
        "[DISCOVER] MQ: search-page tier — paginating "
        "/search?query=&category=courses"
    )

    out: dict[str, dict] = {}
    _PER_PAGE = 100    # num_ranks per request; Squiz Matrix cap is typically 100
    _MAX_PAGES = 10    # hard cap: 10 × 100 = 1 000 > 367

    # JS: extract all anchors pointing at /study/find-a-course/ paths.
    _SEARCH_EXTRACT_JS = r"""
() => {
  const ORIGIN = 'https://www.mq.edu.au';
  const PREFIX = '/study/find-a-course/';
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    if (!raw) return;
    let url;
    try { url = new URL(raw, ORIGIN).href; } catch (_) { return; }
    if (!url.startsWith(ORIGIN + PREFIX)) return;
    const path = url.slice(ORIGIN.length);
    const text = (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim();
    out.push({ href: url, path, text });
  });
  return out;
}
"""

    # Selectors for a "Next page" or "Load more" button — tried in order if
    # the URL-param pagination stalls.
    _NEXT_PAGE_SELECTORS = (
        "a:has-text('Next')",
        "button:has-text('Next')",
        "a[rel='next']",
        "a:has-text('Load more')",
        "button:has-text('Load more')",
        "a:has-text('Show more')",
        "button:has-text('Show more')",
    )

    def _accept_search_url(href: str) -> str | None:
        """Return the clean course URL if *href* matches a real course page."""
        try:
            from urllib.parse import urlparse as _up
            p = _up(href).path
        except Exception:  # noqa: BLE001
            return None
        if not _SEARCH_COURSE_LINK_RE.match(p):
            return None
        # Drop query-string / fragment; normalise trailing slash.
        return f"https://www.mq.edu.au{p.rstrip('/')}"

    # Broader JS extractor — pulls ALL anchors from the page so we can
    # emit a diagnostic sample when course-specific extraction yields 0.
    _ALL_ANCHORS_JS = r"""
() => {
  const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const raw = (a.getAttribute('href') || '').trim();
    if (!raw || raw.startsWith('javascript:') || raw.startsWith('mailto:')
        || raw.startsWith('tel:') || raw.startsWith('#')) return;
    const text = (a.innerText || a.textContent || '')
      .replace(/\s+/g, ' ').trim().slice(0, 80);
    out.push({ href: raw, text });
  });
  return out;
}
"""

    try:
        async with stealth_context() as ctx:
            page = await ctx.new_page()

            # ── URL-param pagination loop ──────────────────────────────────
            url_pagination_stalled = False
            for page_num in range(_MAX_PAGES):
                start_rank = page_num * _PER_PAGE + 1
                search_url = (
                    f"https://www.mq.edu.au/search?query=&category=courses"
                    f"&start_rank={start_rank}&num_ranks={_PER_PAGE}"
                )
                try:
                    await page.goto(
                        search_url,
                        # networkidle waits until all in-flight XHR/fetch
                        # requests complete — Funnelback fires its search
                        # XHR AFTER domcontentloaded so domcontentloaded
                        # alone leaves the result DOM empty every time.
                        wait_until="networkidle",
                        timeout=50_000,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_search_discover: goto page %d failed: %s",
                        page_num + 1, exc,
                    )
                    break

                # Belt-and-suspenders: wait for a course-level anchor to
                # appear (not just any /study/find-a-course/ nav link).
                # Funnelback result anchors include an explicit level token
                # after the prefix (undergraduate|postgraduate|research|
                # courses), which navigation links do NOT — so this selector
                # fires only after actual search results render.
                selector_found = False
                _RESULT_SELECTOR = (
                    "a[href*='/study/find-a-course/undergraduate/'], "
                    "a[href*='/study/find-a-course/postgraduate/'], "
                    "a[href*='/study/find-a-course/research/'], "
                    "a[href*='/study/find-a-course/courses/']"
                )
                try:
                    await page.wait_for_selector(
                        _RESULT_SELECTOR,
                        timeout=15_000,
                    )
                    selector_found = True
                except Exception:  # noqa: BLE001
                    pass  # proceed with whatever is in the DOM

                # Short settle for any lazy-render after the selector fires.
                settle_s = 2.0 if page_num == 0 else 1.5
                await asyncio.sleep(settle_s)

                anchors = await page.evaluate(_SEARCH_EXTRACT_JS) or []
                added_this_page = 0
                for a in anchors:
                    clean = _accept_search_url(a.get("href", ""))
                    if not clean or clean in out:
                        continue
                    out[clean] = {
                        "url": clean,
                        "name": (a.get("text") or "").strip(),
                    }
                    added_this_page += 1
                    if len(out) >= max_courses:
                        break

                await emit_fn(
                    f"[DISCOVER] MQ: search page {page_num + 1} "
                    f"(start_rank={start_rank}) → "
                    f"+{added_this_page} new (total {len(out)}) "
                    f"[selector_found={selector_found}]"
                )

                if added_this_page == 0:
                    # Emit a diagnostic sample so the operator can see
                    # what links the search page actually contains, which
                    # helps debug when Funnelback changes its URL shape or
                    # the CF bot check silently serves a shell page.
                    try:
                        all_a = await page.evaluate(_ALL_ANCHORS_JS) or []
                        study_hrefs = [
                            a["href"] for a in all_a
                            if "/study/" in a.get("href", "")
                            or "/find-a-course" in a.get("href", "")
                        ][:10]
                        sample_hrefs = [a["href"] for a in all_a[:8]]
                        await emit_fn(
                            f"[DISCOVER] MQ: search page {page_num + 1} "
                            f"diagnostic — total_anchors={len(all_a)}, "
                            f"study_hrefs={study_hrefs or sample_hrefs}"
                        )
                    except Exception:  # noqa: BLE001
                        pass

                    # Either pagination exhausted naturally, or start_rank is
                    # ignored and the page always shows the same first batch.
                    # Detect the latter: if we harvested ≥ 5 courses on page 1
                    # but 0 on page 2, params are probably ignored — fall back
                    # to click-based next-page navigation.
                    if page_num == 1 and len(out) >= 5:
                        url_pagination_stalled = True
                    break

                if len(out) >= max_courses:
                    break

            # ── Click-based "next page" fallback ──────────────────────────
            # Only runs when URL-param pagination stalled after page 1, which
            # means the search page ignores start_rank and we already have the
            # first batch in ``out``.  Navigate back to page 1 and click "Next"
            # repeatedly.
            if url_pagination_stalled:
                await emit_fn(
                    "[DISCOVER] MQ: start_rank ignored — switching to "
                    "click-based next-page navigation"
                )
                # Start fresh on page 1 to get the pagination widget rendered.
                try:
                    await page.goto(
                        "https://www.mq.edu.au/search?query=&category=courses"
                        f"&num_ranks={_PER_PAGE}",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    await asyncio.sleep(3.0)
                except Exception as exc:  # noqa: BLE001
                    log.warning("mq_search_discover: fallback goto failed: %s", exc)

                for _click_page in range(_MAX_PAGES):
                    anchors = await page.evaluate(_SEARCH_EXTRACT_JS) or []
                    added = 0
                    for a in anchors:
                        clean = _accept_search_url(a.get("href", ""))
                        if not clean or clean in out:
                            continue
                        out[clean] = {
                            "url": clean,
                            "name": (a.get("text") or "").strip(),
                        }
                        added += 1
                        if len(out) >= max_courses:
                            break

                    await emit_fn(
                        f"[DISCOVER] MQ: search click-page {_click_page + 1} "
                        f"→ +{added} new (total {len(out)})"
                    )

                    if len(out) >= max_courses:
                        break

                    # Try to click a "Next" button to load the next page.
                    clicked = False
                    for sel in _NEXT_PAGE_SELECTORS:
                        try:
                            loc = page.locator(sel).first
                            if await loc.count() == 0 or not await loc.is_visible():
                                continue
                            await loc.click(timeout=4_000)
                            await asyncio.sleep(3.0)
                            clicked = True
                            break
                        except Exception:  # noqa: BLE001
                            continue

                    if not clicked:
                        await emit_fn(
                            "[DISCOVER] MQ: no next-page button found — "
                            "click pagination complete"
                        )
                        break

            try:
                await page.close()
            except Exception:  # noqa: BLE001
                pass

    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: search-page harvest raised: %s", exc)

    results = sorted(out.values(), key=lambda d: d["url"])
    await emit_fn(
        f"[DISCOVER] MQ: search-page harvest complete — "
        f"{len(results)} unique course URL(s)"
    )
    return results[:max_courses]


def _is_mq_course_url(url: str) -> bool:
    """Return True when *url* is a Macquarie course detail page.

    Pure-Python mirror of the path filter applied to the JS-harvested
    anchors; exposed so the unit tests can assert behaviour without
    spinning up a browser.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except Exception:
        return False

    host = (parsed.hostname or "").lower()
    if host != "www.mq.edu.au" and host != "mq.edu.au":
        return False

    path = parsed.path or ""
    # Strip query string + fragment for matching.
    if not _COURSE_PATH_RE.match(path):
        return False

    # Block sub-degree pages (majors / specialisations).
    lowered = path.lower()
    if any(sub in lowered for sub in _BLOCKED_PATH_SUBSTRINGS):
        return False

    # Block listing-root last segments (combined-bachelor-master-degrees etc.).
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last in _LISTING_LAST_SEGMENTS:
        return False

    return True


def filter_mq_course_anchors(
    anchors: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Apply ``_is_mq_course_url`` + dedup to a list of ``{href, text}``.

    Exposed for the unit tests so the URL filter can be exercised against
    real captured anchor fixtures without a live browser.
    """
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for entry in anchors:
        raw = (entry.get("href") or "").strip()
        if not raw:
            continue
        # Resolve same-origin relative paths (the in-browser JS does this via
        # `new URL(href, origin)`; mirror it here so the helper can be unit
        # tested against raw anchor dicts).
        if raw.startswith("/") and not raw.startswith("//"):
            raw = _MQ_ORIGIN + raw
        url = raw.split("#")[0].split("?")[0]
        if not _is_mq_course_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append({
            "url": url,
            "name": (entry.get("text") or "").strip(),
        })
    return out


async def _interactive_filter_harvest(
    *,
    page,
    seed: str,
    merged: dict[str, dict],
    emit,
) -> int:
    """Try to coax SPA catalogue pages into rendering course anchors.

    Defensive: every selector is wrapped in try/except so a missing
    button never aborts the sweep.  Returns the number of NEW course
    URLs added to ``merged`` (zero if nothing rendered).

    Strategy:
      1. For each selector in ``_FILTER_CLICK_SELECTORS``, click the
         first visible match (best-effort).
      2. After each click, settle + re-extract anchors.
      3. Stop early as soon as any click yields >= 5 new course URLs
         (heuristic — a populated result list will overshoot this on
         the first click; a still-empty SPA will yield 0 every time).
    """
    added_total = 0
    for selector in _FILTER_CLICK_SELECTORS:
        try:
            locator = page.locator(selector).first
            if await locator.count() == 0:
                continue
            if not await locator.is_visible():
                continue
        except Exception:  # noqa: BLE001
            continue

        try:
            await locator.click(timeout=3_000)
        except Exception:  # noqa: BLE001
            continue

        try:
            await emit(
                f"[DISCOVER] MQ: interactive click on {seed} → {selector!r}"
            )
        except Exception:  # noqa: BLE001
            pass

        await asyncio.sleep(_SCROLL_SETTLE_S)
        # Trigger a scroll to provoke lazy load after the filter populates.
        try:
            await page.evaluate(
                "window.scrollTo(0, document.body.scrollHeight)"
            )
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(_SCROLL_SETTLE_S)

        try:
            anchors = await page.evaluate(_EXTRACT_ANCHORS_JS)
        except Exception:  # noqa: BLE001
            anchors = []

        kept = filter_mq_course_anchors(anchors or [])
        added_this_click = 0
        for item in kept:
            if item["url"] not in merged:
                merged[item["url"]] = item
                added_this_click += 1
            if len(merged) >= _HARD_MAX_LINKS:
                break

        added_total += added_this_click
        if added_this_click >= 5:
            # Filter populated the result grid — no need to try more
            # selectors on this seed.
            break

    return added_total


async def browser_discover_mq(
    emit=None,
    *,
    max_courses: int = 500,
) -> list[dict]:
    """Discover Macquarie course URLs via Playwright across all catalogue seeds.

    Returns a list of ``{"url": str, "name": str}`` dicts (one per
    discovered MQ course URL).  Returns ``[]`` only when the browser
    pool is unavailable OR every seed fails to harvest a single link
    (so the caller can fall back to BFS / generic browser / Wayback).

    Partial harvests below ``_DISCOVERY_FLOOR`` (150) are **returned as
    is** rather than discarded — on Cloudflare-walled MQ the downstream
    BFS (403) and generic browser (URL-shape miss) tiers would only
    drop the partial result and stage zero courses.  Operators are
    notified of below-floor harvests via the
    ``discovery_failure_alerts`` row that ``orchestrator.py`` persists
    immediately after this function returns.

    The function is intentionally tolerant: if seed N fails or returns
    nothing, seeds N+1..K are still attempted.  All successful harvests
    are merged and deduped.
    """

    async def _emit(msg: str, **kw) -> None:
        if emit is None:
            return
        try:
            await emit(
                "status", msg, phase="discover",
                kind="mq_browser_discover", **kw,
            )
        except Exception:  # noqa: BLE001
            pass

    try:
        from app.services.scraper.browser_pool import pool as _pool
        from playwright.async_api import TimeoutError as _PwTimeout
    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: browser pool unavailable — %s", exc)
        return []

    # ── Tier 0: Funnelback JSON API (fastest, most complete) ────────────
    # Single HTTP request to squiz.cloud returns all 338+ courses with
    # real admissions URLs (no resolver needed) + metaData (studyLevel,
    # courseDuration).  Concurrent page-data.json fetches add fees, IELTS,
    # description.  Returns scrapy_result-shaped links so per-course HTML
    # extraction is bypassed entirely.
    #
    # If this tier returns a full catalogue (≥ 300 courses), skip the
    # expensive Tier 1 (coursehandbook sitemap + stealth browser resolver),
    # Tier 1.5 (search-page browser harvest), and Tier 2 (faculty BFS sweep).
    try:
        fb_links = await _discover_from_funnelback_api(
            _emit, max_courses=max_courses,
        )
    except Exception as _fb_exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: Funnelback API tier raised: %s", _fb_exc,
        )
        fb_links = []

    if len(fb_links) >= 300:
        await _emit(
            f"[DISCOVER] MQ: Tier 0 success — {len(fb_links)} courses from "
            "Funnelback API; skipping coursehandbook + browser tiers"
        )
        return fb_links[:max_courses]

    if fb_links:
        await _emit(
            f"[DISCOVER] MQ: Tier 0 partial — {len(fb_links)} courses from "
            "Funnelback API; supplementing with coursehandbook + search tiers"
        )
    else:
        await _emit(
            "[DISCOVER] MQ: Tier 0 returned 0 — falling through to "
            "coursehandbook sitemap"
        )

    # ── Tier 1: coursehandbook sitemap (real catalogue host) ────────
    # Try the structured handbook sitemap FIRST.  The www.mq.edu.au SPA
    # widget sweep below is fragile (Svelte mount, filter UI selectors,
    # CF challenges) and only yields anchors on a small subset of pages.
    # The handbook sitemap is a static XML index that returns the full
    # current-year course list deterministically when reachable.
    try:
        ch_links = await _discover_from_coursehandbook_sitemap(
            _emit, max_courses=max_courses,
        )
    except Exception as _ch_exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: coursehandbook sitemap raised: %s",
            _ch_exc,
        )
        ch_links = []
    # Seed the merged dict with Funnelback + handbook results so BFS
    # additions below de-duplicate on admissions URL.
    # Funnelback links carry scrapy_result; handbook links are URL-only.
    # setdefault means Funnelback's richer form wins over handbook duplicates.
    merged: dict[str, dict] = {d["url"]: d for d in fb_links}
    for d in ch_links:
        merged.setdefault(d["url"], d)

    # ── Tier 1.5: /search page harvest ──────────────────────────────
    # MQ's search page (https://www.mq.edu.au/search?query=&category=courses)
    # indexes the complete catalogue — 367 international courses as of
    # August 2026 — including research degrees and combined degrees that
    # are absent from the coursehandbook sitemap.
    #
    # The coursehandbook resolver is the RIGHT source for UG/PG courses
    # (it resolves handbook → admissions URLs reliably); the search page
    # is the ONLY reliable source for research degrees (Doctor of
    # Philosophy, Professional Doctorates, etc.) whose URLs are at
    # ``/study/find-a-course/research/<slug>`` — a path the handbook
    # resolver never constructs (it always uses ``/courses/``).
    #
    # Run this tier even when the handbook succeeds so both sources
    # contribute to the merged set.  Dedup on admissions URL is free.
    try:
        sp_links = await _discover_from_search_page(
            _emit, max_courses=max_courses,
        )
    except Exception as _sp_exc:  # noqa: BLE001
        log.warning(
            "mq_browser_discover: search-page tier raised: %s", _sp_exc,
        )
        sp_links = []

    for d in sp_links:
        merged.setdefault(d["url"], d)

    await _emit(
        f"[DISCOVER] MQ: after handbook + search-page tiers: "
        f"{len(merged)} unique course URL(s)"
    )

    # If the two structured tiers already produced a full catalogue, skip
    # the expensive browser sweep.  The threshold is 450 (well above the
    # 367 target) to allow for a small future catalogue growth before
    # the sweep is wrongly skipped.
    if len(merged) >= 450:
        await _emit(
            f"[DISCOVER] MQ: {len(merged)} courses from handbook + search — "
            f"skipping browser sweep (catalogue is complete)"
        )
        return sorted(merged.values(), key=lambda d: d["url"])[:max_courses]

    await _emit(
        f"[DISCOVER] MQ: starting browser sweep across {len(_SEED_URLS)} "
        f"catalogue seed(s) to supplement {len(merged)} handbook+search courses"
    )

    try:
        async with _pool.page() as page:
            await page.set_extra_http_headers(
                {"Referer": "https://www.google.com/"}
            )

            for seed in _SEED_URLS:
                await _emit(f"[DISCOVER] MQ: seed → {seed}")
                # ── Navigate ────────────────────────────────────────────
                try:
                    await page.goto(seed, wait_until="networkidle",
                                    timeout=60_000)
                except _PwTimeout:
                    log.warning(
                        "mq_browser_discover: goto networkidle timed out on %s "
                        "— continuing with partial DOM", seed,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_browser_discover: goto failed on %s — %s",
                        seed, exc,
                    )
                    await _emit(
                        f"[DISCOVER] MQ: seed {seed} navigation failed ({exc})"
                    )
                    continue

                # Error-page sniff (Chromium interstitials).
                try:
                    partial = await asyncio.wait_for(
                        page.content(), timeout=5.0,
                    )
                    lowered = (partial or "")[:4096].lower()
                    if (
                        "neterror" in lowered
                        or "chrome-error://" in lowered
                        or "err_name_not_resolved" in lowered
                        or "err_connection_" in lowered
                        or "err_cert_" in lowered
                    ):
                        log.warning(
                            "mq_browser_discover: Chromium error page on %s",
                            seed,
                        )
                        await _emit(
                            f"[DISCOVER] MQ: Chromium error page on {seed}"
                        )
                        continue
                except Exception:  # noqa: BLE001
                    pass

                # ── Wait for hydration (course-anchor selector) ─────────
                try:
                    await page.wait_for_selector(
                        _HYDRATE_WAIT_SELECTOR,
                        timeout=_HYDRATE_WAIT_MS,
                    )
                except _PwTimeout:
                    log.info(
                        "mq_browser_discover: hydration selector not seen on "
                        "%s within %dms — extracting whatever is in the DOM",
                        seed, _HYDRATE_WAIT_MS,
                    )
                except Exception:  # noqa: BLE001
                    pass

                await asyncio.sleep(_INITIAL_SETTLE_S)

                # ── Scroll loop to trigger lazy load ────────────────────
                prev_count = -1
                stall_streak = 0
                for it in range(_MAX_SCROLL_ITERS):
                    try:
                        await page.evaluate(
                            "window.scrollTo(0, document.body.scrollHeight)"
                        )
                    except Exception:  # noqa: BLE001
                        break
                    await asyncio.sleep(_SCROLL_SETTLE_S)
                    try:
                        anchors = await page.evaluate(_EXTRACT_ANCHORS_JS)
                    except Exception:  # noqa: BLE001
                        anchors = []
                    current = len(filter_mq_course_anchors(anchors or []))
                    if current == prev_count:
                        stall_streak += 1
                        if stall_streak >= 2:
                            break
                    else:
                        stall_streak = 0
                        if it == 0 or current - prev_count >= 10:
                            await _emit(
                                f"[DISCOVER] MQ: {seed} scroll iter "
                                f"{it + 1} → {current} course link(s)"
                            )
                    prev_count = current
                    if current >= _HARD_MAX_LINKS:
                        break

                # ── Final extract for this seed ─────────────────────────
                try:
                    anchors = await page.evaluate(_EXTRACT_ANCHORS_JS)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "mq_browser_discover: final extract failed on %s — %s",
                        seed, exc,
                    )
                    anchors = []

                kept = filter_mq_course_anchors(anchors or [])
                added = 0
                for item in kept:
                    if item["url"] not in merged:
                        merged[item["url"]] = item
                        added += 1
                    if len(merged) >= _HARD_MAX_LINKS:
                        break
                await _emit(
                    f"[DISCOVER] MQ: seed {seed} contributed +{added} "
                    f"new course(s) (total now {len(merged)})"
                )

                # ── 0-anchor diagnostics ────────────────────────────────
                # If a seed returned 0 course-shape anchors, dump page
                # title + raw-anchor count so the operator can tell whether
                # the page is a Cloudflare shell, a pre-hydration SPA, or
                # a genuinely empty catalogue page (so the next debug
                # iteration knows what to fix instead of guessing).
                if added == 0:
                    try:
                        title = await page.title()
                    except Exception:  # noqa: BLE001
                        title = "?"
                    raw_count = len(anchors or [])
                    await _emit(
                        f"[DISCOVER] MQ: seed {seed} yielded 0 course "
                        f"anchors — page title={title!r}, total <a> "
                        f"tags on DOM={raw_count}"
                    )

                # ── Interactive filter fallback for catalogue seeds ─────
                # Catalogue landing pages (/study/find-a-course[/level])
                # are SPA search shells.  If we got 0 anchors after the
                # passive scroll loop, try clicking the known filter
                # buttons to coax the SPA into rendering results.  Skip
                # for faculty pages which are plain HTML and either work
                # or genuinely have no courses.
                if added == 0 and seed in _CATALOGUE_SEED_URLS:
                    interactive_added = await _interactive_filter_harvest(
                        page=page, seed=seed, merged=merged, emit=_emit,
                    )
                    if interactive_added:
                        await _emit(
                            f"[DISCOVER] MQ: interactive filter rescue on "
                            f"{seed} → +{interactive_added} course(s) "
                            f"(total now {len(merged)})"
                        )
                if len(merged) >= _HARD_MAX_LINKS:
                    break

    except Exception as exc:  # noqa: BLE001
        log.warning("mq_browser_discover: unexpected error — %s", exc)
        await _emit(f"[DISCOVER] MQ: browser discovery error — {exc}")
        return list(merged.values())[:max_courses]

    out = list(merged.values())

    # ── Discovery floor warning ────────────────────────────────────────
    if len(out) < _DISCOVERY_FLOOR:
        log.warning(
            "mq_browser_discover: only %d course URL(s) discovered (floor=%d) "
            "— Cloudflare challenge or catalogue regression",
            len(out), _DISCOVERY_FLOOR,
        )
        await _emit(
            f"[DISCOVER] MQ: WARNING — only {len(out)} course URL(s) found "
            f"(expected ≥{_DISCOVERY_FLOOR}); possible Cloudflare challenge "
            "or catalogue regression",
        )

    # Don't return [] for partial harvests (1-2 links): on Macquarie the
    # downstream fallbacks (BFS → 403, generic browser → URL-shape miss)
    # would BOTH discard the partial result and stage zero courses.  Better
    # to return what we have and let the downstream alert layer flag the
    # low count (handled by the `discovery_failure_alerts` table when the
    # final candidate stream is < 3).
    if not out:
        log.warning(
            "mq_browser_discover: harvested 0 links — caller will fall "
            "back to generic browser / Wayback",
        )
        return []

    log.info(
        "mq_browser_discover: discovered %d course URL(s) across %d seed(s)",
        len(out), len(_SEED_URLS),
    )
    await _emit(
        f"[DISCOVER] MQ: total {len(out)} unique course URL(s) discovered"
    )
    return out[:max_courses]
