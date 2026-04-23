"""Run all extractors over one course page and return a merged record.

Output shape is keyed for direct insertion into ``scraped_courses`` via
``stage_course``. Each extractor's ``normalized`` payload contributes
fields; a missing extractor simply leaves its slot empty.
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.scraper.extractors import ai_fallback, duration, english_test, fee, intake
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)


# Each entry: (module, kwargs the extractor accepts beyond html/url)
_EXTRACTORS = (
    (fee, ("country",)),
    (english_test, ()),
    (intake, ()),
    (duration, ()),
)


async def extract_course(
    url: str,
    *,
    country: str | None = None,
    html: str | None = None,
    use_ai_fallback: bool = True,
) -> dict[str, Any]:
    """Fetch (if needed) and run all extractors. Returns merged payload + raw evidence."""
    if html is None:
        html = await fetch_html(url)
    if not html:
        return {"url": url, "error": "fetch_failed", "payload": {}, "evidence": []}

    payload: dict[str, Any] = {"course_website": url}
    evidence: list[dict[str, Any]] = []

    for module, extra_keys in _EXTRACTORS:
        kwargs = {k: country for k in extra_keys if k == "country"}
        try:
            results: list[ExtractionResult] = await module.extract(html, url, **kwargs)
        except Exception as exc:  # one extractor must never break the others
            log.warning("Extractor %s failed on %s: %s", module.__name__, url, exc)
            continue
        for r in results:
            evidence.append(
                {
                    "field_key": r.field_key,
                    "value": r.value,
                    "confidence": r.confidence,
                    "method": r.method,
                    "snippet": r.snippet,
                }
            )
            if r.normalized:
                for k, v in r.normalized.items():
                    if v is None:
                        continue
                    # First-write-wins so the highest-confidence result (which
                    # the extractor returned first) is preserved.
                    payload.setdefault(k, v)

    if use_ai_fallback:
        try:
            ai_filled = await ai_fallback.fill_missing(payload, html=html, url=url)
        except Exception as exc:  # never break extraction on AI failure
            log.warning("AI fallback errored on %s: %s", url, exc)
            ai_filled = {}
        for k, v in ai_filled.items():
            payload.setdefault(k, v)
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.5,
                    "method": "ai_fallback",
                    "snippet": None,
                }
            )

    return {"url": url, "payload": payload, "evidence": evidence}
