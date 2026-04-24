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

import logging
from typing import Any, Awaitable, Callable

from app.services.scraper.browser_pool import pool as browser_pool
from app.services.scraper.extractors import english_test
from app.services.scraper.extractors.base import ExtractionResult

log = logging.getLogger(__name__)

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
            f"[FALLBACK] [per-course browser ↻] {url}",
            phase="fallback",
            kind="per_course_browser_start",
            url=url,
        )
    try:
        rendered = await browser_pool.fetch_html(url)
    except Exception as exc:  # noqa: BLE001 — never abort on browser failure
        log.warning("per_course_browser fetch %s failed: %s", url, exc)
        if emit:
            await emit(
                "status",
                f"[FALLBACK] [per-course browser ✗] {url}: {exc}",
                phase="fallback",
                kind="per_course_browser_error",
                url=url,
            )
        return {}, [], None
    if not rendered:
        if emit:
            await emit(
                "status",
                f"[FALLBACK] [per-course browser ✗] {url}: empty response",
                phase="fallback",
                kind="per_course_browser_empty",
                url=url,
            )
        return {}, [], None

    try:
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
            f"[FALLBACK] [per-course browser ✓] {url} — "
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
