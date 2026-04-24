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
import re
from typing import Any

import httpx

from app.services.scraper.extractors import english_test, fee
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.pdf_fetcher import download_pdf_text
from app.services.scraper.pdf_vision import extract_via_vision

_VISION_TIMEOUT_S = 30.0
_VISION_MAX_BYTES = 12 * 1024 * 1024

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


async def _download_raw_pdf(url: str) -> bytes:
    """Fetch the raw PDF bytes once so we can feed both ``pypdf`` and the
    vision-OCR fallback without two round-trips. Returns ``b""`` on any
    error — vision degrades to "no fallback" the same way."""
    if not url:
        return b""
    try:
        async with httpx.AsyncClient(
            timeout=_VISION_TIMEOUT_S, follow_redirects=True
        ) as client:
            r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code != 200:
                return b""
            content = r.content
            if len(content) > _VISION_MAX_BYTES:
                return b""
            # Light MIME guard — some uni sites return 200 + HTML when the
            # PDF link is broken.
            ct = (r.headers.get("content-type") or "").lower()
            if "pdf" not in ct and not content.startswith(b"%PDF"):
                return b""
            return content
    except Exception as exc:  # noqa: BLE001
        log.debug("_download_raw_pdf failed for %s: %s", url, exc)
        return b""


async def _vision_fallback_text(pdf_bytes: bytes, kind: str, url: str, emit) -> str:
    """Render a PDF and ask Gemini Vision to dump its facts as text.

    ``kind`` is "fee" or "requirements" — used only for the verbose log
    line. Returns "" when vision is disabled, fails, or yields nothing."""
    if not pdf_bytes:
        return ""
    if emit:
        await emit(
            "status",
            f"[FALLBACK] vision OCR on {kind} PDF: {url}",
            phase="extract",
            kind="pdf_vision_start",
        )
    text = await extract_via_vision(pdf_bytes)
    if emit:
        msg = (
            f"[FALLBACK] vision OCR {kind} PDF returned {len(text)} chars"
            if text
            else f"[FALLBACK] vision OCR {kind} PDF returned nothing (skipped or empty)"
        )
        await emit(
            "status",
            msg,
            phase="extract",
            kind="pdf_vision_done",
            chars=len(text),
        )
    return text


_AMOUNT_RE = re.compile(r"\$\s*([\d,]+(?:\.\d+)?)")
_FEE_YEAR_RE = re.compile(r"\b(20\d{2})\b")


def _pick_amounts_from_pdf_text(text: str) -> dict[str, Any]:
    """Port of Node's ``pickAmounts`` heuristic for fee PDFs.

    Rationale (Bug G): the single-page ``fee.extract`` extractor scores
    candidates by proximity to a tuition cue word ("international", "fee",
    "tuition"), then returns the single best-scoring candidate. That works
    on web pages — but fee PDFs typically lay tuition out as a multi-row
    table where ALL the candidates appear next to the same cue, so the
    scorer misses the obvious signal: the LARGEST amount in the document
    is the full-course (international) tuition, not a per-trimester
    instalment. Result: prod was reporting per-trimester or per-unit fees
    as the international fee.

    This helper mirrors Node's ``extractFeesFromPdf``:

    * Dedupe all ``$X,XXX[.XX]`` amounts in the 1k–200k window.
    * Pick the max.
    * Mark as "Full Course" when there are ≥3 unique amounts, OR the max
      is ≥1.4× the next-largest, OR the text mentions "full course".
    * Per Unit when "per unit" appears anywhere.
    * Else "Annual".
    * Currency hard-coded to AUD (matches Node — every uni in scope is AU).
    """
    amounts: list[int] = []
    for m in _AMOUNT_RE.finditer(text):
        try:
            n = round(float(m.group(1).replace(",", "")))
        except ValueError:
            continue
        if 1000 < n < 200000:
            amounts.append(n)
    if not amounts:
        return {}

    unique = sorted(set(amounts))
    # Architect-flagged edge case: a single ``$5,000`` deposit alongside
    # an ``AUD 25,000`` tuition (no $ sign) used to short-circuit the
    # cue-aware ``fee.extract`` and return the deposit. With only one
    # ``$`` amount in the document we have no signal that it is
    # tuition vs. a deposit / textbook fee / scholarship value, so
    # we defer to ``fee.extract`` (which reads ``AUD 25,000`` style
    # amounts too) by returning empty. Two or more candidates is the
    # "real fee table" signal that justifies short-circuiting.
    if len(unique) < 2:
        return {}

    chosen = unique[-1]
    next_largest = unique[-2] if len(unique) > 1 else None

    looks_like_full_course = (
        len(unique) >= 3
        or (next_largest is not None and chosen >= next_largest * 1.4)
        or bool(re.search(r"\bfull\s+course\b", text, re.I))
    )

    if looks_like_full_course:
        term = "Full Course"
    elif re.search(r"\bper\s+unit\b", text, re.I):
        term = "Per Unit"
    else:
        term = "Annual"

    out: dict[str, Any] = {
        "international_fee": chosen,
        "currency": "AUD",
        "fee_term": term,
    }
    year_match = _FEE_YEAR_RE.search(text)
    if year_match:
        out["fee_year"] = int(year_match.group(1))
    return out


async def _parse_fee_pdf(url: str, country: str | None, emit=None) -> dict[str, Any]:
    raw = await _download_raw_pdf(url)
    if not raw:
        return {}
    text = ""
    try:
        # ``download_pdf_text`` re-fetches the URL; instead reuse the
        # bytes we already have via the in-memory ``PdfReader`` path.
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:80])
    except Exception as exc:  # noqa: BLE001
        log.debug("fee PDF text extraction failed for %s: %s", url, exc)
        text = ""

    out: dict[str, Any] = {}
    if text:
        # Bug G: try the PDF-specific pickAmounts heuristic FIRST. The
        # single-page fee extractor is preserved as a safety net for the
        # rare case where the PDF is actually a one-page web-style page
        # with a single tuition number.
        out = _pick_amounts_from_pdf_text(text)
        if not out:
            html = _wrap_text_as_html(text)
            try:
                results = await fee.extract(html, url, country=country)
                out = _first_filled(results, _FEE_KEYS)
            except Exception as exc:  # noqa: BLE001
                log.warning("fee extractor failed on PDF %s: %s", url, exc)

    # Vision fallback fires when the text path returned no usable fee data
    # — typically a scanned/image-only PDF.
    if not out:
        vision_text = await _vision_fallback_text(raw, "fee", url, emit)
        if vision_text:
            out = _pick_amounts_from_pdf_text(vision_text)
            if not out:
                html = _wrap_text_as_html(vision_text)
                try:
                    results = await fee.extract(html, url, country=country)
                    out = _first_filled(results, _FEE_KEYS)
                except Exception as exc:  # noqa: BLE001
                    log.warning("fee extractor failed on vision text for %s: %s", url, exc)
    if out:
        log.info("fee PDF %s yielded %s", url, sorted(out))
    return out


async def _parse_requirements_pdf(url: str, emit=None) -> dict[str, Any]:
    raw = await _download_raw_pdf(url)
    if not raw:
        return {}
    text = ""
    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw))
        text = "\n".join((p.extract_text() or "") for p in reader.pages[:80])
    except Exception as exc:  # noqa: BLE001
        log.debug("requirements PDF text extraction failed for %s: %s", url, exc)
        text = ""

    out: dict[str, Any] = {}
    if text:
        html = _wrap_text_as_html(text)
        try:
            results = await english_test.extract(html, url)
            out = _first_filled(results, _ENGLISH_KEYS)
        except Exception as exc:  # noqa: BLE001
            log.warning("english extractor failed on PDF %s: %s", url, exc)

    if not out:
        vision_text = await _vision_fallback_text(raw, "requirements", url, emit)
        if vision_text:
            html = _wrap_text_as_html(vision_text)
            try:
                results = await english_test.extract(html, url)
                out = _first_filled(results, _ENGLISH_KEYS)
            except Exception as exc:  # noqa: BLE001
                log.warning("english extractor failed on vision text for %s: %s", url, exc)
    if out:
        log.info("requirements PDF %s yielded %s", url, sorted(out))
    return out


async def load_university_pdf_data(
    scrape_config: dict[str, Any] | None,
    country: str | None,
    *,
    emit=None,
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

    ``emit`` is the same async log-callback used elsewhere in the
    pipeline; when provided, vision-OCR fallback emits ``[FALLBACK]``
    lines so reviewers can see when AI is reading a scanned PDF.
    """
    pages = ((scrape_config or {}).get("uniPages") or {})
    fees_pdf_url = (pages.get("feesPdf") or "").strip()
    reqs_pdf_url = (pages.get("requirementsPdf") or "").strip()

    fee_data = await _parse_fee_pdf(fees_pdf_url, country, emit=emit) if fees_pdf_url else {}
    english_data = await _parse_requirements_pdf(reqs_pdf_url, emit=emit) if reqs_pdf_url else {}

    out: dict[str, Any] = {}
    if fee_data:
        out["fee"] = fee_data
        out["fees_pdf_url"] = fees_pdf_url
    if english_data:
        out["english"] = english_data
        out["requirements_pdf_url"] = reqs_pdf_url
    return out
