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
# Full-course-total context — strongly prefer over annual / per-year amounts
# (Murdoch shows "Full course fee: $125,970" alongside "First year fee: $41,990").
_FULL_COURSE_LABEL_CTX = re.compile(
    r"\b(?:full\s+course|total\s+course|complete\s+course|total\s+program(?:me)?)\s+fee",
    re.IGNORECASE,
)
# First-year fee context — penalise: picking the first-year sticker as the
# representative international fee always under-reports the total programme cost.
_FIRST_YEAR_FEE_CTX = re.compile(
    r"\b(?:first\s+year|1st\s+year|year\s+1)\s+fee",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Commonwealth Supported Place / HECS / student contribution labels signal
# a domestic government-subsidised fee.  These amounts must never be stored
# as the international tuition fee (e.g. UTAS CSP ~$9,000–$16,000/yr).
_CSP_DOMESTIC_CTX = re.compile(
    r"\b(?:commonwealth\s+supported(?:\s+place)?|"
    r"HECS(?:-HELP)?|"
    r"student\s+contribution(?:\s+amount)?|"
    r"domestic\s+(?:student\s+)?(?:tuition\s+)?fee)\b",
    re.IGNORECASE,
)

_COUNTRY_CURRENCY = {
    "australia": "AUD",
    "au": "AUD",
    "new zealand": "NZD",
    "nz": "NZD",
    "canada": "CAD",
    "ca": "CAD",
    "united states": "USD",
    "usa": "USD",
    "us": "USD",
    "united kingdom": "GBP",
    "uk": "GBP",
    "england": "GBP",
    "scotland": "GBP",
    "wales": "GBP",
    "singapore": "SGD",
    "sg": "SGD",
    "ireland": "EUR",
    "germany": "EUR",
    "netherlands": "EUR",
    "france": "EUR",
}


def _infer_currency_from_url(url: str) -> str | None:
    """Infer default currency from URL TLD when context text carries no explicit
    currency marker (e.g. AUT uses bare '$' with no 'NZ$' prefix).

    Only used as a last-resort override when ``_detect_currency`` would
    otherwise fall back to AUD (the code's global default).
    """
    from urllib.parse import urlparse as _up

    host = (_up(url).hostname or "").lower()
    if host.endswith(".nz"):
        return "NZD"
    if host.endswith(".ac.uk") or host.endswith(".co.uk") or host.endswith(".uk"):
        return "GBP"
    if host.endswith(".ca"):
        return "CAD"
    if host.endswith(".sg") or host.endswith(".edu.sg"):
        return "SGD"
    return None


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
    # "for/per N points" → Annual. NZ/AU universities quote per-year fees as
    # "$X for 120 points" (120 credit-points = 1 FTE year of full-time study).
    # This must be checked BEFORE the Full Course block so that a fee page
    # saying "for 120 points" is not accidentally tagged as Full Course.
    if re.search(r"\b(?:for|per)\s+\d{2,4}\s+(?:credit\s+)?points?\b", ctx, re.I):
        return "Annual"
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

# Mirrors `study_mode._extract_strong_label_value`: a structural pre-pass
# that reads the value cell directly out of the DOM so a flattened-text
# boundary collision can't bleed an adjacent paragraph's currency
# figure (scholarship, deposit, building cost) into the fee capture.
# Only "international"-flavoured labels are whitelisted here so the
# pre-pass never claims a domestic-only fee as the international tuition;
# the keyword fallback (with its salary/intl-context scoring) still
# handles the ambiguous cases below.
_FEE_LABEL_RE = re.compile(
    # ── "international …" labels (original set) ────────────────────────
    r"(?:international\s+(?:tuition\s+)?(?:fees?|cost|tuition)|"
    r"international\s+student\s+(?:tuition\s+)?fees?|"
    r"international\s+tuition|"
    r"tuition\s+fees?\s*\(international\)|"
    r"fees?\s*\(international\)|"
    r"international\s+annual\s+fees?|"
    # ── UTAS-style labels: "2026 annual international student tuition fee"
    # The label includes an optional 4-digit year prefix (e.g. "2026 annual
    # …") and the words "annual international student tuition fee".
    # Must appear BEFORE the generic "annual …" block below so the more
    # specific pattern wins when both could match.
    r"(?:\d{4}\s+)?annual\s+international\s+(?:student\s+)?tuition\s+fees?|"
    # ── Generic annual / indicative labels (UOW, UniSQ, etc.) ──────────
    # These appear when the page is already filtered to the international
    # view (e.g. ?students=international query param) so "international"
    # is not repeated in the label text itself.
    r"annual\s+tuition\s+fee|"
    r"indicative\s+annual\s+(?:tuition\s+)?fee|"
    r"annual\s+fee|"
    r"tuition\s+fee|"
    r"course\s+fee|"
    r"program(?:me)?\s+fee|"
    # ── UOW-specific label variants ─────────────────────────────────────
    r"fee\s+per\s+(?:year|annum)|"
    r"annual\s+(?:course\s+)?cost)",
    re.IGNORECASE,
)
_STRONG_VALUE_CHAR_CAP = 300


def _classify_fee_value(value: str) -> tuple[int, str] | None:
    """Parse the first plausible currency amount from a label-value
    cell. Returns ``(amount, surrounding_value_text)`` so the caller
    can run the existing currency / fee-term / year detectors over
    the same context. Bounds match the keyword extractor's sanity
    range (5_000 - 200_000)."""
    m = _AMOUNT_RE.search(value)
    if not m:
        return None
    raw = m.group(2) or m.group(3) or ""
    try:
        amount = int(float(raw.replace(",", "")))
    except ValueError:
        return None
    if amount < 5_000 or amount > 200_000:
        return None
    return amount, value


def _extract_strong_label_value(
    html: str,
) -> tuple[tuple[int, str] | None, str | None]:
    """Structural pre-pass for `<strong>International tuition fees</strong>`
    style label/value idioms. See
    :func:`study_mode._extract_strong_label_value` for the full
    rationale.

    Recognised idioms:

    * ``<strong>International tuition fees</strong>`` — value either
      inline after the bold tag or in a sibling element. Walks forward
      until the next labelled boundary.
    * ``<dt>International tuition</dt><dd>$42,000 per year</dd>``
      — definition lists.
    * ``<th>International fees</th><td>A$45,000</td>`` — table rows.
    """
    if not html:
        return None, None
    try:
        from bs4 import BeautifulSoup
        from bs4.element import NavigableString, Tag
    except ImportError:  # pragma: no cover - bs4 is a hard dep
        return None, None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - defensive
        return None, None

    for label_tag in soup.find_all(("strong", "b", "dt", "th")):
        label_raw = label_tag.get_text(" ", strip=True).rstrip(":").strip()
        if not label_raw or not _FEE_LABEL_RE.fullmatch(label_raw):
            continue

        value_text: str | None = None
        if label_tag.name == "dt":
            sibling = label_tag.find_next_sibling("dd")
            if sibling is not None:
                value_text = sibling.get_text(" ", strip=True)
        elif label_tag.name == "th":
            sibling = label_tag.find_next_sibling("td")
            if sibling is not None:
                value_text = sibling.get_text(" ", strip=True)
        else:
            parts: list[str] = []
            char_count = 0
            for node in label_tag.next_elements:
                if isinstance(node, Tag):
                    if node is label_tag:
                        continue
                    if node.name in ("strong", "b", "h1", "h2", "h3",
                                     "h4", "h5", "h6", "dt", "th",
                                     "tr"):
                        break
                    continue
                if isinstance(node, NavigableString):
                    text = str(node).strip()
                    if not text:
                        continue
                    parts.append(text)
                    char_count += len(text) + 1
                    if char_count >= _STRONG_VALUE_CHAR_CAP:
                        break
            value_text = " ".join(parts)

        if not value_text:
            continue
        value_text = value_text.lstrip(":-– ").strip()
        if not value_text:
            continue
        # Salary-context guard: the label says "international tuition"
        # but if the value cell explicitly mentions salary/wages/income
        # we're looking at marketing copy ("graduate salary outcomes
        # for international students"), not a fee figure.
        if _SALARY_CTX.search(value_text):
            continue
        parsed = _classify_fee_value(value_text)
        if parsed is not None:
            amount, _ = parsed
            snippet = (
                f"<{label_tag.name}>{label_raw}</{label_tag.name}> -> "
                f"{value_text[:80]}"
            )
            return (amount, value_text), snippet
    return None, None


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
        # CSP / domestic fee guard: reject amounts whose immediate context
        # mentions "Commonwealth Supported Place", "HECS", "student
        # contribution", or "domestic fee".  These are government-subsidised
        # domestic fees and must never be stored as the international tuition
        # (e.g. UTAS CSP ~$9,000–$16,000/yr appearing alongside the real
        # international fee of ~$35,000+/yr).
        if _CSP_DOMESTIC_CTX.search(ctx):
            continue
        yield amount, cur, ctx


def _score(amount: int, ctx: str) -> int:
    s = 0
    if _INTL_CTX.search(ctx):
        s += 5
    if _TUITION_CTX.search(ctx):
        s += 3
    # "Full course fee" / "Total course fee" label — strongly prefer over
    # per-year or first-year amounts (e.g. Murdoch $125,970 full-course total
    # vs $41,990 first-year fee).
    if _FULL_COURSE_LABEL_CTX.search(ctx):
        s += 4
    # "First year fee" / "1st year fee" — penalise: this is the per-year
    # sticker, not the total programme cost we want to surface.
    elif _FIRST_YEAR_FEE_CTX.search(ctx):
        s -= 3
    elif _PER_YEAR_CTX.search(ctx):
        s += 2
    # Prefer amounts in the realistic international tuition band.
    # Extend upper bound to 400k so full-course totals also receive the bonus.
    if 12_000 <= amount <= 400_000:
        s += 1
    return s


async def extract(
    html: str, url: str, *, country: str | None = None
) -> list[ExtractionResult]:
    # Structural pre-pass FIRST — see _extract_strong_label_value for
    # the rationale. When the page publishes the international tuition
    # fee as an unambiguous `<strong>International tuition fees</strong>`
    # / `<dt>/<dd>` / `<th>/<td>` pair, read the value cell out of the
    # DOM directly so a flattened-text boundary collision can't bleed
    # an adjacent paragraph's currency figure (scholarship, deposit,
    # building cost) into the fee capture.
    structural, snippet = _extract_strong_label_value(html)
    if structural is not None:
        amount, value_ctx = structural
        currency = _detect_currency(value_ctx, country)
        # Bug 10: bare "$" on .ac.nz pages resolves to AUD by default; override
        # with TLD-inferred currency so NZ universities always emit NZD.
        if currency == "AUD":
            _url_cur = _infer_currency_from_url(url)
            if _url_cur:
                currency = _url_cur
        fee_term = _normalize_fee_term(value_ctx)
        method = "fee.structural"
        rollup = _maybe_compute_full_course(
            amount, fee_term, compact(html_to_text(html))
        )
        if rollup is not None:
            amount, fee_term = rollup
            method = "fee.structural+per_unit_rollup"
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": currency,
                    "fee_term": fee_term,
                    "fee_year": _extract_year(value_ctx),
                },
                confidence=0.85,
                snippet=snippet,
                method=method,
            )
        ]

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
    # Bug 10: same TLD-based override for the keyword path.
    if currency == "AUD":
        _url_cur = _infer_currency_from_url(url)
        if _url_cur:
            currency = _url_cur
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
