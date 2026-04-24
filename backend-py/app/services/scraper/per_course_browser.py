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

from app.services.scraper.browser_pool import pool as browser_pool
from app.services.scraper.extractors import english_test
from app.services.scraper.extractors.base import ExtractionResult

log = logging.getLogger(__name__)

# Hard ceiling on the entire browser-fallback round-trip for ONE course.
# browser_pool.fetch_html already passes a 30s timeout to page.goto, but
# page.content(), context teardown, and the per-call semaphore acquire
# can each block past that — and a single hung page can wedge the whole
# Celery worker (prod incident: job_2dc0ba6bf4c9 sat at 0/10 for 32min,
# zero log output). asyncio.wait_for is unconditional: if the wrapped
# coroutine doesn't return within the budget, it gets cancelled and we
# move on. 60s comfortably covers a real slow-but-working SPA page
# (Akamai + networkidle + 3s settle + content + teardown ≈ 12–25s
# typical) while bounding worst-case at one fallback attempt per
# course. Bumped from 45 → 60 in PR-1.5 alongside the wait-strategy
# upgrade below — prod regression on VIT showed every per-course
# browser pass extracting empty data because the table hydrated AFTER
# the load event. networkidle + a 3s settle catches the post-load XHR.
_BROWSER_FETCH_TIMEOUT_SEC = 60
# Tell Playwright to wait for the network to go idle (≥500ms with no
# in-flight requests) instead of bailing at domcontentloaded. SPAs
# render the english requirements <table> via an XHR after DCL fires;
# without networkidle we grab the skeleton HTML and english_test.extract
# returns nothing (PR-1.5 prod regression: VIT MBA pages staged with
# IELTS/PTE/TOEFL/CAE all empty on 23/24 URLs).
_BROWSER_WAIT_UNTIL = "networkidle"
# Extra static wait after the load event. Some pages hydrate after
# networkidle has fired (animation-driven reveal, intersection-observer
# lazy-load on a tab/accordion). 3s is enough to catch a one-shot
# hydration without doubling the per-course budget.
_BROWSER_SETTLE_MS = 3000
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


async def maybe_browser_refetch(
    url: str,
    payload: dict[str, Any],
    *,
    emit: Callable[..., Awaitable[None]] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], str | None]:
    """If the english-test slots are empty, re-fetch the page via
    Playwright and re-run :func:`english_test.extract` against the
    rendered HTML.

    Returns a 3-tuple ``(filled_values, evidence_rows, rendered_html)``:
    * ``filled_values`` — slot keys & values to merge into the existing
      payload (caller decides via ``setdefault`` so first-write-wins).
    * ``evidence_rows`` — provenance rows tagged ``method=per_course_browser``.
    * ``rendered_html`` — the Playwright HTML so the vision-OCR fallback
      (T208) can scan ``<img>`` tags without paying for a second browser
      hit. ``None`` when the browser fetch returned nothing.

    All three are empty / ``None`` when the slots were already populated
    or the browser fetch failed — the caller can treat the no-op case
    identically to the "browser disabled" case.
    """
    if not _all_english_empty(payload):
        return {}, [], None

    if emit:
        await emit(
            "status",
            f"[per-course browser ↻] {url}",
            phase="fallback",
            kind="per_course_browser_start",
            url=url,
        )
    try:
        rendered = await asyncio.wait_for(
            browser_pool.fetch_html(
                url,
                wait_until=_BROWSER_WAIT_UNTIL,
                settle_ms=_BROWSER_SETTLE_MS,
            ),
            timeout=_BROWSER_FETCH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        # Hard ceiling hit. Log a warning BEFORE the abort so the celery
        # journal has a breadcrumb (the prod incident had zero log lines
        # during the 32min hang — even an error would have helped).
        log.warning(
            "browser fallback exceeded %ss on URL %s — aborting this course",
            _BROWSER_FETCH_TIMEOUT_SEC,
            url,
        )
        if emit:
            await emit(
                "status",
                f"timeout: per-course browser exceeded "
                f"{_BROWSER_FETCH_TIMEOUT_SEC}s on {url} — moving on",
                phase="fallback",
                kind="per_course_browser_timeout",
                url=url,
                timeout_seconds=_BROWSER_FETCH_TIMEOUT_SEC,
                level="warn",
            )
        return {}, [], None
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
        return {}, [], None
    if not rendered:
        if emit:
            await emit(
                "status",
                f"[per-course browser ✗] {url}: empty response",
                phase="fallback",
                kind="per_course_browser_empty",
                url=url,
            )
        return {}, [], None

    try:
        # NOTE: english_test.extract is `async def` but contains no await
        # points — it's a pure-CPU regex pipeline. asyncio.wait_for cannot
        # preempt CPU-bound code without yield points, so wrapping this
        # call in wait_for would be dead code (the timer never fires until
        # the function returns on its own). If the extractor is ever
        # rewritten to do async I/O (HTML streaming, etc.) re-add the
        # wait_for then. Today, runaway-regex protection has to live
        # INSIDE the extractor itself (length caps, non-backtracking
        # patterns) — see extractors/english_test.py.
        results: list[ExtractionResult] = await english_test.extract(rendered, url)
    except Exception as exc:  # noqa: BLE001
        log.warning("english_test re-extract failed on rendered %s: %s", url, exc)
        return {}, [], rendered

    filled: dict[str, Any] = {}
    evidence: list[dict[str, Any]] = []
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
                    "confidence": min(1.0, (r.confidence or 0.5) + 0.05),
                    "method": "per_course_browser",
                    "snippet": (r.snippet or "")[:240],
                }
            )

    if emit:
        # Compose the IELTS=N PTE=N TOEFL=N CAE=N summary the spec asks
        # for. Use "—" for slots we still couldn't fill so the line is
        # readable in the live log.
        def _fmt(k: str) -> str:
            v = filled.get(k)
            return str(v) if v not in (None, "", 0) else "—"

        await emit(
            "status",
            f"[per-course browser ✓] {url} — "
            f"IELTS={_fmt('ielts_overall')} "
            f"PTE={_fmt('pte_overall')} "
            f"TOEFL={_fmt('toefl_overall')} "
            f"CAE={_fmt('cambridge_overall')}",
            phase="fallback",
            kind="per_course_browser_done",
            url=url,
            filled=list(filled.keys()),
        )

    return filled, evidence, rendered
