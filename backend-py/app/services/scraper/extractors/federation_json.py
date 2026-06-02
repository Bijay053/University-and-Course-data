"""Federation University JSON-block pre-extractor.

Federation's NextJS-driven CMS embeds the authoritative course
metadata as a serialised JSON tree inside the static HTML, e.g.::

    {
      "heading": "Duration",
      "summary": "4 years full-time or part-time equivalent",
      "tooltip": null
    },
    {
      "heading": "Locations",
      "summary": "Berwick (on campus)<br>Gippsland (on campus)<br>Mt Helen (on campus)",
      "tooltip": null
    }

The standard text-strip wipes the entire ``<script>`` / ``<...>`` block,
so the regex extractors in :mod:`extractors.duration` /
:mod:`extractors.location` never see this data. The result is fleet-wide
NULL durations (e.g. Master of Data Science, B Occupational Therapy
Honours) and Gemini-hallucinated locations (e.g. "Sydney" appearing on
M Social Work which only runs at Berwick / Gippsland / Mt Helen).

This module reads those JSON blocks directly off the *raw* HTML — runs
BEFORE AI fallback / PDF merge so its values become the source of truth
for Federation pages, while still being a no-op for every other uni
(callers gate on hostname before invoking).

Also exposes :func:`is_stub_page` so the orchestrator can reject
content-stub pages such as Cert II in Furniture Making (the page is a
nav/footer skeleton — no Duration JSON, no StudentTypeBlock — and
Gemini hallucinates fields when asked to extract from it).
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger("uniportal.scraper.fed_json")

# Match a single "{ heading: ..., summary: ... }" block.
# The JSON in Federation pages is pretty-printed across multiple lines,
# so we use DOTALL and bound the gap between the keys to keep the regex
# from greedily spanning across unrelated blocks.
_BLOCK_RE = re.compile(
    r'"heading"\s*:\s*"(?P<heading>[^"]{1,40})"'
    r'\s*,\s*'
    r'"summary"\s*:\s*"(?P<summary>(?:[^"\\]|\\.){0,400})"',
    re.DOTALL,
)

# Duration parser — pulls the leading numeric value and a unit token from
# strings like "4 years full-time or part-time equivalent" /
# "2 years part-time" / "18 months".  We only consider the FIRST numeric
# pair so that "3 years full time, 1 year accelerated" anchors on 3 (the
# canonical full-time value Federation publishes), matching the upstream
# B30 / Federation duration-rescue convention.
_DURATION_VALUE_RE = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*(?P<unit>year|years|month|months|"
    r"semester|semesters|trimester|trimesters|week|weeks)\b",
    re.IGNORECASE,
)
_UNIT_NORMAL = {
    "year": "Year", "years": "Year",
    "month": "Month", "months": "Month",
    "semester": "Semester", "semesters": "Semester",
    "trimester": "Trimester", "trimesters": "Trimester",
    "week": "Week", "weeks": "Week",
}

# Location summary tokens — Federation joins multiple campuses with
# literal "<br>" tags inside the JSON string (e.g.
# "Berwick (on campus)<br>Gippsland (on campus)").  We split on that
# literal token (NOT real markup, since this string lives inside JSON)
# and strip the trailing "(on campus)" / "(online)" qualifiers.
_LOC_QUALIFIER_RE = re.compile(r"\s*\(\s*(?:on\s*campus|online)\s*\)\s*$", re.IGNORECASE)
# Token that means "this offering has no campus location" — we drop these
# from the location list entirely, and use them to detect online-only pages.
_ONLINE_TOKEN_RE = re.compile(r"^\s*online\s*$", re.IGNORECASE)
# Federation's online-delivery brand name.  It appears as a campus-list
# entry on multi-mode courses (e.g. "Berwick (on campus)<br>Federation
# University Online") but is NEVER a physical location — it just means the
# course has an online-delivery pathway.  Strip it from the campuses list so
# it is never stored as course_location, but do NOT treat it as an
# "online-only" signal: the static JSON for multi-mode courses frequently
# lists "Federation University Online" as the sole Locations entry while the
# browser-rendered page also shows the physical campus.  Setting online_only
# in that case would wrongly drop a course that is genuinely available
# on-campus.
_FED_ONLINE_BRAND_RE = re.compile(
    r"^\s*federation\s+university\s+online\s*$", re.IGNORECASE
)

# Month name → 3-letter abbrev for intake parsing. Federation publishes
# Start dates as full dates like "20 July 2026" / "07 September 2026" /
# "01 March 2027" joined with literal "<br>" inside the JSON summary.
_MONTH_NAME_TO_ABBR = {
    "january":   "Jan", "jan": "Jan",
    "february":  "Feb", "feb": "Feb",
    "march":     "Mar", "mar": "Mar",
    "april":     "Apr", "apr": "Apr",
    "may":       "May",
    "june":      "Jun", "jun": "Jun",
    "july":      "Jul", "jul": "Jul",
    "august":    "Aug", "aug": "Aug",
    "september": "Sep", "sep": "Sep", "sept": "Sep",
    "october":   "Oct", "oct": "Oct",
    "november":  "Nov", "nov": "Nov",
    "december":  "Dec", "dec": "Dec",
}
# Canonical month order so we return them in calendar order (Mar, Jul, Sep)
# rather than the order they appear in the JSON.
_MONTH_ORDER = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]
# Date token in a Federation Start dates summary, e.g. "20 July 2026".
# Year is captured but unused — we only emit the month abbreviation.
_DATE_RE = re.compile(
    r"\b\d{1,2}\s+(?P<mon>[A-Za-z]{3,9})\s+\d{4}\b"
)

# Federation URL prefix (before the canonical slug):
#   dsz8-bachelor-of-science-honours
#   dgc4-graduate-certificate-in-social-and-community-services
#   dct5.nsm-bachelor-of-information-technology
# i.e. 2-6 alphanumerics, optional ``.<dot-suffix>``, then a hyphen and
# the slugified canonical name. Conservative — refuses to derive a name
# when the URL doesn't match this exact shape, so non-Federation URLs
# never produce a spurious override.
_URL_SLUG_RE = re.compile(
    r"/courses/(?P<prefix>[a-z0-9]{2,6}(?:\.[a-z0-9]{1,8})?)-(?P<slug>[a-z0-9-]+?)/?$"
)


def _iter_blocks(html: str) -> list[tuple[str, str]]:
    """Yield ``(heading, summary)`` pairs from the embedded JSON blocks."""
    if not html:
        return []
    out: list[tuple[str, str]] = []
    for m in _BLOCK_RE.finditer(html):
        out.append((m.group("heading").strip(), m.group("summary").strip()))
    return out


def extract_duration(html: str) -> tuple[float | None, str | None, str | None]:
    """Pull duration value + term from the embedded JSON.

    Returns ``(value, term, raw_summary)`` where:
      * ``value`` is the leading numeric value (float)
      * ``term`` is one of ``Year``/``Month``/``Semester``/``Trimester``/``Week``
      * ``raw_summary`` is the unparsed summary string (for evidence logging)

    Returns ``(None, None, None)`` when no Duration block is present or
    when the summary lacks a parseable number+unit pair.
    """
    for heading, summary in _iter_blocks(html):
        if heading.strip().lower() != "duration":
            continue
        dm = _DURATION_VALUE_RE.search(summary)
        if not dm:
            return None, None, summary or None
        try:
            val = float(dm.group("val"))
        except ValueError:
            return None, None, summary
        unit = _UNIT_NORMAL.get(dm.group("unit").lower())
        if not unit:
            return None, None, summary
        return val, unit, summary
    return None, None, None


def extract_locations(html: str) -> tuple[list[str], bool, str | None]:
    """Pull on-campus locations from the embedded JSON.

    Returns ``(campuses, online_only, raw_summary)`` where:
      * ``campuses`` is the list of distinct on-campus location names
        (with the trailing ``(on campus)`` qualifier stripped) — empty
        when the only location was "Online"
      * ``online_only`` is True when the JSON locations list contains
        ONLY online entries (no on-campus presence at all)
      * ``raw_summary`` is the unparsed summary string

    Returns ``([], False, None)`` when no Locations block is present.
    """
    for heading, summary in _iter_blocks(html):
        h_lower = heading.strip().lower()
        if h_lower not in ("location", "locations"):
            continue
        # Federation joins campuses with literal "<br>" inside the JSON
        # string.  Split on that exact token first, then fall back to
        # comma-splitting for single-campus rows where there is no <br>.
        raw_parts = re.split(r"(?i)<br\s*/?>", summary) if "<br" in summary.lower() else [summary]
        campuses: list[str] = []
        had_online = False
        had_any = False
        for part in raw_parts:
            cleaned = _LOC_QUALIFIER_RE.sub("", part).strip(" ,")
            if not cleaned:
                continue
            had_any = True
            if _ONLINE_TOKEN_RE.match(cleaned):
                had_online = True
                continue
            if _FED_ONLINE_BRAND_RE.match(cleaned):
                # Online-delivery brand — not a physical campus.  Drop from
                # the campus list but do NOT set had_online: we cannot infer
                # online_only from this token alone because multi-mode courses
                # put it alongside physical campuses in the rendered HTML.
                continue
            if cleaned not in campuses:
                campuses.append(cleaned)
        online_only = had_any and had_online and not campuses
        return campuses, online_only, summary
    return [], False, None


def extract_intake_months(html: str) -> tuple[list[str], str | None]:
    """Pull intake months from the embedded "Start dates" JSON block.

    Returns ``(months, raw_summary)`` where ``months`` is a deduplicated
    calendar-ordered list of 3-letter month abbreviations
    (e.g. ``["Mar", "Jul"]``) and ``raw_summary`` is the unparsed
    summary string for evidence logging.

    Federation publishes Start dates as full dates like
    ``"20 July 2026<br>01 March 2027<br>19 July 2027"`` inside the
    summary value. We split on ``<br>``, parse each date with
    :data:`_DATE_RE`, and emit the unique calendar-ordered month set
    so a page with three offerings in March / July / July returns
    ``["Mar", "Jul"]``.

    Returns ``([], None)`` when no Start dates block is present.
    """
    for heading, summary in _iter_blocks(html):
        if heading.strip().lower() != "start dates":
            continue
        seen: set[str] = set()
        for m in _DATE_RE.finditer(summary):
            abbr = _MONTH_NAME_TO_ABBR.get(m.group("mon").lower())
            if abbr:
                seen.add(abbr)
        ordered = [m for m in _MONTH_ORDER if m in seen]
        return ordered, summary
    return [], None


def extract_canonical_course_name(
    html: str, url: str
) -> tuple[str | None, str | None]:
    """Pull the canonical course name from the embedded JSON for a URL.

    Returns ``(canonical_name, source_heading)`` or ``(None, None)``.

    The URL slug after the Federation course-code prefix IS the
    canonical course name (e.g. ``dct5.nsm-bachelor-of-information-
    technology`` → ``"bachelor of information technology"``). The page
    JSON contains a ``"heading": "Bachelor of Information Technology"``
    block matching that slug, alongside marketing variants like the
    ``og:title`` ``"Bachelor of IT – Industry-Ready Degree"``.

    Conservative match — only returns a name when the URL slug
    (lowercased, dashes-to-spaces, punctuation-stripped) matches an
    existing heading block exactly. Refuses to invent a name from the
    slug alone, because the slug may be abbreviated or omit qualifiers.
    """
    if not html or not url:
        return None, None
    try:
        path = urlparse(url).path or ""
    except (ValueError, AttributeError):
        return None, None
    m = _URL_SLUG_RE.search(path.lower())
    if not m:
        return None, None
    slug = m.group("slug").replace("-", " ").strip()
    if not slug:
        return None, None
    # Normalise both sides for the comparison: lower, collapse whitespace,
    # strip punctuation that may differ between URL slug and heading
    # (en-dash, parentheses, ampersand).
    def _norm(s: str) -> str:
        s = s.lower()
        s = re.sub(r"[^\w\s]+", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s
    target = _norm(slug)
    for heading, _summary in _iter_blocks(html):
        if not heading:
            continue
        if _norm(heading) == target:
            return heading.strip(), heading
    return None, None


def is_stub_page(html: str) -> bool:
    """True when the Federation page lacks the canonical course-detail JSON.

    A real course-detail page always emits at least one
    ``"heading": "Duration"`` block AND references ``StudentTypeBlock``
    in its block-name manifest.  Content stubs (Cert II in Furniture
    Making etc.) carry only nav / footer chrome — when fed to Gemini
    they yield hallucinated fee / IELTS / duration values that pollute
    the staged data.

    Returns True when BOTH anchors are absent, so we never reject a
    real course page even if one of the two markers is missing for
    template-version reasons.
    """
    if not html:
        return True
    has_duration = '"heading": "Duration"' in html or '"heading":"Duration"' in html
    has_student_block = "StudentTypeBlock" in html
    return not has_duration and not has_student_block


def is_federation_host(url: str) -> bool:
    """Strict netloc check so callers can short-circuit non-Federation pages.

    Uses :func:`urllib.parse.urlparse` and requires the netloc to be
    ``federation.edu.au`` or one of its subdomains. A naive substring
    check would false-positive on URLs that mention "federation.edu.au"
    in their query string or path (e.g. tracker links from another uni
    that link out to Federation), which would silently hijack their
    duration / location values.
    """
    if not url:
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    return host == "federation.edu.au" or host.endswith(".federation.edu.au")


def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str = "",
    rendered_html: str | None = None,
) -> dict[str, Any]:
    """Apply Federation JSON overrides to *payload* in place.

    Returns a small dict describing which overrides fired (for logging /
    evidence trails). Empty dict means nothing applied.

    The override is intentionally aggressive: when the JSON publishes a
    Duration / Locations value, that value REPLACES whatever the regex
    extractors / AI fallback produced. Federation's CMS treats this JSON
    as the canonical course summary, so it is strictly more reliable
    than the noisy text scrape.

    Online-only detection: when the JSON Locations block contains ONLY
    online entries, ``study_mode`` is forced to ``"Online"`` so the
    downstream staging_gate ``online_only`` rejection fires and the row
    never lands in ``scraped_courses``.

    ``rendered_html`` — when the per-course browser pass has already
    rendered the page, pass the result here.  Location extraction uses
    the rendered HTML in preference to the static HTML because Federation
    per-course pages are JS-rendered SPAs: the static JSON Locations
    block may only list "Federation University Online" for a multi-mode
    course whose on-campus offerings (e.g. "Berwick (on campus)") are
    only injected by the React app at runtime.
    Duration extraction always uses the static HTML because the Duration
    JSON block is present in both and the static value is reliable.
    """
    applied: dict[str, Any] = {}

    dur_val, dur_term, dur_raw = extract_duration(html)
    if dur_val is not None and dur_term:
        prev = (payload.get("duration"), payload.get("duration_term"))
        payload["duration"] = dur_val
        payload["duration_term"] = dur_term
        applied["duration"] = {
            "old": prev,
            "new": (dur_val, dur_term),
            "summary": dur_raw,
        }

    # Prefer rendered HTML for location — the static JSON may be incomplete
    # for JS-rendered multi-mode courses where on-campus entries only appear
    # after the React app hydrates (see _FED_ONLINE_BRAND_RE docstring).
    _loc_source = rendered_html if rendered_html else html

    # Diagnostic logging (2026-05-13): user reports DTY5 Bachelor of Education
    # (Early Childhood and Primary) staged with all 4 Federation campuses
    # ("Berwick, Camp St, Gippsland, Mt Helen") even though the live page
    # only shows 2 ("Berwick (on campus), Mt Helen (on campus)"). Hypothesis:
    # multiple "Locations" JSON blocks on the page and extract_locations()
    # picks the first.  Emit a one-liner showing every Locations heading +
    # its summary so the next scrape tells us how many blocks exist and what
    # they each contain.  Additive only — no behaviour change.
    try:
        _all_loc_blocks = [
            (h, s) for (h, s) in _iter_blocks(_loc_source)
            if h.strip().lower() in ("location", "locations")
        ]
        if len(_all_loc_blocks) > 1:
            _src_label = "rendered" if rendered_html else "static"
            log.warning(
                "[FED LOC DIAG] %s — %d Locations blocks (src=%s): %s",
                url or "(no url)",
                len(_all_loc_blocks),
                _src_label,
                [(h, (s[:80] + "…" if len(s) > 80 else s))
                 for (h, s) in _all_loc_blocks],
            )
    except Exception:
        pass  # diagnostic only — never block

    campuses, online_only, loc_raw = extract_locations(_loc_source)
    if campuses:
        prev_loc = payload.get("course_location")
        payload["course_location"] = ", ".join(campuses)
        applied["course_location"] = {
            "old": prev_loc,
            "new": payload["course_location"],
            "summary": loc_raw,
        }
    elif online_only:
        # No on-campus presence: force Online so the staging gate rejects.
        prev_mode = payload.get("study_mode")
        payload["study_mode"] = "Online"
        # Drop any campus list AI may have hallucinated.
        if payload.get("course_location"):
            payload["course_location"] = None
        applied["online_only"] = {
            "old_mode": prev_mode,
            "summary": loc_raw,
        }

    # Intake months from "Start dates" block — REPLACE so AI hallucinations
    # / stale regex hits never override Federation's authoritative list.
    # Use rendered_html in preference to static html: Federation course pages
    # are React SPAs — the "Start dates" JSON block is only injected by the
    # React runtime (same reason locations already use rendered_html here).
    _intake_source = rendered_html if rendered_html else html
    months, intake_raw = extract_intake_months(_intake_source)
    if not months and _intake_source is not html:
        # Fall back to static HTML if rendered HTML had no Start dates block
        months, intake_raw = extract_intake_months(html)
    if months:
        prev_intake = payload.get("intake_months")
        payload["intake_months"] = months
        applied["intake_months"] = {
            "old": prev_intake,
            "new": months,
            "summary": intake_raw,
        }

    # Canonical course name from URL-slug-matching heading block —
    # REPLACE so the marketing og:title / H1 (e.g. "Bachelor of IT –
    # Industry-Ready Degree") doesn't shadow the catalogue name
    # (e.g. "Bachelor of Information Technology").
    canonical_name, source_heading = extract_canonical_course_name(html, url)
    if canonical_name:
        prev_name = payload.get("course_name")
        # Only replace when DIFFERENT — avoid logging a no-op override
        # for the common case where the page H1 already matches.
        if (prev_name or "").strip().lower() != canonical_name.lower():
            payload["course_name"] = canonical_name
            applied["course_name"] = {
                "old": prev_name,
                "new": canonical_name,
                "source_heading": source_heading,
            }

    if applied:
        log.info(
            "[FED JSON] %s — overrides applied: %s",
            url or "(no url)",
            sorted(applied.keys()),
        )

    return applied
