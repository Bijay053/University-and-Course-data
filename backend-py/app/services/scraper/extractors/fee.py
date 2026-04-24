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


_UNIT_COUNT_RE = re.compile(r"\b(\d{1,3})\s+units?\b", re.IGNORECASE)
_CREDIT_POINT_RE = re.compile(
    r"\b(\d{2,4})\s+credit\s+points?\b", re.IGNORECASE
)
_CP_PER_UNIT_RE = re.compile(
    r"\b(\d{1,2})\s+credit\s+points?\s+(?:per|each)\b|"
    r"\bunits?\s+of\s+(\d{1,2})\s+credit\s+points?\b",
    re.IGNORECASE,
)


def _find_total_units(text: str) -> int | None:
    """Best-effort total-unit count for a degree program.

    Returns the largest plausible value because the Node side observed
    pages that mention both per-trimester unit loads ("4 units per trimester")
    and the total ("24 units total"). For credit-point structures we divide
    by the per-unit credit-point load (default 8 — the Australian standard;
    overridden when the page explicitly says "12 credit points each", etc).
    """
    candidates: list[int] = []
    for m in _UNIT_COUNT_RE.finditer(text):
        n = int(m.group(1))
        # 4-60 captures realistic programmes (Bachelor ≈ 24, Masters ≈ 12).
        if 4 <= n <= 60:
            candidates.append(n)
    cp_per_unit = 8
    for m in _CP_PER_UNIT_RE.finditer(text):
        raw = m.group(1) or m.group(2)
        if raw and 4 <= int(raw) <= 24:
            cp_per_unit = int(raw)
            break
    for m in _CREDIT_POINT_RE.finditer(text):
        cp = int(m.group(1))
        if 48 <= cp <= 480:
            derived = cp // cp_per_unit
            if 4 <= derived <= 60:
                candidates.append(derived)
    if not candidates:
        return None
    return max(candidates)


def _maybe_compute_full_course(amount: int, fee_term: str, text: str) -> tuple[int, str] | None:
    """If fee is per-unit and a unit count is parseable, compute the
    full-course total and re-tag.

    Returns ``(total_amount, "Full Course")`` on success, ``None`` when no
    unit count is recoverable. Caller decides whether to override the
    extracted fee.
    """
    if fee_term != "Per Unit":
        return None
    units = _find_total_units(text)
    if not units:
        return None
    total = amount * units
    # Final sanity gate — protect against a per-unit value that was
    # actually mis-parsed (e.g. an Annual fee tagged Per Unit from a
    # noisy paragraph). Real full-course totals sit in $20K-$500K.
    if not (15_000 <= total <= 500_000):
        return None
    return total, "Full Course"


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


_PER_UNIT_HINT_RE = re.compile(
    r"per\s*(?:credit\s*)?(?:unit|point|credit|subject|module)", re.IGNORECASE
)


def _candidates(text: str) -> Iterable[tuple[int, str, str]]:
    """Yield (amount, currency_token_in_match, surrounding_context)."""
    for m in _AMOUNT_RE.finditer(text):
        cur = m.group(1) or m.group(4) or ""
        raw = m.group(2) or m.group(3) or ""
        try:
            amount = int(float(raw.replace(",", "")))
        except ValueError:
            continue
        # Compute the local context first so the per-unit floor can use
        # it. Per-unit tuition typically sits at $1.5K-$8K per subject;
        # the standard $5K floor would reject every legitimate per-unit
        # fee (the user's exact T203 bug). Drop to $1.5K when the
        # surrounding window mentions "per unit" so the rollup branch
        # downstream gets a chance to multiply it back up to a Full
        # Course total.
        start = max(0, m.start() - 160)
        end = min(len(text), m.end() + 160)
        ctx = text[start:end]
        floor = 1_500 if _PER_UNIT_HINT_RE.search(ctx) else 5_000
        if amount < floor or amount > 200_000:
            continue
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
    fee_term = _normalize_fee_term(ctx)
    method = "regex"
    # Per-Unit → Full Course rollup (T203). Mirrors Node's behaviour at
    # routes/scrape.ts:2102: when a per-unit fee is detected and the page
    # also discloses a total-unit count, prefer the rolled-up Full Course
    # value so the Review table shows the full programme cost rather than
    # a per-subject sticker shock. Falls back silently when no unit count
    # is parseable.
    rollup = _maybe_compute_full_course(amount, fee_term, text)
    if rollup is not None:
        amount, fee_term = rollup
        method = "regex+per_unit_rollup"
    return [
        ExtractionResult(
            field_key="international_fee",
            value=amount,
            normalized={
                "international_fee": amount,
                "currency": currency,
                "fee_term": fee_term,
                "fee_year": _extract_year(ctx),
            },
            confidence=min(1.0, 0.4 + score * 0.1),
            snippet=ctx[:240],
            method=method,
        )
    ]
