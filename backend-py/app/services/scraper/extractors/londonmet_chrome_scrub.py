"""London Metropolitan University (londonmet.ac.uk) — brand-chrome scrub.

Background
----------
A May 2026 review-queue inspection showed that London Met (a UK
university with 100+ active programmes) was staging only **5 courses**
out of 322 discovered, and every one of those 5 carried clearly bogus
contamination from page brand chrome:

  * **All 5 fees were exactly £10,000/Annual.**  Gemini consistently
    returned ``international_fee=null`` for every London Met course,
    so the £10,000 had to be coming from a regex sweep.  The source
    is a sentence that appears in the chrome of every London Met
    postgraduate page:

        "Many of our students are eligible for a Postgraduate Loan
         of over £10,000.  Use the apply button to begin your
         application."

    This is **not** a tuition fee — it's a UK government loan
    advertisement — but the fee regex was matching ``£10,000`` and
    stamping it as the international fee.  The real per-programme
    fees live on a shared tuition-fees page (NOT on the per-course
    page itself) and require a separate fetcher to recover.

  * **2 of the 5 rows had** ``course_location = "London
    Metropolitan University"``.  The literal university name
    appears 3-4 times on every page (header, footer, breadcrumb,
    OG-meta) and was being picked up by the bag-of-text location
    fallback.

  * **3 of the 5 had** ``ielts_overall = 6``.  Gemini also returned
    ``null`` for every IELTS field.  The MBA page contains "Level 6
    or above from professional institutions such as the Chartered
    Management Institute" — almost certainly the source.

This module fixes the first two contamination sources by NULL-ing the
contaminated fields after extraction completes.  The third (IELTS)
is left for the longer-term fees-page-fetcher work because it
involves the same regex-vs-chrome trade-off and the cleanest fix is
to source IELTS from the dedicated tuition-fees / entry-requirements
page rather than blocklist phrases on the chrome page.

Why null instead of fixing the fee?
-----------------------------------
After this scrub runs, every London Met row will have
``international_fee=None`` and the existing ``should_stage_course``
gate will reject it with reason ``no_international_fee``.  This is
the correct outcome until the real fees can be sourced — a row with
a known-bogus £10,000 is worse than no row at all because it
silently corrupts downstream pricing displays.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_LM_HOSTS = ("www.londonmet.ac.uk", "londonmet.ac.uk")

# The exact phrase that triggers the £10,000 false positive.  Matched
# case-insensitively, with flexible whitespace, against the raw page
# HTML (lowercased).  We deliberately key on ``postgraduate loan`` —
# the unambiguous tell that this £ amount is a loan advert, not a fee.
_LOAN_BANNER_RE = re.compile(
    r"postgraduate\s+loan\s+of\s+over\s+(?:£|&pound;|&#163;)\s?10[,.]?000",
    re.IGNORECASE,
)

# The institution name as it appears in the brand chrome.  Allow
# common ALL-CAPS / Title-Case variations and incidental whitespace.
_BRAND_NAME_RE = re.compile(
    r"^\s*london\s+metropolitan\s+university\s*$",
    re.IGNORECASE,
)

# London Met embeds real per-mode tuition fees in HTML data-* attributes
# on each course page inside a <select id="course-entry-point-selector">:
#
#   <option data-fee-type="International" data-mode="Full-time"
#           data-cost="£17,500 for year 1, £10,000 for year 2, £5,000 for year 3"
#           data-m="September" data-y="2026" data-location="Holloway"
#           data-duration="3 years" ...>
#
# data-fee-type is "UK" or "International" — the explicit label we use to
# separate domestic from international entries.  Courses with NO
# data-fee-type="International" option at all are UK-only (domestic-only)
# and must be rejected by the pipeline.
#
# Fee format: either "£N per year" (simple) or tiered "£N for year 1,
# £M for year 2, ..." — in both cases the first £ amount is the correct
# Year 1 / annual fee to store (do NOT require "per year" in the string).
_DATA_COST_TAG_RE = re.compile(
    r'<[a-z]+\b[^>]*\bdata-cost="[^"]+"[^>]*>',
    re.IGNORECASE,
)
# data-([a-z-]+) — note the hyphen so data-fee-type is captured as key "fee-type".
_DATA_ATTR_RE = re.compile(r'data-([a-z][a-z-]*)="([^"]*)"', re.IGNORECASE)
_AMOUNT_RE = re.compile(r'(?:£|&pound;|&#163;)\s?([\d,]{2,12})')
# Kept for the legacy MIN/MAX fallback path (pages without data-fee-type labels).
_PER_YEAR_RE = re.compile(r'(?:per\s+year|/\s*yr|annual)', re.IGNORECASE)
# Detect the entry-point selector — its presence means the page IS a real
# course page with a fee table; its absence means no fee data at all.
_SELECTOR_RE = re.compile(r'id=["\']course-entry-point-selector["\']', re.IGNORECASE)
# Detect at least one International option in that selector.
_INTL_OPTION_RE = re.compile(r'data-fee-type=["\']International["\']', re.IGNORECASE)

_MONTH_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_data_cost_entries(html: str) -> list[dict[str, Any]]:
    """Parse every ``<elem ... data-cost="..." data-mode="..." ...>`` tag.

    Returns a list of dicts with keys:
      ``cost``      — first £ amount from data-cost (float)
      ``fee_type``  — "UK" | "International" | "" (from data-fee-type)
      ``mode``      — "Full-time" | "Part-time" | ""
      ``month``     — lowercase month name from data-m
      ``year``      — int from data-y, or None
      ``location``  — campus string from data-location, or None
      ``duration``  — duration string from data-duration, or None
      ``per_year``  — True iff cost string contains "per year" / "annual"
    """
    entries: list[dict[str, Any]] = []
    if not html:
        return entries
    for tag_match in _DATA_COST_TAG_RE.finditer(html):
        # data-fee-type uses a hyphen — _DATA_ATTR_RE now captures [a-z-]+ keys.
        attrs = {k.lower(): v for k, v in _DATA_ATTR_RE.findall(tag_match.group(0))}
        cost_str = attrs.get("cost", "")
        if not cost_str:
            continue
        amt_match = _AMOUNT_RE.search(cost_str)
        if not amt_match:
            continue
        try:
            amount = float(amt_match.group(1).replace(",", ""))
        except (ValueError, TypeError):
            continue
        try:
            year = int(attrs.get("y", "")) if attrs.get("y") else None
        except (ValueError, TypeError):
            year = None
        entries.append({
            "cost": amount,
            "fee_type": (attrs.get("fee-type") or "").strip(),
            "mode": (attrs.get("mode") or "").strip(),
            "month": (attrs.get("m") or "").strip().lower(),
            "year": year,
            "location": (attrs.get("location") or "").strip() or None,
            "duration": (attrs.get("duration") or "").strip() or None,
            "per_year": bool(_PER_YEAR_RE.search(cost_str)),
        })
    return entries


def extract_real_fees(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the international annual fee, intake months, location, and
    duration from parsed data-cost entries.

    Strategy (preferred path):
      Filter to entries with ``fee_type == "International"`` and
      ``mode == "Full-time"``.  Take the first £ amount as the annual
      international fee — this handles both simple "£N per year" and tiered
      "£N for year 1, £M for year 2, ..." formats correctly (Year 1 fee is
      the right value to display).

    Legacy fallback (pages without data-fee-type labels):
      If no explicitly-labelled International entries exist, fall back to the
      original MIN/MAX heuristic on Full-time per-year entries.
    """
    out: dict[str, Any] = {}

    # --- Preferred path: explicit data-fee-type="International" entries ---
    intl_ft = [
        e for e in entries
        if e.get("fee_type", "").lower() == "international"
        and "full-time" in e.get("mode", "").lower()
    ]
    if intl_ft:
        # Cohort isolation: use the earliest academic year to avoid mixing
        # a 2026 and a 2027 entry.
        years_present = {e["year"] for e in intl_ft if e["year"] is not None}
        if years_present:
            target_year = min(years_present)
            intl_ft = [e for e in intl_ft if e["year"] == target_year]
        # First entry is authoritative (same fee repeated per intake month).
        target = intl_ft[0]
        out["international_fee"] = target["cost"]
        out["fee_term"] = "Annual"
        out["currency"] = "GBP"
        if target["location"]:
            out["course_location"] = target["location"]
        if target["duration"]:
            out["duration"] = target["duration"]
        # Intake months from ALL International entries (full + part-time).
        all_intl = [e for e in entries if e.get("fee_type", "").lower() == "international"]
        months = sorted({
            _MONTH_TO_NUM[e["month"]] for e in all_intl if e["month"] in _MONTH_TO_NUM
        })
        if months:
            out["intake_months"] = months
        return out

    # --- Legacy fallback: no data-fee-type labels → use MIN/MAX heuristic ---
    full_year = [
        e for e in entries
        if e["per_year"] and "full-time" in e["mode"].lower()
    ]
    if not full_year:
        return out
    years_present = {e["year"] for e in full_year if e["year"] is not None}
    if years_present:
        target_year = min(years_present)
        full_year = [e for e in full_year if e["year"] == target_year]
    costs = sorted({e["cost"] for e in full_year})
    if len(costs) >= 2:
        out["domestic_fee"] = costs[0]
        out["international_fee"] = costs[-1]
    else:
        out["international_fee"] = costs[0]
    out["fee_term"] = "Annual"
    out["currency"] = "GBP"
    months = sorted({
        _MONTH_TO_NUM[e["month"]] for e in full_year if e["month"] in _MONTH_TO_NUM
    })
    if months:
        out["intake_months"] = months
    locations = [e["location"] for e in full_year if e["location"]]
    if locations:
        out["course_location"] = max(set(locations), key=locations.count)
    return out


def has_international_options(html: str) -> bool:
    """True iff the page has at least one ``data-fee-type="International"``
    option in the entry-point selector.

    When the selector is present but has NO International option the course
    is UK-only — international students cannot enrol.  The caller should
    treat this as a domestic-only rejection.

    Returns True (pass-through) when the selector is absent entirely, because
    the page may be a non-standard course format where we cannot determine
    eligibility from the selector alone — _DOMESTIC_ONLY_RE will handle it.
    """
    if not html:
        return True  # can't determine → don't block
    if not _SELECTOR_RE.search(html):
        return True  # no selector on this page → pass through
    return bool(_INTL_OPTION_RE.search(html))


def is_londonmet_host(url: str | None) -> bool:
    """True iff ``url`` is on the London Metropolitan University domain."""
    if not url:
        return False
    try:
        return (urlparse(url).netloc or "").lower() in _LM_HOSTS
    except Exception:  # noqa: BLE001
        return False


def page_has_loan_banner(html: str) -> bool:
    """True iff the page contains the ``Postgraduate Loan of over £10,000``
    advert that gives every London Met PG page a spurious fee match."""
    if not html:
        return False
    return bool(_LOAN_BANNER_RE.search(html))


def location_is_brand_name(value: str | None) -> bool:
    """True iff ``value`` is exactly the institution name (in any
    casing).  We deliberately do NOT match substring ("London
    Metropolitan University, Holloway") because such composite
    strings carry useful campus information and must be preserved."""
    if not value:
        return False
    return bool(_BRAND_NAME_RE.match(value))


def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Strip brand-chrome contamination from ``payload`` and detect
    domestic-only courses.

    Operations (in order):

    0. Domestic-only detection — if the page has the entry-point selector
       but zero ``data-fee-type="International"`` options, the course is
       UK-only.  ``applied["is_domestic_only"] = True`` is set so the
       caller can reject the course without staging it.

    1. If the page contains the ``Postgraduate Loan of over £10,000``
       advert AND ``payload["international_fee"]`` is exactly 10000,
       null out the fee triple (fee, fee_term, currency).

    2. If ``payload["course_location"]`` equals the literal institution
       name, null it out (brand-chrome leak).

    3. Real-fee recovery from ``data-cost`` attributes — fills
       ``international_fee``, ``intake_months``, ``course_location``,
       and ``duration`` when those fields are still empty after steps 1-2.

    Returns a dict describing which overrides fired.  Callers MUST check
    ``applied.get("is_domestic_only")`` and reject the course if True.
    """
    applied: dict[str, Any] = {}
    if not html:
        return applied

    # Operation 0 — domestic-only detection.
    if not has_international_options(html):
        applied["is_domestic_only"] = True
        log.info("[LM CHROME SCRUB] %s — no International options → domestic-only", url or "(no url)")
        return applied  # nothing else to do; caller will reject the course

    # Bug 1 — kill the loan-banner false-positive fee.
    fee = payload.get("international_fee")
    if fee is not None and abs(float(fee) - 10000.0) < 0.5 and page_has_loan_banner(html):
        prev_fee = payload["international_fee"]
        prev_term = payload.get("fee_term")
        prev_currency = payload.get("currency")
        payload["international_fee"] = None
        payload["fee_term"] = None
        payload["currency"] = None
        applied["international_fee"] = {"old": prev_fee, "new": None}
        if evidence is not None:
            evidence.append({
                "field_key": "international_fee",
                "method": "londonmet_chrome_scrub:loan_banner",
                "source_url": url,
                "raw_value": None,
            })
        log.info(
            "[LM CHROME SCRUB] %s — fee=%r/%s/%s nulled (loan banner)",
            url or "(no url)",
            prev_fee, prev_term, prev_currency,
        )

    # Bug 2 — strip brand-name location.
    loc = payload.get("course_location")
    if location_is_brand_name(loc):
        payload["course_location"] = None
        applied["course_location"] = {"old": loc, "new": None}
        if evidence is not None:
            evidence.append({
                "field_key": "course_location",
                "method": "londonmet_chrome_scrub:brand_name",
                "source_url": url,
                "raw_value": None,
            })
        log.info(
            "[LM CHROME SCRUB] %s — course_location=%r nulled (brand name)",
            url or "(no url)", loc,
        )

    # Real-fee recovery — pull tuition fees from the data-cost attributes
    # that London Met embeds on every course page.  This runs AFTER the
    # loan-banner null above so the real fee replaces the bogus one.
    # Only fields that are currently empty on the payload are filled.
    real = extract_real_fees(parse_data_cost_entries(html))
    for field in ("international_fee", "domestic_fee", "fee_term", "currency",
                  "intake_months", "course_location"):
        new_val = real.get(field)
        if new_val is None:
            continue
        cur_val = payload.get(field)
        # Treat empty list / empty string as missing so we still fill them.
        is_empty = cur_val is None or cur_val == "" or cur_val == []
        if not is_empty:
            continue
        payload[field] = new_val
        applied[field] = {"old": cur_val, "new": new_val}
        if evidence is not None:
            evidence.append({
                "field_key": field,
                "method": "londonmet_chrome_scrub:data_cost_attr",
                "source_url": url,
                "raw_value": new_val,
            })
    if any(k in applied for k in ("international_fee", "domestic_fee")) and (
        real.get("international_fee") or real.get("domestic_fee")
    ):
        log.info(
            "[LM CHROME SCRUB] %s — data-cost fees recovered: intl=%s dom=%s",
            url or "(no url)",
            real.get("international_fee"), real.get("domestic_fee"),
        )

    return applied
