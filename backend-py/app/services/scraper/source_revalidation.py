"""Phase 9B — T001: Source Revalidation.

When a field conflict cannot be resolved by source-priority rules or
normalization equivalence, this module re-fetches the original HTML source
URLs and extracts a fresh value to detect stale-data conflicts.

Design constraints
------------------
* HTML only (v1) — PDF and API re-fetches are out of scope for v1 (PDFs
  require binary download + re-parse; API re-fetches need auth tokens that
  may have rotated).
* Lightweight extraction — uses field-specific regex patterns on the raw
  page text, NOT the full scraper pipeline.  False-negative rate is higher
  than the full pipeline but false-positive rate is negligible (we only
  act on clear matches).
* Timeout: 8 seconds per URL.  Max 3 URLs per repair call.
* Soft-fail everywhere — any error returns (None, False).
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger(__name__)

_MAX_URLS_PER_CALL = 3
_FETCH_TIMEOUT = 8          # seconds
_UA = (
    "Mozilla/5.0 (compatible; UniPortalBot/1.0; +https://university-portal.local)"
)


# ---------------------------------------------------------------------------
# Field-specific lightweight extractors (regex on raw HTML text)
# ---------------------------------------------------------------------------

def _extract_fee(text: str) -> str | None:
    """Extract the most prominent fee amount from page text."""
    patterns = [
        # AUD / USD with commas: "AUD 45,000" / "$45,000"
        r"(?:AUD|USD|A\$|\$)\s*([\d,]+)",
        # "45,000" followed by whitespace and currency
        r"([\d,]{5,})\s*(?:AUD|USD|per year|p\.a\.)",
        # Any large 5-digit number
        r"\b(\d{5,})\b",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            val = m.group(1).replace(",", "")
            try:
                f = float(val)
                if 1_000 <= f <= 200_000:
                    return f"{round(f / 100) * 100:.1f}"
            except ValueError:
                continue
    return None


def _extract_duration(text: str) -> str | None:
    """Extract duration in months from page text."""
    from app.services.scraper.field_normalizers import normalize_duration
    patterns = [
        r"\d+(?:\.\d+)?\s*(?:year|yr)s?",
        r"\d+(?:\.\d+)?\s*months?",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            result = normalize_duration(m.group(0))
            if result:
                return result
    return None


def _extract_ielts(text: str) -> str | None:
    """Extract overall IELTS score from page text."""
    m = re.search(
        r"(?:ielts|ielts\s+overall|overall\s+band)[:\s]+(\d+(?:\.\d+)?)",
        text, re.I,
    )
    if m:
        return f"{float(m.group(1)):.1f}"
    m = re.search(r"\bielts\b.*?(\d+(?:\.\d)?)\b", text, re.I)
    if m:
        return f"{float(m.group(1)):.1f}"
    return None


_FIELD_EXTRACTOR: dict[str, Any] = {
    "international_fee": _extract_fee,
    "domestic_fee": _extract_fee,
    "fee_year": _extract_fee,
    "duration": _extract_duration,
    "ielts_overall": _extract_ielts,
}


def _get_extractor(field_name: str):
    for key, fn in _FIELD_EXTRACTOR.items():
        if key in field_name.lower():
            return fn
    return None


# ---------------------------------------------------------------------------
# HTTP fetch (sync, runs in thread)
# ---------------------------------------------------------------------------

def _fetch_url(url: str) -> str | None:
    """Synchronously fetch URL text. Called via asyncio.to_thread."""
    try:
        import requests as _req
        resp = _req.get(
            url,
            timeout=_FETCH_TIMEOUT,
            headers={"User-Agent": _UA},
            allow_redirects=True,
        )
        if resp.status_code == 200:
            return resp.text
    except Exception as exc:  # noqa: BLE001
        log.debug("[REVAL] fetch failed for %s: %s", url, exc)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def revalidate_field_from_html(
    source_urls: list[str],
    field_name: str,
    existing_normalized_values: set[str],
) -> dict[str, Any]:
    """Re-fetch HTML source URLs and extract a fresh value for ``field_name``.

    Returns:
        {
            "revalidated": bool,
            "action": "stale_source_resolved" | "unchanged" | "fetch_failed",
            "fresh_value": str | None,
            "url_checked": str | None,
            "stale_url": str | None,
        }
    """
    extractor = _get_extractor(field_name)
    if extractor is None:
        return {"revalidated": False, "action": "no_extractor", "fresh_value": None,
                "url_checked": None, "stale_url": None}

    checked = 0
    for url in source_urls[:_MAX_URLS_PER_CALL]:
        if not url or not url.startswith("http"):
            continue
        checked += 1

        try:
            html = await asyncio.to_thread(_fetch_url, url)
        except Exception as exc:  # noqa: BLE001
            log.debug("[REVAL] thread failed for %s: %s", url, exc)
            continue

        if html is None:
            continue

        fresh_val = extractor(html)
        if fresh_val is None:
            continue

        # Check whether the fresh value matches one of the OTHER sources
        # (i.e. the old value from this URL was stale and now matches consensus)
        if fresh_val in existing_normalized_values:
            return {
                "revalidated": True,
                "action": "stale_source_resolved",
                "fresh_value": fresh_val,
                "url_checked": url,
                "stale_url": url,
            }

        # The fresh value is DIFFERENT from what we had — update it but
        # don't mark as resolved if it still doesn't match consensus.
        return {
            "revalidated": False,
            "action": "unchanged",
            "fresh_value": fresh_val,
            "url_checked": url,
            "stale_url": None,
        }

    return {
        "revalidated": False,
        "action": "fetch_failed" if checked > 0 else "no_urls",
        "fresh_value": None,
        "url_checked": None,
        "stale_url": None,
    }
