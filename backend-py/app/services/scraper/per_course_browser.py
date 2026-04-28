"""Per-course browser fallback (T207).

When the HTTP fetcher returns HTML for a course requirements page but the
page is JavaScript-rendered (Akamai/Cloudflare gate, React SPA, accordions
that load via XHR on click), the english-test extractor sees an empty
table and emits no IELTS/PTE/TOEFL/CAE values. Node's scraper handles this
by re-fetching the same URL through Playwright when the cheerio extractor
returns nothing useful, then re-running the extractor against the rendered
HTML — see ``routes/scrape.ts:11243`` (``perCourseBrowserFallback``).

This module is the Python port of that hook. Public entry-point:
:func:`maybe_browser_refetch`. It only activates when *all* english-test
slots are empty (so we never spend a browser slot on a page that already
parsed cleanly), and it merges using first-write-wins so any non-empty
slot the original extractor populated wins over the browser pass.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

from app.services.scraper.browser_pool import pool as browser_pool
from app.services.scraper.extractors import (
    duration,
    english_test,
    fee,
    intake,
    location,
    study_mode,
)
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.extractors._text import compact, html_to_text

# T005: hosts where the per-course browser pass should also click the
# "International students" toggle to surface the international fees /
# admissions panel. Add new hosts here as we encounter them.
_INTERNATIONAL_TOGGLE_HOSTS = (
    "vit.edu.au",
    # Murdoch: "What type of student are you?" Domestic | International toggle.
    # Without clicking International the rendered HTML shows domestic fees only
    # (hides Full course fee, IELTS requirements, intake dates).
    "murdoch.edu.au",
)


def _needs_international_toggle(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        return False
    return any(host.endswith(h) for h in _INTERNATIONAL_TOGGLE_HOSTS)

log = logging.getLogger(__name__)

# PR-5 Bug 3: per-host browser config (wait_until, settle, outer ceiling,
# inner goto timeout). PR-1.5 made `networkidle` + 60s budget the
# universal default to fix VIT's SPA hydration (the english <table>
# rendered via XHR after DCL fired, so the cheap path saw an empty
# skeleton). But ASA / Torrens / similar marketing sites embed
# long-poll widgets (Intercom, Hotjar, GA stream) that prevent the
# network from EVER going idle, so every per-course browser hit on
# those hosts ate the full 60s budget and timed out (prod sweeps
# job_8af4a... ASA 9/9 timeouts, Torrens 22/22 timeouts). Allow-list
# networkidle to SPAs that need it; default everyone else to fast
# `domcontentloaded` with a tight 20s outer ceiling. Add new hosts
# here when a regression sweep proves they need the slow path.
# Issue 1: VIT /vocational/* pages embed a heavy third-party widget that
# prevents networkidle from ever firing, causing every vocational URL to
# sit for the full 30s outer ceiling (10 courses × 30s = 5min wall-time).
# The VIT static fallback (vit_static_extract.py) rescues duration /
# intakes / location from the same static HTML so the end result is fine
# — we skip the browser pass entirely for these paths rather than wasting
# the budget on a guaranteed timeout.
_SKIP_BROWSER_PATH_PREFIXES: dict[str, tuple[str, ...]] = {
    "vit.edu.au": ("/vocational/",),
}

# Hosts for which the browser is ALWAYS skipped because a dedicated static
# extractor handles the full field set (no path restrictions needed).
# CSU: 1.3MB SSR pages already contain fees / IELTS / duration / intakes as
# embedded JS variables.  The browser was causing rate-limiting at concurrency
# ≥5 and never produced better data than the static extractor.
_SKIP_BROWSER_HOSTS: tuple[str, ...] = (
    "study.csu.edu.au",
)


def _skip_browser_for_url(url: str) -> bool:
    """Return True for URLs where a host-specific static fallback is
    sufficient and the browser pass is known to always time out."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        path = (parsed.path or "").lower()
    except Exception:
        return False
    # Whole-host skip (e.g. CSU static extractor covers all paths)
    if any(host == h or host.endswith("." + h) for h in _SKIP_BROWSER_HOSTS):
        return True
    for h, prefixes in _SKIP_BROWSER_PATH_PREFIXES.items():
        if host == h or host.endswith("." + h):
            if any(path.startswith(p) for p in prefixes):
                return True
    return False


_NETWORKIDLE_HOSTS: tuple[str, ...] = (
    # VIT: SPA that requires JS rendering + 3s settle to surface the
    # International tab's fee and english-requirements sections.
    "vit.edu.au",
    # Murdoch: heavy React SPA (450KB static → 1MB rendered). Must wait for
    # networkidle before the International toggle click fires correctly, otherwise
    # the toggle target element hasn't mounted yet.
    "murdoch.edu.au",
    # UOW: course details (fee, IELTS, intakes, campus) are loaded via XHR
    # into accordion panels after initial page load. domcontentloaded misses
    # all of it; networkidle + 3s settle is required.
    "uow.edu.au",
    # UniSQ: course detail panels (fees, entry requirements, study modes) are
    # React-rendered after page load. Need networkidle to catch the XHR content.
    "unisq.edu.au",
)

# Hosts that need the full 60s / networkidle treatment.
# These are sites that are either genuinely slow from our DigitalOcean
# IP, publish critical data via images (ASA), or use heavy React/Drupal
# frontends that take >30s to reach idle (KBS, CSU).
#
# ASA  — English requirements are image-only; vision OCR can't fire
#         unless the browser fully loads each page.
# KBS  — Drupal-rendered pages take >20s; without rendered HTML
#         Gemini-primary sees only the React shell.
# CSU  — React SPA with 800KB-1.3MB pages; static HTML is a 39-byte
#         shell, so every extractor gets nothing without a full render.
_SLOW_HOSTS: tuple[str, ...] = (
    "asahe.edu.au",
    "kbs.edu.au",
    "study.csu.edu.au",
)

# Hosts whose static HTML contains misleading site-wide IELTS/English
# statements that cause the browser pass to be skipped too early (the
# generic value gets extracted from the static page, marking the slot
# as "populated", so the per-course Entry Requirements tab — which is
# JS-rendered — is never fetched).  For these hosts we ALWAYS run the
# Playwright browser and allow its English-test result to OVERRIDE the
# static value (higher-specificity course page wins over generic footer
# text).  Federation is the canonical example: its static HTML contains
# "minimum IELTS 6.0" in a site-wide section, but course-specific pages
# can require 7.0 or higher.
_FORCE_BROWSER_HOSTS: tuple[str, ...] = (
    "federation.edu.au",
    "une.edu.au",
    # UOW and UniSQ: fee + IELTS data is served via JS-rendered components
    # (accordion panels, dynamic tab content). Static HTML contains only a
    # skeleton and a few meta tags. We ALWAYS render these hosts via Playwright
    # so that fee.extract / english_test.extract / intake.extract etc. see the
    # fully hydrated DOM. The "override" flag lets rendered values overwrite any
    # misleading fragment the static fetcher may have picked up.
    "uow.edu.au",
    "unisq.edu.au",
)

_NETWORKIDLE_SETTLE_MS = 3000
_DEFAULT_SETTLE_MS = 1500
# Outer ceilings keep a single hung page from wedging the Celery worker
# (prod incident: job_2dc0ba6bf4c9 sat at 0/10 for 32min, zero log
# output).
# _SLOW_HOSTS get 60s outer / 50s goto / networkidle.
# _NETWORKIDLE_HOSTS (VIT) get 30s outer / 25s goto / networkidle.
# Default raised to 60s outer / 50s goto after KBS/ASA/CSU all proved
# that the previous 20s ceiling was too aggressive for real education
# sites on our DigitalOcean IP.
_SLOW_OUTER_TIMEOUT_SEC = 60
_SLOW_GOTO_TIMEOUT_MS = 50_000
_NETWORKIDLE_OUTER_TIMEOUT_SEC = 30
_NETWORKIDLE_GOTO_TIMEOUT_MS = 25_000
_DEFAULT_OUTER_TIMEOUT_SEC = 60
_DEFAULT_GOTO_TIMEOUT_MS = 50_000


def _browser_config_for(url: str) -> tuple[str, int, int, int]:
    """Return (wait_until, settle_ms, outer_timeout_sec, goto_timeout_ms)
    for the given URL.

    * Hosts in :data:`_SLOW_HOSTS` (ASA, KBS, CSU) get ``networkidle``
      + 3s settle, 60s outer ceiling, 50s goto.
    * Hosts in :data:`_NETWORKIDLE_HOSTS` (VIT) get ``networkidle`` + 3s
      settle, 30s outer ceiling, 25s goto.
    * Everyone else gets ``domcontentloaded`` + 1.5s settle, 60s outer
      ceiling, 50s goto.  The default was raised from 20s after multiple
      Australian education sites proved too slow for the old ceiling.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    if any(host == h or host.endswith("." + h) for h in _SLOW_HOSTS):
        return (
            "networkidle",
            _NETWORKIDLE_SETTLE_MS,
            _SLOW_OUTER_TIMEOUT_SEC,
            _SLOW_GOTO_TIMEOUT_MS,
        )
    if any(host == h or host.endswith("." + h) for h in _NETWORKIDLE_HOSTS):
        return (
            "networkidle",
            _NETWORKIDLE_SETTLE_MS,
            _NETWORKIDLE_OUTER_TIMEOUT_SEC,
            _NETWORKIDLE_GOTO_TIMEOUT_MS,
        )
    return (
        "domcontentloaded",
        _DEFAULT_SETTLE_MS,
        _DEFAULT_OUTER_TIMEOUT_SEC,
        _DEFAULT_GOTO_TIMEOUT_MS,
    )


# The four slot keys we care about — IELTS overall, PTE overall, TOEFL
# overall, Cambridge Advanced English overall. If any of these are
# already populated we skip the browser pass entirely (the page DID
# render server-side, the extractor just failed on a different field).
_ENGLISH_SLOTS = (
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
)


def _all_english_empty(payload: dict[str, Any]) -> bool:
    """Return True when no english-test value has been extracted yet."""
    return all(payload.get(k) in (None, "", 0) for k in _ENGLISH_SLOTS)


# Hosts for which the browser pass should run the FULL extractor suite
# (fee + intake + duration + location + study_mode + english_test) on the
# rendered HTML, not just english_test.  Without this, fee.extract never
# sees the JS-rendered accordion content and always returns empty for UOW/UniSQ.
_EXTENDED_EXTRACT_HOSTS: frozenset[str] = frozenset({
    "uow.edu.au",
    "www.uow.edu.au",
    "unisq.edu.au",
    "www.unisq.edu.au",
})

# Field slots that each extended extractor fills — used to guard against
# overwriting a previously-populated value from the static pass.
_EXTENDED_SLOTS: tuple[str, ...] = (
    "international_fee",
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "intake_months",
    "duration_text",
    "location_text",
    "study_mode",
)

# Keywords used by the rendered-DOM debug sampler.  When a critical field is
# still missing after the full browser extraction we log the 300-char window
# around each keyword so devs can verify the data is/isn't in the rendered DOM.
_DEBUG_KEYWORDS: tuple[str, ...] = (
    "fee",
    "tuition",
    "international",
    "IELTS",
    "English",
    "requirements",
    "ATAR",
    "duration",
    "campus",
    "session",
)


def _rendered_dom_debug(
    rendered: str, url: str, field: str, host: str
) -> dict[str, Any]:
    """Return a structured debug record with 300-char windows around each
    ``_DEBUG_KEYWORDS`` keyword in the rendered text.  Used when a critical
    field is still empty after the full browser extraction pass so that devs
    can distinguish "data not in DOM" from "extractor regex miss"."""
    text = compact(html_to_text(rendered)) if rendered else ""
    snippets: dict[str, str] = {}
    text_lc = text.lower()
    for kw in _DEBUG_KEYWORDS:
        idx = text_lc.find(kw.lower())
        if idx == -1:
            snippets[kw] = "[not found in rendered text]"
        else:
            start = max(0, idx - 120)
            end = min(len(text), idx + 180)
            snippets[kw] = text[start:end].replace("\n", " ")
    return {
        "provider": host,
        "course_url": url,
        "field_name": field,
        "static_text_found": False,
        "rendered_text_found": any(v != "[not found in rendered text]" for v in snippets.values()),
        "rendered_size_chars": len(text),
        "keyword_windows": snippets,
    }


async def _extended_extract(
    rendered: str,
    url: str,
    existing_payload: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run ALL field extractors against rendered HTML and return only the
    slots that are still missing from ``existing_payload``.

    This is called for hosts in :data:`_EXTENDED_EXTRACT_HOSTS` (UOW, UniSQ)
    after the browser has obtained a fully JS-rendered page. The function never
    overwrites a slot that already has a truthy value in ``existing_payload``
    so that a correct static extraction always wins over a lower-confidence
    browser extraction.
    """
    extractors = [
        (fee, ["international_fee", "fee_currency", "fee_term", "fee_year"]),
        (english_test, list(_ENGLISH_SLOTS)),
        (intake, ["intake_months"]),
        (duration, ["duration_text"]),
        (location, ["location_text"]),
        (study_mode, ["study_mode"]),
    ]
    filled: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []

    for extractor_mod, slot_keys in extractors:
        # Skip if ALL slots for this extractor are already populated
        if all(
            existing_payload.get(k) not in (None, "", 0, [])
            for k in slot_keys
        ):
            continue
        try:
            results: list[ExtractionResult] = await extractor_mod.extract(rendered, url)
        except Exception as exc:  # noqa: BLE001 — never abort on extractor failure
            log.warning("extended_extract: %s failed on rendered %s: %s",
                        extractor_mod.__name__, url, exc)
            continue
        for r in results:
            if not r.normalized:
                continue
            for k, v in r.normalized.items():
                if v in (None, "", 0, []):
                    continue
                if k not in _EXTENDED_SLOTS:
                    continue
                # Only fill slots that are still empty in existing_payload
                if existing_payload.get(k) not in (None, "", 0, []):
                    continue
                if k in filled:
                    continue
                filled[k] = v
                evidence.append({
                    "field_key": k,
                    "value": v,
                    # enforce_source_evidence checks for "source_url" AND "snippet"
                    # (not "source_text") — both must be non-empty or the field is
                    # dropped from the payload before staging.
                    "source_url": url,
                    "snippet": (r.snippet or f"browser-rendered: {k}={v}")[:240],
                    "confidence": min(1.0, (r.confidence or 0.6) + 0.05),
                    "method": "per_course_browser_extended",
                })

    return filled, evidence


def _force_browser_for_url(url: str) -> bool:
    """Return True for hosts that always need a browser render, even when
    english-test slots are already populated from static HTML (the static
    value is a generic site-wide statement, not course-specific)."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:
        host = ""
    return any(host == h or host.endswith("." + h) for h in _FORCE_BROWSER_HOSTS)


async def maybe_browser_refetch(
    url: str,
    payload: dict[str, Any],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
    force: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None, bool]:
    """If the english-test slots are empty, re-fetch the page via
    Playwright and re-run :func:`english_test.extract` against the
    rendered HTML.

    Returns a 4-tuple ``(filled_values, evidence_rows, rendered_html, override)``:
    * ``filled_values`` — slot keys & values to merge into the existing
      payload.  When ``override`` is True the caller should use direct
      assignment rather than ``setdefault`` so the rendered (course-
      specific) value wins over the static (generic) value.
    * ``evidence_rows`` — provenance rows tagged ``method=per_course_browser``.
    * ``rendered_html`` — the Playwright HTML so Gemini-primary and the
      vision-OCR fallback (T208) can use JS-rendered content.  ``None``
      when the browser fetch returned nothing.
    * ``override`` — True when ``force=True`` was passed, meaning the
      caller should let browser values overwrite existing payload slots
      (e.g. Federation whose static HTML has a generic IELTS 6.0 but
      the rendered Entry Requirements tab has the course-specific 7.0).

    All four are empty / ``None`` / False when the slots were already
    populated (and ``force`` is False), or the browser fetch failed.
    """
    if not _all_english_empty(payload) and not force:
        return {}, [], None, False

    # Issue 1: skip browser pass for paths where a static fallback is
    # sufficient and the browser is known to always time out (e.g. VIT
    # /vocational/* pages). Log a single info line so the sweep log is
    # diagnostic without being noisy.
    if _skip_browser_for_url(url):
        log.info("per_course_browser: skipping browser pass for %s (static fallback sufficient)", url)
        if emit:
            await emit(
                "status",
                f"[per-course browser skipped] {url} — vocational static fallback",
                phase="fallback",
                kind="per_course_browser_skipped",
                url=url,
            )
        return {}, [], None, False

    if emit:
        await emit(
            "status",
            f"[per-course browser ↻] {url}",
            phase="fallback",
            kind="per_course_browser_start",
            url=url,
        )
    wait_until, settle_ms, outer_sec, goto_ms = _browser_config_for(url)
    try:
        rendered = await asyncio.wait_for(
            browser_pool.fetch_html(
                url,
                wait_until=wait_until,
                settle_ms=settle_ms,
                timeout=goto_ms,
                click_international=_needs_international_toggle(url),
            ),
            timeout=outer_sec,
        )
    except asyncio.TimeoutError:
        # Hard ceiling hit. Log a warning BEFORE the abort so the celery
        # journal has a breadcrumb (the prod incident had zero log lines
        # during the 32min hang — even an error would have helped).
        log.warning(
            "browser fallback exceeded %ss on URL %s — aborting this course",
            outer_sec,
            url,
        )
        if emit:
            await emit(
                "status",
                f"timeout: per-course browser exceeded "
                f"{outer_sec}s on {url} — moving on",
                phase="fallback",
                kind="per_course_browser_timeout",
                url=url,
                timeout_seconds=outer_sec,
                level="warn",
            )
        return {}, [], None, False
    except Exception as exc:  # noqa: BLE001 — never abort on browser failure
        log.warning("per_course_browser fetch %s failed: %s", url, exc)
        if emit:
            await emit(
                "status",
                f"[per-course browser ✗] {url}: {exc}",
                phase="fallback",
                kind="per_course_browser_error",
                url=url,
            )
        return {}, [], None, False
    if not rendered:
        if emit:
            await emit(
                "status",
                f"[per-course browser ✗] {url}: empty response",
                phase="fallback",
                kind="per_course_browser_empty",
                url=url,
            )
        return {}, [], None, False

    host = (urlparse(url).hostname or "").lower()
    _is_extended = host in _EXTENDED_EXTRACT_HOSTS

    if _is_extended:
        # UOW / UniSQ: run the FULL extractor suite (fee + IELTS + intake +
        # duration + location + study_mode) against the rendered HTML.  The
        # plain english_test-only path below never sees fee at all.
        filled, evidence = await _extended_extract(rendered, url, payload)

        # ── Rendered-DOM debug for still-missing critical fields ────────
        # When fee or IELTS are still empty after the full render pass, emit
        # structured debug so devs can see what keywords are (or aren't)
        # present in the rendered DOM — distinguishes regex miss from data
        # genuinely absent from the page.
        _critical_missing = {
            k for k in ("international_fee", "ielts_overall")
            if payload.get(k) in (None, "", 0)
            and filled.get(k) in (None, "", 0, [])
        }
        for _cm in _critical_missing:
            _dbg = _rendered_dom_debug(rendered, url, _cm, host)
            log.warning(
                "[RENDERED DOM DEBUG] %s still empty after browser render — "
                "rendered_size=%d chars, %s found in DOM: %s",
                _cm,
                _dbg["rendered_size_chars"],
                "keywords" if _dbg["rendered_text_found"] else "NO keywords",
                url,
            )
            if emit:
                await emit(
                    "status",
                    f"[RENDERED DOM DEBUG] {_cm}: rendered_size={_dbg['rendered_size_chars']}c "
                    f"keywords_found={_dbg['rendered_text_found']} — {url}",
                    phase="fallback",
                    kind="rendered_dom_debug",
                    url=url,
                    field=_cm,
                    debug=_dbg,
                )
    else:
        # Standard path: english_test only.
        # NOTE: english_test.extract is `async def` but contains no await
        # points — it's a pure-CPU regex pipeline. asyncio.wait_for cannot
        # preempt CPU-bound code without yield points. See comment above.
        try:
            results: list[ExtractionResult] = await english_test.extract(rendered, url)
        except Exception as exc:  # noqa: BLE001
            log.warning("english_test re-extract failed on rendered %s: %s", url, exc)
            return {}, [], rendered, force

        filled = {}
        evidence = []
        for r in results:
            if not r.normalized:
                continue
            for k, v in r.normalized.items():
                if v in (None, "", 0):
                    continue
                if k not in _ENGLISH_SLOTS:
                    continue
                if k in filled:
                    continue
                filled[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "source_url": url,
                        "source_text": (r.snippet or "")[:240],
                        "confidence": min(1.0, (r.confidence or 0.5) + 0.05),
                        "method": "per_course_browser",
                    }
                )

    if emit:
        def _fmt(k: str) -> str:
            v = filled.get(k) or payload.get(k)
            return str(v) if v not in (None, "", 0, []) else "—"

        _all_filled = sorted(filled.keys())
        await emit(
            "status",
            f"[per-course browser ✓] {url} — "
            f"IELTS={_fmt('ielts_overall')} "
            f"PTE={_fmt('pte_overall')} "
            f"TOEFL={_fmt('toefl_overall')} "
            f"CAE={_fmt('cambridge_overall')} "
            f"fee={_fmt('international_fee')} "
            f"filled={_all_filled}",
            phase="fallback",
            kind="per_course_browser_done",
            url=url,
            filled=_all_filled,
        )

    return filled, evidence, rendered, force
