"""Course-duration extractor — STUB. Port from Node ``extractDuration``."""
from __future__ import annotations

from app.services.scraper.extractors.base import ExtractionResult


field_key = "duration"


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    return []
