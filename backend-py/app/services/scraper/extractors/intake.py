"""Intake-month extractor — STUB. Port from Node ``extractIntakes``."""
from __future__ import annotations

from app.services.scraper.extractors.base import ExtractionResult


field_key = "intake_months"


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    return []
