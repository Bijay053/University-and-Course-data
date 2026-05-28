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
# on each course page, e.g.:
#   <... data-cost="£11,000 per year" data-mode="Full-time"
#        data-m="September" data-y="2026" data-location="Holloway" ...>
# Every page has 6 entries (3 domestic, 3 international) — domestic is the
# LOWEST "Full-time per year" amount and international is the HIGHEST.
# Part-time-per-module entries are ignored (different fee unit).
_DATA_COST_TAG_RE = re.compile(
    r'<[a-z]+\b[^>]*\bdata-cost="[^"]+"[^>]*>',
    re.IGNORECASE,
)
_DATA_ATTR_RE = re.compile(r'data-([a-z]+)="([^"]*)"', re.IGNORECASE)
_PER_YEAR_RE = re.compile(r'(?:per\s+year|/\s*yr|annual)', re.IGNORECASE)
_AMOUNT_RE = re.compile(r'(?:£|&pound;|&#163;)\s?([\d,]{2,12})')

_MONTH_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def parse_data_cost_entries(html: str) -> list[dict[str, Any]]:
    """Parse every ``<elem ... data-cost="..." data-mode="..." ...>`` tag.

    Returns a list of dicts with keys ``cost`` (float £), ``mode``,
    ``month`` (lowercase), ``year`` (int), ``location``, and ``per_year``
    (bool — True iff the cost string contains "per year").
    """
    entries: list[dict[str, Any]] = []
    if not html:
        return entries
    for tag_match in _DATA_COST_TAG_RE.finditer(html):
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
            "mode": (attrs.get("mode") or "").strip(),
            "month": (attrs.get("m") or "").strip().lower(),
            "year": year,
            "location": (attrs.get("location") or "").strip() or None,
            "per_year": bool(_PER_YEAR_RE.search(cost_str)),
        })
    return entries


def extract_real_fees(entries: list[dict[str, Any]]) -> dict[str, Any]:
    """From parsed data-cost entries, derive the real domestic + international
    annual fees, the canonical intake months, and the campus location.

    Strategy: filter to ``Full-time``/``per year`` entries, then take the
    MIN cost as the domestic fee and the MAX as the international fee
    (London Met always lists both side-by-side in this format).
    Intake months are collected from any per-year entry.
    """
    out: dict[str, Any] = {}
    full_year = [
        e for e in entries
        if e["per_year"] and "full-time" in e["mode"].lower()
    ]
    if not full_year:
        return out
    # Cohort isolation: if multiple academic years are present (e.g. 2026
    # alongside 2027), confine the MIN/MAX comparison to the EARLIEST year
    # so we never pair a 2026 domestic fee with a 2027 international fee.
    years_present = {e["year"] for e in full_year if e["year"] is not None}
    if years_present:
        target_year = min(years_present)
        full_year = [e for e in full_year if e["year"] == target_year]
    costs = sorted({e["cost"] for e in full_year})
    if len(costs) >= 2:
        # Distinct domestic + international values present.
        out["domestic_fee"] = costs[0]
        out["international_fee"] = costs[-1]
    else:
        # Only one value — assume international (safer for our gate).
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
        # Most common location wins.
        out["course_location"] = max(set(locations), key=locations.count)
    return out


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
    """Strip brand-chrome contamination from ``payload``.

    Two independent operations:

    1. If the page contains the ``Postgraduate Loan of over £10,000``
       advert AND ``payload["international_fee"]`` is exactly 10000,
       null out the fee triple (fee, fee_term, currency).  We do
       NOT touch other fees — only the bogus loan-advert match.

    2. If ``payload["course_location"]`` equals the literal
       institution name (any casing, e.g. ``"London Metropolitan
       University"``), null it out.  Brand-chrome leak from
       header/footer/OG-meta.

    Returns a dict describing which overrides fired.
    """
    applied: dict[str, Any] = {}
    if not html:
        return applied

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
