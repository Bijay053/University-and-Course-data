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
# Full-course-total context — strongly prefer over annual / per-year amounts
# (Murdoch shows "Full course fee: $125,970" alongside "First year fee: $41,990").
_FULL_COURSE_LABEL_CTX = re.compile(
    # Allow up to one optional adjective between "total" / "full" and "course"
    # so labels like Curtin's "Total indicative course fee" still match
    # (previously only literal "total course fee" matched, so $141,348 was
    # silently classified as Annual instead of Full Course).
    r"\b(?:full|total|complete)\s+(?:\w+\s+)?(?:course|program(?:me)?)\s+fee",
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
    r"domestic\s+(?:student\s+)?(?:tuition\s+)?fee|"
    # UK home-student labels
    r"home\s+(?:student\s+)?(?:tuition\s+)?fee|"
    r"uk\s+(?:student\s+)?(?:tuition\s+)?fee|"
    r"(?:for\s+)?(?:uk|home)\s+students?|"
    # Per-module / per-credit / CPD prices (never annual international tuition)
    r"per\s+(?:module|credit|unit\s+of\s+credit)|"
    r"module\s+fee|credit\s+fee|"
    r"(?:continuing\s+professional\s+development|cpd)\s+(?:fee|rate|price)|"
    r"part[\s\-]time\s+(?:fee|rate|tuition))\b",
    re.IGNORECASE,
)

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
        if amount < 1_000 or amount > 200_000:
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
        # CSP / domestic fee guard: reject amounts whose immediate context
        # mentions "Commonwealth Supported Place", "HECS", "student
        # contribution", "domestic fee", "home student fee", "UK student fee",
        # "per module", "per credit", "CPD fee", or "part-time fee".
        # These are domestic/module prices and must never be stored as the
        # international annual tuition fee.
        if _CSP_DOMESTIC_CTX.search(ctx):
            continue
        # GBP floor guard for UK universities.
        # Genuine international annual fees at UK universities are ≥ £10,000.
        # Any GBP amount below this threshold is almost certainly a domestic
        # (home-student) fee, a per-module price, a CPD rate, or a part-time
        # module charge — unless the immediate context contains an explicit
        # "international" cue that confirms this is the international fee.
        if cur == "GBP" and amount < _GBP_INTL_MIN and not _INTL_CTX.search(ctx):
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
    return s


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
        # No JSON blob (checked above) AND no select — not a recognisable UWL
        # fee layout.  Fall through to the generic cascade.
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
    _table_result = _extract_fee_table_row(html)
    if _table_result is _FEE_TABLE_FOUND_NO_INTL:
        # Structured fee table exists but has NO International + Full-time row
        # (e.g. a part-time-only course like HNC Building Studies).
        # Return empty so the text-scan fallback never picks up a Home fee.
        return []
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

    text = compact(html_to_text(html))
    if not text:
        return []
    best: tuple[int, int, str] | None = None  # (score, amount, ctx)
    for amount, _cur, ctx in _candidates(text):
        sc = _score(amount, ctx, prefer_year_one=prefer_yr1)
        if best is None or sc > best[0]:
            best = (sc, amount, ctx)
        elif sc == best[0]:
            # Default tiebreak: prefer larger amount (full-course wins).
            # When the per-uni knob flips the preference, prefer smaller
            # so the year-1 figure beats any larger numeric on the page
            # that happens to score equally (e.g. mid-year intake total).
            if (not prefer_yr1 and amount > best[1]) or (
                prefer_yr1 and amount < best[1]
            ):
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
                "fee_year": _extract_year(ctx),
            },
            confidence=min(1.0, 0.4 + score * 0.1),
            snippet=ctx[:240],
            method=method,
        )
    ]
