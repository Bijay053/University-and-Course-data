"""CQUniversity (cqu.edu.au) JSON-block pre-extractor.

CQU is a NextJS / Sitecore site that ships every course's authoritative
data inside ``<script id="__NEXT_DATA__">…</script>`` as a serialised
JSON tree. The standard text-strip wipes the ``<script>`` block, so the
downstream regex extractors in :mod:`extractors.location` /
:mod:`extractors.intake` / :mod:`extractors.english_test` /
:mod:`extractors.fee` either return nothing or pull noisy
intake-panel UI strings ("2026", "Next start term Anytime",
"& 3 more") into ``course_location``.

This module reads the canonical AIMS data tree directly off the raw
HTML and EXPOSES four overrides:

* ``course_location``  — distinct international-eligible campuses
* ``intake_months``    — unique calendar-ordered month abbrevs from
                         ``availabilities[].term_year_begin_date``
* ``duration``         — ``full_time_years`` (Year unit)
* ``international_fee``— ``fees.<latest_year>.IFYF.amount``
                         (or ``IFTF.amount * 3`` term-fee fallback,
                          or ``DFFPIFYF.amount`` when CQU charges the
                          same full-fee rate to international students)
* ``ielts_overall``    — parsed from ``english_proficiency_text``
                         "IELTS Academic … overall band score of at
                         least X.Y" + per-skill subscores when present

Hostname-gated (strict ``urllib.parse.urlparse`` netloc match) so this
module is a no-op for every other uni — a substring URL like
``?ref=cqu.edu.au`` from another uni cannot hijack the override path.

Mirrors the structure of ``federation_json.py`` (Federation override,
2026-05-10).
"""
from __future__ import annotations

import html as _html_stdlib
import json
import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("uniportal.scraper.cqu_json")

# Match the NextJS data script. The id attr can be quoted with single
# or double quotes and may carry additional attributes.
_NEXT_DATA_RE = re.compile(
    r'<script[^>]*id\s*=\s*["\']__NEXT_DATA__["\'][^>]*>(.+?)</script>',
    re.DOTALL,
)

# CQU encodes one offering per ``availabilities[i]`` row. Locations the
# AIMS catalogue treats as "no campus" — we drop them from the campus
# list so course_location only ever lists physical campuses.
_NON_CAMPUS_LOCATIONS = {"online", "mixed mode", "external"}

# Month index -> 3-letter abbrev for term_year_begin_date parsing.
_MONTH_ABBR = [
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
]
_MONTH_ORDER = list(_MONTH_ABBR)

# IELTS overall band score — anchored on the literal phrase CQU uses
# in english_proficiency_text. Conservative: requires the word "IELTS"
# within 200 chars before the score so we never grab a stray decimal.
_IELTS_OVERALL_RE = re.compile(
    r"IELTS[^.<>]{0,200}?(?:overall\s+band\s+score|overall)"
    r"[^.<>]{0,80}?(?P<score>\d(?:\.\d)?)",
    re.IGNORECASE,
)
# Per-skill subscore phrasing CQU uses, e.g.
#   "minimum 7.0 for Reading and Writing and 8.0 for Speaking and Listening"
#   "no individual band lower than 6.0"
_IELTS_SUBSCORE_RE = re.compile(
    r"(?P<score>\d(?:\.\d)?)\s+for\s+(?P<skills>[A-Za-z, ]+?)(?=(?:\s+and\s+\d|\.|<|$))",
    re.IGNORECASE,
)
_IELTS_NO_LOWER_RE = re.compile(
    # (?!\d) prevents matching the first digit of a two-digit number like
    # "46" (from "no sub-score less than 46" in the PTE section), which
    # would otherwise set an IELTS floor of 4.0 instead of 5.5.
    r"no\s+(?:individual\s+)?(?:band|skill|component|sub-?score)\s+(?:lower|less)\s+than\s+(?P<score>\d(?:\.\d)?)(?!\d)",
    re.IGNORECASE,
)
_SKILL_KEYS = ("listening", "reading", "speaking", "writing")

# PTE Academic & TOEFL iBT overall scores (2026-05-13 — fleet-wide
# ENG-DEFAULT bug fix).  CQU's english_proficiency_text always lists
# all three test minimums in the same paragraph, e.g.:
#   "(IELTS Academic) overall band score of at least 6.0 ..."
#   "(TOEFL) iBT - Requires 75 or better overall ..."    ← score BEFORE "overall"
#   "(PTE Academic) overall score of at least 54 ..."
# Without per-test extraction every CQU course was getting the
# institutional default (6.5/58/79) instead of the real per-course
# value, because cqu_json overwrote IELTS without evidence (then
# enforce_source_evidence nulled it) and never even tried PTE/TOEFL.
_PTE_OVERALL_RE = re.compile(
    r"PTE[^.<>]{0,200}?overall(?:\s+score)?[^.<>]{0,80}?(?P<score>\d{2,3})",
    re.IGNORECASE,
)
# Primary: "X or better overall" — CQU's schema.org phrasing puts the score
# BEFORE the word "overall" ("Requires 75 or better overall and no score
# less than 17"), so the old regex matched "17" (the no-score-below) instead
# of "75" (the required overall).  Secondary pattern keeps backward compat
# for any course that uses "overall score of at least 75" ordering.
_TOEFL_OR_BETTER_RE = re.compile(
    r"TOEFL[^.<>]{0,200}?(?:requires?\s+)?(?P<score>\d{2,3})\s+or\s+better",
    re.IGNORECASE,
)
_TOEFL_OVERALL_RE = re.compile(
    r"TOEFL[^.<>]{0,200}?overall(?:\s+(?:band\s+)?score)?[^.<>]{0,80}?(?P<score>\d{2,3})",
    re.IGNORECASE,
)
# "minimum 5.5 in each subset/band/component" — CQU's schema.org
# coursePrerequisites uses this phrasing for per-band floors rather than
# the "no individual band lower than X" form that _IELTS_NO_LOWER_RE matches.
_IELTS_MIN_PER_SUBSET_RE = re.compile(
    r"minimum\s+(?P<score>\d(?:\.\d)?)\s+in\s+each",
    re.IGNORECASE,
)
# Schema.org LD+JSON block — CQU embeds English requirements HTML-encoded
# inside coursePrerequisites of the Course schema.  This is the canonical
# source when AIMSData is absent from the __NEXT_DATA__ JSON tree.
_LD_JSON_RE = re.compile(
    r'<script[^>]*type\s*=\s*["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.DOTALL | re.IGNORECASE,
)


def is_cqu_host(url: str) -> bool:
    """Strict netloc check — apex ``cqu.edu.au`` or ``*.cqu.edu.au`` only.

    Substring-on-URL would false-positive on tracker links from other
    unis (e.g. ``?ref=cqu.edu.au``). We use :func:`urllib.parse.urlparse`
    so the host is parsed once, lower-cased once, and matched exactly.
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host == "cqu.edu.au" or host.endswith(".cqu.edu.au")


def parse_aims_data(html: str) -> dict[str, Any] | None:
    """Pull and decode the AIMSData dict from a CQU NextJS page.

    Returns ``None`` when the script is missing, malformed, or doesn't
    contain the expected ``layoutData.sitecore.context.route.fields
    .customRouteContent.value.AIMSData`` path. Never raises — every
    call site is non-critical fall-back logic, so a parse failure
    must always degrade to the regex extractors silently.
    """
    if not html:
        return None
    m = _NEXT_DATA_RE.search(html)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None
    try:
        crc = (
            data["props"]["pageProps"]["layoutData"]["sitecore"]
            ["context"]["route"]["fields"]["customRouteContent"]["value"]
        )
    except (KeyError, TypeError):
        return None
    aims = crc.get("AIMSData") if isinstance(crc, dict) else None
    return aims if isinstance(aims, dict) else None


def extract_locations(aims: dict[str, Any]) -> list[str]:
    """Return distinct international-eligible campuses, alphabetically.

    Falls back to the union of all availabilities locations (still
    excluding ``Online``/``Mixed Mode``) when no entry is flagged
    ``is_international`` — better to show real campus names than
    leaving a noisy regex value like "& 3 more".
    """
    intl: set[str] = set()
    fallback: set[str] = set()
    for av in aims.get("availabilities") or []:
        if not isinstance(av, dict):
            continue
        for loc in av.get("locations") or []:
            if not isinstance(loc, dict):
                continue
            name = (loc.get("location") or "").strip()
            if not name or name.lower() in _NON_CAMPUS_LOCATIONS:
                continue
            fallback.add(name)
            if loc.get("is_international"):
                intl.add(name)
    chosen = intl or fallback
    return sorted(chosen)


def extract_intake_months(aims: dict[str, Any]) -> list[str]:
    """Return calendar-ordered, deduplicated month abbreviations.

    Reads each availability's ``term_year_begin_date`` (ISO ``YYYY-MM-DD``)
    and emits the unique month set. Prefers international-flagged
    availabilities; falls back to all when no intl flag is present.
    """
    intl_months: set[str] = set()
    fallback_months: set[str] = set()
    for av in aims.get("availabilities") or []:
        if not isinstance(av, dict):
            continue
        date_str = av.get("term_year_begin_date")
        if not isinstance(date_str, str):
            continue
        m = re.match(r"^\d{4}-(\d{2})-\d{2}", date_str)
        if not m:
            continue
        try:
            month_idx = int(m.group(1)) - 1
        except ValueError:
            continue
        if not 0 <= month_idx < 12:
            continue
        abbr = _MONTH_ABBR[month_idx]
        fallback_months.add(abbr)
        # Per-availability intl flag may be missing; fall back to any
        # intl-eligible location on this offering.
        if av.get("is_international") or any(
            (loc or {}).get("is_international")
            for loc in av.get("locations") or []
            if isinstance(loc, dict)
        ):
            intl_months.add(abbr)
    chosen = intl_months or fallback_months
    return [m for m in _MONTH_ORDER if m in chosen]


def extract_duration(aims: dict[str, Any]) -> tuple[float | None, str | None]:
    """Return ``(value, "Year")`` from ``duration.full_time_years``.

    Returns ``(None, None)`` when the key is missing or non-numeric.
    """
    dur = aims.get("duration")
    if not isinstance(dur, dict):
        return None, None
    val = dur.get("full_time_years")
    if val is None:
        return None, None
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None, None
    if f <= 0:
        return None, None
    return f, "Year"


def extract_international_fee(
    aims: dict[str, Any],
) -> tuple[float | None, str | None, int | None, str | None]:
    """Return ``(amount, fee_term, fee_year, source_code)``.

    Selection order across years (latest first):
      1. ``IFYF`` (International Indicative First Year Fee) — annual
      2. ``IFTF`` (International Indicative First Term Fee) × 3
         (CQU runs three trimesters per year)
      3. ``DFFPIFYF`` (Domestic Full Fee Paying Indicative First Year
         Fee) — CQU charges international students the same full-fee
         rate when no IFYF/IFTF is published, so this is the published
         intl rate by site convention.

    Returns ``(None, None, None, None)`` when no fee row is present.
    """
    fees = aims.get("fees")
    if not isinstance(fees, dict):
        return None, None, None, None
    # Sort years descending (numeric) so the latest published value wins.
    try:
        years = sorted(
            (y for y in fees.keys() if isinstance(y, str) and y.isdigit()),
            key=int,
            reverse=True,
        )
    except (TypeError, ValueError):
        years = []
    for year in years:
        bucket = fees.get(year)
        if not isinstance(bucket, dict):
            continue
        for code, term_kind in (("IFYF", "Annual"), ("IFTF", "Term"), ("DFFPIFYF", "Annual")):
            payload = bucket.get(code)
            if not isinstance(payload, dict):
                continue
            amount = payload.get("amount")
            if amount in (None, "", 0):
                continue
            try:
                amt = float(amount)
            except (TypeError, ValueError):
                continue
            if amt <= 0:
                continue
            # IFTF is per-trimester; CQU runs 3 trimesters/year.
            if code == "IFTF":
                amt = amt * 3
            try:
                fee_year = int(year)
            except ValueError:
                fee_year = None
            return amt, "Annual", fee_year, code
    return None, None, None, None


def _strip_html(text: str) -> str:
    """Naive HTML stripper for IELTS phrase matching."""
    return re.sub(r"<[^>]+>", " ", text or "")


def extract_ielts(aims: dict[str, Any]) -> dict[str, float] | None:
    """Parse ``english_proficiency_text`` for IELTS overall + subscores.

    Returns ``None`` when no overall score is found. Per-skill keys
    (``ielts_listening`` etc.) are only included when the source phrase
    explicitly attributes a score to that skill — there is no implicit
    floor.
    """
    raw = aims.get("english_proficiency_text")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _strip_html(raw)
    om = _IELTS_OVERALL_RE.search(text)
    if not om:
        return None
    try:
        overall = float(om.group("score"))
    except ValueError:
        return None
    if not 4.0 <= overall <= 9.0:
        return None
    out: dict[str, float] = {"ielts_overall": overall}
    # Per-skill phrases like "7.0 for Reading and Writing and 8.0 for
    # Speaking and Listening".
    for sm in _IELTS_SUBSCORE_RE.finditer(text):
        try:
            sub = float(sm.group("score"))
        except ValueError:
            continue
        if not 4.0 <= sub <= 9.0:
            continue
        skills_blob = sm.group("skills").lower()
        for skill in _SKILL_KEYS:
            if skill in skills_blob:
                out[f"ielts_{skill}"] = sub
    # "no individual band lower than 6.0" — apply as floor for any
    # subscore not yet set.
    nm = _IELTS_NO_LOWER_RE.search(text)
    floor: float | None = None
    if nm:
        try:
            floor = float(nm.group("score"))
        except ValueError:
            floor = None
    # "minimum 5.5 in each subset/band" — CQU's schema.org phrasing for the
    # same concept.  Only used when the "no lower than" form was not found.
    if floor is None:
        mm = _IELTS_MIN_PER_SUBSET_RE.search(text)
        if mm:
            try:
                floor = float(mm.group("score"))
            except ValueError:
                floor = None
    if floor is not None and 4.0 <= floor <= 9.0:
        for skill in _SKILL_KEYS:
            out.setdefault(f"ielts_{skill}", floor)
    return out


def extract_pte(aims: dict[str, Any]) -> float | None:
    """Parse PTE Academic overall score from ``english_proficiency_text``.

    Returns ``None`` when no PTE phrase is present or score is out of
    band (PTE Academic uses a 10–90 scale).
    """
    raw = aims.get("english_proficiency_text")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _strip_html(raw)
    m = _PTE_OVERALL_RE.search(text)
    if not m:
        return None
    try:
        score = float(m.group("score"))
    except ValueError:
        return None
    if not 10 <= score <= 90:
        return None
    return score


def extract_toefl(aims: dict[str, Any]) -> float | None:
    """Parse TOEFL iBT overall score from ``english_proficiency_text``.

    Returns ``None`` when no TOEFL phrase is present or score is out of
    band (TOEFL iBT uses a 0–120 scale; CQU minimums are typically 70+).

    CQU's schema.org phrasing puts the score BEFORE "overall":
        "Requires 75 or better overall and no score less than 17"
    The old ``_TOEFL_OVERALL_RE`` (score AFTER "overall") matched "17"
    (the no-score-below) instead of "75" (the required overall).
    We try ``_TOEFL_OR_BETTER_RE`` first so the correct value wins.
    """
    raw = aims.get("english_proficiency_text")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = _strip_html(raw)
    # Try "X or better overall" phrasing first (CQU schema.org format).
    m = _TOEFL_OR_BETTER_RE.search(text) or _TOEFL_OVERALL_RE.search(text)
    if not m:
        return None
    try:
        score = float(m.group("score"))
    except ValueError:
        return None
    if not 0 <= score <= 120:
        return None
    return score


def _parse_english_from_schema_org(html: str) -> str | None:
    """Extract and normalise english proficiency text from schema.org LD+JSON.

    CQU's ``<script type="application/ld+json">`` Course block stores the
    English requirements as HTML-encoded HTML under ``coursePrerequisites``,
    e.g.::

        "coursePrerequisites": "&lt;ul&gt;&lt;li&gt;IELTS Academic …6.0…&lt;/li&gt;"

    This is the reliable fallback when ``AIMSData.english_proficiency_text``
    is absent from the NextJS ``__NEXT_DATA__`` tree (which is the case for
    most CQU pages as of mid-2026 after a Sitecore schema update removed
    the ``AIMSData`` key).

    Returns the plain-text English requirements string, or ``None`` when the
    page does not contain a Course LD+JSON block with a populated
    ``coursePrerequisites`` field that mentions at least one test name.
    """
    for m in _LD_JSON_RE.finditer(html or ""):
        try:
            d = json.loads(m.group(1))
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(d, dict) or d.get("@type") != "Course":
            continue
        prereq = d.get("coursePrerequisites")
        if not isinstance(prereq, str) or not prereq.strip():
            continue
        # coursePrerequisites is double-encoded in CQU's LD+JSON output
        # (the entire HTML block is JSON-string-encoded AND HTML-entity-escaped).
        decoded = _html_stdlib.unescape(prereq)
        # Strip residual HTML tags so the regex extractors get plain text.
        text = re.sub(r"<[^>]+>", " ", decoded)
        text = re.sub(r"&nbsp;", " ", text, flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", text).strip()
        # Only return when the block is genuinely about language proficiency.
        if any(kw in text for kw in ("IELTS", "TOEFL", "PTE", "Duolingo")):
            return text
    return None


def is_domestic_only(aims: dict[str, Any]) -> bool:
    """True when the AIMS catalogue flags this course domestic-only.

    Conservative — requires the explicit ``is_international == False``
    AND ``is_domestic == True`` pair (so a missing flag never causes
    a false rejection).
    """
    return aims.get("is_international") is False and aims.get("is_domestic") is True


def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Apply CQU JSON overrides to *payload* in place.

    Returns a small dict describing which overrides fired (for logging
    / evidence trails). Empty dict means nothing applied.

    The JSON values REPLACE whatever the regex extractors / AI
    fallback produced, mirroring the Federation override convention:
    CQU's NextJS data tree is the canonical source of truth for these
    fields, strictly more reliable than the noisy text scrape.
    """
    applied: dict[str, Any] = {}
    aims = parse_aims_data(html)
    if not aims:
        return applied

    # Locations (REPLACE — almost always wrong on CQU regex output).
    # When AIMS lists physical campuses, write the comma-joined list.
    # When AIMS parses but every availability is Online / Mixed Mode
    # only, the regex fallback is *guaranteed* to be a bogus intake-
    # panel fragment ("2026", "Next start term Anytime", "& 3 more"),
    # so scrub to "Online" and force study_mode=Online — mirrors the
    # federation_json online-only behavior.
    campuses = extract_locations(aims)
    has_any_location = any(
        isinstance(loc, dict) and (loc.get("location") or "").strip()
        for av in (aims.get("availabilities") or [])
        if isinstance(av, dict)
        for loc in (av.get("locations") or [])
    )
    if campuses:
        prev_loc = payload.get("course_location")
        new_loc = ", ".join(campuses)
        if prev_loc != new_loc:
            payload["course_location"] = new_loc
            applied["course_location"] = {"old": prev_loc, "new": new_loc}
    elif has_any_location:
        prev_loc = payload.get("course_location")
        prev_mode = payload.get("study_mode")
        if prev_loc != "Online":
            payload["course_location"] = "Online"
            applied["course_location"] = {"old": prev_loc, "new": "Online"}
        if prev_mode != "Online":
            payload["study_mode"] = "Online"
            applied["study_mode"] = {"old": prev_mode, "new": "Online"}

    # Intake months (REPLACE — the regex pulls "Anytime" / year-only
    # strings on CQU, AI hallucinates from the 57-char text-strip).
    months = extract_intake_months(aims)
    if months:
        prev_in = payload.get("intake_months")
        if prev_in != months:
            payload["intake_months"] = months
            applied["intake_months"] = {"old": prev_in, "new": months}

    # Duration (REPLACE — JSON is canonical).
    dur_val, dur_term = extract_duration(aims)
    if dur_val is not None:
        prev = (payload.get("duration"), payload.get("duration_term"))
        if prev != (dur_val, dur_term):
            payload["duration"] = dur_val
            payload["duration_term"] = dur_term
            applied["duration"] = {"old": prev, "new": (dur_val, dur_term)}

    # International fee (REPLACE — the regex picks up CSP / domestic
    # rates inadvertently because the page lists multiple fee tables).
    amt, fee_term, fee_year, code = extract_international_fee(aims)
    if amt is not None:
        prev_fee = payload.get("international_fee")
        payload["international_fee"] = amt
        if fee_term:
            payload["fee_term"] = fee_term
        if fee_year:
            payload["fee_year"] = fee_year
        applied["international_fee"] = {
            "old": prev_fee,
            "new": amt,
            "source_code": code,
            "fee_year": fee_year,
        }

    # IELTS / PTE / TOEFL (REPLACE only when a source publishes a valid
    # value — leaves existing values intact when neither source has text).
    #
    # Source priority (first non-empty wins):
    #   1. AIMSData.english_proficiency_text  — present when __NEXT_DATA__
    #      still uses the AIMSData schema (pre-mid-2026 Sitecore update)
    #   2. Schema.org coursePrerequisites      — reliably present as of
    #      mid-2026; HTML-encoded HTML blob decoded by
    #      _parse_english_from_schema_org()
    #
    # Evidence rows are appended for the *_overall slots so
    # guards.enforce_source_evidence keeps the values at staging time —
    # without them every CQU course was getting nulled and then refilled
    # with the YAML institutional default (6.5/58/79) by the ENG-DEFAULT
    # block, fleet-wide.
    raw_eng_text = (
        (aims.get("english_proficiency_text") if isinstance(aims, dict) else None)
        or _parse_english_from_schema_org(html)
    )
    eng_source = (
        "cqu_json:english_proficiency_text"
        if (isinstance(aims, dict) and aims.get("english_proficiency_text"))
        else "cqu_json:schema_org_coursePrerequisites"
    )
    eng_snippet_base = (
        _strip_html(raw_eng_text)[:280].strip()
        if isinstance(raw_eng_text, str) and raw_eng_text.strip()
        else "CQU english proficiency requirements"
    )

    def _emit_eng_evidence(field_key: str, value: float) -> None:
        if evidence is None:
            return
        evidence.append({
            "field_key": field_key,
            "value": value,
            "confidence": 0.85,
            "method": eng_source,
            "source_url": url or "",
            "snippet": eng_snippet_base,
        })

    # Build a fake aims-like dict keyed to english_proficiency_text so the
    # extract_* functions (which read aims.get("english_proficiency_text"))
    # work whether the source is AIMSData OR schema.org coursePrerequisites.
    _eng_aims: dict[str, Any] = (
        {"english_proficiency_text": raw_eng_text}
        if isinstance(raw_eng_text, str) and raw_eng_text.strip()
        else (aims or {})
    )

    ielts = extract_ielts(_eng_aims)
    if ielts:
        prev_overall = payload.get("ielts_overall")
        for k, v in ielts.items():
            payload[k] = v
        applied["ielts"] = {"old_overall": prev_overall, "new": ielts, "source": eng_source}
        if "ielts_overall" in ielts:
            _emit_eng_evidence("ielts_overall", ielts["ielts_overall"])

    pte = extract_pte(_eng_aims)
    if pte is not None:
        prev_pte = payload.get("pte_overall")
        payload["pte_overall"] = pte
        applied["pte_overall"] = {"old": prev_pte, "new": pte}
        _emit_eng_evidence("pte_overall", pte)

    toefl = extract_toefl(_eng_aims)
    if toefl is not None:
        prev_toefl = payload.get("toefl_overall")
        payload["toefl_overall"] = toefl
        applied["toefl_overall"] = {"old": prev_toefl, "new": toefl}
        _emit_eng_evidence("toefl_overall", toefl)

    if applied:
        log.info(
            "[CQU JSON] %s — overrides applied: %s",
            url or "(no url)",
            sorted(applied.keys()),
        )

    return applied
