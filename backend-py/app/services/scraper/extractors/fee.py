"""International tuition fee extractor.

Ported from Node ``extractInternationalFees`` in
``artifacts/api-server/src/routes/scrape.ts`` (lines 2033-2270, plus the
helper ``extractAllFeeAmounts`` and ``normalizeFeeTerm``).

Strategy:
1. Find every currency-tagged amount in the visible text.
2. Score each one by proximity to "international" / "tuition" / "per year"
   / fee-table cues, and by sanity bounds (5_000-200_000).
3. Reject salary contexts (e.g. "graduate salary $85,000").
4. Pick the highest-scoring amount; emit its currency, fee term and year.
"""
from __future__ import annotations

import re
from typing import Iterable

from app.services.scraper.extractors._text import compact, html_to_text
from app.services.scraper.extractors.base import ExtractionResult


field_key = "international_fee"

# Currency tokens recognised in either prefix or suffix position.
_CURRENCY_TOKEN = r"A\$|NZ\$|CA\$|US\$|S\$|£|€|\$|AUD|NZD|CAD|USD|GBP|SGD|EUR"
_AMOUNT_RE = re.compile(
    rf"(?:({_CURRENCY_TOKEN})\s*([\d,]+(?:\.\d+)?))"
    rf"|(?:([\d,]+(?:\.\d+)?)\s*({_CURRENCY_TOKEN}))",
    re.IGNORECASE,
)
_SALARY_CTX = re.compile(
    r"\b(salary|salaries|earn|earning|earnings|wage|wages|income|"
    r"starting\s+pay|graduate\s+(?:salary|outcomes?|income))\b",
    re.IGNORECASE,
)
_INTL_CTX = re.compile(
    r"\b(international|overseas|non[-\s]?resident|out[-\s]?of[-\s]?(?:state|country)|foreign)\b",
    re.IGNORECASE,
)
_TUITION_CTX = re.compile(r"\b(tuition|fee|fees|cost\s+of\s+study)\b", re.IGNORECASE)
_PER_YEAR_CTX = re.compile(r"\b(per\s+year|per\s+annum|p\.?a\.?|annual|annually|yearly)\b", re.IGNORECASE)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

_COUNTRY_CURRENCY = {
    "australia": "AUD",
    "new zealand": "NZD",
    "canada": "CAD",
    "united states": "USD",
    "usa": "USD",
    "us": "USD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "england": "GBP",
    "scotland": "GBP",
    "wales": "GBP",
    "singapore": "SGD",
    "ireland": "EUR",
    "germany": "EUR",
    "netherlands": "EUR",
    "france": "EUR",
}


def _detect_currency(ctx: str, country: str | None) -> str:
    if re.search(r"NZ\$|NZD", ctx, re.I):
        return "NZD"
    if re.search(r"CA\$|C\$|CAD", ctx, re.I):
        return "CAD"
    if re.search(r"S\$|SGD", ctx, re.I):
        return "SGD"
    if re.search(r"US\$|USD", ctx, re.I):
        return "USD"
    if re.search(r"£|GBP", ctx, re.I):
        return "GBP"
    if re.search(r"€|EUR", ctx, re.I):
        return "EUR"
    if re.search(r"A\$|AUD", ctx, re.I):
        return "AUD"
    if country:
        return _COUNTRY_CURRENCY.get(country.lower(), "AUD")
    return "AUD"


def _normalize_fee_term(ctx: str) -> str:
    if re.search(r"per\s*trimester|per\s*trim\b", ctx, re.I):
        return "Trimester"
    if re.search(r"per\s*semester", ctx, re.I):
        return "Semester"
    if re.search(r"per\s*term\b", ctx, re.I):
        return "Term"
    if re.search(r"per\s*session\b", ctx, re.I):
        return "Session"
    if re.search(r"per\s*(?:credit\s*)?(?:unit|point|credit)", ctx, re.I):
        return "Per Unit"
    if re.search(
        r"total\s*(?:course|program|tuition)|full\s*course|complete\s*(?:course|program)",
        ctx,
        re.I,
    ):
        return "Full Course"
    return "Annual"


def _extract_year(ctx: str) -> int | None:
    from datetime import datetime as _dt

    cur = _dt.utcnow().year
    for raw in _YEAR_RE.findall(ctx):
        y = int(raw)
        if cur - 1 <= y <= cur + 3:
            return y
    return None


def _candidates(text: str) -> Iterable[tuple[int, str, str]]:
    """Yield (amount, currency_token_in_match, surrounding_context)."""
    for m in _AMOUNT_RE.finditer(text):
        cur = m.group(1) or m.group(4) or ""
        raw = m.group(2) or m.group(3) or ""
        try:
            amount = int(float(raw.replace(",", "")))
        except ValueError:
            continue
        # Sanity: real-world international tuition is roughly $5K-$200K.
        if amount < 5000 or amount > 200_000:
            continue
        start = max(0, m.start() - 160)
        end = min(len(text), m.end() + 160)
        ctx = text[start:end]
        # Salary filter: reject only when the *nearest* salary cue is closer
        # to the amount than the nearest tuition/fee/international cue.
        anchor = m.start() - start  # offset of the amount inside ctx
        sal_dist = min(
            (abs(s.start() - anchor) for s in _SALARY_CTX.finditer(ctx)),
            default=float("inf"),
        )
        tui_dist = min(
            (
                abs(s.start() - anchor)
                for pat in (_TUITION_CTX, _INTL_CTX)
                for s in pat.finditer(ctx)
            ),
            default=float("inf"),
        )
        if sal_dist < tui_dist:
            continue
        yield amount, cur, ctx


def _score(amount: int, ctx: str) -> int:
    s = 0
    if _INTL_CTX.search(ctx):
        s += 5
    if _TUITION_CTX.search(ctx):
        s += 3
    if _PER_YEAR_CTX.search(ctx):
        s += 2
    # Prefer amounts in the realistic international tuition band.
    if 12_000 <= amount <= 80_000:
        s += 1
    return s


async def extract(
    html: str, url: str, *, country: str | None = None
) -> list[ExtractionResult]:
    text = compact(html_to_text(html))
    if not text:
        return []
    best: tuple[int, int, str] | None = None  # (score, amount, ctx)
    for amount, _cur, ctx in _candidates(text):
        sc = _score(amount, ctx)
        if best is None or sc > best[0] or (sc == best[0] and amount > best[1]):
            best = (sc, amount, ctx)
    if best is None:
        return []
    score, amount, ctx = best
    # Hard gate: never emit a fee unless the amount has at least one tuition
    # OR international cue in its window. This prevents random currency
    # numbers (deposits, scholarships, room costs) from being labelled as
    # the international tuition fee.
    if not (_TUITION_CTX.search(ctx) or _INTL_CTX.search(ctx)):
        return []
    currency = _detect_currency(ctx, country)
    return [
        ExtractionResult(
            field_key="international_fee",
            value=amount,
            normalized={
                "international_fee": amount,
                "currency": currency,
                "fee_term": _normalize_fee_term(ctx),
                "fee_year": _extract_year(ctx),
            },
            confidence=min(1.0, 0.4 + score * 0.1),
            snippet=ctx[:240],
            method="regex",
        )
    ]
