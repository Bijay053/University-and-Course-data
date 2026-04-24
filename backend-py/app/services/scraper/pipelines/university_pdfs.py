"""University-level pipeline: read fee + requirements PDFs ONCE per scrape job
and parse them with the same extractors used for course HTML pages.

Many universities (ASA, Torrens, …) publish their international tuition
schedule and admissions/IELTS policy as PDFs linked from the public site
rather than encoding the data on every course page. The per-course HTML
extractors will therefore find nothing and the resulting course rows are
empty for fee/IELTS even though the data exists in a known PDF.

This module:

1. Reads the URLs from ``university.scrape_config['uniPages']``:
   ``feesPdf``, ``requirementsPdf``.
2. Downloads + extracts text via :mod:`app.services.scraper.pdf_fetcher`.
3. Wraps the text as minimal HTML and runs the existing
   :func:`fee.extract` and :func:`english_test.extract` extractors so we
   share the regexes, currency detection, and IELTS sub-band logic
   instead of forking a parallel parser.
4. Returns a normalised payload that downstream callers merge into each
   course as a *last-resort* fallback (after page extractors + AI).
"""
from __future__ import annotations

import logging
from typing import Any

from app.services.scraper.extractors import english_test, fee
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.pdf_fetcher import download_pdf_text

log = logging.getLogger(__name__)


# Keys we will ever fill from a PDF. Anything outside this set stays
# course-page-only (course_name, location, intake, duration, eligibility).
_FEE_KEYS = ("international_fee", "currency", "fee_term", "fee_year")
_ENGLISH_KEYS = (
    "ielts_overall",
    "ielts_listening",
    "ielts_reading",
    "ielts_writing",
    "ielts_speaking",
    "pte_overall",
    "toefl_overall",
    "cambridge_overall",
    "duolingo_overall",
)


def _wrap_text_as_html(text: str) -> str:
    """Render plain text as a minimal HTML document.

    The existing extractors call :func:`html_to_text` first; <pre> keeps
    line breaks so currency/IELTS regexes that depend on whitespace
    proximity continue to work the same as on real pages.
    """
    # Escape only the bare minimum — the extractors only care about
    # textual content, not attributes.
    safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"<html><body><pre>{safe}</pre></body></html>"


def _first_filled(results: list[ExtractionResult], keys: tuple[str, ...]) -> dict[str, Any]:
    """Take the highest-confidence (first-emitted) value per key from extractor results."""
    out: dict[str, Any] = {}
    for r in results:
        if not r.normalized:
            continue
        for k, v in r.normalized.items():
            if k not in keys or v is None:
                continue
            out.setdefault(k, v)
    return out


async def _parse_fee_pdf(url: str, country: str | None) -> dict[str, Any]:
    text = await download_pdf_text(url)
    if not text:
        return {}
    html = _wrap_text_as_html(text)
    try:
        results = await fee.extract(html, url, country=country)
    except Exception as exc:  # noqa: BLE001
        log.warning("fee extractor failed on PDF %s: %s", url, exc)
        return {}
    out = _first_filled(results, _FEE_KEYS)
    if out:
        log.info("fee PDF %s yielded %s", url, sorted(out))
    return out


async def _parse_requirements_pdf(url: str) -> dict[str, Any]:
    text = await download_pdf_text(url)
    if not text:
        return {}
    html = _wrap_text_as_html(text)
    try:
        results = await english_test.extract(html, url)
    except Exception as exc:  # noqa: BLE001
        log.warning("english extractor failed on PDF %s: %s", url, exc)
        return {}
    out = _first_filled(results, _ENGLISH_KEYS)
    if out:
        log.info("requirements PDF %s yielded %s", url, sorted(out))
    return out


async def load_university_pdf_data(
    scrape_config: dict[str, Any] | None,
    country: str | None,
) -> dict[str, Any]:
    """Read both PDFs (if configured) and return uni-level fallback data.

    Shape::

        {
            "fee": {"international_fee": 24000, "currency": "AUD", ...},
            "english": {"ielts_overall": 6.0, "ielts_listening": 5.5, ...},
            "fees_pdf_url": "https://.../fees.pdf",      # only if data extracted
            "requirements_pdf_url": "https://.../req.pdf",
        }

    Empty dict if neither PDF is configured or both failed. Safe to call
    even when ``scrape_config`` is ``None``.
    """
    pages = ((scrape_config or {}).get("uniPages") or {})
    fees_pdf_url = (pages.get("feesPdf") or "").strip()
    reqs_pdf_url = (pages.get("requirementsPdf") or "").strip()

    fee_data = await _parse_fee_pdf(fees_pdf_url, country) if fees_pdf_url else {}
    english_data = await _parse_requirements_pdf(reqs_pdf_url) if reqs_pdf_url else {}

    out: dict[str, Any] = {}
    if fee_data:
        out["fee"] = fee_data
        out["fees_pdf_url"] = fees_pdf_url
    if english_data:
        out["english"] = english_data
        out["requirements_pdf_url"] = reqs_pdf_url
    return out
