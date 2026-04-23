"""International tuition fee extractor — STUB.

The Node implementation lives in ``artifacts/api-server/src/routes/scrape.ts``
under the function ``extractInternationalFee``. Port carefully: the regex set
covers AUD/USD/GBP/EUR with ~12 currency-symbol variants and fuzzy matching
for "per year"/"p.a."/"annual". Return >=1 ExtractionResult per candidate so
the conflict reviewer can pick the best one.
"""
from __future__ import annotations

from app.services.scraper.extractors.base import ExtractionResult


field_key = "international_fee"


async def extract(html: str, url: str) -> list[ExtractionResult]:  # noqa: ARG001
    return []
