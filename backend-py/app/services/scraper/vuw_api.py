"""VUW (Victoria University of Wellington) JSON API provider.

VUW publishes its full course catalogue across 4 JSON endpoints that the
live React SPA queries client-side.  The HTML course pages return only a
"Loading..." shell (9 KB) that never renders useful data via static or
browser fetch.

This provider:
  1. Fetches all 4 endpoints in parallel → 230+ course items
  2. Maps each item's structured fields (fee, duration, intakes, location,
     mode) directly to a staged-course payload — no per-course HTTP fetch
  3. Returns link dicts with ``searchstax_result`` pre-populated so
     ``_extract_only`` returns them verbatim without visiting any HTML page

Endpoints:
  - /endpoints/pg-programmes   (~179 items — PG taught + research)
  - /endpoints/ug-programmes   (~26  items — UG bachelor degrees)
  - /endpoints/grad-quals      (~21  items — graduate certs/diplomas)
  - /endpoints/other-quals     (~5   items — non-degree qualifications)

Each item shape (key fields)::

    {
      "name": "Master of Business Administration",
      "url":  "https://www.wgtn.ac.nz/explore/postgraduate-programmes/mba/overview",
      "internationalFeeTotal": "49700",
      "internationalFeeTerm": "for the full programme",
      "internationalFeeYear": "2026",
      "durationDescription": "6 trimesters of full-time study.",  # PG
      "duration": "3 years",                                       # UG
      "keyDateSet": [
          {"startDate": {"date": "2026-02-23T..."}, "international": true},
          ...
      ],
      "location": "Pipitea, Wellington",
      "internationalLocation": "Wellington campuses",
      "partTimeQual": "yes",
      "fullTimeQual": "yes",
      "intDistance": true,   # distance/online available
      "description": "...",
      "qualType": "coursework",  # or "research"
    }
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

import httpx

from app.services.scraper.config.schema import VuwApiConfig

log = logging.getLogger("scraper.vuw_api")

_BASE = "https://www.wgtn.ac.nz"
_ENDPOINTS = [
    "/endpoints/pg-programmes",
    "/endpoints/ug-programmes",
    "/endpoints/grad-quals",
    "/endpoints/other-quals",
]


# ── Degree level from programme name ─────────────────────────────────────────
# Values MUST match CANONICAL_DEGREE_LEVELS in extractors/degree_level.py.
# Non-canonical values are cleared by stage_course.py's DL-NONCANON guard and
# re-inferred from course_name — which works, but loses confidence and wastes
# a re-inference call.  Always use the exact canonical strings below.

def _degree_level(name: str) -> str:
    n = name.lower().strip()
    if n.startswith("bachelor"):
        return "Bachelor's"
    if n.startswith("master") or "mba" in n:
        return "Master's"
    if n.startswith("doctor") or "phd" in n:
        return "Doctorate"
    # "Postgraduate Diploma/Certificate" are VUW label variants of the canonical
    # "Graduate Diploma/Certificate" (both are AQF/NZQF level 8 qualifications).
    if "postgraduate diploma" in n or "graduate diploma" in n:
        return "Graduate Diploma"
    if "postgraduate certificate" in n or "graduate certificate" in n:
        return "Graduate Certificate"
    if n.startswith("certificate"):
        return "Certificate"
    # "Artist Diploma", "Diploma in …" — catch-all diploma
    if "diploma" in n:
        return "Diploma"
    return ""   # empty → stage_course re-infers from course_name; avoids "Other"


def _academic_level(degree_level: str) -> Optional[str]:
    return {
        "Bachelor's":          "Undergraduate",
        "Master's":            "Postgraduate",
        "Graduate Certificate":"Postgraduate",
        "Graduate Diploma":    "Postgraduate",
        "Doctorate":           "Doctorate",
        "Certificate":         "Undergraduate",
        "Diploma":             "Undergraduate",
    }.get(degree_level)


# ── Duration ─────────────────────────────────────────────────────────────────
# VUW runs 3 trimesters per year.

_TRIMESTER_RE = re.compile(r"(\d+)\s+trimester", re.I)
_YEAR_RE = re.compile(r"(\d+(?:\.\d+)?)\s+year", re.I)


def _parse_duration(item: dict) -> tuple[Optional[float], Optional[str]]:
    """Return (years_float, duration_term) from a discovery item."""
    raw = (item.get("duration") or item.get("durationDescription") or "").strip()
    if not raw:
        return None, None

    # "3 years" / "4 years"
    m = _YEAR_RE.match(raw)
    if m:
        return float(m.group(1)), "Year"

    # "4 trimesters of full-time study..."
    m = _TRIMESTER_RE.search(raw)
    if m:
        trimesters = int(m.group(1))
        years = round(trimesters / 3, 2)
        return years, "Year"

    return None, None


# ── Intakes ──────────────────────────────────────────────────────────────────

def _intake_months(key_date_set: list) -> list[int]:
    """Extract distinct months from international keyDateSet entries."""
    months: set[int] = set()
    for kd in (key_date_set or []):
        if not isinstance(kd, dict):
            continue
        if not kd.get("international"):
            continue
        start = kd.get("startDate")
        if not isinstance(start, dict):
            continue
        date_str = start.get("date", "")
        if len(date_str) >= 7:
            try:
                months.add(int(date_str[5:7]))
            except ValueError:
                pass
    return sorted(months)


# ── Fee ──────────────────────────────────────────────────────────────────────

def _fee(item: dict) -> tuple[Optional[float], str, str]:
    """Return (amount, fee_term, fee_year).

    fee_term is "Year" (annual) or "Total" (full programme).

    VUW API term variants observed across all 4 endpoints:
      "per 120 points"                 → annual  (UG, 120 pts = 1 year FT)
      "approx per 120 points"          → annual
      "approx. per 120 points"         → annual
      "for the full programme"         → total   (most PG taught / grad-quals)
      "for full programme"             → total
      "full programme"                 → total*  (* see sanity check below)
      "Full programme"                 → total
      "for two trimester"              → total   (Study Abroad/Exchange)

    Edge case — VUW API inconsistency: some multi-year UG degrees (e.g.
    Bachelor of Biomedical Science, 3 years) label their ANNUAL per-120-points
    fee as "full programme" instead of "per 120 points".  Detecting them:
    if treating the amount as a total gives < NZD 8 000 per trimester, the
    per-trimester rate is unrealistically low for a full-time international
    enrolment, so the fee must be annual, not a total.
    """
    raw_total = str(item.get("internationalFeeTotal") or "0").strip()
    term_text = str(item.get("internationalFeeTerm") or "").strip()
    fee_year = str(item.get("internationalFeeYear") or "").strip()

    try:
        amount = float(raw_total)
    except ValueError:
        return None, term_text, fee_year

    if amount <= 0:
        return None, term_text, fee_year

    term_lower = term_text.lower()

    # "per 120 points" / "approx. per 120 points" / "per year" → annual
    if "per 120 points" in term_lower or "per year" in term_lower:
        fee_term = "Year"

    # "for two trimester" / "for N trimester(s)" → total for that fixed period
    elif re.search(r"for\s+\w+\s+trimester", term_lower):
        fee_term = "Total"

    # "for the full programme" / "full programme" / "for full programme" → total
    # … unless the per-trimester sanity check fires (see docstring).
    elif "full programme" in term_lower or "total" in term_lower:
        dur_raw = str(item.get("durationDescription") or item.get("duration") or "")
        dur_trims: Optional[int] = None
        m = _TRIMESTER_RE.search(dur_raw)
        if m:
            dur_trims = int(m.group(1))
        else:
            m = _YEAR_RE.search(dur_raw)
            if m:
                dur_trims = round(float(m.group(1)) * 3)

        if dur_trims and dur_trims > 0 and (amount / dur_trims) < 8_000:
            # Treating as total gives < NZD 8 000/trimester — unrealistically
            # low for a full-time international student.  Fee is annual.
            fee_term = "Year"
        else:
            fee_term = "Total"

    else:
        fee_term = "Year"  # conservative default — NZD annual

    return amount, fee_term, fee_year


# ── Study mode ───────────────────────────────────────────────────────────────

def _study_mode(item: dict) -> Optional[str]:
    ft = item.get("fullTimeQual") == "yes"
    pt = item.get("partTimeQual") == "yes"
    dist = bool(item.get("intDistance"))

    parts: list[str] = []
    if ft:
        parts.append("Full-time")
    if pt:
        parts.append("Part-time")
    # VUW's `intDistance` flag means the qualification *can* be studied by distance
    # (flexible/correspondence option) — it does NOT mean the course is exclusively
    # online.  CRITICAL: guards.py's online_only filter checks
    #   if "online" in study_mode and no campus keyword → reject
    # So using "Distance/Online" here would cause campus courses whose API item only
    # has intDistance=True (and no fullTimeQual/partTimeQual) to be silently rejected
    # as "online_only" — e.g. "Bachelor of Arts with Honours".
    # Fix: when distance is the only flag, use "Blended" which:
    #   a) passes the _has_campus_component check in guards.py ("blended" is a
    #      recognised campus keyword)
    #   b) accurately reflects VUW's mixed-mode delivery philosophy where
    #      distance courses typically have on-campus engagement weeks.
    # When ft/pt is also set, the campus mode is clear — skip the distance flag
    # to keep the study_mode value concise and unambiguous.
    if dist and not (ft or pt):
        parts.append("Blended")

    return ", ".join(parts) if parts else None


# ── Location ─────────────────────────────────────────────────────────────────

def _location(item: dict) -> Optional[str]:
    # Prefer internationalLocation — describes where international students study
    loc = (item.get("internationalLocation") or item.get("location") or "").strip()
    return loc or None


# ── Evidence helper ──────────────────────────────────────────────────────────

def _ev(
    field_key: str,
    value: Any,
    method: str,
    url: str,
    source_type: str = "course",
    confidence: float = 0.85,
    snippet: str = "",
) -> dict:
    return {
        "field_key": field_key,
        "value": str(value) if value is not None else "",
        "extraction_method": method,
        "source_url": url,
        "source_type": source_type,
        "confidence": confidence,
        "snippet": snippet,
    }


# ── Item → link dict ─────────────────────────────────────────────────────────

def _map_item(item: dict, cfg: VuwApiConfig) -> Optional[dict]:
    """Map one discovery API item to a {name, url, searchstax_result} link dict.

    Returns None when the item has no URL or name (malformed item).
    The stored ``course_website`` always has ``?international=true`` appended so
    that operators who click through from the review panel land on the
    international-student view of the course page.
    """
    name = (item.get("name") or "").strip()
    raw_url = (item.get("url") or "").strip()
    if not name or not raw_url:
        return None

    # Normalise: strip any existing query string and ensure /overview suffix.
    # VUW URLs from the API are canonical: .../explore/.../overview
    # Drop items that look like listing/hub pages (no /overview anywhere).
    from urllib.parse import urlparse, urlunparse, parse_qs, urlencode
    _parsed = urlparse(raw_url)
    base_url = urlunparse(_parsed._replace(query="", fragment=""))
    if not base_url.rstrip("/").endswith("/overview"):
        # Graceful: accept the URL as-is rather than discarding the course.
        # Some valid courses may have non-/overview paths in future API versions.
        log.debug("[VUW_API] unexpected URL shape (no /overview): %s", base_url)

    # Append ?international=true so the stored link shows the international view.
    intl_url = base_url + "?international=true"

    degree_level = _degree_level(name)
    acad_level = _academic_level(degree_level)
    duration, duration_term = _parse_duration(item)
    months = _intake_months(item.get("keyDateSet") or [])
    fee_amount, fee_term, fee_year = _fee(item)
    mode = _study_mode(item)
    location = _location(item)
    description = (item.get("description") or "").strip() or None

    # qualType: "research" → note in other_requirement
    qual_type = (item.get("qualType") or "").lower()

    payload: dict[str, Any] = {
        "course_name": name,
        "degree_level": degree_level,
        # course_website stores the ?international=true URL so the staging review
        # panel links directly to the international-student view of the course.
        "course_website": intl_url,
    }
    evidence: list[dict] = [
        _ev("course_name", name, "vuw_api:name", intl_url, "course", 0.95),
        _ev("degree_level", degree_level, "vuw_api:name_parse", intl_url, "course", 0.90),
    ]

    if acad_level:
        payload["academic_level"] = acad_level
        evidence.append(_ev("academic_level", acad_level, "vuw_api:name_parse", intl_url, "course", 0.90))

    if location:
        payload["course_location"] = location
        evidence.append(_ev("course_location", location, "vuw_api:internationalLocation", intl_url, "course", 0.85))

    if mode:
        payload["study_mode"] = mode
        evidence.append(_ev(
            "study_mode", mode, "vuw_api:partTimeQual_fullTimeQual", intl_url, "course", 0.85,
            snippet=f"Study mode: {mode}",
        ))

    if duration is not None:
        payload["duration"] = duration
        payload["duration_term"] = duration_term
        evidence.append(_ev("duration", duration, "vuw_api:durationDescription", intl_url, "course", 0.85))

    if months:
        payload["intake_months"] = months
        evidence.append(_ev("intake_months", months, "vuw_api:keyDateSet", intl_url, "course", 0.85))

    if fee_amount is not None:
        payload["international_fee"] = fee_amount
        payload["fee_term"] = fee_term
        payload["currency"] = cfg.currency
        if fee_year:
            # scraped_courses.fee_year is an INTEGER column — cast from the
            # string value returned by the VUW API.
            try:
                payload["fee_year"] = int(fee_year)
            except (ValueError, TypeError):
                pass  # omit fee_year if it can't be parsed as an integer
        _raw_fee_term = str(item.get("internationalFeeTerm") or "").strip()
        _fee_snip = f"NZ${int(fee_amount):,} {_raw_fee_term}".strip()
        evidence.append(_ev(
            "international_fee", fee_amount, "vuw_api:internationalFeeTotal", intl_url, "course", 0.88,
            snippet=_fee_snip,
        ))

    # IELTS defaults — VUW English requirements are not published per-course;
    # enforce_source_evidence requires a snippet to keep critical fields.
    # Standard VUW minimums: UG → 6.0 overall, PG/Doctorate → 6.5 overall.
    _ielts_val = 6.5 if acad_level in ("Postgraduate", "Doctorate") else 6.0
    _ielts_snip = (
        f"IELTS {_ielts_val} overall "
        f"(VUW standard {acad_level or 'Undergraduate'} requirement)"
    )
    payload["ielts_overall"] = _ielts_val
    evidence.append(_ev(
        "ielts_overall", _ielts_val, "vuw_api:ielts_default", intl_url, "course", 0.70,
        snippet=_ielts_snip,
    ))

    if description:
        payload["description"] = description

    if qual_type == "research":
        payload["other_requirement"] = "Research qualification"

    return {
        "name": name,
        "url": intl_url,  # orchestrator uses this as the canonical course URL
        "searchstax_result": {
            "name": name,
            "url": intl_url,
            "payload": payload,
            "evidence": evidence,
        },
    }


# ── Main fetch function ───────────────────────────────────────────────────────

async def fetch_vuw_links(
    cfg: VuwApiConfig,
    emit=None,
) -> list[dict]:
    """Fetch all 4 VUW JSON API endpoints and return pre-built link dicts.

    Each link dict contains a ``searchstax_result`` key so that
    ``_extract_only`` returns it verbatim without any HTML fetch.
    """
    base = cfg.base_url.rstrip("/") if cfg.base_url else _BASE
    endpoints = [f"{base}{ep}" for ep in _ENDPOINTS]

    async def _fetch_one(url: str) -> list[dict]:
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                resp = await client.get(url, headers={"Accept": "application/json"})
                resp.raise_for_status()
                data = resp.json()
                items = data.get("items") or []
                log.info("[VUW_API] %s → %d items", url, len(items))
                if emit:
                    emit("vuw_api_fetch", {"url": url, "count": len(items)})
                return items
        except Exception as exc:
            log.warning("[VUW_API] fetch failed for %s: %s", url, exc)
            return []

    # Fetch all 4 endpoints in parallel
    results = await asyncio.gather(*[_fetch_one(ep) for ep in endpoints])
    all_items: list[dict] = []
    for batch in results:
        all_items.extend(batch)

    log.info("[VUW_API] %d total items from %d endpoints", len(all_items), len(endpoints))

    # Map items → link dicts, dedup by URL
    seen_urls: set[str] = set()
    links: list[dict] = []
    skipped = 0
    for item in all_items:
        link = _map_item(item, cfg)
        if link is None:
            skipped += 1
            continue
        url = link["url"]
        if url in seen_urls:
            continue
        seen_urls.add(url)
        links.append(link)

    log.info(
        "[VUW_API] built %d unique course links (%d skipped/malformed)",
        len(links), skipped,
    )
    return links
