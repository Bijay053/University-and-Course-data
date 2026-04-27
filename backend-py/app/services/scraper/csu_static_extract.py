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
``course_location``      – comma-separated physical campus names ("Bathurst Campus, Wagga Wagga Campus")
                          Always included in the result dict (None when no active
                          offerings) to block the regex extractor's "test" garbage.
``study_mode``           – comma-separated delivery modes ("On Campus, Online")
                          Always included (None when no active offerings) to block
                          the regex extractor's "Blended" mis-fire.
``has_central_fee_page`` – always True.  Lets the staging gate pass CSU courses
                          that have no extractable international_fee (e.g. research
                          degrees, courses with no current INT intake) for human
                          review instead of auto-rejecting with "no_international_fee".

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


def _english_from_lang_req(
    lang_reqs: list,
) -> tuple[float | None, float | None, bool, bool]:
    """Parse IELTS overall and PTE overall from language_requirements HTML.

    Returns ``(ielts_overall, pte_overall, ielts_pattern_found, pte_pattern_found)``.

    ``ielts_pattern_found`` is ``True`` when an IELTS-shaped numeric pattern was
    matched in the text, **even if the extracted value fell outside the valid
    4.0–9.0 range** (i.e. the match was deliberately discarded).  Callers use
    this flag to distinguish two "IELTS is None" situations:

    * ``not ielts_pattern_found`` → no inline score exists; it's safe to fall
      back to the CSU-standard default from the central requirements page.
    * ``ielts_pattern_found`` → a score was present but out of range; the
      page data is unreliable, so do **not** substitute a default.

    Likewise for ``pte_pattern_found``.

    IELTS patterns: "average band score of 7.5", "minimum overall score of 6.0"
    PTE patterns:   "PTE Academic score of 58", "PTE score of 58", "PTE: 58"
                    Only matched when the score is plausibly a real PTE entry
                    requirement (>= 36, which maps to IELTS 5.0+).
    """
    ielts: float | None = None
    pte: float | None = None
    ielts_pattern_found = False
    pte_pattern_found = False
    for req in lang_reqs:
        text = req.get("requirements", "")
        if ielts is None:
            for ielts_pattern in [
                r"average\s+band\s+score\s+of\s+(\d+(?:\.\d+)?)",
                r"minimum\s+overall\s+(?:band\s+)?score\s+of\s+(\d+(?:\.\d+)?)",
                r"IELTS[^0-9]{0,40}?(\d+(?:\.\d+)?)",
            ]:
                m = re.search(ielts_pattern, text, re.I)
                if m:
                    ielts_pattern_found = True
                    try:
                        val = float(m.group(1))
                        if 4.0 <= val <= 9.0:
                            ielts = val
                            break
                    except ValueError:
                        pass
        if pte is None:
            # Mark pte_pattern_found whenever the text mentions "PTE" at all,
            # even if the numeric value that follows is absent, too short, or
            # out of range.  This prevents the IELTS-derived fallback from
            # silently substituting a PTE when the page explicitly mentions
            # PTE (even with an implausible value like "PTE score of 5").
            if re.search(r"\bPTE\b", text, re.I):
                pte_pattern_found = True
                # Require PTE score >= 36 to avoid false positives
                # (PTE 36 ≈ IELTS 4.5, the lowest plausible entry requirement).
                m = re.search(r"PTE\s*(?:Academic|Academic\s+score)?[^0-9]{0,30}?(\d{2,3})", text, re.I)
                if m:
                    try:
                        val = float(m.group(1))
                        if 36 <= val <= 90:
                            pte = val
                    except ValueError:
                        pass
    return ielts, pte, ielts_pattern_found, pte_pattern_found


# CSU central requirements page (https://study.csu.edu.au/international/how-to-apply/course-entry-requirements)
# specifies IELTS-only standards; PTE equivalences follow Australian DHA table.
_IELTS_TO_PTE: dict[float, float] = {
    5.0: 36,
    5.5: 42,
    6.0: 50,
    6.5: 58,
    7.0: 65,
    7.5: 79,
    8.0: 85,
}


def _pte_from_ielts(ielts: float) -> float | None:
    """Return the closest standard PTE Academic equivalent for a given IELTS score."""
    # Exact match first
    if ielts in _IELTS_TO_PTE:
        return _IELTS_TO_PTE[ielts]
    # Round to nearest 0.5 and look up
    rounded = round(ielts * 2) / 2
    return _IELTS_TO_PTE.get(rounded)


def _csu_default_ielts(course: dict) -> float:
    """Return the standard CSU IELTS requirement based on AQF level.

    From https://study.csu.edu.au/international/how-to-apply/course-entry-requirements:
      - UG / PG coursework (AQF 5-9): overall 6.0, no band < 5.5 (UG) / 6.0 (PG)
      - HDR / research (AQF 9 research, 10): overall 6.5, no band < 6.0

    We use 6.5 for AQF 10 (doctoral) and research masters; 6.0 for everything else.
    """
    aqf = (course.get("aqf_level") or {}).get("value", "")
    # Doctoral degrees
    if "10" in aqf or "doctoral" in aqf.lower():
        return 6.5
    # Research-flavoured AQF 9 (Masters by Research / Professional Doctorate)
    title = (course.get("title") or "").lower()
    if "research" in title and ("master" in title or "doctor" in title):
        return 6.5
    return 6.0


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


_INT_STUDENT_TYPE_CODES: frozenset[str] = frozenset({"INT", "INTL"})


def _offering_is_international(offering: dict) -> bool:
    """Return True when an offering is available to international students.

    An offering is considered international when:
    - its ``student_type_code`` is ``"INT"`` or ``"INTL"``, **or**
    - it has no ``student_type_code`` at all (i.e. unrestricted).

    Offerings that carry any other explicit code (e.g. ``"DOM"``) are
    treated as domestic-only and excluded.
    """
    stc = offering.get("student_type_code")
    return stc is None or stc in _INT_STUDENT_TYPE_CODES


def _locations_and_modes(course: dict) -> tuple[str | None, str | None]:
    """Return ``(course_location, study_mode)`` from active international offerings.

    Only active offerings that pass ``_offering_is_international`` are
    considered.  Returns ``(None, None)`` when there are no such offerings.

    CSU's JSON uses ``location.value = "Online"`` for distance-delivery
    offerings — this is a mode descriptor, not a physical campus name.
    Any offering whose ``location.value`` is "Online" (case-insensitive) is
    excluded from the locations list; "Online" is added to ``modes`` instead
    so the study_mode field still reflects online availability.
    """
    locations: list[str] = []
    modes: list[str] = []
    for offering in course.get("offerings", []):
        if offering.get("active") != "true":
            continue
        if not _offering_is_international(offering):
            continue
        loc = (offering.get("location") or {}).get("value", "")
        if loc:
            if loc.strip().lower() == "online":
                # "Online" is a delivery mode in CSU data, not a campus name.
                # Promote it to modes and skip it as a location.
                if "Online" not in modes:
                    modes.append("Online")
            elif loc not in locations:
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
        # Staging gate: when international_fee cannot be extracted from the
        # page JS (e.g. research degrees, courses with no current INT intake),
        # this flag lets the course pass through for human review instead of
        # being auto-rejected with "no_international_fee".
        "has_central_fee_page": True,
        # NOTE: ielts_overall and pte_overall are NOT pre-initialised here.
        # They are only written when a non-None value is available (inline parse
        # or CSU-standard default for courses that reference the central page).
        # single_course.py blocks the regex extractors for CSU pages separately.
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
            # ── IELTS + PTE ──────────────────────────────────────────────────
            # Step 1: try to parse inline scores from language_requirements.
            lang_reqs = course.get("language_requirements", [])
            ielts, pte, ielts_pat, pte_pat = _english_from_lang_req(lang_reqs)

            # Step 2: if no inline IELTS found AND no out-of-range IELTS pattern
            # was detected, fall back to the CSU-standard default from the central
            # requirements page.  This handles the ~90% of courses that link out
            # to the central page instead of listing a score inline.
            # We do NOT fall back when:
            #   - lang_reqs is empty  (course has no language requirement at all)
            #   - an IELTS pattern was found but the value was out of range
            #     (data is present but unreliable; don't substitute a guess)
            if ielts is None and lang_reqs and not ielts_pat:
                ielts = _csu_default_ielts(course)

            if ielts is not None:
                result["ielts_overall"] = ielts

            # Step 3: derive PTE from IELTS using Australian DHA equivalence table
            # when no inline PTE was found AND no out-of-range PTE pattern was seen.
            # (If a PTE value was detected but rejected as out-of-range, do not
            # silently substitute a PTE derived from IELTS — the page data is
            # unreliable for PTE.)
            if pte is None and not pte_pat and ielts is not None:
                pte = _pte_from_ielts(ielts)

            if pte is not None:
                result["pte_overall"] = pte

            # Duration
            dur, dur_term = _duration(course)
            if dur is not None:
                result["duration"] = dur
                result["duration_term"] = dur_term  # type: ignore[assignment]

            # Intake months (list; None blocked above)
            result["intake_months"] = _intakes(course, sess_data)

            # ── Location + mode ──────────────────────────────────────────────
            # Primary: collect from active international offerings.
            loc, mode = _locations_and_modes(course)
            # No fallback to "Online" or inactive offerings — if there are no
            # active international offerings the location is genuinely unknown
            # and should be None.  Returning None lets the caller decide, and
            # keeps inactive-offering campus names from bleeding through.
            result["course_location"] = loc
            result["study_mode"] = mode

    log.debug("csu_static: %s → %s", url, result)
    return result
