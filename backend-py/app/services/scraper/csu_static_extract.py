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

Fields produced  (DB-aligned key names)
----------------------------------------
``domestic_fee``      – annual indicative fee for domestic students (float, AUD)
``international_fee`` – annual indicative fee for international students (float, AUD)
``fee_term``          – "year"
``ielts_overall``     – IELTS overall band score parsed from language-req HTML text
``pte_overall``       – PTE Academic overall score parsed from language-req HTML text
``duration``          – full-time standard years (float)
``duration_term``     – "years"
``intake_months``     – list of intake month names (e.g. ["March", "July"])
                        Falls back to standard session calendar (is_session=Y) when
                        the course has no active offerings.
``course_location``   – comma-separated campus names ("Bathurst Campus, Online")
                        Always included in the result dict (None when no active
                        offerings) to block the regex extractor's "test" garbage.
``study_mode``        – comma-separated delivery modes ("On Campus, Online")
                        Always included (None when no active offerings) to block the
                        regex extractor's "Blended" mis-fire.

Design note
-----------
``apply_csu_static_extraction`` is designed to be called as a **pre-seed** before
the standard regex extractor chain so that ``payload.setdefault(k, v)`` in the
extractor loop is a no-op for every CSU field.  The result dict always contains
``course_location``, ``intake_months``, and ``study_mode`` — even when their value
is ``None`` — so the caller can do a plain ``payload[k] = v`` and the regex
extractors that always mis-fire on CSU pages never win.

Public entry-point
------------------
:func:`is_csu_url`                  – quick host check
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
    entries.sort(key=lambda e: int(e.get("session_year", "0") or "0"))
    for e in entries:
        try:
            val = float(e["annual_indicative_fee_ft"])
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return None


def _international_fee(fees_data: dict) -> float | None:
    """Annual indicative international fee (full-time), or None.

    CSU uses student_type_code ``"INT"`` (not ``"INTL"``) for international fees.
    Both codes are accepted defensively.
    """
    entries = [
        e for e in fees_data.get("courseFee", [])
        if e.get("student_type_code") in ("INT", "INTL")
        and e.get("annual_indicative_fee_ft")
    ]
    if not entries:
        return None
    entries.sort(key=lambda e: int(e.get("session_year", "0") or "0"))
    for e in entries:
        try:
            val = float(e["annual_indicative_fee_ft"])
            if val > 0:
                return val
        except (ValueError, TypeError):
            pass
    return None


def _english_from_lang_req(lang_reqs: list) -> tuple[float | None, float | None]:
    """Parse IELTS overall and PTE overall from language_requirements HTML.

    Returns ``(ielts_overall, pte_overall)``.  Either or both may be ``None``
    when the course page links out to a central requirements page instead of
    listing scores inline (e.g. MBA, BParamedic).

    IELTS pattern: "average band score of 7.5 across all four skill areas"
    PTE patterns:  "PTE Academic score of 58", "PTE score of 58", "PTE: 58"
    """
    ielts: float | None = None
    pte: float | None = None
    for req in lang_reqs:
        text = req.get("requirements", "")
        if ielts is None:
            m = re.search(
                r"average\s+band\s+score\s+of\s+(\d+(?:\.\d+)?)", text, re.I
            )
            if m:
                try:
                    val = float(m.group(1))
                    if 4.0 <= val <= 9.0:
                        ielts = val
                except ValueError:
                    pass
        if pte is None:
            # Match "PTE" followed by up to 30 non-digit chars then 2-3 digits
            m = re.search(r"PTE[^0-9]{0,30}?(\d{2,3})", text, re.I)
            if m:
                try:
                    val = float(m.group(1))
                    if 10 <= val <= 90:
                        pte = val
                except ValueError:
                    pass
    return ielts, pte


def _duration(course: dict) -> tuple[float | None, str | None]:
    """Return (years, "years") or (None, None)."""
    for key in ("actual_full_time", "full_time_maximum_years"):
        raw = course.get(key)
        if raw:
            try:
                val = float(raw)
                if val > 0:
                    return val, "years"
            except (TypeError, ValueError):
                pass
    for entry in course.get("full_time_standard_eftsl", []):
        try:
            val = float(entry.get("short_description", ""))
            if val > 0:
                return val, "years"
        except (TypeError, ValueError):
            pass
    return None, None


def _intakes(course: dict, sess_data: dict) -> list[str] | None:
    """Return intake month names as a list, e.g. ``["March", "July"]``.

    Strategy
    --------
    1. Build a ``term_code_suffix → start_Date`` map from sessions where
       ``is_session=Y`` (standard semester sessions, not 8-week terms).
       The suffix is the last 2 digits of the 6-digit term_code: ``"202630"``
       → ``"30"`` (Session 1, starts March), ``"202660"`` → ``"60"`` (Session 2,
       starts July), etc.

    2. For courses **with active offerings**: collect the teaching_period codes
       from those offerings and cross-reference with the map.

    3. For courses **without active offerings** (e.g. MBA with
       ``active_offerings=0``): fall back to ALL ``is_session=Y`` entries.
       This gives the university's standard intake calendar even when
       enrolments are temporarily closed.
    """
    code_to_date: dict[str, str] = {}
    for s in sess_data.get("session", []):
        if s.get("is_session") != "Y":
            continue
        tc = s.get("term_code", "")
        start = s.get("start_Date", "")
        if len(tc) == 6 and start:
            code = tc[4:]
            if code not in code_to_date:
                code_to_date[code] = start

    tp_codes: set[str] = set()
    for offering in course.get("offerings", []):
        if offering.get("active") == "true":
            tp_val = offering.get("teaching_period", {}).get("value", "")
            if tp_val:
                tp_codes.add(tp_val.zfill(2))

    if not tp_codes and code_to_date:
        tp_codes = set(code_to_date.keys())

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
    return months if months else None


def _locations_and_modes(course: dict) -> tuple[str | None, str | None]:
    """Return ``(course_location, study_mode)`` from active offerings.

    Returns ``(None, None)`` when there are no active offerings.
    """
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
    for block in meta.get("ocb", []):
        courses = block.get("course", [])
        if courses:
            return courses[0]
    return None


# ---------------------------------------------------------------------------
# Public entry-point
# ---------------------------------------------------------------------------

def apply_csu_static_extraction(url: str, html: str) -> dict[str, Any]:
    """Parse the 1.3 MB static HTML of a CSU course page and return a dict
    of extracted fields.

    The result is designed for use as a **pre-seed** before the standard
    extractor chain.  The caller should do a plain ``payload[k] = v`` for
    every key — this blocks the regex extractors (which always mis-fire on
    CSU pages) from winning via ``payload.setdefault()``.

    Three keys are **always present** in the result (even when ``None``):
    ``course_location``, ``intake_months``, ``study_mode``.  The caller must
    pre-seed even the ``None`` values so that the garbage regex extractors
    cannot set ``course_location="test"``, ``intake_months=["February"]``,
    ``study_mode="Blended"``.
    """
    result: dict[str, Any] = {
        "course_location": None,
        "intake_months": None,
        "study_mode": None,
    }

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
        intl_fee = _international_fee(fees_data)
        if intl_fee is not None:
            result["international_fee"] = intl_fee
            result.setdefault("fee_term", "year")

    # --- ocb_metadata variable -----------------------------------------------
    meta_raw = _extract_js_var(html, "ocb_metadata")
    meta = _parse_json(meta_raw, "ocb_metadata")

    # --- session_data variable -----------------------------------------------
    sess_raw = _extract_js_var(html, "session_data")
    sess_data = _parse_json(sess_raw, "session_data") or {}

    if meta:
        course = _first_course(meta)
        if course:
            # IELTS + PTE
            ielts, pte = _english_from_lang_req(course.get("language_requirements", []))
            if ielts is not None:
                result["ielts_overall"] = ielts
            if pte is not None:
                result["pte_overall"] = pte

            # Duration
            dur, dur_term = _duration(course)
            if dur is not None:
                result["duration"] = dur
                result["duration_term"] = dur_term  # type: ignore[assignment]

            # Intake months (list; None blocked above)
            result["intake_months"] = _intakes(course, sess_data)

            # Location + mode (always overwrite; None is correct for 0-offering courses)
            loc, mode = _locations_and_modes(course)
            result["course_location"] = loc
            result["study_mode"] = mode

    log.debug("csu_static: %s → %s", url, result)
    return result
