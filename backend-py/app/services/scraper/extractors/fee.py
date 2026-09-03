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
# Amount grammar: 1-3 leading digits, then any number of (separator + 3-digit
# group) repetitions, optional decimal tail.  Separator includes comma AND
# the European/Australian space-style thousands separators (regular space,
# NBSP U+00A0, narrow NBSP U+202F).  La Trobe writes "A$42 200" with a
# regular space — without space-as-separator support every La Trobe fee
# silently failed extraction.  The strict 3-digit grouping prevents the
# regex from greedily slurping unrelated whitespace-separated digits in
# the same paragraph (e.g. "A$42 200 ... 80 students" stops at 200).
_THOUSANDS_SEP = r"[,\u00a0\u202f ]"
# Amount body alternation (order matters — leftmost-longest preferred):
#   1. Grouped: 1-3 leading digits + (sep + 3-digit group)+ + optional decimal
#      → "$1,500", "A$42 200", "$1,500,000"
#   2. Raw 4-7 digit: no thousands separator at all
#      → "$17136", "$102816"  (UOW renders fees this way fleet-wide;
#      without this branch the engine matched only "$171" / "$102" and
#      both were filtered out as implausible by the downstream gate)
#   3. Raw 1-3 digit + optional decimal — keeps "$0", "$500", "AUD 9999.50"
# The grouped branch is listed first so "$1,500" still parses as 1500
# rather than being split into "1" + ",500".
_AMOUNT_BODY = (
    rf"(?:\d{{1,3}}(?:{_THOUSANDS_SEP}\d{{3}})+(?:\.\d+)?"
    rf"|\d{{4,7}}(?:\.\d+)?"
    rf"|\d{{1,3}}(?:\.\d+)?)"
)
_AMOUNT_RE = re.compile(
    rf"(?:({_CURRENCY_TOKEN})\s*({_AMOUNT_BODY}))"
    rf"|(?:({_AMOUNT_BODY})\s*({_CURRENCY_TOKEN}))",
    re.IGNORECASE,
)
# Characters stripped before int() — keeps the parser tolerant of any
# combination of comma / space / NBSP separators within a single amount.
_AMOUNT_STRIP_RE = re.compile(r"[,\u00a0\u202f ]")


def _parse_amount(raw: str) -> int | None:
    """Parse a regex-captured amount string (possibly containing comma,
    space, NBSP, or narrow-NBSP thousands separators) to an int.  Returns
    ``None`` when the cleaned string is not a valid number."""
    cleaned = _AMOUNT_STRIP_RE.sub("", raw)
    try:
        return int(float(cleaned))
    except ValueError:
        return None


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
# Scholarship / discount context — amounts in this context must NEVER win as
# the international tuition fee.  Sheffield pages show "£2,500 per year
# scholarships for international students" where "international" passes
# _INTL_CTX but the amount is a scholarship, not a tuition fee.
_SCHOLARSHIP_CTX = re.compile(
    r"\b(scholarships?|bursar(?:y|ies)|discounts?|waivers?|prizes?|awards?|grants?|reductions?)\b",
    re.IGNORECASE,
)
# Full-course-total context — strongly prefer over annual / per-year amounts
# (Murdoch shows "Full course fee: $125,970" alongside "First year fee: $41,990").
_FULL_COURSE_LABEL_CTX = re.compile(
    # Allow up to one optional adjective between "total" / "full" and "course"
    # so labels like Curtin's "Total indicative course fee" still match
    # (previously only literal "total course fee" matched, so $141,348 was
    # silently classified as Annual instead of Full Course).
    r"\b(?:full|total|complete)\s+(?:\w+\s+)?(?:course|program(?:me)?)\s+fee"
    r"|\b(?:indicative\s+)?total\s+tuition\s+fee",
    re.IGNORECASE,
)
# First-year fee context — penalise by default (picking the first-year sticker
# as the representative international fee always under-reports the total
# programme cost). Per-uni YAML knob ``prefer_year_one_over_total`` flips
# the sign for universities (e.g. Curtin) where the year-1 amount IS the
# operator-facing Annual figure.
_FIRST_YEAR_FEE_CTX = re.compile(
    # "year one" added so "Indicative year one fee" / similar variants match;
    # "year 1 fee" remains the most common form.
    r"\b(?:first\s+year|1st\s+year|year\s+(?:1|one))\s+fee",
    re.IGNORECASE,
)
_YEAR_RE = re.compile(r"\b(20\d{2})\b")

# Domestic / non-international fee context labels.  Any amount whose
# surrounding window contains one of these phrases is NOT a valid
# international tuition fee and must be skipped.
#
# Covers:
#  AU/NZ: Commonwealth Supported Place, HECS-HELP, student contribution
#  UK:    Home fee, Home student fee, UK fee, UK student fee
#  Any:   per-module / per-credit / CPD / professional development fees
#         (these are module-unit prices, never annual international tuition)
_CSP_DOMESTIC_CTX = re.compile(
    r"\b(?:commonwealth\s+supported(?:\s+place)?|"
    r"HECS(?:-HELP)?|"
    r"student\s+contribution(?:\s+amount)?|"
    # Per-module / per-credit / CPD prices (never annual international tuition)
    r"per\s+(?:module|credit|unit\s+of\s+credit)|"
    r"module\s+fee|credit\s+fee|"
    r"(?:continuing\s+professional\s+development|cpd)\s+(?:fee|rate|price)|"
    r"part[\s\-]time\s+(?:fee|rate|tuition))\b",
    re.IGNORECASE,
)

# Audience-owned fee labels in either word order. The original domestic guard
# covered "domestic student tuition fee" but missed UTS's equally explicit
# "tuition fee for domestic students". Keep these separate from CSP/module
# markers so pages that print domestic and international rows side-by-side can
# assign each amount to the nearest audience label rather than rejecting both.
_DOMESTIC_FEE_LABEL_CTX = re.compile(
    r"\b(?:domestic|home|local|uk)\s+(?:students?\s+)?"
    r"(?:indicative\s+|total\s+|annual\s+)*(?:tuition\s+)?fees?\b"
    r"|\b(?:indicative\s+|total\s+|annual\s+)*(?:tuition\s+|course\s+|program(?:me)?\s+)"
    r"fees?\s+for\s+(?:domestic|home|local|uk)\s+students?\b",
    re.IGNORECASE,
)
_INTERNATIONAL_FEE_LABEL_CTX = re.compile(
    r"\b(?:international|overseas|non[-\s]?resident)\s+(?:students?\s+)?"
    r"(?:indicative\s+|total\s+|annual\s+)*(?:tuition\s+)?fees?\b"
    r"|\b(?:indicative\s+|total\s+|annual\s+)*(?:tuition\s+|course\s+|program(?:me)?\s+)"
    r"fees?\s+for\s+(?:international|overseas|non[-\s]?resident)\s+students?\b",
    re.IGNORECASE,
)


def _nearest_label_distance(pattern: re.Pattern, ctx: str, anchor: int) -> float:
    return min(
        (abs(((match.start() + match.end()) / 2) - anchor) for match in pattern.finditer(ctx)),
        default=float("inf"),
    )


def _is_domestic_owned_fee(ctx: str, anchor: int) -> bool:
    """Return True when the amount is owned by the nearest domestic fee label."""
    domestic_distance = _nearest_label_distance(_DOMESTIC_FEE_LABEL_CTX, ctx, anchor)
    if domestic_distance == float("inf"):
        return False
    international_distance = _nearest_label_distance(
        _INTERNATIONAL_FEE_LABEL_CTX, ctx, anchor
    )
    return domestic_distance < international_distance


def _audience_owned_amounts(text: str) -> tuple[set[int], set[int]]:
    """Collect amounts explicitly owned by domestic/international labels.

    A page can repeat a domestic amount later in an unlabeled tuition section.
    The explicit occurrence remains authoritative for that duplicated value.
    """
    domestic: set[int] = set()
    international: set[int] = set()
    for match in _AMOUNT_RE.finditer(text):
        raw = match.group(2) or match.group(3) or ""
        amount = _parse_amount(raw)
        if amount is None:
            continue
        start = max(0, match.start() - 160)
        end = min(len(text), match.end() + 160)
        ctx = text[start:end]
        anchor = match.start() - start
        domestic_distance = _nearest_label_distance(
            _DOMESTIC_FEE_LABEL_CTX, ctx, anchor
        )
        international_distance = _nearest_label_distance(
            _INTERNATIONAL_FEE_LABEL_CTX, ctx, anchor
        )
        if domestic_distance < international_distance:
            domestic.add(amount)
        elif international_distance < domestic_distance:
            international.add(amount)
    return domestic, international


# Minimum plausible international annual fee in GBP for a UK university.
# Genuine international UG/PG fees at UK universities are ≥ £10,000/yr.
# Amounts below this threshold without an explicit "international" label in
# the surrounding context are almost certainly domestic/home/module fees.
_GBP_INTL_MIN = 10_000

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
    # Southeast Asia
    "malaysia": "MYR",
    "my": "MYR",
    "indonesia": "IDR",
    "id": "IDR",
    "thailand": "THB",
    "th": "THB",
    "vietnam": "VND",
    "vn": "VND",
    "philippines": "PHP",
    "ph": "PHP",
    # South Asia
    "india": "INR",
    "in": "INR",
    "sri lanka": "LKR",
    # Other
    "japan": "JPY",
    "jp": "JPY",
    "china": "CNY",
    "cn": "CNY",
    "south korea": "KRW",
    "kr": "KRW",
}


def _infer_currency_from_url(url: str) -> str | None:
    """Infer fee currency from URL TLD when page text carries no explicit marker.

    **Returns ``None`` for unknown TLDs** (``.com``, ``.org``, ``.net``, etc.).
    Callers must use the ``if _url_cur:`` guard (already in place) so that
    ``None`` does not override a currency already detected from fee text.
    This preserves the precedence chain:
      text-extracted → API/PDF → per-uni YAML → TLD map → (data_quality only) default

    The TLD→currency map is read from ``scraper_config/defaults.yaml``
    (``currency_detection.tld_currency_map``); no hardcoded list here.
    """
    from app.services.scraper.currency_utils import infer_currency_from_url as _icfu
    return _icfu(url)


def _detect_currency(ctx: str, country: str | None) -> str:
    if re.search(r"NZ\$|NZD", ctx, re.I):
        return "NZD"
    # CA\$ = unambiguous Canadian-dollar prefix (e.g. "CA$18,645").
    # Bare C\$ is intentionally excluded: it false-matches text like
    # "Course$18,645" or "Contact…C $18,645" on UK university pages,
    # producing CAD amounts that are actually GBP.
    if re.search(r"\bCA\$|\bCAD\b", ctx, re.I):
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


def _normalize_fee_term(ctx: str, *, prefer_year_one: bool = False) -> str:
    # When the per-uni knob ``extraction.fees.prefer_year_one_over_total``
    # is set, a year-1 label near the amount means the operator wants the
    # value stored as Annual.  Must run BEFORE the total/full-course
    # branch below — Curtin pages publish both "Indicative year 1 fee"
    # and "Total indicative course fee" inside the same 320-char window
    # so without this gate the term would still resolve to Full Course.
    if prefer_year_one and _FIRST_YEAR_FEE_CTX.search(ctx):
        return "Annual"
    if re.search(r"per\s*trimester|per\s*trim\b", ctx, re.I):
        return "Trimester"
    if re.search(r"per\s*semester", ctx, re.I):
        return "Semester"
    # UOW labels the per-period amount as "Session fee" and places the
    # full-programme amount beside it as "Course fee".  The session label is
    # sometimes in a table header rather than next to the value, so callers
    # that preserve the header in the context must classify it explicitly.
    if re.search(r"\bsession\s+fee\b", ctx, re.I):
        return "Session"
    if re.search(r"per\s*term\b", ctx, re.I):
        return "Term"
    if re.search(r"per\s*session\b", ctx, re.I):
        return "Session"
    if re.search(r"per\s*(?:credit\s*)?(?:unit|point|credit)", ctx, re.I):
        return "Per Unit"
    # "for/per N points" OR "(N points)" → Annual.
    # NZ/AU universities quote per-year fees as "$X for 120 points" OR
    # "$47,300 (120 points)" (120 credit-points = 1 FTE year of full-time
    # study).  Both the preposition form and the parenthesised form must be
    # caught BEFORE the Full Course block so they are never mis-tagged as a
    # full-course total.
    if re.search(
        r"\b(?:for|per)\s+\d{2,4}\s+(?:credit\s+)?points?\b"
        r"|\(\s*\d{2,4}\s+(?:credit\s+)?points?\s*\)",
        ctx,
        re.I,
    ):
        return "Annual"
    # Explicit "annual" / "per year" / "per annum" label overrides any "total"
    # that may appear later in the same context window.  UTAS pages show:
    #   "2026 annual international student tuition fee: $42,950 AUD
    #    Indicative total tuition fee for international students: $135,400 AUD"
    # Both candidates share a wide context window, so without this guard the
    # $42,950 candidate would be tagged "Full Course" because "total tuition"
    # appears further along in its window.
    if re.search(r"\bannual\b|\bper\s+year\b|\bper\s+annum\b", ctx, re.I):
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

    cur = _dt.now(tz=None).year
    for raw in _YEAR_RE.findall(ctx):
        y = int(raw)
        if cur - 1 <= y <= cur + 3:
            return y
    return None


def _extract_year_for_amount(ctx: str, amount: int) -> int | None:
    """Return the valid publication year nearest a specific fee amount."""
    from datetime import datetime as _dt

    cur = _dt.now(tz=None).year
    year_matches = [
        match
        for match in _YEAR_RE.finditer(ctx)
        if cur - 1 <= int(match.group(0)) <= cur + 3
    ]
    if not year_matches:
        return None

    amount_matches = []
    for match in _AMOUNT_RE.finditer(ctx):
        raw = match.group(2) or match.group(3) or ""
        if _parse_amount(raw) == amount:
            amount_matches.append(match)
    if not amount_matches:
        return None

    amount_match = min(amount_matches, key=lambda match: abs(match.start() - len(ctx) // 2))
    sentence_start = max(
        ctx.rfind(mark, 0, amount_match.start())
        for mark in (".", "!", "?", ";", "\n")
    ) + 1
    following_boundaries = [
        pos
        for mark in (".", "!", "?", ";", "\n")
        if (pos := ctx.find(mark, amount_match.end())) >= 0
    ]
    sentence_end = min(following_boundaries) if following_boundaries else len(ctx)
    sentence_years = [
        year
        for year in year_matches
        if sentence_start <= year.start() and year.end() <= sentence_end
    ]
    if not sentence_years:
        return None
    preceding_years = [
        year for year in sentence_years if year.end() <= amount_match.start()
    ]
    if preceding_years:
        year_match = max(preceding_years, key=lambda year: year.end())
    else:
        year_match = min(
            sentence_years,
            key=lambda year: abs(year.start() - amount_match.end()),
        )
    return int(year_match.group(0))


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
    amount = _parse_amount(raw)
    if amount is None:
        return None
    # Week 2 P6 — log-and-accept low values; only the upper bound rejects.
    # Pathway-program / TAFE / short-course fees can fall below the historic
    # 5_000 floor; the module-level _MIN_INTL_FEE_FLOOR (1_000) defines the
    # new floor.  Above 200_000 still rejects (those are CRICOS errors).
    if amount > 200_000:
        return None
    if amount < 1_000:
        return None
    if amount < 5_000:
        from app.services.scraper.sanity_floors import sanity_check
        sanity_check("international_fee", amount)
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
        # Clip at "See also" cross-reference sections that appear on UC-style
        # pages immediately after the fee amount.  Without this clip the value
        # text can include navigation phrases like "See also Domestic students"
        # or "See also Domestic tuition fees" which then contaminate the
        # evidence snippet and fire a false FEE_REJECT later in the pipeline.
        _see_also_m = re.search(r"\bsee\s+also\b", value_text, re.IGNORECASE)
        if _see_also_m:
            value_text = value_text[: _see_also_m.start()].strip()
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


def _extract_audience_scoped_fee(
    html: str,
    *,
    prefer_year_one: bool,
) -> tuple[int, str] | None:
    """Read a fee from a machine-readable international audience container.

    Some course pages publish domestic and international values in the same
    SSR document, then use JavaScript only to toggle visibility.  Flattening
    that document destroys the audience boundary and lets a nearby domestic
    amount win.  Prefer explicit audience attributes before any text scan.

    Supported attribute idioms are intentionally generic:
    ``data-student-type``, ``data-audience``, and ``data-fee-audience``.
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover - bs4 is a hard dependency
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - defensive
        return None

    audience_attrs = ("data-student-type", "data-audience", "data-fee-audience")
    candidates: list[tuple[int, int, str]] = []

    def _has_audience_attr(tag: object) -> bool:
        attrs = getattr(tag, "attrs", {}) or {}
        return any(attr in attrs for attr in audience_attrs)

    for block in soup.find_all(True):
        audience = " ".join(
            str(block.attrs.get(attr) or "") for attr in audience_attrs
        ).strip()
        if not audience or not _INTL_CTX.search(audience):
            continue
        # Mixed/shared owners do not prove which audience a descendant amount
        # belongs to.  Fail closed and let the established contextual cascade
        # handle them rather than promoting a possibly domestic value at high
        # confidence.
        mixed_audience = re.sub(
            r"\bnon[-\s]?resident\b",
            "",
            audience,
            flags=re.I,
        )
        if re.search(
            r"\b(?:domestic|home|local|resident)\b",
            mixed_audience,
            re.I,
        ):
            continue

        for label_tag in block.find_all(("dt", "th")):
            # A broad international wrapper may contain a nested domestic card.
            # Only use labels whose nearest audience owner is this exact block.
            # This prevents ancestor inheritance from crossing audience scopes.
            nearest_owner = label_tag.find_parent(_has_audience_attr)
            if nearest_owner is not block:
                continue

            label = label_tag.get_text(" ", strip=True).rstrip(":").strip()
            if not label:
                continue

            # Audience scoping alone is not enough: international sections also
            # contain application, student-services, insurance, deposit, and
            # scholarship charges.  Require explicit tuition/course/year
            # semantics before assigning the value to international tuition.
            is_year_one = bool(_FIRST_YEAR_FEE_CTX.search(label))
            is_total = bool(_FULL_COURSE_LABEL_CTX.search(label))
            is_annual = bool(_PER_YEAR_CTX.search(label))
            is_explicit_tuition = bool(
                re.search(
                    r"\b(?:tuition|course|program(?:me)?)\s+fees?\b|"
                    r"\bfees?\s+(?:per|for)\s+(?:year|annum)\b",
                    label,
                    re.I,
                )
            )
            if not (is_year_one or is_total or is_annual or is_explicit_tuition):
                continue
            if re.search(
                r"\b(?:application|admission|acceptance|enrol(?:ment|lment)|"
                r"student\s+services?|amenities|insurance|visa|deposit|"
                r"materials?|administration|registration|scholarship|"
                r"bursary|discount|waiver)\b",
                label,
                re.I,
            ):
                continue

            value_tag = label_tag.find_next_sibling(
                "dd" if label_tag.name == "dt" else "td"
            )
            if value_tag is None:
                continue
            value_text = value_tag.get_text(" ", strip=True)
            parsed = _classify_fee_value(value_text)
            if parsed is None:
                continue
            amount, _ = parsed

            score = 1
            if prefer_year_one:
                if is_year_one:
                    score += 10
                elif is_total:
                    score -= 4
                elif is_annual:
                    score += 6
            else:
                if is_total:
                    score += 10
                elif is_year_one:
                    score -= 2
                elif is_annual:
                    score += 5

            ctx = f"{label}: {value_text} for international students"
            candidates.append((score, amount, ctx))

    if not candidates:
        return None

    # Deterministic tie-break follows the configured catalogue semantics:
    # lower annual/year-one amount when preferred, otherwise the larger total.
    if prefer_year_one:
        _score_value, amount, ctx = max(candidates, key=lambda x: (x[0], -x[1]))
    else:
        _score_value, amount, ctx = max(candidates, key=lambda x: (x[0], x[1]))
    return amount, ctx


# ── Structured fee table extractor ───────────────────────────────────────────
# UK universities (Wolverhampton, Coventry, etc.) publish fee tables with rows
# shaped like:
#
#   Home          | Full time  | £9,535 per year  | 2025 to 26
#   Home          | Full time  | £9,790 per year  | 2026 to 27
#   Home          | Part time  | £4,768 per year  | 2025 to 26
#   Home          | Part time  | £4,895 per year  | 2026 to 27
#   International | Full time  | £17,000 per year | 2025 to 26
#   International | Full time  | £18,700 per year | 2026 to 27
#
# The flat-text scanner has no concept of row boundaries so it picks amounts
# indiscriminately (often the first Home row value).  This pre-pass reads the
# <table> DOM directly and returns the International + Full-time row for the
# latest available year.
#
# Return values (used by extract() for branching):
#   (amount, ctx_str)          — valid International + Full-time row found
#   _FEE_TABLE_FOUND_NO_INTL  — fee table detected but no Intl+FT row exists
#                                (caller must return [] to prevent fallback
#                                picking up a Home / part-time amount)
#   None                       — no structured fee table found; fall through

_FEE_TABLE_FOUND_NO_INTL = object()  # sentinel — must compare with `is`

# Row-level student-type and study-mode matchers (applied to joined cell text).
_ROW_INTL_RE    = re.compile(r"\bInternational\b|\bOverseas\b",  re.IGNORECASE)
_ROW_HOME_RE    = re.compile(r"\bHome\b|\bDomestic\b",           re.IGNORECASE)
_ROW_FULLTIME_RE = re.compile(r"Full[\s\-]?time",                re.IGNORECASE)
_ROW_PARTTIME_RE = re.compile(r"Part[\s\-]?time",                re.IGNORECASE)
# Takes the first "20XX" in a year-range cell: "2026 to 27", "2026/27", "2026"
_ROW_YEAR_RE    = re.compile(r"20(\d{2})")


def _extract_fee_table_row(
    html: str,
) -> "tuple[int, str] | object | None":
    """Parse a structured multi-row fee table and return the International +
    Full-time row for the latest available year.

    **Return-value contract** (callers must use ``is`` for the sentinel)::

        (amount, ctx_str)        — valid Intl + Full-time row found
        _FEE_TABLE_FOUND_NO_INTL — fee table detected, but no Intl+FT row
        None                     — no structured fee table in this page
    """
    if not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover — bs4 is a hard dep
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover — defensive
        return None

    best_year: int = -1
    best_amount: int | None = None
    best_ctx: str | None = None
    found_fee_table = False

    for table in soup.find_all("table"):
        # Flatten each <tr> into a list of stripped cell-text values.
        rows: list[list[str]] = []
        for tr in table.find_all("tr"):
            cells = [
                td.get_text(" ", strip=True)
                for td in tr.find_all(["td", "th"])
            ]
            if cells:
                rows.append(cells)

        if len(rows) < 2:
            continue

        # A table qualifies as a structured fee table when ≥1 data row
        # contains (Home OR International) AND (Full/Part time) AND an amount.
        is_fee_table = False
        for cells in rows:
            row_text = " | ".join(cells)
            if (
                (_ROW_INTL_RE.search(row_text) or _ROW_HOME_RE.search(row_text))
                and (_ROW_FULLTIME_RE.search(row_text) or _ROW_PARTTIME_RE.search(row_text))
                and _AMOUNT_RE.search(row_text)
            ):
                is_fee_table = True
                break

        if not is_fee_table:
            # Secondary check: column-based fee table where "International" is a
            # column header rather than a row label.  Canonical example is
            # Ulster University's credit-band table:
            #   Credit Points | NI/ROI Cost | GB Cost | International Cost*
            # Requires the header row to have "International" as a column AND a
            # credit-band/NI-ROI marker so we don't trigger on unrelated tables.
            if rows:
                _hdr_text = " | ".join(rows[0])
                _intl_hdr_col: "int | None" = None
                for _ci, _cell in enumerate(rows[0]):
                    if _ROW_INTL_RE.search(_cell):
                        _intl_hdr_col = _ci
                        break
                _is_credit_band = bool(
                    re.search(r"NI[/\s]*ROI|credit\s*points?", _hdr_text, re.I)
                    and _intl_hdr_col is not None
                )
                if _is_credit_band:
                    _cb_credits_seen: int = 0
                    _cb_amount: "int | None" = None
                    _cb_ctx: "str | None" = None
                    for _row in rows[1:]:
                        if len(_row) <= (_intl_hdr_col or 0):
                            continue
                        _credit_m = re.search(r"\b(\d{2,3})\b", _row[0])
                        if not _credit_m:
                            continue
                        _credits = int(_credit_m.group(1))
                        if _credits < 30 or _credits > 240:
                            continue
                        _intl_cell = _row[_intl_hdr_col]
                        _am = _AMOUNT_RE.search(_intl_cell)
                        if not _am:
                            continue
                        _raw = _am.group(2) or _am.group(3) or ""
                        _amt = _parse_amount(_raw)
                        if _amt is None or _amt < 5_000:
                            continue
                        # Prefer 120 credits (one FT year); otherwise highest ≤ 120.
                        if _credits == 120 or (
                            _credits <= 120 and _credits > _cb_credits_seen
                        ):
                            _cb_credits_seen = _credits
                            _cb_amount = _amt
                            _cb_ctx = (
                                f"credit-band table ({_credits} credits): "
                                + " | ".join(_row)
                            )
                    if _cb_amount is not None:
                        found_fee_table = True
                        if _cb_amount > (best_amount or 0):
                            best_amount = _cb_amount
                            best_ctx = _cb_ctx
            # ── Nationality-column layout ─────────────────────────────────────
            # Some UK universities (e.g. Canterbury Christ Church) put UK and
            # Overseas/International as *column headers*, with Full-time /
            # Part-time as row labels:
            #   <tr><th></th><th>UK</th><th>Overseas</th></tr>
            #   <tr><td>Full-time</td><td>£9,790</td><td>£17,000</td></tr>
            # The row-label check above never fires because no single row
            # contains both "Overseas" and "Full-time". Detect by requiring
            # BOTH a home column (UK / Home / Domestic) AND an
            # international column (International / Overseas) in the header,
            # then pick the international-column cell from Full-time rows.
            if not found_fee_table and rows:
                _hdr_nc = rows[0]
                _intl_col_nc: "int | None" = None
                _home_col_nc: "int | None" = None
                for _ci_nc, _cell_nc in enumerate(_hdr_nc):
                    if _ROW_INTL_RE.search(_cell_nc) and _intl_col_nc is None:
                        _intl_col_nc = _ci_nc
                    elif (
                        re.search(r"\b(UK|Home|Domestic)\b", _cell_nc, re.I)
                        and _home_col_nc is None
                    ):
                        _home_col_nc = _ci_nc
                if _intl_col_nc is not None and _home_col_nc is not None:
                    for _row_nc in rows[1:]:
                        if len(_row_nc) <= _intl_col_nc:
                            continue
                        _row_nc_text = " | ".join(_row_nc)
                        if not _ROW_FULLTIME_RE.search(_row_nc_text):
                            continue
                        # Skip combined header rows that show both modes.
                        if _ROW_PARTTIME_RE.search(_row_nc[0] if _row_nc else ""):
                            continue
                        _intl_cell_nc = _row_nc[_intl_col_nc]
                        _am_nc = _AMOUNT_RE.search(_intl_cell_nc)
                        if not _am_nc:
                            continue
                        _raw_nc = _am_nc.group(2) or _am_nc.group(3) or ""
                        _amt_nc = _parse_amount(_raw_nc)
                        if _amt_nc is None or _amt_nc < 5_000:
                            continue
                        _yr_nc = -1
                        _ym_nc = _ROW_YEAR_RE.search(_row_nc_text)
                        if _ym_nc:
                            _yr_nc = int("20" + _ym_nc.group(1))
                        if _yr_nc > best_year or (
                            _yr_nc == best_year and _amt_nc > (best_amount or 0)
                        ):
                            best_year = _yr_nc
                            best_amount = _amt_nc
                            best_ctx = "nationality-column table: " + " | ".join(
                                _row_nc
                            )
                        found_fee_table = True
            if not found_fee_table:
                continue

        found_fee_table = True

        # Scan rows for International + Full-time entries.
        for cells in rows:
            row_text = " | ".join(cells)
            if not _ROW_INTL_RE.search(row_text):
                continue
            if not _ROW_FULLTIME_RE.search(row_text):
                continue
            # Guard: skip rows that contain BOTH Full-time and Part-time tokens
            # (typically a combined header cell — not a data row).
            if _ROW_PARTTIME_RE.search(row_text):
                continue
            # Extract the fee amount from this row.
            am = _AMOUNT_RE.search(row_text)
            if not am:
                continue
            raw = am.group(2) or am.group(3) or ""
            amount = _parse_amount(raw)
            if amount is None or amount < 5_000:
                continue
            # Extract year (start year of range), e.g. "2026 to 27" → 2026.
            yr = -1
            ym = _ROW_YEAR_RE.search(row_text)
            if ym:
                yr = int("20" + ym.group(1))
            # Keep highest-year row; break ties with larger amount.
            if yr > best_year or (yr == best_year and amount > (best_amount or 0)):
                best_year = yr
                best_amount = amount
                best_ctx = row_text

    if not found_fee_table:
        return None
    if best_amount is None:
        # Fee table detected but zero International + Full-time rows exist.
        # Signal caller to return [] and suppress the text-scan fallback so a
        # Home or part-time figure is never mistakenly stored.
        return _FEE_TABLE_FOUND_NO_INTL
    return best_amount, best_ctx


def _candidates(text: str) -> Iterable[tuple[int, str, str]]:
    """Yield (amount, currency_token_in_match, surrounding_context)."""
    domestic_owned_amounts, international_owned_amounts = _audience_owned_amounts(text)
    for m in _AMOUNT_RE.finditer(text):
        cur = m.group(1) or m.group(4) or ""
        raw = m.group(2) or m.group(3) or ""
        amount = _parse_amount(raw)
        if amount is None:
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
        # Week 2 P6 — log-and-accept low values.  The historic floor of
        # 5_000 silently dropped pathway / TAFE / micro-credential courses;
        # we now reject only below 1_000 (clearly noise) or above 200_000
        # (clearly a CRICOS-code mis-parse), and emit a SANITY log line
        # for the 1_000–4_999 grey zone so reviewers can audit if needed.
        # The per-unit floor (1_500) is kept as the *upper* limit for
        # per-unit context — it gates whether the rollup branch should
        # multiply the value, not whether to reject it outright.
        # Full-course totals for long combined degrees can exceed A$200k
        # (UTS Biomedical Engineering is A$251,437.94). Permit up to A$500k
        # only when the local label explicitly identifies a total tuition /
        # course/program fee; retain the conservative ceiling otherwise.
        max_amount = 500_000 if _FULL_COURSE_LABEL_CTX.search(ctx) else 200_000
        if amount < 1_000 or amount > max_amount:
            continue
        per_unit = bool(_PER_UNIT_HINT_RE.search(ctx))
        if (per_unit and amount < 1_500) or (not per_unit and amount < 5_000):
            from app.services.scraper.sanity_floors import sanity_check
            sanity_check("international_fee", amount)
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
        if (
            amount in domestic_owned_amounts
            and amount not in international_owned_amounts
        ):
            continue
        # CSP / domestic fee guard: reject amounts whose immediate context
        # mentions "Commonwealth Supported Place", "HECS", "student
        # contribution", "domestic fee", "home student fee", "UK student fee",
        # "per module", "per credit", "CPD fee", or "part-time fee".
        # These are domestic/module prices and must never be stored as the
        # international annual tuition fee.
        if _is_domestic_owned_fee(ctx, anchor):
            continue
        if _CSP_DOMESTIC_CTX.search(ctx):
            continue
        # GBP floor guard for UK universities.
        # Genuine international annual fees at UK universities are ≥ £10,000.
        # Any GBP amount below this threshold is almost certainly a domestic
        # (home-student) fee, a per-module price, a CPD rate, or a part-time
        # module charge — unless the immediate context contains an explicit
        # "international" cue that confirms this is the international fee.
        # Exception: credit-band tables (e.g. Ulster: NI/ROI | GB Cost | Intl*)
        # have "International" as a COLUMN HEADER — that is not a fee label for
        # this specific cell value, so the GBP floor guard must still apply.
        if cur == "GBP" and amount < _GBP_INTL_MIN:
            _has_intl_ctx = bool(_INTL_CTX.search(ctx))
            if _has_intl_ctx and re.search(
                r"NI[/\s]*ROI|\bGB\s+Cost\b", ctx, re.I
            ):
                _has_intl_ctx = False  # column header, not a fee label
            if not _has_intl_ctx:
                continue
        yield amount, cur, ctx


_NZ_POINTS_IN_CTX = re.compile(
    r"\(\s*(\d{2,4})\s+(?:credit\s+)?points?\s*\)", re.IGNORECASE
)


def _score(amount: int, ctx: str, *, prefer_year_one: bool = False) -> int:
    s = 0
    if _INTL_CTX.search(ctx):
        s += 5
    if _TUITION_CTX.search(ctx):
        s += 3
    # NZ/AU credit-point fee format: "$47,300 (120 points)" / "$96,965 (240 points)".
    # 120 credit-points = 1 FTE year — prefer that entry; penalise higher
    # point counts which represent multi-year totals.
    _pts_m = _NZ_POINTS_IN_CTX.search(ctx)
    if _pts_m:
        pts = int(_pts_m.group(1))
        if pts == 120:
            s += 3   # exact annual-year entry
        elif pts > 120:
            s -= 2   # multi-year total — deprioritise
    if prefer_year_one:
        # Per-uni override (e.g. Curtin): both labels typically appear in
        # the same 320-char window, so use proximity — whichever label is
        # closer to the amount wins.  The amount itself sits roughly at
        # ctx[160] (the candidate window is m.start()-160 .. m.end()+160).
        anchor = min(160, len(ctx) // 2)
        full_dist = min(
            (abs(m.start() - anchor) for m in _FULL_COURSE_LABEL_CTX.finditer(ctx)),
            default=float("inf"),
        )
        yr1_dist = min(
            (abs(m.start() - anchor) for m in _FIRST_YEAR_FEE_CTX.finditer(ctx)),
            default=float("inf"),
        )
        if yr1_dist < full_dist and yr1_dist != float("inf"):
            s += 5  # strong preference for year-1 amount
        elif full_dist < yr1_dist and full_dist != float("inf"):
            s -= 4  # penalise total-course amount
        elif _PER_YEAR_CTX.search(ctx):
            s += 2
    else:
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
    # Scholarship/bursary/discount: heavy penalty so these never win even
    # when they carry an "international" cue (e.g. "£2,500 scholarships for
    # international students").
    if _SCHOLARSHIP_CTX.search(ctx):
        s -= 8
    return s


def _select_latest_explicit_dated_fee(
    html: str,
    *,
    prefer_year_one: bool,
) -> tuple[int, int, str, int, str] | None:
    """Select the newest of multiple explicitly dated international fees.

    The override is deliberately narrow: at least two different valid years
    must be attached to amount clauses that themselves contain both
    international and tuition/fee semantics.
    """
    text = compact(html_to_text(html))
    if not text:
        return None

    non_tuition_charge = re.compile(
        r"\b(?:deposit|application|acceptance|enrolment|registration|"
        r"student\s+services?|amenities|SSAF|materials?|equipment|insurance|"
        r"visa|accommodation)\s+(?:charge|cost|fee|fees|payment)\b",
        re.IGNORECASE,
    )

    def eligible_clause(clause: str, amount: int) -> bool:
        return bool(
            1_000 <= amount <= 500_000
            and _INTL_CTX.search(clause)
            and re.search(r"\btuition\b|\bcost\s+of\s+study\b", clause, re.IGNORECASE)
            and not _SCHOLARSHIP_CTX.search(clause)
            and not non_tuition_charge.search(clause)
        )

    candidates: list[tuple[int, int, int, str]] = []
    seen: set[tuple[int, int]] = set()
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
        for label_tag in soup.find_all(("dt", "th")):
            value_tag = label_tag.find_next_sibling(
                "dd" if label_tag.name == "dt" else "td"
            )
            if value_tag is None:
                continue
            label = compact(label_tag.get_text(" ", strip=True))
            value_text = compact(value_tag.get_text(" ", strip=True))
            ctx = compact(f"{label}: {value_text}")
            year = _extract_year(ctx)
            if year is None:
                continue
            for match in _AMOUNT_RE.finditer(value_text):
                raw = match.group(2) or match.group(3) or ""
                amount = _parse_amount(raw)
                if amount is None or not eligible_clause(ctx, amount):
                    continue
                identity = (year, amount)
                if identity in seen:
                    continue
                seen.add(identity)
                candidates.append(
                    (
                        year,
                        _score(amount, ctx, prefer_year_one=prefer_year_one),
                        amount,
                        ctx,
                    )
                )
    except Exception:
        pass

    if len({year for year, _score_value, _amount, _ctx in candidates}) < 2:
        candidates.clear()
        seen.clear()
        for amount, _currency_token, ctx in _candidates(text):
            year = _extract_year_for_amount(ctx, amount)
            if year is None:
                continue

            matching_amounts = []
            for match in _AMOUNT_RE.finditer(ctx):
                raw = match.group(2) or match.group(3) or ""
                if _parse_amount(raw) == amount:
                    matching_amounts.append(match)
            if not matching_amounts:
                continue
            amount_match = min(
                matching_amounts,
                key=lambda match: abs(match.start() - len(ctx) // 2),
            )
            clause_start = max(
                ctx.rfind(mark, 0, amount_match.start())
                for mark in (".", "!", "?", ";", "\n")
            ) + 1
            following_boundaries = [
                pos
                for mark in (".", "!", "?", ";", "\n")
                if (pos := ctx.find(mark, amount_match.end())) >= 0
            ]
            clause_end = min(following_boundaries) if following_boundaries else len(ctx)
            clause = ctx[clause_start:clause_end]
            if not eligible_clause(clause, amount):
                continue

            identity = (year, amount)
            if identity in seen:
                continue
            seen.add(identity)
            candidates.append(
                (
                    year,
                    _score(amount, ctx, prefer_year_one=prefer_year_one),
                    amount,
                    ctx,
                )
            )

    if len({year for year, _score_value, _amount, _ctx in candidates}) < 2:
        return None

    latest_year = max(year for year, _score_value, _amount, _ctx in candidates)
    latest = [candidate for candidate in candidates if candidate[0] == latest_year]
    if prefer_year_one:
        year, score, amount, ctx = max(
            latest,
            key=lambda candidate: (candidate[1], -candidate[2]),
        )
    else:
        year, score, amount, ctx = max(
            latest,
            key=lambda candidate: (candidate[1], candidate[2]),
        )
    return amount, year, ctx, score, text


_UWL_DOMESTIC_ONLY = object()  # sentinel: select exists, no International option

# UWL Angular SSR JSON-blob fee fields (present in static render=False HTML).
#   field_p_cv_int_main_fee   → International annual fee (£16,750)
#   field_p_cv_uk_eu_main_fee → UK/EU annual fee (£9,790)
# Each is a `"field_…":{"target_id":N,"name":"<digits>"}` object.  The FIRST
# int_main_fee is the headline full-time international fee shown in the fees
# panel; later occurrences belong to linked/related courses on the same page.
_UWL_JSON_INT_FEE_RE = re.compile(
    r'field_p_cv_int_main_fee"?\s*:\s*\{[^}]*?"name"\s*:\s*"(\d[\d,]*)"'
)
_UWL_JSON_UK_FEE_RE = re.compile(
    r'field_p_cv_uk_eu_main_fee"?\s*:\s*\{[^}]*?"name"\s*:\s*"(\d[\d,]*)"'
)


def _from_uwl_json_blob(html: str) -> "tuple[int, str] | None | object":
    """Fallback UWL fee reader for static (Scrape.do render=False) pages.

    The nationality-switcher ``<select>`` options are populated by Angular at
    runtime, so on the *static* SSR HTML the select is an empty shell and
    :func:`_from_uwl_nationality_select` finds no option text.  But the SSR
    JSON blob still embeds the fees as ``field_p_cv_int_main_fee`` (International)
    and ``field_p_cv_uk_eu_main_fee`` (UK/EU).

    Rule (per operator policy): *if a course is offered to international
    students it always has an international fee* — so whenever an
    ``int_main_fee`` value is present we capture it as the international fee.

    Returns:
      * ``(amount, ctx)``   — International fee found in the blob.
      * ``_UWL_DOMESTIC_ONLY`` — no international fee but a UK fee exists →
        domestic-only course; caller returns ``[]``.
      * ``None``            — no UWL fee blob at all → fall through to the
        generic cascade.
    """
    m = _UWL_JSON_INT_FEE_RE.search(html)
    if m:
        amount = _parse_amount(m.group(1))
        if amount is not None:
            return amount, f"UWL int_main_fee JSON blob: £{m.group(1)}"
    if _UWL_JSON_UK_FEE_RE.search(html):
        # UK fee present, no international fee → domestic-only course.
        return _UWL_DOMESTIC_ONLY
    return None


def _from_uwl_nationality_select(
    html: str, url: str
) -> "tuple[int, str] | None | object":
    """UWL-specific fee extractor: reads the annual fee from the
    ``<select id="nationality_pricing_input_mobile">`` dropdown.

    UWL Angular SPA pages expose a nationality-switcher select element
    rendered by Scrape.do::

        <select id="nationality_pricing_input_mobile">
          <option value="[object Object]">£16,750 – International</option>
          <option value="[object Object]">£9,790 – UK</option>
        </select>

    The generic text scanner has no boundary awareness and may mis-score
    the UK option when "– International" from the adjacent first option
    appears in the context window of the second option's amount.

    Return values:
      * ``(amount, ctx)``   — International option found; use this fee.
      * ``_UWL_DOMESTIC_ONLY`` — select exists but has NO "– International"
        option.  The caller returns ``[]`` so ``international_fee`` is
        left blank and the ``no_international_fee`` guard rejects the
        course (domestic-only course — never available to international
        students).
      * ``None``            — not a UWL page or select not found; fall
        through to the generic extractor cascade.
    """
    from urllib.parse import urlparse as _urlparse

    host = (_urlparse(url or "").hostname or "").lower()
    if not (host == "www.uwl.ac.uk" or host.endswith(".uwl.ac.uk")):
        return None

    # Research-degree pages (/course/research/…) MUST be handled before the
    # standard blob reader.  All research pages embed a shared generic blob
    # entry (int=14000 / uk=4400) as the FIRST occurrence of the fee key —
    # this is a CMS placeholder, not a real fee.  The actual per-study-option
    # fees follow it in the SSR JSON:
    #   14000 → generic placeholder (skip)
    #   16000 → Full-time international  ← CORRECT (shown in JS dropdown)
    #    8000 → Part-time per-year rate A
    #    7000 → Part-time per-year rate B
    # The standard blob reader takes the first match (14000) and returns the
    # wrong fee for every research course.
    #
    # Fix: take the MAXIMUM int fee across ALL blob occurrences.  For research
    # courses the maximum is always the full-time fee (16000 > 14000 > 8000 >
    # 7000).  UG/PG courses are not affected — this branch only fires for
    # /course/research/ URLs.
    _path = (_urlparse(url or "").path or "").lower()
    if "/course/research/" in _path:
        _all_int_fees = re.findall(
            r'"field_p_cv_int_main_fee"\s*:\s*\{[^}]*"name"\s*:\s*"(\d+)"',
            html,
        )
        if _all_int_fees:
            _max_fee = max(int(f) for f in _all_int_fees)
            if _max_fee > 0:
                _amt = _parse_amount(str(_max_fee))
                if _amt is not None:
                    return _amt, f"UWL research blob max int_main_fee: £{_max_fee}"
        # No int blob at all — fall through to select / safety net below.
        # (If there IS a UK fee only the trailing guard returns domestic-only.)

    # Authoritative source FIRST: the Angular SSR JSON blob is embedded in both
    # static (render=False) and headless (render=True) HTML and always reflects
    # the real fees.  The JS-rendered <select> below is unreliable on static
    # HTML — its options are populated client-side, so on render=False pages it
    # can carry a partial/UK-only option set and yield a FALSE domestic-only
    # verdict even when an international fee exists.  Trust the blob when present.
    _blob = _from_uwl_json_blob(html)
    if _blob is not None:
        return _blob

    try:
        from bs4 import BeautifulSoup as _BS4
    except ImportError:  # pragma: no cover
        return None

    try:
        soup = _BS4(html, "html.parser")
    except Exception:  # pragma: no cover
        return None

    # Prefer the named select; fall back to ANY select whose options contain
    # the "– International" / "– UK" UWL fee pattern.
    _INTL_OPT_RE = re.compile(r"–\s*international", re.IGNORECASE)
    _UWL_FEE_OPT_RE = re.compile(r"£\s*[\d,]+\s*–\s*(international|uk)", re.IGNORECASE)

    select = soup.find("select", id="nationality_pricing_input_mobile")
    if select is None:
        # Try to find any select whose options match the UWL fee pattern
        for sel in soup.find_all("select"):
            opts = sel.find_all("option")
            if any(_UWL_FEE_OPT_RE.search(o.get_text(strip=True)) for o in opts):
                select = sel
                break

    if select is None:
        # No JSON blob (checked above) AND no select.
        # UWL research-degree pages (/course/research/…) use a completely
        # different Angular template that has neither the nationality-pricing
        # widget nor the SSR JSON blob.  Those pages expose a self-funded fee
        # schedule containing only the UK home rate (e.g. £6,000/yr), with no
        # explicit international split.  Letting the generic scanner run would
        # extract the domestic rate as the international fee — wrong.
        # Return domestic-only so these courses are skipped by the
        # no_international_fee gate and operators can fill the fee manually.
        from urllib.parse import urlparse as _up_res
        _path = (_up_res(url or "").path or "").lower()
        if "/course/research/" in _path:
            return _UWL_DOMESTIC_ONLY
        # Not a research page and no recognisable UWL fee layout — fall
        # through to the generic cascade.
        return None

    # Select is present.  Look for the "– International" option.
    intl_option_text: str | None = None
    has_any_option = False
    for opt in select.find_all("option"):
        opt_text = opt.get_text(strip=True)
        if not opt_text:
            continue
        has_any_option = True
        if _INTL_OPT_RE.search(opt_text):
            intl_option_text = opt_text
            break

    if not has_any_option:
        # Empty select and no JSON blob (checked above) — don't block on
        # incomplete render; fall through to the generic cascade.
        return None

    if intl_option_text is None:
        # Select exists and has options, but NONE are "– International".
        # This is a domestic-only course.
        return _UWL_DOMESTIC_ONLY

    # Parse the amount out of the option text (e.g. "£16,750 – International").
    m = _AMOUNT_RE.search(intl_option_text)
    if not m:
        return None
    raw_num = m.group(2) or m.group(3) or ""
    amount = _parse_amount(raw_num)
    if amount is None:
        return None
    ctx = f"UWL nationality select: {intl_option_text}"
    return amount, ctx


# ── Sheffield fee helpers ──────────────────────────────────────────────────
# Sheffield renders tuition fees inside <div class="feebox"> elements, which
# are injected by JavaScript from two sources:
#
#   UG courses (2026 entry, confirmed fees):
#     The browser-rendered page has .feebox divs in the #fees section:
#       <div class="feecost">£25,000</div>
#       <strong>Overseas students</strong>
#
#   PG taught courses:
#     The page's Drupal settings JSON embeds courseFees.pgt.{code, year}
#     which maps to: GET /api/course-fees/pgt/{code}/{year}
#     → JSON { "feesHtml": "<div class='feebox'>…</div>" }
#       <div class="feecost">£32,905</div>
#       <strong>Overseas students</strong>
#
#   UG 2027 courses:
#     Fees not confirmed for 2027-28 entry, page shows only home fee
#     and scholarship amounts. Neither feebox path applies.
#
# NOTE: The static HTML (without browser rendering) only contains scholarship
# amounts (£2,500/£3,000 "scholarships for international students") which
# would pass _INTL_CTX and be wrongly captured as fees.  When the Sheffield
# pre-pass fires and finds nothing, the cascade returns [] (NULL fee) so the
# scholarship amount never pollutes the staged record.
_SHEFFIELD_HOST_RE = re.compile(r"sheffield\.ac\.uk", re.IGNORECASE)
_UC_CANBERRA_HOST_RE = re.compile(r"(?:^|[./])canberra\.edu\.au(?:/|$)", re.IGNORECASE)
_SHEFFIELD_PGT_CODE_RE = re.compile(
    r'"courseFees"\s*:\s*\{"pgt"\s*:\s*\{"code"\s*:\s*"([A-Z0-9]+)"\s*,'
    r'\s*"year"\s*:\s*"(\d{4})"',
)


def _from_sheffield_feebox(html: str) -> "tuple[int, str] | None":
    """Parse Sheffield's .feebox fee section from browser-rendered HTML.

    Structure: <div class='feecost'>£AMOUNT</div><strong>Overseas students</strong>
    The label is BELOW the amount so the generic text-window scanner cannot pair them.
    Returns (amount, ctx) for the Overseas/International fee, or None if absent.
    """
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for box in soup.select(".feebox"):
        label_el = box.find("strong")
        cost_el = box.select_one(".feecost")
        if (
            label_el
            and cost_el
            and re.search(r"\b(overseas|international)\b", label_el.get_text(), re.IGNORECASE)
        ):
            # cost_el text looks like "£25,000" — use _AMOUNT_RE to strip
            # the currency symbol, then _parse_amount for the numeric part.
            _am = _AMOUNT_RE.search(cost_el.get_text())
            raw_num = (_am.group(2) or _am.group(3)) if _am else None
            amount = _parse_amount(raw_num) if raw_num else None
            if amount is not None and amount > 5_000:
                ctx = (
                    f"Sheffield feebox: {cost_el.get_text().strip()}"
                    f" ({label_el.get_text().strip()})"
                )
                return amount, ctx
    return None


async def _from_sheffield_pgt_api(html: str) -> "tuple[int, str] | None":
    """Call Sheffield's course-fees API for PG taught courses.

    The page's Drupal settings JSON exposes ``courseFees.pgt.{code, year}``.
    Returns (amount, ctx) for the Overseas fee, or None when not a PG page
    or the API is unavailable.
    """
    m = _SHEFFIELD_PGT_CODE_RE.search(html)
    if not m:
        return None
    code, year = m.group(1), m.group(2)
    import httpx as _httpx
    try:
        async with _httpx.AsyncClient(timeout=8.0) as _cl:
            r = await _cl.get(
                f"https://sheffield.ac.uk/api/course-fees/pgt/{code}/{year}",
                headers={"User-Agent": "Mozilla/5.0"},
            )
        if r.status_code != 200:
            return None
        data = r.json()
        fee_html = data.get("feesHtml", "")
        result = _from_sheffield_feebox(fee_html)
        if result is not None:
            amount, _ = result
            return amount, f"Sheffield PGT API ({code}/{year}): overseas fee"
    except Exception:
        pass
    return None


def _from_uc_hidden_inputs(html: str) -> "tuple[int, str] | None":
    """University of Canberra fee pre-pass.

    UC course pages embed annual fee history as hidden inputs in the static
    (plain-httpx) HTML:

        <input type="hidden" id="N-year"                 value="2026">
        <input type="hidden" id="N-eftsl-international"  value="41500">
        <input type="hidden" id="current-year"           value="2026">

    The index N is arbitrary (not sorted by year).  We build a year→fee dict
    from all present rows, then pick:
      1. The row whose year == current-year (from id="current-year").
      2. Fallback: the row with the latest year that is ≤ current-year.
      3. Fallback: the row with the globally latest year.

    Returns (amount_int, ctx_string) or None if no matching pair is found.
    """
    # Parse current-year from hidden input
    _cy_m = re.search(r'id="current-year"\s+value="(\d{4})"', html)
    _current_year = int(_cy_m.group(1)) if _cy_m else None

    # Collect all {N}-year values
    _year_by_idx: dict[str, int] = {}
    for m in re.finditer(r'id="(\d+)-year"\s+value="(\d{4})"', html):
        _year_by_idx[m.group(1)] = int(m.group(2))

    # Collect all {N}-eftsl-international values (strip whitespace)
    _fee_by_idx: dict[str, int] = {}
    for m in re.finditer(
        r'id="(\d+)-eftsl-international"\s+value="\s*(\d+)\s*"', html
    ):
        try:
            _fee_by_idx[m.group(1)] = int(m.group(2))
        except ValueError:
            pass

    if not _year_by_idx or not _fee_by_idx:
        return None

    # Build year→fee pairs (only indices that have both)
    _pairs: dict[int, int] = {}  # year → fee
    for idx, yr in _year_by_idx.items():
        if idx in _fee_by_idx:
            _pairs[yr] = _fee_by_idx[idx]

    if not _pairs:
        return None

    # Pick the best year
    if _current_year and _current_year in _pairs:
        _best_year = _current_year
    elif _current_year:
        # Latest year that does not exceed current-year
        _candidates = [y for y in _pairs if y <= _current_year]
        _best_year = max(_candidates) if _candidates else max(_pairs)
    else:
        _best_year = max(_pairs)

    _fee = _pairs[_best_year]
    if _fee <= 0:
        return None

    _ctx = f"UC hidden-input fee: id={{N}}-year={_best_year}, eftsl-international={_fee}"
    return _fee, _ctx


_UOW_HOST_RE = re.compile(r"(?:^|[./])uow\.edu\.au(?:/|$)", re.IGNORECASE)
_UOW_SESSION_FEE_RE = re.compile(r"\bsession\s+fee\b", re.IGNORECASE)
_UOW_COURSE_FEE_RE = re.compile(r"\bcourse\s+fee\b", re.IGNORECASE)


def _from_uow_session_fee_table(
    html: str, url: str
) -> "tuple[int, str] | None":
    """Read UOW's session fee instead of its adjacent full-course total.

    UOW course pages publish a compact table like::

        Campus | Delivery method | Session fee* | Course fee*
        Wollongong | On Campus | $22032 (2026) | $88128 (2026)

    The generic amount scanner sees both values in one short text window and
    breaks the tie by choosing the larger course total, which is then
    incorrectly labelled Annual.  This pre-pass is deliberately host-gated
    and requires both headers so it cannot reinterpret unrelated Australian
    fee tables.
    """
    if not _UOW_HOST_RE.search(url or "") or not html:
        return None
    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover
        return None

    for table in soup.find_all("table"):
        rows = [
            [cell.get_text(" ", strip=True) for cell in tr.find_all(["th", "td"])]
            for tr in table.find_all("tr")
        ]
        rows = [row for row in rows if row]
        if not rows:
            continue

        header_index: int | None = None
        session_index: int | None = None
        course_index: int | None = None
        for row_index, row in enumerate(rows):
            for cell_index, cell in enumerate(row):
                if _UOW_SESSION_FEE_RE.search(cell):
                    session_index = cell_index
                if _UOW_COURSE_FEE_RE.search(cell):
                    course_index = cell_index
            if session_index is not None and course_index is not None:
                header_index = row_index
                break
        if header_index is None or session_index is None:
            continue

        best: tuple[int, int, str] | None = None
        for row in rows[header_index + 1:]:
            if len(row) <= session_index:
                continue
            match = _AMOUNT_RE.search(row[session_index])
            if not match:
                continue
            raw = match.group(2) or match.group(3) or ""
            amount = _parse_amount(raw)
            if amount is None or not 1_000 <= amount <= 200_000:
                continue
            row_text = " | ".join(row)
            year = _extract_year(row_text) or -1
            if best is None or year > best[0]:
                best = (
                    year,
                    amount,
                    f"UOW fee table: Session fee {row[session_index]}"
                    f" | Course fee {row[course_index] if len(row) > course_index else ''}",
                )

        if best is not None:
            return best[1], best[2]
    return None


def _from_roehampton_int_tab(
    html: str, url: str
) -> "tuple[int, str] | None":
    """Roehampton-specific fee pre-pass: reads the International students fee
    from the 'Fees and funding' tabset.

    Roehampton course pages render two tab panels in the Fees section:
        class="tab-panel col-12 active"  → UK students  (£9,535 – £11,250)
        class="tab-panel col-12"         → International (£18,980 – £25,250)

    The generic cascade flattens all tab text and either picks the UK fee
    (it appears first in the DOM) or a spurious small amount whose context
    window accidentally captures an "International" nav link, bypassing the
    GBP floor guard.  This pre-pass reads ONLY the non-active International
    tab panel and returns the first plausible GBP amount (≥ £10,000).

    Returns (amount_int, ctx_string) or None when not applicable / section
    absent.
    """
    from urllib.parse import urlparse as _urlparse

    host = (_urlparse(url or "").hostname or "").lower()
    if "roehampton.ac.uk" not in host:
        return None

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover
        return None

    # Locate the "Fees and funding" tabset by its feature-heading h2.
    fees_tabset = None
    for tabset in soup.find_all("div", class_="tabset"):
        h2 = tabset.find(
            "h2",
            string=re.compile(r"Fees\s+and\s+funding", re.I),
        )
        if h2:
            fees_tabset = tabset
            break
    if fees_tabset is None:
        return None

    # Within the fee tabset find the non-active tab panel (International).
    for panel in fees_tabset.find_all("div", class_=True):
        cls: list[str] = panel.get("class") or []
        if "tab-panel" not in cls or "active" in cls:
            continue
        # Confirm International students panel.
        heading_text = " ".join(
            el.get_text(strip=True)
            for el in panel.find_all(["h2", "h3", "h4"])
        )
        if not re.search(r"\binternational\b", heading_text, re.I):
            continue
        # Extract first fee amount ≥ £10,000 from table cells.
        for td in panel.find_all("td"):
            raw = td.get_text(strip=True)
            m = _AMOUNT_RE.search(raw)
            if not m:
                continue
            raw_num = m.group(2) or m.group(3) or ""
            amount = _parse_amount(raw_num)
            if amount is None or amount < _GBP_INTL_MIN:
                continue
            ctx = f"Roehampton intl-tab: {raw[:80]}"
            return amount, ctx

    return None


def _from_bcu_int_fee_panel(
    html: str, url: str
) -> "tuple[int, str] | None":
    """BCU-specific fee extractor: reads the Full-Time annual fee from
    the 'International Student' tab section of the BCU fee panel.

    BCU course pages render two tab panels inside ``#fees_how_to_apply``:

        div#uk-students   → UK/home fee (£9,790 – £10,050)
        div#int-students  → International fee (£17,000 – £19,220)

    The generic regex cascade has no tab-boundary awareness and reads the
    UK fee first (it appears first in the DOM).  This reader reads only
    ``div#int-students div.course-fees-table__mode-row ul`` rows and
    returns the Full-Time fee, falling back to the first available row.

    Returns (amount_int, ctx_string) or None when not applicable /
    section absent.  Never returns a sentinel — callers treat None as
    "not found, continue with fallback".
    """
    from urllib.parse import urlparse as _urlparse

    host = (_urlparse(url or "").hostname or "").lower()
    if not (host == "www.bcu.ac.uk" or host.endswith(".bcu.ac.uk")):
        return None

    try:
        from bs4 import BeautifulSoup
    except ImportError:  # pragma: no cover
        return None

    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover
        return None

    int_panel = soup.find(id="int-students") or soup.select_one(
        ".course-fees__table--int-students"
    )
    if not int_panel:
        return None

    # Each mode row is: <div.course-fees-table__mode-row><ul> with
    #   <li class="mode">, <li class="duration">, <li class="fees ...">
    first_amount: int | None = None
    first_ctx: str | None = None
    for row_ul in int_panel.select("div.course-fees-table__mode-row ul"):
        mode_li = row_ul.select_one("li.mode")
        fees_li = row_ul.select_one("li.fees")
        if not fees_li:
            continue
        raw_fee = fees_li.get_text(strip=True)
        m = _AMOUNT_RE.search(raw_fee)
        if not m:
            continue
        raw_num = m.group(2) or m.group(3) or ""
        amount = _parse_amount(raw_num)
        if amount is None:
            continue
        ctx = f"BCU int-students Full Time: {raw_fee}"
        mode_text = (mode_li.get_text(strip=True) if mode_li else "").lower()
        if "full" in mode_text:
            # Exact Full-Time match — return immediately
            return amount, ctx
        # Keep first non-Full-Time row as fallback
        if first_amount is None:
            first_amount = amount
            first_ctx = ctx

    if first_amount is not None:
        return first_amount, first_ctx  # type: ignore[return-value]
    return None


def _from_scu_international_snapshot(
    html: str,
    url: str,
) -> tuple[int, str] | None:
    """Read SCU's authoritative annual fee from the international snapshot.

    SCU also publishes the same amount as ``$3,250 per unit`` beside total
    credit/equivalent-unit counts. The generic per-unit rollup can multiply
    that amount by the largest page-wide unit count and manufacture a
    full-course total. The audience-specific ``#int_snapshot_fee`` value is
    already the annual/first-year fee and must win unchanged.
    """
    if not re.search(r"https?://(?:www\.)?scu\.edu\.au/", url, re.I):
        return None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - malformed HTML / missing parser
        return None

    fee_node = soup.select_one("#int_snapshot_fee")
    if fee_node is None:
        return None
    value_text = compact(fee_node.get_text(" ", strip=True))
    amount_match = _AMOUNT_RE.search(value_text)
    if not amount_match:
        return None
    raw_num = amount_match.group(2) or amount_match.group(3) or ""
    amount = _parse_amount(raw_num)
    if amount is None:
        return None
    return amount, f"SCU International snapshot Indicative fee: {value_text}"


def _from_aut_international_fee_panel(
    html: str,
    url: str,
) -> tuple[float, str] | None:
    """Read and annualise AUT's headline international points-based total.

    AUT fee cards publish the total for the stated points first, followed by a
    parenthesised tuition-only subtotal and student-services levy. Generic
    candidate scoring can choose the tuition subtotal because it is explicitly
    labelled "tuition fees". The card's first currency amount is authoritative,
    includes the levy, and is converted to the standard 120-point annual load.
    """
    if not re.search(r"https?://(?:www\.)?aut\.ac\.nz/", url, re.I):
        return None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - malformed HTML / missing parser
        return None

    for heading in soup.select(".heading"):
        if compact(heading.get_text(" ", strip=True)).casefold() != "international":
            continue
        owner = heading.parent
        value_node = owner.select_one(".value") if owner is not None else None
        if value_node is None:
            continue
        value_text = compact(value_node.get_text(" ", strip=True))
        if not re.search(r"\b(?:points?|tuition\s+fees?)\b", value_text, re.I):
            continue
        # Match an explicitly currency-prefixed amount so a leading year in
        # "Not offered to new students in 2027" cannot be mistaken for the fee.
        amount_match = re.search(
            r"(?:NZD?\s*)?\$\s*([0-9][0-9,]*(?:\.\d+)?)",
            value_text,
            re.I,
        )
        if not amount_match:
            continue
        raw_num = amount_match.group(1)
        cleaned = _AMOUNT_STRIP_RE.sub("", raw_num)
        try:
            amount = float(cleaned)
        except ValueError:
            continue
        points_match = re.search(
            r"\(\s*for\s+(\d+(?:\.\d+)?)\s+points?\s*\)",
            value_text[amount_match.end():],
            re.I,
        )
        if points_match is None:
            continue
        points = float(points_match.group(1))
        if points <= 0:
            continue
        annual_amount = round(amount * 120.0 / points, 2)
        if 5_000 <= annual_amount <= 200_000:
            return (
                annual_amount,
                f"AUT International fee: {value_text} "
                f"[annualised from {points:g} points to 120 points]",
            )
    return None


def _from_uts_international_total(
    html: str,
    url: str,
) -> tuple[float, str] | None:
    """Read UTS's explicitly labelled international full-course total.

    UTS pages also contain a first-year amount and prose about paying fees
    "per session". A wide generic context can therefore select the first-year
    amount and mislabel it Session. The key-facts value is explicitly the
    indicative total for the entire course and should be annualised later by
    the UTS recipe using the course duration.
    """
    if not re.search(r"https?://(?:www\.)?uts\.edu\.au/", url, re.I):
        return None
    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - malformed HTML / missing parser
        return None

    text = compact(soup.get_text(" ", strip=True))
    label = re.search(
        r"\bindicative\s+total\s+tuition\s+fee\s+for\s+international\s+students\b",
        text,
        re.I,
    )
    if label is None:
        return None
    amount_match = _AMOUNT_RE.search(text, label.end())
    if amount_match is None or amount_match.start() - label.end() > 160:
        return None
    raw_num = amount_match.group(2) or amount_match.group(3) or ""
    cleaned = _AMOUNT_STRIP_RE.sub("", raw_num)
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if not 5_000 <= amount <= 500_000:
        return None
    ctx = text[label.start():min(len(text), amount_match.end() + 80)]
    return amount, ctx


def _from_swinburne_international_panel(
    html: str,
    url: str,
) -> tuple[float, str] | None:
    """Read Swinburne's SSR international yearly-fee panel.

    Swinburne emits domestic and international fee containers in the same
    document and toggles them with CSS.  Both use the ambiguous label
    ``Yearly fee* ($AUD)``, so flattening the page makes the first domestic
    yearly/total amount win.  Preserve the explicit ``.international`` DOM
    boundary and ignore the neighbouring SSAF block.
    """
    try:
        from urllib.parse import urlparse

        host = (urlparse(url or "").hostname or "").lower()
        if host != "swinburne.edu.au" and not host.endswith(".swinburne.edu.au"):
            return None

        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "html.parser")
    except Exception:  # pragma: no cover - malformed HTML / missing parser
        return None

    for container in soup.select(".course-fees__container.international"):
        for block in container.select(".course-fees__block"):
            label_el = block.select_one(".course-fees__sub-title")
            label = compact(label_el.get_text(" ", strip=True)) if label_el else ""
            if not re.search(r"\byearly\s+fee\b", label, re.I):
                continue
            value_el = block.select_one(".course-fees__total")
            value_text = compact(value_el.get_text(" ", strip=True)) if value_el else ""
            parsed = _classify_fee_value(value_text)
            ctx = compact(container.get_text(" ", strip=True))
            if parsed is None:
                # Swinburne uses an explicit $0.00 placeholder when no
                # international fee is published. Preserve that state as a
                # sentinel so the generic flattened-text fallback cannot claim
                # the neighbouring domestic yearly/total amount.
                amount_match = _AMOUNT_RE.search(value_text)
                if amount_match is None:
                    continue
                raw = amount_match.group(2) or amount_match.group(3) or ""
                amount = _parse_amount(raw)
                if amount is None or amount != 0:
                    continue
                return 0.0, ctx
            amount, _ = parsed
            return amount, ctx
    return None


def _from_leeds_beckett_international_panel(
    html: str,
    url: str,
) -> "tuple[float, str] | None":
    """Read Leeds Beckett's International tab without crossing into UK fees.

    Leeds Beckett renders the active UK panel first and a hidden International
    sibling in the same SSR document.  The international fee card has the
    stable ``color-bg-green-int`` class.  A zero sentinel means the page has an
    International tab but no published amount, preventing the generic flat-text
    cascade from claiming the preceding UK fee.
    """
    from urllib.parse import urlparse as _urlparse

    host = (_urlparse(url or "").hostname or "").lower()
    if host != "leedsbeckett.ac.uk" and not host.endswith(".leedsbeckett.ac.uk"):
        return None

    try:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html or "", "html.parser")
    except Exception:  # pragma: no cover - defensive
        return None

    root = soup.select_one("#fees-and-funding-component")
    if root is None:
        return None

    international_tab_exists = any(
        re.fullmatch(r"\s*international\s*", button.get_text(" ", strip=True), re.I)
        for button in root.select("button[role='tab']")
    )
    international_card = root.select_one(".color-bg-green-int")
    if international_card is not None:
        value_el = international_card.select_one(".key-info__item-value")
        value_text = value_el.get_text(" ", strip=True) if value_el else ""
        parsed = _classify_fee_value(value_text)
        if parsed is not None:
            amount, _ = parsed
            ctx = compact(international_card.get_text(" ", strip=True))
            return float(amount), f"Leeds Beckett international tab: {ctx}"

    if international_tab_exists:
        return 0.0, "Leeds Beckett International tab has no published fee"
    return None


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
    # Per-uni knob: when set, prefer the year-1 amount over a full-course
    # total when both labels appear on the same per-course page (Curtin).
    prefer_yr1 = False
    try:
        from app.services.scraper.config.context import get_uni_config
        _cfg = get_uni_config()
        if _cfg is not None:
            prefer_yr1 = bool(_cfg.extraction.fees.prefer_year_one_over_total)
    except Exception:  # noqa: BLE001 — defensive; keep extractor working
        prefer_yr1 = False

    latest_dated_fee = _select_latest_explicit_dated_fee(
        html,
        prefer_year_one=prefer_yr1,
    )
    if latest_dated_fee is not None:
        amount, fee_year, ctx, score, text = latest_dated_fee
        currency = _detect_currency(ctx, country)
        if currency in ("AUD", "CAD"):
            url_currency = _infer_currency_from_url(url)
            if url_currency:
                currency = url_currency
        fee_term = _normalize_fee_term(ctx, prefer_year_one=prefer_yr1)
        method = "fee.latest_explicit_year"
        rollup = _maybe_compute_full_course(amount, fee_term, text)
        if rollup is not None:
            amount, fee_term = rollup
            method += "+per_unit_rollup"
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": currency,
                    "fee_term": fee_term,
                    "fee_year": fee_year,
                },
                confidence=min(1.0, 0.4 + score * 0.1),
                snippet=ctx[:240],
                method=method,
            )
        ]

    swinburne_fee = _from_swinburne_international_panel(html, url)
    if swinburne_fee is not None:
        amount, ctx = swinburne_fee
        if amount == 0:
            return []
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": "AUD",
                    "fee_term": "Annual",
                    "fee_year": _extract_year(ctx),
                },
                confidence=0.99,
                snippet=ctx[:200],
                method="fee.swinburne_international_panel",
            )
        ]

    leeds_beckett_fee = _from_leeds_beckett_international_panel(html, url)
    if leeds_beckett_fee is not None:
        amount, ctx = leeds_beckett_fee
        if amount == 0:
            return []
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": "GBP",
                    "fee_term": _normalize_fee_term(
                        ctx, prefer_year_one=prefer_yr1
                    ),
                    "fee_year": _extract_year(ctx),
                },
                confidence=0.99,
                snippet=ctx[:200],
                method="fee.leeds_beckett_international_panel",
            )
        ]

    uts_total_fee = _from_uts_international_total(html, url)
    if uts_total_fee is not None:
        amount, ctx = uts_total_fee
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": "AUD",
                    "fee_term": "Full Course",
                    "fee_year": _extract_year(ctx),
                },
                confidence=0.99,
                snippet=ctx[:200],
                method="fee.uts_international_total",
            )
        ]

    aut_panel_fee = _from_aut_international_fee_panel(html, url)
    if aut_panel_fee is not None:
        amount, ctx = aut_panel_fee
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": "NZD",
                    "fee_term": "Annual",
                    "fee_year": _extract_year(ctx),
                },
                confidence=0.99,
                snippet=ctx[:200],
                method="fee.aut_international_panel",
            )
        ]

    scu_snapshot_fee = _from_scu_international_snapshot(html, url)
    if scu_snapshot_fee is not None:
        amount, ctx = scu_snapshot_fee
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": "AUD",
                    "fee_term": "Annual",
                    "fee_year": _extract_year(ctx),
                },
                confidence=0.98,
                snippet=ctx,
                method="fee.scu_int_snapshot",
            )
        ]

    # ── Pre-pass: audience-scoped SSR fee blocks ─────────────────────────────
    # Course pages can contain both domestic and international fee cards, with
    # CSS/JavaScript hiding the inactive audience.  Read the machine-readable
    # audience attribute before flattening the page so values cannot bleed
    # across student types.
    audience_fee = _extract_audience_scoped_fee(
        html, prefer_year_one=prefer_yr1
    )
    if audience_fee is not None:
        amount, ctx = audience_fee
        return [
            ExtractionResult(
                field_key="international_fee",
                value=amount,
                normalized={
                    "international_fee": amount,
                    "currency": _detect_currency(ctx, country),
                    "fee_term": _normalize_fee_term(
                        ctx, prefer_year_one=prefer_yr1
                    ),
                    "fee_year": _extract_year(ctx),
                },
                confidence=0.96,
                snippet=ctx[:200],
                method="fee.audience_structural",
            )
        ]

    # ── Pre-pass Sheffield: .feebox DOM + PGT fee API ────────────────────────
    # Sheffield course pages inject fees via JavaScript (browser-rendered .feebox
    # divs for UG 2026, or from GET /api/course-fees/pgt/{code}/{year} for PGT).
    # The static HTML only contains scholarship amounts (£2,500/£3,000
    # "scholarships for international students") which would pass _INTL_CTX and
    # be wrongly captured as tuition fees by the generic cascade.
    # Priority: feebox (browser-rendered) > PGT API > return [] (no fee).
    # For UG 2027 courses where fees are unconfirmed, both paths return None
    # → we return [] so the staged record gets NULL fee (correct) rather than
    # a scholarship amount.
    if _SHEFFIELD_HOST_RE.search(url):
        _shef_fee = _from_sheffield_feebox(html)
        if _shef_fee is None:
            _shef_fee = await _from_sheffield_pgt_api(html)
        if _shef_fee is not None:
            _shef_amount, _shef_ctx = _shef_fee
            return [
                ExtractionResult(
                    field_key="international_fee",
                    value=_shef_amount,
                    normalized={
                        "international_fee": _shef_amount,
                        "currency": "GBP",
                        "fee_term": "Annual",
                    },
                    confidence=0.93,
                    snippet=_shef_ctx[:200],
                    method="fee.sheffield",
                )
            ]
        return []  # Sheffield page but no fee found — suppress scholarship amounts

    # ── Pre-pass UWL: nationality-pricing select ─────────────────────────────
    # UWL (University of West London) Angular SPA pages expose a
    # nationality-switcher select (#nationality_pricing_input_mobile) with
    # options "£X – International" and "£Y – UK".  The generic text scanner
    # cannot distinguish these two options and occasionally picks the lower
    # UK amount because "– International" from the adjacent first option
    # bleeds into the context window of the second option's GBP figure,
    # giving it a spurious _INTL_CTX score.
    #
    # Three outcomes from _from_uwl_nationality_select:
    #   (amount, ctx)        → return the International fee immediately.
    #   _UWL_DOMESTIC_ONLY   → select present, no International option →
    #                          return [] (no fee) so the guard rejects the
    #                          course as domestic-only.
    #   None                 → not a UWL page or select absent → fall
    #                          through to the BCU / table / generic cascade.
    _uwl_result = _from_uwl_nationality_select(html, url)
    if _uwl_result is _UWL_DOMESTIC_ONLY:
        return []
    if _uwl_result is not None:
        _uwl_amount, _uwl_ctx = _uwl_result  # type: ignore[misc]
        return [
            ExtractionResult(
                field_key="international_fee",
                value=_uwl_amount,
                normalized={
                    "international_fee": _uwl_amount,
                    "currency": "GBP",
                    "fee_term": "Annual",
                },
                confidence=0.97,
                snippet=_uwl_ctx[:120],
                method="fee.uwl_nationality_select",
            )
        ]

    # ── Pre-pass Roehampton: International students tab panel ────────────────
    # Roehampton renders UK (active) and International (hidden) tab panels in
    # the "Fees and funding" tabset on every course page.  The generic cascade
    # picks the UK fee (appears first) or a small spurious amount whose context
    # window captured an "International" nav link (bypassing the GBP floor).
    # This pre-pass reads only the non-active International panel directly.
    _roehampt_fee = _from_roehampton_int_tab(html, url)
    if _roehampt_fee is not None:
        _rh_amount, _rh_ctx = _roehampt_fee
        return [
            ExtractionResult(
                field_key="international_fee",
                value=_rh_amount,
                normalized={
                    "international_fee": _rh_amount,
                    "currency": "GBP",
                    "fee_term": "Annual",
                    "fee_year": _extract_year(_rh_ctx),
                },
                confidence=0.96,
                snippet=_rh_ctx[:120],
                method="fee.roehampton_intl_tab",
            )
        ]

    # ── Pre-pass UC Canberra: hidden-input fee table ─────────────────────────
    # University of Canberra course pages at /course/CODE/VERSION/YEAR embed
    # historical-fee rows as hidden inputs in the static HTML:
    #   <input type="hidden" id="N-year" value="2026">
    #   <input type="hidden" id="N-eftsl-international" value="41500">
    #   <input type="hidden" id="current-year" value="2026">
    # The matching N-year → N-eftsl-international gives the annual fee for
    # each year.  We pick the row whose year == current-year (fallback: latest
    # year ≤ current-year) and return it as the AUD international fee.
    # The generic text cascade sees the International/domestic labelling in the
    # fee table summary row but no dollar amount, so it returns NULL — this
    # pre-pass is the ONLY reliable path to the fee for UC courses.
    if _UC_CANBERRA_HOST_RE.search(url or ""):
        _uc_fee = _from_uc_hidden_inputs(html)
        if _uc_fee is not None:
            _uc_amount, _uc_ctx = _uc_fee
            return [
                ExtractionResult(
                    field_key="international_fee",
                    value=_uc_amount,
                    normalized={
                        "international_fee": _uc_amount,
                        "currency": "AUD",
                        "fee_term": "Annual",
                    },
                    confidence=0.95,
                    snippet=_uc_ctx[:120],
                    method="fee.uc_hidden_input",
                )
            ]

    # ── Pre-pass UOW: session fee column ──────────────────────────────────────
    # UOW places the per-session amount beside a larger full-course total.
    # The latter must never be treated as the annual international fee.
    _uow_fee = _from_uow_session_fee_table(html, url)
    if _uow_fee is not None:
        _uow_amount, _uow_ctx = _uow_fee
        _uow_currency = _detect_currency(_uow_ctx, country)
        if _uow_currency == "AUD":
            _url_cur = _infer_currency_from_url(url)
            if _url_cur:
                _uow_currency = _url_cur
        return [
            ExtractionResult(
                field_key="international_fee",
                value=_uow_amount,
                normalized={
                    "international_fee": _uow_amount,
                    "currency": _uow_currency,
                    "fee_term": "Session",
                    "fee_year": _extract_year(_uow_ctx),
                },
                confidence=0.98,
                snippet=_uow_ctx[:160],
                method="fee.uow_session_table",
            )
        ]

    # ── Pre-pass BCU: tab-aware International Student fee panel ─────────────
    # BCU course pages expose two sibling div panels inside #fees_how_to_apply:
    #   div#uk-students   — UK/home fee (£9,790–£10,050, rendered FIRST in DOM)
    #   div#int-students  — International fee (£17k–£19k, rendered SECOND)
    # The generic regex cascade has no tab-boundary awareness and picks the
    # UK fee because it appears first in the flattened text.  This pre-pass
    # reads only div#int-students and returns the Full-Time fee directly.
    _bcu_fee = _from_bcu_int_fee_panel(html, url)
    if _bcu_fee is not None:
        _bcu_amount, _bcu_ctx = _bcu_fee
        return [
            ExtractionResult(
                field_key="international_fee",
                value=_bcu_amount,
                normalized={
                    "international_fee": _bcu_amount,
                    "currency": "GBP",
                    "fee_term": "Annual",
                    "fee_year": _extract_year(_bcu_ctx),
                },
                confidence=0.96,
                snippet=_bcu_ctx[:120],
                method="fee.bcu_int_panel",
            )
        ]

    # ── Pre-pass 0: structured fee table (highest priority) ─────────────────
    # UK universities publish multi-row tables with Home / International ×
    # Full-time / Part-time rows.  The flat-text scanner has no row-boundary
    # awareness and picks the first plausible amount (often the Home row).
    # This pre-pass reads the <table> DOM directly and returns the
    # International + Full-time row for the latest year — or blocks the
    # fallback entirely when no such row exists.
    #
    # GUARD: This logic is UK-specific.  Non-UK universities (e.g. Australian
    # .edu.au, NZ .ac.nz) may have course-page tables that superficially match
    # the "Home/International × Full-time/Part-time" pattern but are NOT fee
    # tables — causing false _FEE_TABLE_FOUND_NO_INTL signals that reject all
    # courses.  Only run this pre-pass for .ac.uk hosts.
    _fee_table_host = url or ""
    try:
        from urllib.parse import urlparse as _up
        _fee_table_host = _up(_fee_table_host).hostname or ""
    except Exception:
        pass
    _is_uk_fee_table_host = (
        _fee_table_host.endswith(".ac.uk") or _fee_table_host == "ac.uk"
    )
    _table_result = _extract_fee_table_row(html) if _is_uk_fee_table_host else None
    if _table_result is _FEE_TABLE_FOUND_NO_INTL:
        # Structured fee table exists but has NO International + Full-time row
        # (e.g. a part-time-only course like HNC Building Studies — Home /
        # Part-time only, no International pricing published for it at all).
        # This is a *definitive* per-course signal that the course is not
        # offered to international students — distinct from "we simply have
        # no fee data yet".  Surface it as its own evidence field so callers
        # (single_course.py's institutional degree_level_defaults fallback)
        # can skip filling in a flat/default international fee for this
        # specific course, instead of silently overwriting a confirmed
        # "no international offering" with a guessed institutional average.
        # Do NOT return a value for international_fee itself — the text-scan
        # fallback must never pick up the Home/part-time figure.
        return [
            ExtractionResult(
                field_key="fee_table_confirmed_no_international",
                value=True,
                normalized={"fee_table_confirmed_no_international": True},
                confidence=0.0,
                snippet="Structured fee table found, but no International + "
                        "Full-time row exists (Home/Part-time only).",
                method="fee.table_no_intl_row",
            )
        ]
    if _table_result is not None:
        _tbl_amount, _tbl_ctx = _table_result  # type: ignore[misc]
        _tbl_currency = _detect_currency(_tbl_ctx, country)
        if _tbl_currency == "AUD":
            _url_cur = _infer_currency_from_url(url)
            if _url_cur:
                _tbl_currency = _url_cur
        _tbl_fee_term = _normalize_fee_term(_tbl_ctx, prefer_year_one=prefer_yr1)
        return [
            ExtractionResult(
                field_key="international_fee",
                value=_tbl_amount,
                normalized={
                    "international_fee": _tbl_amount,
                    "currency": _tbl_currency,
                    "fee_term": _tbl_fee_term,
                    "fee_year": _extract_year(_tbl_ctx),
                },
                confidence=0.92,
                snippet=f"fee-table row: {_tbl_ctx[:120]}",
                method="fee.table_row",
            )
        ]

    # ── Pre-pass 1: strong label / dt-dd / th-td structural extractor ────────
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
        fee_term = _normalize_fee_term(value_ctx, prefer_year_one=prefer_yr1)
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

    # ── Pre-pass: "fee not yet published" sentinel ───────────────────────────
    # Some universities publish the page structure before setting the
    # international fee.  E.g. QMUL 2027 entry pages:
    #   <dt>Home fees</dt><dd>£9,790</dd>
    #   <dt>Overseas fees</dt><dd>Fees for 2027 entry will appear here shortly</dd>
    # When the overseas/international label's dd explicitly says the fee
    # hasn't been published yet, block the text-scan fallback so the
    # adjacent home fee is never captured as the international tuition fee.
    # The _CSP_DOMESTIC_CTX fix (home fees? plural) already handles this
    # for the candidates loop; this sentinel is belt-and-suspenders to
    # ensure zero false positives regardless of context window width.
    if html and re.search(
        r"<dt[^>]*>[^<]*(?:overseas|international)[^<]*</dt>\s*<dd[^>]*>[^<]*"
        r"(?:will\s+appear|not\s+yet\s+(?:set|published|available|confirmed)|"
        r"to\s+be\s+(?:confirmed|announced)|coming\s+soon|TBC|TBD)[^<]*</dd>",
        html,
        re.IGNORECASE | re.DOTALL,
    ):
        return []

    text = compact(html_to_text(html))
    if not text:
        return []
    best: tuple[int, int, int, str] | None = None  # (score, year, amount, ctx)
    for amount, _cur, ctx in _candidates(text):
        sc = _score(amount, ctx, prefer_year_one=prefer_yr1)
        candidate_year = _extract_year_for_amount(ctx, amount) or -1
        if best is None or sc > best[0]:
            best = (sc, candidate_year, amount, ctx)
        elif sc == best[0]:
            # Equally authoritative international tuition candidates are
            # ordered by their explicitly associated year before amount. This
            # prevents a larger old fee from beating a newer published fee.
            if candidate_year > best[1]:
                best = (sc, candidate_year, amount, ctx)
            elif candidate_year == best[1]:
                # Final deterministic tiebreak follows catalogue semantics:
                # normally larger/full-course, optionally smaller/year-one.
                if (not prefer_yr1 and amount > best[2]) or (
                    prefer_yr1 and amount < best[2]
                ):
                    best = (sc, candidate_year, amount, ctx)
    if best is None:
        return []
    score, _candidate_year, amount, ctx = best
    # Hard gate: never emit a fee unless the amount has at least one tuition
    # OR international cue in its window. This prevents random currency
    # numbers (deposits, scholarships, room costs) from being labelled as
    # the international tuition fee.
    if not (_TUITION_CTX.search(ctx) or _INTL_CTX.search(ctx)):
        return []
    currency = _detect_currency(ctx, country)
    # TLD-based override for the keyword path: when the scanner produces
    # AUD or CAD for a URL that implies a different currency (e.g. .ac.uk
    # → GBP, .ac.nz → NZD), replace with the TLD-inferred value.
    # This catches the CAD false-positive from bare C$ on UK pages.
    if currency in ("AUD", "CAD"):
        _url_cur = _infer_currency_from_url(url)
        if _url_cur:
            currency = _url_cur
    fee_term = _normalize_fee_term(ctx, prefer_year_one=prefer_yr1)
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
                "fee_year": _extract_year_for_amount(ctx, amount),
            },
            confidence=min(1.0, 0.4 + score * 0.1),
            snippet=ctx[:240],
            method=method,
        )
    ]
