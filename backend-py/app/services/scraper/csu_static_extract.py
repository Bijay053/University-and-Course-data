"""CSU-specific static extractor — no browser required.

CSU's ``study.csu.edu.au`` course pages are server-side rendered with the full
course dataset embedded as plain JavaScript variable assignments inside several
``<script>`` blocks.  The page is 1.2–1.4 MB of raw HTML but the browser is
NOT needed because every field we care about is already in the static payload.

Embedded JS variables we exploit
---------------------------------
``fees``          JSON: ``{courseFee:[{student_type_code, annual_indicative_fee_ft, …}]}``
``ocb_metadata``  JSON: rich course object with language_requirements, offerings,
                  duration (actual_full_time), locations, study modes, AQF level, etc.
``session_data``  JSON: ``{session:[{term_code, start_Date, is_session, …}]}``

Fields produced
---------------
``domestic_fee``    – annual indicative fee for domestic students (float, AUD)
``ielts_overall``   – IELTS overall band score parsed from language-req HTML text
``duration``        – full-time standard years (float)
``duration_term``   – "years"
``intake_text``     – comma-separated intake month names ("March, July")
``location_text``   – comma-separated campus names ("Bathurst Campus, Online")
``study_mode_text`` – comma-separated delivery modes ("On Campus, Online")

Public entry-point
------------------
:func:`is_csu_url`               – quick host check
:func:`apply_csu_static_extraction` – ``(url, html) → dict[str, Any]``
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

_CSU_HOST = "study.csu.edu.au"


def is_csu_url(url: str) -> bool:
    """Return True when *url* is a CSU course page."""
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == _CSU_HOST or host.endswith("." + _CSU_HOST)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Low-level JS-variable extractor
# ---------------------------------------------------------------------------

def _extract_js_var(html: str, varname: str) -> str | None:
    """Return the raw JSON text of a top-level JS variable assignment.

    Handles both object ``{ … }`` and array ``[ … ]`` initialisers.
    Searches for the pattern ``varname =`` and walks depth-first through
    matching brackets so nested objects/arrays are captured correctly.
    Returns ``None`` when the variable is absent.
    """
    needle = f"{varname} ="
    start = html.find(needle)
    if start < 0:
        return None
    bracket_start = -1
    for i in range(start + len(needle), min(start + len(needle) + 50, len(html))):
        if html[i] in ("{", "["):
            bracket_start = i
            break
    if bracket_start < 0:
        return None
    open_ch = html[bracket_start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    for j in range(bracket_start, len(html)):
        c = html[j]
        if c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return html[bracket_start : j + 1]
    return None


def _parse_json(raw: str | None, label: str) -> Any | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        log.debug("csu_static: JSON parse error for %s: %s", label, exc)
        return None


# ---------------------------------------------------------------------------
# Field parsers
# ---------------------------------------------------------------------------

def _domestic_fee(fees_data: dict) -> float | None:
    """Annual indicative domestic fee (full-time), or None."""
    entries = [
        e for e in fees_data.get("courseFee", [])
        if e.get("student_type_code") == "DOM"
        and e.get("annual_indicative_fee_ft")
    ]
    if not entries:
        return None
    # Prefer the current/earliest year that has actual fee data
    entries.sort(key=lambda e: int(e.get("session_year", "0") or "0"))
    for e in entries:
        try:
            val = float(e["annual_indicative_fee_ft"])
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return None


def _ielts_from_lang_req(lang_reqs: list) -> float | None:
    """Parse IELTS overall from the raw HTML inside language_requirements."""
    for req in lang_reqs:
        text = req.get("requirements", "")
        # CSU format: "average band score of 7.5 across all four skill areas"
        m = re.search(
            r"average\s+band\s+score\s+of\s+(\d+(?:\.\d+)?)", text, re.I
        )
        if m:
            try:
                val = float(m.group(1))
                if 4.0 <= val <= 9.0:
                    return val
            except ValueError:
                pass
    return None


def _duration(course: dict) -> tuple[float | None, str | None]:
    """Return (years, "years") or (None, None)."""
    # Best source: actual_full_time (e.g. "4" or "1.5")
    for key in ("actual_full_time", "full_time_maximum_years"):
        raw = course.get(key)
        if raw:
            try:
                val = float(raw)
                if val > 0:
                    return val, "years"
            except (TypeError, ValueError):
                pass
    # Fallback: EFTSL list
    for entry in course.get("full_time_standard_eftsl", []):
        try:
            val = float(entry.get("short_description", ""))
            if val > 0:
                return val, "years"
        except (TypeError, ValueError):
            pass
    return None, None


def _intakes(course: dict, sess_data: dict) -> str | None:
    """Derive intake month names from offering teaching_period codes
    cross-referenced with session_data."""
    # Build code → earliest start_Date lookup from sessions with is_session=Y
    code_to_date: dict[str, str] = {}
    for s in sess_data.get("session", []):
        if s.get("is_session") != "Y":
            continue
        tc = s.get("term_code", "")
        start = s.get("start_Date", "")
        if len(tc) == 6 and start:
            # term_code ends in the period code: "202630" → code "30"
            code = tc[4:]  # "30", "60", "90"
            if code not in code_to_date:
                code_to_date[code] = start

    # Collect unique teaching-period codes from all active offerings
    tp_codes: set[str] = set()
    for offering in course.get("offerings", []):
        if offering.get("active") == "true":
            tp_val = offering.get("teaching_period", {}).get("value", "")
            if tp_val:
                # The offering stores period code as e.g. "30"; ensure
                # 2-digit zero-padded to match session_data keys
                tp_codes.add(tp_val.zfill(2))

    months: list[str] = []
    for code in sorted(tp_codes):
        date_str = code_to_date.get(code)
        if not date_str:
            continue
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
            month_name = dt.strftime("%B")
            if month_name not in months:
                months.append(month_name)
        except ValueError:
            pass
    return ", ".join(months) if months else None


def _locations_and_modes(course: dict) -> tuple[str | None, str | None]:
    """Return (location_text, study_mode_text) from active offerings."""
    locations: list[str] = []
    modes: list[str] = []
    for offering in course.get("offerings", []):
        if offering.get("active") != "true":
            continue
        loc = (offering.get("location") or {}).get("value", "")
        if loc and loc not in locations:
            locations.append(loc)
        mode = (offering.get("mode") or {}).get("value", "")
        if mode and mode not in modes:
            modes.append(mode)
    return (
        ", ".join(locations) if locations else None,
        ", ".join(modes) if modes else None,
    )


# ---------------------------------------------------------------------------
# Course object from ocb_metadata
# ---------------------------------------------------------------------------

def _first_course(meta: dict) -> dict | None:
    """Navigate to ocb_metadata.ocb[1].course[0] — the main course record."""
    try:
        return meta["ocb"][1]["course"][0]
    except (KeyError, IndexError, TypeError):
        pass
    # Fallback: scan all blocks for a non-empty course list
    for block in meta.get("ocb", []):
        courses = block.get("course", [])
        if courses:
            return courses[0]
    return None


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def apply_csu_static_extraction(url: str, html: str) -> dict[str, Any]:
    """Parse the 1.3 MB static HTML of a CSU course page and return a
    ``setdefault``-safe dict of extracted fields.

    Only non-None, non-empty values are included so the caller can safely
    apply ``payload.setdefault(k, v)`` without overwriting already-filled slots.

    Fields returned (all optional):
        domestic_fee (float), ielts_overall (float),
        duration (float), duration_term (str),
        intake_text (str), location_text (str), study_mode_text (str)
    """
    result: dict[str, Any] = {}

    if not html:
        return result

    # --- fees variable -------------------------------------------------------
    fees_raw = _extract_js_var(html, "fees")
    fees_data = _parse_json(fees_raw, "fees")
    if fees_data:
        dom_fee = _domestic_fee(fees_data)
        if dom_fee is not None:
            result["domestic_fee"] = dom_fee
            result["fee_term"] = "year"

    # --- ocb_metadata variable -----------------------------------------------
    meta_raw = _extract_js_var(html, "ocb_metadata")
    meta = _parse_json(meta_raw, "ocb_metadata")

    # --- session_data variable -----------------------------------------------
    sess_raw = _extract_js_var(html, "session_data")
    sess_data = _parse_json(sess_raw, "session_data") or {}

    if meta:
        course = _first_course(meta)
        if course:
            # IELTS
            ielts = _ielts_from_lang_req(course.get("language_requirements", []))
            if ielts is not None:
                result["ielts_overall"] = ielts

            # Duration
            dur, dur_term = _duration(course)
            if dur is not None:
                result["duration"] = dur
                result["duration_term"] = dur_term  # type: ignore[assignment]

            # Intakes
            intake_text = _intakes(course, sess_data)
            if intake_text:
                result["intake_text"] = intake_text

            # Locations + modes
            loc_text, mode_text = _locations_and_modes(course)
            if loc_text:
                result["location_text"] = loc_text
            if mode_text:
                result["study_mode_text"] = mode_text

    log.debug(
        "csu_static: %s → %s",
        url,
        {k: v for k, v in result.items()},
    )
    return result
