"""Pipeline for extracting one course page — SCAFFOLD."""
from __future__ import annotations

import logging

from app.services.scraper.extractors import duration, english_test, fee, intake
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.http_fetcher import fetch_html

log = logging.getLogger(__name__)

ALL_EXTRACTORS = (fee, english_test, intake, duration)


async def extract_course(url: str) -> dict:
    html = await fetch_html(url)
    if not html:
        return {"url": url, "error": "fetch_failed", "results": []}
    results: list[ExtractionResult] = []
    for ex in ALL_EXTRACTORS:
        try:
            results.extend(await ex.extract(html, url))
        except Exception as exc:
            log.warning("Extractor %s failed on %s: %s", ex.__name__, url, exc)
    return {"url": url, "results": [r.__dict__ for r in results]}
