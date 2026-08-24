"""Run all extractors over one course page and return a merged record.

Output shape is keyed for direct insertion into ``scraped_courses`` via
``stage_course``. Each extractor's ``normalized`` payload contributes
fields; a missing extractor simply leaves its slot empty.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

if TYPE_CHECKING:
    # Type-checking-only import to avoid pulling per_course_vision (and
    # its heavy gemini_client transitive imports) at module load time.
    # The real runtime import happens lazily inside ``extract_course``
    # alongside the other per_course_* fallbacks.
    from app.services.scraper.per_course_vision import VisionImageCache  # noqa: F401

from app.services.scraper.category import classify_category, map_course_to_category
from app.services.scraper.config.context import get_uni_config
from app.services.scraper.guards import should_trust_generic_university_fee_fallback
from app.services.scraper.extractors import (
    ai_fallback,
    course_name,
    degree_level,
    description,
    duration,
    eligibility,
    english_test,
    fee,
    intake,
    location,
    study_mode,
)
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.http_fetcher import fetch_html, scrape_do_render_scope, scrape_do_static_scope
from app.services.scraper.provenance import build_course_page_provenance_footer

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Domestic-only detection — regex patterns on visible page text
# ---------------------------------------------------------------------------
import re as _re

# ── Location chrome-text guard ────────────────────────────────────────────────
# UTAS course pages embed the Key Information panel headings ("Key Information",
# "Entry requirements", "Course rules") as plain text immediately after the
# "Location" heading in the hidden #tabInternational panel.  When AI extractors
# (Gemini PRIMARY and FALLBACK) read the page they sometimes return these
# verbatim as location_text / course_location.  Any value matching ≥2 of these
# chrome phrases is treated as noise and discarded so the course gets
# location=None → the online-only rejection filter can fire correctly.
_LOCATION_CHROME_RE = _re.compile(
    r"\b(?:"
    # UTAS panel-heading chrome (#tabInternational hidden panel).
    r"key\s+information|entry\s+requirements?|course\s+rules?"
    # UniSQ (unisq.edu.au) footer quick-links column.  When the structural
    # `_from_unisq_quickfacts` reader returns None on an off-shape course
    # page, Gemini's location_text falls through and reads the homepage-
    # footer quick-links ("Accommodation UniSQ Events Contributing to our
    # communities") as the location.  Observed 2026-05-18 fleet-wide on
    # ~40 % of staged UniSQ rows (Master of Laws, Bachelor of Laws Honours,
    # Diploma of Multidisciplinary Studies, Master of Nursing, etc.).
    # The full junk string contains all three phrases; the ≥2-matches
    # guard rejects it while real UniSQ campus names (Ipswich, Toowoomba,
    # Springfield, Online, External) never match any of these phrases.
    r"|accommodation|unisq\s+events|contributing\s+to\s+our\s+communit(?:y|ies)"
    # Generic university website nav/footer menu labels seen fleet-wide.
    # UEL (uel.ac.uk) course pages scraped "STUDENT INFORMATION Campus life
    # Current students New students Accommodation Term dates" as location —
    # these are footer/nav column headings, not campus names.
    # A string containing ≥2 of these tokens is almost certainly nav chrome.
    r"|student\s+information|campus\s+life"
    r"|current\s+students|new\s+students|prospective\s+students"
    r"|term\s+dates?|open\s+days?|how\s+to\s+apply"
    r"|student\s+portal|student\s+services|student\s+union"
    r"|clearing|freshers|induction\s+week"
    r"|apply\s+now|contact\s+us|get\s+in\s+touch"
    r"|international\s+students|home\s+students"
    r"|tuition\s+fees?|scholarships?\s+and\s+bursaries"
    r")\b",
    _re.IGNORECASE,
)


# AI-target slot → canonical regex-output slot for the UOW timeout guard's
# canonical-slot bypass (2026-05-17).  When the regex pass on static HTML
# already filled the canonical slot, the AI fallback won't actually synthesise
# a new value, so the guard must not block the row.  Module-private so the
# regression test can import it instead of mirroring the mapping locally.
_UOW_CANONICAL_FOR: dict[str, str] = {
    "duration_text": "duration",
    "intake_text": "intake_months",
}


def _uow_timeout_guessed_fields(
    payload: dict, missing: list[str] | set[str]
) -> list[str]:
    """Return the UOW render-required fields that the AI fallback WOULD have
    to guess given the current ``payload`` and ``missing`` list.

    Used by the UOW browser-timeout guard at single_course.py inside
    ``extract_course``.  A text-shape slot (``duration_text`` / ``intake_text``)
    only counts as "would be AI-guessed" when BOTH the text slot is missing
    AND the canonical slot (``duration`` / ``intake_months``) is blank —
    if the canonical slot is already populated by the static-HTML regex
    pass, the AI fallback won't synthesise a new value, so the guard must
    not flip ``parser_error`` and drop the row from staging.

    ``study_mode`` has no canonical/text duality, so a blank value always
    counts.
    """
    out: list[str] = []
    for f in ("duration_text", "intake_text"):
        if f in missing and not payload.get(_UOW_CANONICAL_FOR.get(f, f)):
            out.append(f)
    if not payload.get("study_mode"):
        out.append("study_mode")
    return out


def _is_location_chrome(text: str) -> bool:
    """Return True when *text* looks like UTAS page-chrome headings.

    Two or more matches of ("key information", "entry requirements",
    "course rules") in the same string means the AI copied a panel heading
    block verbatim rather than extracting a real campus name.
    """
    return bool(text) and len(_LOCATION_CHROME_RE.findall(text)) >= 2


_DOMESTIC_ONLY_RE = _re.compile(
    # All patterns require the COURSE / PROGRAM to be the explicit subject.
    # Bare phrases like "Available to domestic students only" or
    # "Domestic students only" are intentionally excluded because they
    # appear in *application-pathway* sections (e.g. SATAC blocks at
    # Flinders University) even when the course IS available to
    # international students, producing a 100% false-positive rate.
    r"(?:"
    # Explicit course-level "not available" statements
    r"this\s+(?:course|program|degree)\s+is\s+(?:only\s+)?not\s+available\s+(?:for|to)\s+international"
    r"|this\s+(?:course|program|degree)\s+is\s+not\s+open\s+to\s+international"
    r"|this\s+(?:course|program|degree)\s+is\s+only\s+available\s+to\s+(?:australian|domestic)"
    # QUT (qut.edu.au): banner reads "This course is only available FOR
    # Australian and New Zealand students." on every domestic-only course
    # page (e.g. /courses/diploma-in-architectural-studies-bachelor-of-
    # architectural-design?domestic). The existing "only available TO" and
    # "only FOR" patterns above don't cover the "only available FOR …"
    # phrasing, so QUT domestic-only courses were staging with full
    # international fees from the regex Fee pattern hit on the page chrome.
    # Verified live 2026-05-17 (job_…, uni 1011): Diploma in Architectural
    # Studies/Bachelor of Architectural Design staged with A$40,700/Annual
    # despite the banner.
    r"|this\s+(?:course|program|degree)\s+is\s+only\s+available\s+for\s+(?:australian|domestic)"
    r"|this\s+(?:program|course|degree)\s+is\s+only\s+for\s+(?:australian|domestic)"
    r"|this\s+(?:program|course|degree)\s+does\s+not\s+accept\s+international"
    # "Sorry, this course is not available to international students"
    r"|sorry[,.]?\s+this\s+(?:course|program)\s+is\s+not\s+available\s+to\s+international"
    # ECU "Important" callout: "This course is not (currently) offered for
    # study on-campus in Australia to international students with a student
    # visa."  Appears in the international-tab Important panel on courses ECU
    # does not offer to visa-holding international students (e.g. Master of
    # Nursing (Nurse Practitioner), Graduate Certificate in News and
    # Entertainment Media). Verified live 2026-05-12. ECU sometimes inserts
    # the word "currently" between "not" and "offered" — both phrasings must
    # match (regression caught 2026-05-12 via news-and-entertainment-media).
    r"|this\s+(?:course|program|degree)\s+is\s+not(?:\s+currently)?\s+offered\s+(?:for\s+study\s+)?on[-\s]campus\s+(?:in\s+australia\s+)?to\s+international\s+students?"
    # "Open to domestic applicants only" — rare but unambiguous
    r"|open\s+to\s+domestic\s+applicants\s+only"
    # NOTE (2026-05-15 fix): the bare "not available to international
    # students" pattern (no "this course/program/degree" anchor) was
    # REMOVED.  It produced fleet-wide false-positive domestic_only
    # rejections at UOW, whose Bachelor / Master pages embed a
    # part-time-mode tooltip:
    #   "Part time study is not available to international students
    #    studying onshore on a student visa."
    # That sentence is about the *study mode*, not the course, yet it
    # tripped the bare pattern and caused the entire UOW catalogue to
    # skip with [DOMESTIC ONLY] (verified live 2026-05-15 against
    # Bachelor of Arts and 145+ other UOW pages).  All legitimate
    # "this course is not available to international students" hits
    # are still covered by the explicit course-anchored patterns
    # above (line ~81), keeping test_explicit_hard_phrase_still_flags
    # green.  Per the file-level rule on line 73 ("All patterns require
    # the COURSE / PROGRAM to be the explicit subject") we should not
    # carve more bare-noun-phrase exceptions.
    # "International applications are not accepted" / "not accepting
    # international student applications" — Federation and similar.
    r"|international\s+(?:student\s+)?applications?\s+(?:are\s+)?not\s+(?:accepted|available|open)"
    r"|not\s+currently\s+accepting\s+international\s+(?:student\s+)?applications?"
    # NOTE (2026-05-10 fix): The Torrens "begin your application to study as
    # a domestic student" phrase was REMOVED from the hard pattern.  The
    # original assumption ("international-eligible courses have a parallel
    # section") was wrong — the phrase appears in the Admission criteria
    # boilerplate on EVERY Torrens course page, including legitimate
    # international courses (Master of Education Advanced, MBA Advanced,
    # Bachelor of Business (Sport Management), Diploma of Health Science,
    # Graduate Diploma of Public Health, etc.) which all have CRICOS codes,
    # international fees, and Domestic/International audience tabs.  Treating
    # it as a hard signal silently dropped ~30+ Torrens courses per scrape.
    # If genuinely domestic-only HDR courses (e.g. Doctor of Philosophy by
    # Prior Works) need filtering, prefer per-course/per-uni YAML rules over
    # a global text pattern that fires on universal page chrome.
    # UTAS distance-courses disclaimer — hard signal even when the page has a
    # structural #tabInternational panel (UTAS includes that tab on every page).
    # The phrase "please see the list of distance courses (i.e. online and
    # taken outside Australia)" always accompanies the soft "may not be
    # available to international students" text and appears exclusively on
    # pages where the course is online-only / not available to student-visa
    # holders.  Treating it as a hard pattern avoids the _has_international_
    # section suppression that would otherwise swallow the soft signal.
    r"|please\s+see\s+the\s+list\s+of\s+distance\s+courses"
    # Federation University: course pages embed a `StudentTypeBlock` JSON in
    # a <script> tag with `"hasInternational": false` for courses that the
    # International tab is greyed out for (e.g. VET / TAFE Certificates and
    # Diplomas with no international offering). HE courses with international
    # availability either omit the block or set the value to true.
    #
    # The pattern requires the `StudentTypeBlock` anchor to appear within a
    # short window before the `hasInternational: false` marker so the rule
    # is effectively scoped to Federation's CMS shape and won't incidentally
    # fire on a generic page that happens to contain the bare token in
    # another context. The 0..400-char window covers the JSON wrapper +
    # `props` block + label fields that sit between the two markers.
    r'|"StudentTypeBlock"[\s\S]{0,400}?"hasInternational"\s*:\s*false'
    # Torrens University: course pages render a `studenttypes="..."`
    # attribute on the audience-tab component. International-eligible
    # courses publish `studenttypes="Domestic, International"` (or
    # similar comma-separated list). Domestic-only courses publish the
    # literal `studenttypes="Domestic"` with no `International` token.
    # The pattern requires the closing quote IMMEDIATELY after `Domestic`
    # so a comma-separated list never matches. Verified live (job_a334b…)
    # against Bachelor of Media Production and Communication +
    # Graduate Diploma of Counselling (both domestic-only) vs Bachelor
    # of Interior Design (Commercial) (international) which carries
    # `studenttypes="Domestic, International"` and does not match.
    r'|studenttypes\s*=\s*"Domestic"'
    # Victoria University: every /courses/<slug>/ URL is rewritten to
    # /courses/<slug>/international by the URL-rewrite block ~line 815 so
    # the international tab (intl fee, IELTS, intake, full campus list)
    # is visible to the regex extractor.  For courses NOT offered to
    # international students (TAFE/VET certificates, domestic-only
    # diplomas) the /international URL returns HTTP 200 with a soft-404
    # body whose <title> is literally "Page not found | Victoria
    # University".  Without this signal the pipeline falls through and
    # Gemini reads the visible domestic VET fee from the body, then the
    # row gets staged with the domestic fee shown as international (e.g.
    # "Diploma of Sport SIS50321" — A$18,360/Annual stamped despite the
    # course carrying a "Domestic students only" badge).  Verified live
    # 2026-05-14 against diploma-of-sport-sis50321,
    # certificate-i-in-work-education vs bachelor-of-business-bbns
    # (which keeps a real <title>Bachelor of Business | Victoria University</title>).
    r"|<title[^>]*>\s*page\s+not\s+found\s*\|\s*victoria\s+university\s*</title>"
    r")",
    _re.IGNORECASE,
)


# "This course may not be available to international students" — UTAS uses
# this soft modal on many pages INCLUDING courses that DO accept international
# students (it functions as a campus-specific caveat, not a hard exclusion).
# Treating it as a hard signal produces false-positive domestic_only rejections
# for courses that have a full international tab and international fee schedule.
#
# It is now separated into _DOMESTIC_ONLY_SOFT_RE and only applied when the
# page has no structural evidence of an international section.
_DOMESTIC_ONLY_SOFT_RE: _re.Pattern[str] = _re.compile(
    r"(?:this\s+course\s+)?may\s+not\s+be\s+available\s+to\s+international\s+students?",
    _re.IGNORECASE,
)


def _has_international_section(html: str) -> bool:
    """True when the raw HTML has structural evidence of an international section.

    Uses cheap string/regex checks on the raw HTML (not stripped text) so the
    DOM attribute ``id="tabInternational"`` is detectable without BeautifulSoup.

    Conservative: a false negative (missing a real international section) is
    worse than a false positive (reporting a section that isn't really there),
    so multiple independent signals are checked — any one is sufficient.
    """
    if not html:
        return False
    # UTAS: hidden `#tabInternational` panel present in DOM from page load.
    if _re.search(r'id=["\']?tabInternational["\']?', html, _re.IGNORECASE):
        return True
    # CRICOS registration appears in international sections of AU course pages.
    if _re.search(r'\bCRICOS\b', html):
        return True
    # Explicit international fee / entry requirements blocks.
    if _re.search(
        r'international.*(?:tuition|entry\s+requirements?|fee)',
        html, _re.IGNORECASE,
    ):
        return True
    return False


# ── "Not currently accepting / no current intake" rejection ──────────────────
# Some universities leave dormant ("rested") course pages live but explicitly
# state the program is closed to applications. Newcastle's Bachelor of
# Midwifery is the canonical 2026-05-15 example: the page renders a sidebar
# panel "<h5>No current intake</h5> <p>This program is not currently accepting
# new applications.</p>" and a meta tag "<meta name=\"UON.Degree.DegreeStatus\"
# content=\"rested\">". Without an explicit guard the pipeline still extracts
# the (stale) campus list and Gemini fills in fee/duration from old structured
# data, staging a course that should never appear in the catalogue.
#
# All patterns are unambiguous course-level closure statements. The Newcastle
# meta-tag pattern is host-shape-anchored (requires the UON namespace) so it
# cannot fire on an unrelated page that happens to mention the word "rested".
_NOT_ACCEPTING_RE = _re.compile(
    r"(?:"
    # Newcastle "<h5>No current intake</h5>" panel — a hard signal that the
    # program is dormant. The h5 wrapper is required so a chance occurrence
    # of the phrase in marketing copy does not match.
    r"<h\d[^>]*>\s*no\s+current\s+intake\s*</h\d>"
    # Generic "this program/course/degree is not currently accepting (new)
    # applications" — Newcastle uses this in the rested-program panel
    # alongside the h5 above. Anchored on "this <noun> is" to keep the
    # course/program the explicit subject (per the file-level rule).
    r"|this\s+(?:program|course|degree)\s+is\s+not\s+currently\s+accepting\s+(?:new\s+)?applications"
    # Newcastle UON.Degree.DegreeStatus meta tag with content="rested" or
    # content="closed" — the canonical machine-readable signal published in
    # the page <head>. Host-shape-anchored on the UON namespace.
    r'|<meta[^>]*name=["\']UON\.Degree\.DegreeStatus["\'][^>]*content=["\'](?:rested|closed|inactive)["\']'
    # JSON-shape variant of the same UON status (sometimes appears inside a
    # <script> body): "UON.Degree.DegreeStatus":"rested".
    r'|"UON\.Degree\.DegreeStatus"\s*:\s*"(?:rested|closed|inactive)"'
    r")",
    _re.IGNORECASE,
)


# ── Newcastle online-only rejection ───────────────────────────────────────────
# Newcastle (newcastle.edu.au) per-degree pages publish a machine-readable
# meta tag listing the campuses the course is offered at:
#
#   <meta name="UON.Degree.Location" content="location_callaghan; location_online">
#   <meta name="UON.Degree.Location" content="location_online">
#
# When the meta value is the bare token "location_online" (no other
# location_* token alongside it), the course is delivered exclusively
# online — the Study-location toggle group has a single Online radio
# and no physical-campus option (verified 2026-05-15 against
# graduate-certificate-in-mental-health-nursing,
# graduate-certificate-in-marketing-and-digital-strategy).
#
# Without this guard the pipeline falls through to Gemini, which often
# misreads the auxiliary `ShortCourses.ModeOfDelivery="Face to Face,
# Online"` meta and stamps `study_mode="On Campus"` — that bypasses the
# generic online_only guard in guards.py (which only fires when
# study_mode contains "online" AND no campus keyword) and the row is
# staged with course_location BLANK and the wrong mode.
#
# The pattern is host-shape-anchored on the UON namespace and requires
# the value to be exactly "location_online" (optionally surrounded by
# whitespace), with no other location_* token, so it cannot fire on a
# multi-campus course that happens to include Online as one option.
_UON_ONLINE_ONLY_META_RE = _re.compile(
    r'<meta[^>]*name=["\']UON\.Degree\.Location["\'][^>]*'
    r'content=["\']\s*location_online\s*["\']',
    _re.IGNORECASE,
)
# JSON-shape variant — same value, inside a <script> body.
_UON_ONLINE_ONLY_JSON_RE = _re.compile(
    r'"UON\.Degree\.Location"\s*:\s*"\s*location_online\s*"',
    _re.IGNORECASE,
)


def _is_uon_online_only_page(html: str) -> bool:
    """True when a Newcastle page's UON.Degree.Location is online-only.

    Matches BOTH the meta-tag form and the JSON-shape form. Returns
    False for multi-campus courses (e.g. content="location_callaghan;
    location_online") because such values do not match the bare
    "location_online" pattern (any preceding/trailing token before the
    closing quote would prevent the match).
    """
    if not html:
        return False
    if _UON_ONLINE_ONLY_META_RE.search(html):
        return True
    if _UON_ONLINE_ONLY_JSON_RE.search(html):
        return True
    return False


# ── UniSQ pure-online course detector ─────────────────────────────────────
#
# UniSQ (unisq.edu.au) renders the primary Location quickfact as an
# unordered list immediately following a `<div class="fw-semibold">Location
# </div>` header.  For pure-online courses (e.g. Graduate Diploma of
# Information Technology, Graduate Diploma of Information Systems, Diploma
# of Multidisciplinary Studies) the list contains ONLY virtual entries
# (`<li>Online</li>`, `<li>External</li>`) with no physical campus.
#
# Without an explicit detector here:
#   1. `_from_unisq_quickfacts` correctly returns "Online" / "External"
#   2. `_sanitise_for_display` strips both via `_REMOVE_VIRTUAL` → None
#   3. payload[course_location] stays None
#   4. study_mode gets stamped "On Campus" by a downstream rule (the page
#      DOES include lower-down delivery-mode panels listing
#      "Springfield, Toowoomba, Online" as alternative cohorts — those
#      panels confuse the mode rule)
#   5. generic `online_only` guard in guards.py never fires (it requires
#      study_mode to contain "online" AND no campus keyword)
#   6. row stages with course_location BLANK and the wrong mode
#
# This is structurally identical to the Newcastle UON online-only bug
# (2026-05-15) so we use the same skip pattern. The detector scans for
# the primary Location `<ul>` and confirms every `<li>` is a virtual
# value; multi-campus courses (e.g. `<li>Toowoomba</li><li>Online</li>`)
# deliberately do NOT match — only courses where EVERY listed campus is
# virtual are skipped.
_UNISQ_PRIMARY_LOCATION_BLOCK_RE = _re.compile(
    r'<div[^>]*class="[^"]*fw-semibold[^"]*"[^>]*>\s*Location\s*</div>\s*'
    r'<ul[^>]*>(.*?)</ul>',
    _re.IGNORECASE | _re.DOTALL,
)
_UNISQ_LI_TEXT_RE = _re.compile(r'<li[^>]*>\s*([^<]+?)\s*</li>', _re.IGNORECASE)
_UNISQ_VIRTUAL_VALUES = {"online", "external", "distance", "remote"}


def _is_unisq_online_only_page(html: str) -> bool:
    """True when a UniSQ page's primary Location list is all-virtual.

    Returns False when:
      * No primary Location panel is found (page doesn't follow the idiom)
      * The `<ul>` is empty
      * ANY `<li>` carries a non-virtual value (physical campus)

    Returns True only when every `<li>` text is one of the virtual
    values in `_UNISQ_VIRTUAL_VALUES`.
    """
    if not html:
        return False
    m = _UNISQ_PRIMARY_LOCATION_BLOCK_RE.search(html)
    if not m:
        return False
    li_texts = _UNISQ_LI_TEXT_RE.findall(m.group(1))
    if not li_texts:
        return False
    for raw in li_texts:
        if raw.strip().lower() not in _UNISQ_VIRTUAL_VALUES:
            return False
    return True


def _is_not_accepting_page(html: str) -> bool:
    """Return True when the page explicitly states the program is closed.

    Covers Newcastle's rested-program signals (h5 panel, generic phrase,
    UON.Degree.DegreeStatus meta tag) and any future host-anchored variant
    added to ``_NOT_ACCEPTING_RE``.

    Runs against RAW html so attribute-bearing markers (the meta tag) and
    h5 wrapper survive — both would be eaten by a tag-strip pass.
    """
    if not html:
        return False
    return bool(_NOT_ACCEPTING_RE.search(html))


def _is_domestic_only_page(html: str) -> bool:
    """Return True when the page explicitly states it is for domestic students only.

    Strips HTML tags before matching so tag noise doesn't break patterns.
    Only fires on unambiguous phrases to avoid false positives.

    Soft signals (e.g. "may not be available to international students") are
    only honoured when no structural international section exists on the page —
    see ``_DOMESTIC_ONLY_SOFT_RE`` and ``_has_international_section``.
    """
    if not html:
        return False
    # Hard patterns: unambiguous course-level exclusion statements.
    # Run against RAW html FIRST so attribute-in-tag markers
    # (e.g. Torrens ``<div data-studenttypes="Domestic">``) survive —
    # the tag-strip below would otherwise eat the whole tag including
    # its attribute and the regex would never see the marker. Federation's
    # ``"hasInternational": false`` JSON sits inside a <script> tag's
    # text content (not an attribute), so it survives the strip and
    # would still match either way.
    if _DOMESTIC_ONLY_RE.search(html):
        return True
    text = _re.sub(r"<[^>]+>", " ", html)
    text = _re.sub(r"\s+", " ", text)
    if _DOMESTIC_ONLY_RE.search(text):
        return True
    # Soft pattern: "may not be available" — only block when there is no
    # structural international section elsewhere on the same page.
    if _DOMESTIC_ONLY_SOFT_RE.search(text) and not _has_international_section(html):
        return True
    return False


_DURATION_LABEL_PAT_RE = _re.compile(
    r"\b(?:course\s*(?:duration|length)|duration|programme?\s*(?:duration|length)"
    r"|study\s*duration)\b",
    _re.IGNORECASE,
)
_PARTTIME_ONLY_PT_RE = _re.compile(r"\bpart[- ]?time\b", _re.IGNORECASE)
_PARTTIME_ONLY_FT_RE = _re.compile(r"\bfull[- ]?time\b", _re.IGNORECASE)

# Task #233: minimum browser-returned body length to count as a genuine
# "browser rescue" (used by the confirmed-browser-only host gate).  A fully
# rendered course page is tens of KB; a Cloudflare/anti-bot challenge
# interstitial or empty shell is far smaller, so this floor prevents a few
# unusable shells from wrongly marking a host browser-only.
_BROWSER_RESCUE_MIN_HTML_LEN = 2000


def _is_parttime_only_page(html: str) -> bool:
    """Return True when the course-length cell contains Part-time but not Full-time.

    Detects WLV-style pages where the duration label/value pair reads
    "Course length: Part-time (1 year)" with no Full-time option listed.
    Such courses are not suitable for international students (visa rules
    typically require full-time enrolment).

    Uses BeautifulSoup to inspect the DOM cell directly so incidental
    occurrences of "part-time" in page prose (e.g. a footer note) don't
    produce false positives.
    """
    if not html:
        return False
    try:
        from bs4 import BeautifulSoup as _BS4_pt

        soup = _BS4_pt(html, "html.parser")
        for label_tag in soup.find_all(("dt", "th", "strong", "b")):
            label_text = label_tag.get_text(" ", strip=True).rstrip(":").strip()
            if not _DURATION_LABEL_PAT_RE.search(label_text):
                continue
            # Retrieve the associated value cell
            if label_tag.name == "dt":
                sibling = label_tag.find_next_sibling("dd")
                value_text = sibling.get_text(" ", strip=True) if sibling else ""
            elif label_tag.name == "th":
                sibling = label_tag.find_next_sibling("td")
                value_text = sibling.get_text(" ", strip=True) if sibling else ""
            else:
                nxt = label_tag.next_sibling
                value_text = str(nxt).strip() if nxt else ""
            if not value_text:
                continue
            # Part-time present AND Full-time absent → reject
            if _PARTTIME_ONLY_PT_RE.search(value_text) and not _PARTTIME_ONLY_FT_RE.search(value_text):
                return True
    except Exception:
        pass
    return False


def _parttime_only_filter_enabled() -> bool:
    """Return True when extraction.filters.reject_parttime_only is set in uni config.

    Fail-closed: returns False when the contextvar is unset (no per-uni context),
    so the filter never fires for universities that haven't explicitly opted in.
    """
    uc = get_uni_config()
    return uc is not None and uc.extraction.filters.reject_parttime_only


_FEDERATION_HOSTS: frozenset[str] = frozenset(
    {"www.federation.edu.au", "federation.edu.au"}
)


def _is_federation_host(url: str | None) -> bool:
    """Strict netloc guard so the Federation-specific domestic-only signals
    below can never fire on another university's pages (same discipline as
    the host-gated UOW/ECU blocks in intake.py)."""
    if not url:
        return False
    return (urlparse(url).netloc or "").lower() in _FEDERATION_HOSTS


# Commonwealth Supported Place (CSP) / HECS-HELP are Australian government
# domestic-funding categories.  International students are NEVER offered a
# CSP — so a course whose ONLY fee signal is "Commonwealth Supported Place"
# (with no parallel international dollar amount) is domestic-only by
# definition.  This is the high-precision signal behind the greyed-out
# International tab on pages like the Bachelor of Exercise and Sport Science
# (federation.edu.au/courses/dpk5-...): Fees = "Commonwealth Supported Place".
_CSP_RE = _re.compile(
    r"commonwealth\s+supported\s+place|\bCSP\b|HECS[-\s]?HELP", _re.IGNORECASE
)
# A real international tuition figure looks like "$42,000" / "A$42,000" /
# "AUD 42000".  Used to confirm the page exposes NO international dollar fee
# before we trust the CSP-only signal.
_INTL_DOLLAR_FEE_RE = _re.compile(
    r"(?:A\$|AUD\s*|\$)\s?\d{2,3}(?:[,\s]?\d{3})\b", _re.IGNORECASE
)


def _federation_domestic_only_signal(rendered_html: str, url: str) -> str | None:
    """Federation-scoped domestic-only detection from the RENDERED DOM.

    Returns a short reason string when the rendered page indicates the course
    is not offered to international students, else None.  Host-gated to
    federation.edu.au so it cannot affect any other university.

    Two independent signals, either is sufficient:

      1. CSP-only fees — the page shows a Commonwealth Supported Place / HECS
         fee and NO international dollar amount anywhere.  CSP is domestic-only
         by definition.
      2. Disabled International tab — the audience toggle's "International"
         button is rendered disabled/greyed-out.

    Designed to run AFTER the existing ``_DOMESTIC_ONLY_RE`` /
    ``_is_domestic_only_page`` checks as a supplement, catching the cases
    where the ``"hasInternational": false`` JSON string your existing guard
    hunts for isn't in the rendered DOM (the React app disables the tab via
    component state rather than emitting that literal string).
    """
    if not rendered_html or not _is_federation_host(url):
        return None

    # Signal 2: disabled International tab.  Check both attribute orderings
    # (disabled-before-label and label-before-disabled).
    #
    # NOTE 2026-05-28: the original `_label_then_disabled` matcher looked
    # back 300 chars from `International</button>` and flagged disabled if
    # ANY `disabled` keyword appeared in the window.  That gave false
    # positives whenever an unrelated disabled UI element (nav buttons,
    # a disabled "Apply now" CTA, etc.) sat within 300 chars BEFORE the
    # International tab — verified live on Federation Nursing /
    # Physiotherapy / IT Cybersecurity pages, which all wrongly got
    # flagged.  The fix: scan back from `International</...>` to the
    # nearest opening `<button` or `<a` tag and check ONLY THAT TAG's
    # own attributes for a disabled marker.  This is the same scope
    # `_disabled_before` already uses (a single element's opening tag),
    # just applied to the label-after ordering too.
    _disabled_before = _re.search(
        r"<(?:button|a)\b[^>]*(?:\bdisabled\b|aria-disabled\s*=\s*[\"']?true"
        r"|class\s*=\s*[\"'][^\"']*(?:is-)?disabled)[^>]*>\s*International\s*<",
        rendered_html, _re.IGNORECASE,
    )
    if _disabled_before:
        return "federation_intl_tab_disabled"
    # Find every `International</button>` / `International</a>` close-tag
    # and, for each, locate the matching opening tag (the nearest preceding
    # `<button` or `<a`, no other intervening tag of the same kind).  Then
    # check only that opening tag's attributes.
    for _m in _re.finditer(
        r"International\s*</(?P<tag>button|a)>",
        rendered_html, _re.IGNORECASE,
    ):
        _tag = _m.group("tag").lower()
        # Scan back from this close-tag for the matching opening tag.
        # `rfind` finds the LAST occurrence in the slice before the
        # close-tag, which is the immediately-enclosing element provided
        # no other tag of the same kind opened in between — good enough
        # for the well-formed React markup Federation produces.
        _prefix = rendered_html[: _m.start()]
        _open_pos = _prefix.rfind(f"<{_tag}")
        if _open_pos == -1:
            # Try uppercase variant (rare but defensive)
            _open_pos = _prefix.rfind(f"<{_tag.upper()}")
        if _open_pos == -1:
            continue
        # Take only up to the end of the opening tag (the first `>` after
        # the opening tag start).  This is the attribute window.
        _open_tag_end = rendered_html.find(">", _open_pos)
        if _open_tag_end == -1 or _open_tag_end >= _m.start():
            continue
        _attrs = rendered_html[_open_pos:_open_tag_end + 1]
        if _re.search(
            r"\bdisabled\b|aria-disabled\s*=\s*[\"']?true"
            r"|class\s*=\s*[\"'][^\"']*(?:is-)?disabled",
            _attrs, _re.IGNORECASE,
        ):
            return "federation_intl_tab_disabled"

    # Signal 1: CSP-only fees.  Only trust this when there is NO international
    # dollar figure anywhere on the rendered page (otherwise a genuinely
    # international course that merely mentions CSP for its domestic cohort
    # would be wrongly dropped).
    if _CSP_RE.search(rendered_html) and not _INTL_DOLLAR_FEE_RE.search(
        rendered_html
    ):
        return "federation_csp_domestic_only"

    return None


def _domestic_only_filter_enabled() -> bool:
    """Return True because domestic-only courses are globally ineligible.

    Historical YAML and admin settings may still contain
    ``filters.domestic_only.enabled: false``.  They are retained for backward
    compatibility only and must not let a confirmed domestic-only programme
    enter any university's international review queue.
    """
    return True


def _vision_ocr_trusted() -> bool:
    """Phase 5 gate: return True when per-course vision OCR should run.

    Reads ``extraction.english.trust_vision_ocr`` from the current
    per-university config contextvar.

    Fail-open policy: if the contextvar is not set (no uni context), returns
    True so that vision OCR continues to run — preserving pre-gate behaviour
    for any code path that hasn't wired set_uni_config() yet.

    Set ``trust_vision_ocr: false`` in the per-uni YAML stub to disable the
    entire vision OCR pass for universities whose course pages contain only
    decorative images (e.g. student portraits) that cause Gemini to hallucinate
    IELTS/PTE/TOEFL values.  ACAP and Kaplan are the canonical examples.
    """
    uc = get_uni_config()
    return uc is None or uc.extraction.english.trust_vision_ocr


# Degree-level values that indicate a postgraduate course.
# The central English-requirements page is fetched via plain HTTP (no JS
# rendering), so it only captures whatever level the static HTML exposes
# first — typically the undergraduate table.  Applying those UG values to
# PG courses produces incorrect (too-low) English scores.
# Courses at these levels are exempt from the central_page:english fallback;
# they will stage with NULL English scores rather than wrong Bachelor's values.
# NULL is always recoverable; wrong data propagates silently.
_CENTRAL_ENGLISH_PG_LEVELS: frozenset[str] = frozenset({
    "Master's",
    "Graduate Certificate",
    "Graduate Diploma",
    "Doctorate",
})


# ── Week 2 P5: SKIP_CENTRAL_ENGLISH_PROPAGATION toggle ─────────────────────
# When enabled, the central-page English values (UG-only IELTS/PTE/TOEFL
# extracted from the university-wide /english-language-requirements/ page)
# are NOT written to per-course evidence rows.  The central-page extraction
# still runs for diagnostic purposes (cost tracking, log emission), but
# course rows that lack their own English signal stay NULL rather than
# inheriting potentially-wrong defaults.
#
# Default: False — preserves the existing pathway/PG-aware Path 1 + Path 2
# logic that has been carefully tuned per university.  Operators can flip
# the toggle on after a 23-uni regression sweep confirms no regressions:
#
#     export SKIP_CENTRAL_ENGLISH_PROPAGATION=true
#
# Bug class addressed: pathway programs (UniSQ UniPrep, ELICOS bridges)
# wrongly inheriting the central UG IELTS=6.5 because their own pages do
# not state an English requirement.  NULL is recoverable; a silently-
# wrong propagated value is not.
def _skip_central_english_propagation() -> bool:
    import os
    val = os.environ.get("SKIP_CENTRAL_ENGLISH_PROPAGATION", "false").strip().lower()
    return val in {"true", "1", "yes", "on"}

# ── Extraction-method authority model ────────────────────────────────────────
# Every extraction method is assigned a numeric authority level.  Higher
# authority wins when two methods disagree about the same field, and the PG
# clear-out only erases values whose best-authority method is below the
# COURSE-SPECIFIC threshold (_AUTHORITY_COURSE_SPECIFIC).
#
# Authority bands:
#   1 — university-wide HTML scrape (central page)
#   2 — university-wide PDF  (fee schedule / admissions PDF)
#   3 — course-specific text (regex, Gemini, browser, AI fallback)
#   4 — visual proof from the course page itself (vision OCR screenshot)
#   5 — hard-coded site-specific extractor (pre-seed; highest confidence)
#
# How to read the PG clear-out rule:
#   "If the best authority for an English slot is < 3, the value came from a
#    university-wide source; clear it.  If ≥ 3, it came from the course page
#    in some form; keep it."
#
# This generalises the old _PER_COURSE_VISION_METHODS frozenset so we don't
# have to hand-add each new extractor that needs to survive the clear-out.
_AUTHORITY_UNIVERSITY_WIDE = 1
_AUTHORITY_UNIVERSITY_PDF = 2
_AUTHORITY_COURSE_SPECIFIC = 3   # threshold: keep values at or above this
_AUTHORITY_COURSE_VISION = 4
_AUTHORITY_PRE_SEED = 5

METHOD_AUTHORITY: dict[str, float] = {
    # 1 — university-wide HTML
    "central_page": _AUTHORITY_UNIVERSITY_WIDE,
    "central_page:english": _AUTHORITY_UNIVERSITY_WIDE,
    "central_page:fees:exact": _AUTHORITY_UNIVERSITY_WIDE,
    "central_page:fees:high": _AUTHORITY_UNIVERSITY_WIDE,
    "central_page:fees:medium": _AUTHORITY_UNIVERSITY_WIDE,
    "sibling_cache": _AUTHORITY_UNIVERSITY_WIDE,
    # 2 — university-wide PDF (fuzzy / uni-wide)
    "uni_pdf:fee": _AUTHORITY_UNIVERSITY_PDF,
    "uni_pdf:fees": _AUTHORITY_UNIVERSITY_PDF,
    "uni_pdf:fees:per_course": _AUTHORITY_UNIVERSITY_PDF,
    "uni_pdf:requirements": _AUTHORITY_UNIVERSITY_PDF,
    "uni_pdf:english": _AUTHORITY_UNIVERSITY_PDF,
    # 2.5 — university-wide PDF matched via CRICOS code (beats fuzzy PDF, below
    #         course-specific text).  Float tier; _method_authority returns float.
    "uni_pdf:cricos_match:fees": 2.5,
    "uni_pdf:cricos_match:requirements": 2.5,
    # 3 — course-specific text
    "gemini_primary": _AUTHORITY_COURSE_SPECIFIC,
    "rule:fee": _AUTHORITY_COURSE_SPECIFIC,
    "rule:english": _AUTHORITY_COURSE_SPECIFIC,
    "rule:duration": _AUTHORITY_COURSE_SPECIFIC,
    "rule:intake": _AUTHORITY_COURSE_SPECIFIC,
    "rule:study_mode": _AUTHORITY_COURSE_SPECIFIC,
    "rule:cricos": _AUTHORITY_COURSE_SPECIFIC,
    "per_course_browser": _AUTHORITY_COURSE_SPECIFIC,
    "ai_fallback": _AUTHORITY_COURSE_SPECIFIC,
    "regex": _AUTHORITY_COURSE_SPECIFIC,
    "vit_static_fallback": _AUTHORITY_COURSE_SPECIFIC,
    # 4 — visual proof from the course page
    "per_course_vision": _AUTHORITY_COURSE_VISION,
    "per_course_vision_cached": _AUTHORITY_COURSE_VISION,
    # 5 — hard-coded site-specific extractor
    "pre_seed": _AUTHORITY_PRE_SEED,
    "csu_static_extract": _AUTHORITY_PRE_SEED,
    "bond_pre_seed": _AUTHORITY_PRE_SEED,
    "ecu_pre_seed": _AUTHORITY_PRE_SEED,
}

# ── Structural course-page method protection ──────────────────────────────────
# Gemini PRIMARY runs after all structural extractors and may overwrite fields
# those extractors already set correctly.  The rules below define which source
# methods represent direct, non-AI parses of the course page's structured
# markup (DOM labels, meta tags, H1s, regex patterns).  When Gemini PRIMARY
# tries to write a field whose current best evidence comes from one of these
# methods, the write is skipped — "course page wins".
#
# NOT protected (Gemini may still fill / override these):
#   ai_fallback         — itself an AI call, no special authority
#   vit_static_fallback — site-specific static lookup, sometimes incomplete
#   sibling_cache       — inherited from a sibling course (weaker than live page)
#   central_page*       — university-wide values deliberately designed to be
#                         overrideable by course-specific reads
#   uni_pdf*            — university-wide PDFs; Gemini can override with per-
#                         course page data (already handled by fee_term guard)
#
# English slots (ielts_overall, pte_overall, toefl_overall, …) are excluded from
# the protection even when set by rule:english, because Gemini reading the actual
# course page is more reliable than a generic degree-level heuristic rule.
_ENGLISH_SLOTS: frozenset[str] = frozenset({
    "ielts_overall", "ielts_reading", "ielts_writing",
    "ielts_listening", "ielts_speaking",
    "pte_overall", "toefl_overall", "toefl_listening",
    "toefl_reading", "toefl_writing", "toefl_speaking",
    "cambridge_overall", "duolingo_overall",
    "english_requirement_text",
})

_STRUCTURAL_COURSE_PAGE_EXACT: frozenset[str] = frozenset({
    "regex",           # structured DOM text via compiled patterns
    "per_course_browser",  # browser-fetched and DOM-parsed course page
})

_STRUCTURAL_COURSE_PAGE_PREFIXES: tuple[str, ...] = (
    "duration.",       # duration.structural — reads explicit Course Duration label
    "course_name.",    # course_name.h1, course_name.title, …
    "description.",    # description.meta, description.og, …
    "study_mode:",     # study_mode:rule — reads explicit Delivery/Mode label
    "location.",       # location.strong, location.structured, …
    "intake.",         # intake.structural, intake.summary_start, intake.session_names,
                       # intake.semester, intake.ecu_semester, intake.campus_pivot —
                       # all non-AI structural / DOM-anchored intake passes.  Without
                       # this prefix the 0.75-confidence gemini_primary value silently
                       # supersedes a 0.80-confidence intake.structural read of the
                       # exact <strong>Start dates (X)</strong> sidebar value
                       # (2026-05-15 Newcastle Bachelor of Physiotherapy bug:
                       # structural read ["January"] from "Semester 1 — 27 Jan 2026"
                       # but Gemini returned ["January","February"] and won).
    "rule:intake",     # rule-based intake inference
    "rule:study_mode", # rule-based study-mode inference
    "rule:cricos",     # rule-based CRICOS inference
    "degree_level:",   # degree_level.*_banner / *_panel — structural award-text
                       # parses (e.g. Leeds Trinity .banner-title__sub, BCU panel)
                       # and their co-derived academic_level. Without this prefix
                       # gemini_primary silently overwrote a correct structural
                       # academic_level="Undergraduate" with a wrong "Year 12"
                       # classification (2026-07-10 Leeds Trinity bug).
    # NOTE: rule:english and rule:fee intentionally excluded — Gemini reading
    # the actual page is more reliable than a generic degree-level heuristic.
)


def _is_structural_course_page_method(method: str) -> bool:
    """Return True when *method* represents a non-AI, structural parse of the
    course page (DOM labels, meta tags, H1 headings, regex patterns).

    Used to enforce "course page wins": when such a method already owns a
    field, ``gemini_primary`` is not allowed to overwrite it.
    """
    if method in _STRUCTURAL_COURSE_PAGE_EXACT:
        return True
    return any(method.startswith(p) for p in _STRUCTURAL_COURSE_PAGE_PREFIXES)


def _method_authority(method: str) -> float:
    """Return the authority level for a given extraction method string.

    Exact-key lookup first; then prefix scan so ``"central_page:english"``
    correctly resolves to ``"central_page"`` → 1.  Falls back to
    ``_AUTHORITY_COURSE_SPECIFIC`` (3) for unknown methods so new extractors
    are not accidentally treated as university-wide.

    Returns float to accommodate the 2.5 tier used by CRICOS-matched PDF
    methods (``uni_pdf:cricos_match:fees``).
    """
    if method in METHOD_AUTHORITY:
        return METHOD_AUTHORITY[method]
    for key, auth in METHOD_AUTHORITY.items():
        if method.startswith(key + ":") or method.startswith(key + "_"):
            return auth
    return _AUTHORITY_COURSE_SPECIFIC


def can_override(existing_method: str, new_method: str) -> bool:
    """Return True if *new_method* may replace a value already set by *existing_method*.

    A higher-authority method always wins.  Equal authority does NOT override
    (first-writer wins for same-tier methods).
    """
    return _method_authority(new_method) > _method_authority(existing_method)


def _finalize_evidence_selection(payload: dict[str, Any], evidence: list[dict[str, Any]]) -> None:
    """Mark the winning evidence row for each field as ``decision_status="selected"``.

    Runs at the END of the pipeline, after all extractors have settled the final
    payload.  For every field that has a non-null value in *payload*, this function
    finds the evidence row whose ``value`` / ``normalized`` matches that final value
    and whose ``decision_status`` is not already ``"superseded"``.  Among ties it
    prefers the row with the highest method authority, then highest confidence.
    That row gets ``decision_status = "selected"``; all other non-superseded rows
    for the same field keep their current status (``"needs_review"`` or remain
    unchanged).

    This guarantees that ``scraped_field_evidence.selected`` (which mirrors
    ``decision_status == "selected"`` in :func:`~stage_course._persist_evidence`)
    always reflects the actual column value in ``scraped_courses`` — the invariant
    the Evidence Review panel relies on to identify the authoritative source.
    """

    def _coerce(val: Any) -> Any:
        """Normalize to float for numeric comparison, else str."""
        if val in (None, "", 0):
            return None
        try:
            return float(val)
        except (TypeError, ValueError):
            return str(val)

    # Group active (non-superseded) evidence by field_key.
    by_field: dict[str, list[dict[str, Any]]] = {}
    for ev in evidence:
        fk = ev.get("field_key")
        if not fk:
            continue
        if ev.get("decision_status") == "superseded":
            continue
        by_field.setdefault(fk, []).append(ev)

    for field_key, candidates in by_field.items():
        final_val = _coerce(payload.get(field_key))
        if final_val is None:
            # Field was cleared or never set — reset any stale "selected" markers
            # left by Stage 0 or other early-write paths so the UI shows no winner.
            for ev in candidates:
                if ev.get("decision_status") == "selected":
                    ev["decision_status"] = "needs_review"
            continue

        # Pick the winner: value must match final_val; rank by authority then confidence.
        winner: dict[str, Any] | None = None
        winner_auth = -1
        winner_conf = -1.0
        for ev in candidates:
            ev_val = _coerce(ev.get("value") if ev.get("value") not in (None, "", 0)
                             else ev.get("normalized"))
            if ev_val != final_val:
                continue
            auth = _method_authority(ev.get("method", ""))
            conf = float(ev.get("confidence") or 0)
            if auth > winner_auth or (auth == winner_auth and conf > winner_conf):
                winner = ev
                winner_auth = auth
                winner_conf = conf

        if winner is not None:
            winner["decision_status"] = "selected"


# Maximum allowable delta between a per-course vision OCR reading and the
# university-wide central-page value for the same English slot.  When vision
# returns a value further away than this threshold the central-page value is
# considered more reliable (vision misread), the vision value is reverted, and
# a ``[VISION SANITY ✗]`` warning is emitted.
_VISION_SANITY_THRESHOLDS: dict[str, float] = {
    "ielts_overall": 1.0,    # e.g. 4.0 vs 6.0 → delta=2.0 > 1.0 → revert
    "pte_overall": 10.0,
    "toefl_overall": 10.0,
    "cambridge_overall": 10.0,
}


# Hard ceiling on the AI fallback Gemini call. Same bug class as the
# Playwright hang that started this hot-fix chain — if Gemini stalls
# (network, model-side queueing, retries inside the SDK), we would
# freeze a whole worker. PR-1.5 prod regression on VIT showed the 60s
# ceiling firing on multiple courses (`AI fallback exceeded 60s on
# https://vit.edu.au/mba — moving on without AI fill`) when the prompt
# had to fill many missing fields against a long page; bumping to 120s
# matches the Node-era timeout and gives a vision-capable Gemini call
# room to finish a multi-field extract on a heavy page (typical 10–25s,
# worst-case 60–90s during a model-side queueing event).
_AI_FALLBACK_TIMEOUT_SEC = 120


# PR-5 Bug 1 was a postgrad-IELTS bump heuristic against the uni-PDF
# backfill — REVERTED. The bump masked the real problem: course-page
# english data is sometimes a screenshot image (e.g. ASA Bachelor of
# Business publishes the english table as PNG only), so the per-course
# extractor fills nothing and the uni-PDF backfill — which holds a
# single bachelor-tier value — gets stamped on every course. Bumping by
# +0.5 IELTS made masters look plausible without being correct (real
# masters minimums vary 6.0–7.5 by program). The course-page-wins
# precedence is already enforced (this function and sibling_cache both
# skip if the slot is non-empty). The right fix lives elsewhere: OCR
# the image, parse a per-degree-level PDF, or surface the gap as
# "needs review" rather than synthesising a number.


def _apply_ai_duration_mapping(payload: dict[str, Any], ai_filled: dict[str, Any]) -> None:
    """Translate AI's `duration_value` / `duration_unit` keys into the
    canonical `duration` / `duration_term` keys used by the staged-course
    schema. Mutates ``ai_filled`` in place. Only fills when the rule
    extractor hasn't already populated the canonical key, so a confident
    regex hit always beats an AI guess. See B20 root-cause notes.

    Safety-net override: when the regex extracted a sub-year duration
    (months/weeks — typically from a placement/practicum sentence that
    slipped through the extractor) AND the AI independently identifies a
    year-level duration, the AI value is more likely correct.  We allow the
    override so that the sanity check (bachelor-floor: <2 years → nullify)
    doesn't drop an otherwise-good course.  The override only fires when:
      • regex term is Month or Week (not Semester/Trimester which are valid)
      • AI unit normalises to Year
      • AI value is a plausible program length (1–10 years)

    B30 (Torrens bachelor "1 year accelerated" rescue): pages such as
    Torrens Bachelor of Game Programming publish duration as
    "3 years full time, 1 year accelerated".  The regex sometimes locks
    onto the accelerated "1 year" token and writes ``duration=1.0,
    duration_term="Year"``.  The downstream bachelor-floor sanity check
    (single_course.py: ``_bachelor_floor_breach``) then nullifies the value
    because it falls below 2.0 years — leaving the course with no duration
    even when AI correctly returned the standard 3-year program length.

    Extension: when the regex value would already be nullified by the
    bachelor-floor (Year unit, value < 2.0) AND the AI independently
    returns a strictly-longer Year-level duration that the validator
    accepts (digits must appear in page text), prefer the AI value.

    The rescue is conservative — it only fires when:
      • the regex value is below the floor and would be nullified anyway, AND
      • the AI value is strictly larger
    so it can never replace a genuine short-program duration with a longer one.

    UOW BoSocSci rescue (2026-05-18): the symmetric above-sanity-max case.
    UOW's static HTML on Bachelor of Social Science variants causes the
    duration regex to extract "12.0 Year" (likely from a "12 subjects"
    or "12 sessions" panel that survives the JS-shell strip), which the
    downstream sanity check at single_course.py:_check_warnings then
    nullifies because 12 > 7.0 (the bachelor/master ceiling). Net effect:
    payload["duration"]=None on every BoSocSci major variant despite AI
    correctly returning duration_value=3, duration_unit="years" via the
    fallback. We mirror the bachelor-floor rescue: when the regex value
    normalised to years would exceed the per-degree-level suspicious
    maximum AND the AI returns a Year-level value that lands strictly
    inside the sanity window, prefer the AI value. The rescue stays
    conservative because:
      • it only fires when regex would be nullified anyway, AND
      • AI must be inside the same sanity window the regex broke, AND
      • AI must be a Year-shape value (Sessions/Trimesters etc. are out)
    so it can never replace a plausible regex hit with an AI guess.
    """
    from app.services.scraper.extractors.duration import _normalise_unit

    existing_term = _normalise_unit(str(payload.get("duration_term") or "")) or ""
    ai_unit_raw = str(ai_filled.get("duration_unit") or "")
    ai_term = _normalise_unit(ai_unit_raw) if ai_unit_raw else None
    ai_val_raw = ai_filled.get("duration_value")

    # Determine whether AI is eligible to rescue a sub-year regex result.
    _sub_year_regex = existing_term in ("Month", "Week") and "duration" in payload
    _ai_says_years = ai_term == "Year"
    try:
        _ai_plausible = ai_val_raw is not None and 1.0 <= float(ai_val_raw) <= 10.0
    except (TypeError, ValueError):
        _ai_plausible = False

    # B30: regex Year value below the bachelor-floor — rescue when the AI
    # value is strictly longer.  Equality (e.g. both say 1.0 Year) does NOT
    # trigger the rescue, so genuine 1-year diplomas are never altered.
    _existing_dur = payload.get("duration")
    _below_floor_year = (
        existing_term == "Year"
        and isinstance(_existing_dur, (int, float))
        and 0 < float(_existing_dur) < 2.0
    )
    _ai_strictly_longer = False
    if _below_floor_year and _ai_says_years and _ai_plausible:
        try:
            _ai_strictly_longer = float(ai_val_raw) > float(_existing_dur)
        except (TypeError, ValueError):
            _ai_strictly_longer = False

    # UOW BoSocSci: regex value above the per-degree-level sanity ceiling.
    # Compute the ceiling using the same rules as the suspicious-duration
    # check at single_course.py:~L4732 so the two stay in lock-step.
    _degree_l = (payload.get("degree_level") or "").lower()
    _is_bachelor_master = any(x in _degree_l for x in ("bachelor", "master", "honours"))
    _is_grad_short = any(x in _degree_l for x in (
        "graduate certificate", "graduate diploma",
        "postgraduate certificate", "postgraduate diploma",
    ))
    _SUSPICIOUS_MAX_YEARS = (
        7.0 if _is_bachelor_master
        else 4.0 if _is_grad_short
        else 12.0
    )

    def _to_years(val: Any, term: str) -> float | None:
        try:
            v = float(val)
        except (TypeError, ValueError):
            return None
        t = (term or "Year").lower()
        if "month" in t:
            return v / 12
        if "week" in t:
            return v / 52
        if "semester" in t:
            return v / 2
        if "trimester" in t:
            return v / 3
        return v  # Year, or unknown unit treated as Year

    _existing_years = _to_years(_existing_dur, existing_term) if _existing_dur is not None else None
    _ai_above_max_rescue = False
    if (
        _existing_years is not None
        and _existing_years > _SUSPICIOUS_MAX_YEARS
        and _ai_says_years
        and _ai_plausible
    ):
        try:
            _ai_years = float(ai_val_raw)
            # AI must land STRICTLY inside the sanity window the regex broke.
            _ai_above_max_rescue = 0.25 <= _ai_years <= _SUSPICIOUS_MAX_YEARS
        except (TypeError, ValueError):
            _ai_above_max_rescue = False

    _rescue = (
        (_sub_year_regex and _ai_says_years and _ai_plausible)
        or _ai_strictly_longer
        or _ai_above_max_rescue
    )

    if ("duration" not in payload or _rescue) and ai_val_raw is not None:
        try:
            ai_filled["duration"] = float(ai_val_raw)
        except (TypeError, ValueError):
            pass
    if ("duration_term" not in payload or _rescue) and ai_unit_raw:
        term = _normalise_unit(ai_unit_raw)
        if term:
            ai_filled["duration_term"] = term

    # B30 rescue: when the rescue branch fires, the existing regex-extracted
    # values in ``payload`` ("duration"/"duration_term") MUST be overwritten —
    # but the downstream merge loop in ``run`` uses ``payload.setdefault(k, v)``
    # which is a no-op for keys already present.  Without this explicit
    # overwrite, the AI value lands in ``ai_filled`` but never reaches the
    # payload, and the bachelor-floor sanity check then nullifies the regex
    # value (e.g. Torrens BGP 1.0y → null; BIDC 1.7y → null) leaving the
    # course with no duration even though AI returned the correct 3 years.
    if _rescue:
        if "duration" in ai_filled:
            payload["duration"] = ai_filled["duration"]
        if "duration_term" in ai_filled:
            payload["duration_term"] = ai_filled["duration_term"]


# Each entry: (module, kwargs the extractor accepts beyond html/url).
# degree_level + study_mode were missing before Bug C — without them the
# Review table's Level / Mode columns showed "--" for every staged course
# and auto_publish_status was permanently stuck on "pending_review".
_EXTRACTORS = (
    (course_name, ()),
    (description, ()),   # meta/p description — runs early on static HTML
    (location, ()),
    (eligibility, ()),
    (fee, ("country",)),
    (english_test, ()),
    (intake, ()),
    (duration, ()),
    (degree_level, ()),
    (study_mode, ()),
)


async def extract_course(
    url: str,
    *,
    country: str | None = None,
    html: str | None = None,
    use_ai_fallback: bool = True,
    uni_pdf_data: dict[str, Any] | None = None,
    emit=None,
    vision_image_cache: "VisionImageCache | None" = None,
    central_data: dict[str, Any] | None = None,
    extraction_rules: dict[str, Any] | None = None,
    seen_pdf_urls: set[str] | None = None,
) -> dict[str, Any]:
    """Fetch (if needed) and run all extractors. Returns merged payload + raw evidence.

    ``uni_pdf_data`` is the (optional) result of
    :func:`app.services.scraper.pipelines.university_pdfs.load_university_pdf_data`,
    used as a *last-resort* fallback for fee/IELTS fields that the per-page
    extractors and AI fallback could not fill.

    ``central_data`` is the (optional) result of
    :func:`app.services.scraper.central_pages.prefetch_central_pages`, used as
    the *absolute last-resort* fallback (lower confidence than ``uni_pdf_data``)
    for universities that publish fees/IELTS only on central pages (Bug 2).
    """
    # Week-1/2 contextvar guard: ensure set_uni_config() was called at the entry
    # point (run_scrape or run_repair).  Soft-fail: logs a WARNING and returns
    # bare defaults if the contextvar is unset.  Never raises in production.
    # Any "extractor called without uni context" log line means a code path is
    # bypassing run_scrape/run_repair — fix by adding set_uni_config() there.
    # _uc is unused in Week 1; Week-2+ extractors will read config from it.
    from app.services.scraper.config.context import require_uni_config as _ruc
    _uc = _ruc()
    # Scrape.do render is expensive (~$0.006/call).  Only activate it for
    # per-course extraction fetches when the YAML explicitly opts in.
    # Discovery / sitemap / central-page fetches never enter this scope.
    _use_scrape_do_render: bool = bool(
        getattr(getattr(_uc, "extraction", None), "scrape_do_render", False)
    )
    # Extraction-phase render wait time (ms). Increase for React/Next.js SPAs
    # that hydrate slowly (e.g. La Trobe's Adobe Target prehide needs >3 s).
    _extr_wait_ms: int = int(
        getattr(getattr(_uc, "extraction", None), "scrape_do_wait_for_ms", 3000) or 3000
    )
    _scrape_do_local_concurrency: int | None = getattr(
        getattr(_uc, "extraction", None),
        "scrape_do_local_concurrency",
        None,
    )
    _skip_render_hydration_retry: bool = bool(
        getattr(
            getattr(_uc, "extraction", None),
            "skip_render_hydration_retry",
            False,
        )
    )
    # Hash-routed SPA tab navigation (e.g. La Trobe). The primary fragment
    # makes scrape.do render the fee/duration tab; the secondary fragment
    # fetches the entry-requirements (IELTS) tab in a separate render call.
    _primary_fragment: str = (
        getattr(getattr(_uc, "extraction", None), "primary_fetch_fragment", None) or ""
    ).strip()
    _secondary_fragment: str = (
        getattr(getattr(_uc, "extraction", None), "secondary_fetch_fragment", None) or ""
    ).strip()
    # Populated by the secondary tab render fetch below; merged into _gp_html
    # before the Gemini extraction call so all tab data is visible in one pass.
    _secondary_html: str | None = None
    # La Trobe can preserve the course shell and top-level-navigate to its
    # authoritative JSON inside one Scrape.do browser request. The decoded
    # detail document is passed to the late authority override so no second
    # provider request is needed.
    _latrobe_prefetched_doc: dict | None = None
    _latrobe_prefetched_url: str | None = None
    # Geo-block bypass: SSR pages that serve country-welcome overlays for US
    # IPs (Lancaster).  Uses Scrape.do static proxy (~$0.0005/call), no JS.
    _use_scrape_do_static: bool = bool(
        getattr(getattr(_uc, "extraction", None), "scrape_do_static", False)
    )

    # ── YAML-driven per-host URL rewrites ──────────────────────────────────
    # Generic version of the hardcoded UNE/UOW/ACU/UniSQ blocks below. Reads
    # extraction.url_rewrites from the active uni config and applies every
    # matching rewrite. Each rewrite matches on (host, optional path_contains)
    # and merges append_query into the URL — idempotent: a key already present
    # in the URL is never overwritten. New unis only need a YAML edit; the
    # hardcoded blocks below stay in place for backwards compatibility.
    try:
        _yaml_rewrites = list(getattr(_uc.extraction, "url_rewrites", []) or [])
    except Exception:  # noqa: BLE001 — config access must never crash a fetch
        _yaml_rewrites = []
    if _yaml_rewrites:
        _parsed_url = urlparse(url)
        _netloc = (_parsed_url.netloc or "").lower()
        _path = _parsed_url.path or ""
        for _rw in _yaml_rewrites:
            _host = (_rw.host or "").lower()
            if not _host:
                continue
            # Match either exact host or its bare-apex/www variant.
            _bare = _host[4:] if _host.startswith("www.") else _host
            _wwwd = _host if _host.startswith("www.") else f"www.{_host}"
            if _netloc not in (_host, _bare, _wwwd):
                continue
            if _rw.path_contains and _rw.path_contains not in _path:
                continue
            _qs = parse_qs(_parsed_url.query)
            _new_qs = parse_qs(_rw.append_query or "")
            _changed = False
            for _k, _v in _new_qs.items():
                if _k not in _qs and _v:
                    _qs[_k] = _v
                    _changed = True
            if _changed:
                url = urlunparse(
                    _parsed_url._replace(
                        query=urlencode({k: v[0] for k, v in _qs.items()})
                    )
                )
                _parsed_url = urlparse(url)  # re-parse for any later rewrite

    # ── YAML-driven fee_url_suffix ────────────────────────────────────────
    # Some universities gate the international fee view behind a valueless
    # query flag (e.g. jcu.edu.au/courses/X?international) that cannot be
    # expressed as a key=value pair in url_rewrites.  fee_url_suffix is
    # appended as-is only when the URL does not already contain it.
    try:
        _fee_suffix = (getattr(_uc.extraction.fees, "fee_url_suffix", None) or "").strip()
    except Exception:  # noqa: BLE001
        _fee_suffix = ""
    if _fee_suffix and _fee_suffix not in url:
        url = url + _fee_suffix

    # ── primary_fetch_fragment: hash-routed SPA tab navigation ──────────────
    # For Next.js/React SPAs using hash routing (e.g. La Trobe), append the
    # configured hash fragment so scrape.do headless Chrome renders the specific
    # tab that shows international fee and duration data instead of the default
    # domestic view.  Only appended when the URL has no '#' yet.
    if _primary_fragment and "#" not in url:
        url = url + _primary_fragment

    # JCU note: The "Domestic / International" Fast Facts toggle at jcu.edu.au
    # is JS-driven — appending ?international=true to the static HTTP request
    # causes Cloudflare to return a bot-challenge page (no course HTML), so a
    # static URL rewrite cannot be used here.  International fees are instead
    # captured via the browser rescue pass with a click_text:"International"
    # action configured in jcu.yaml.  The follow_links config handles research-
    # degree courses that link to an "International postgraduate research fees"
    # PDF instead of showing an inline fee.

    # UNE: international student info (IELTS, PTE, fees, campus availability)
    # is only visible on the ?international=true variant of each course page.
    # Rewrite the URL before fetching so extractors always see the right tab.
    _parsed_url = urlparse(url)
    if _parsed_url.netloc in ("www.une.edu.au", "une.edu.au") and "/study/courses/" in _parsed_url.path:
        _qs = parse_qs(_parsed_url.query)
        if "international" not in _qs:
            _qs["international"] = ["true"]
            url = urlunparse(_parsed_url._replace(query=urlencode({k: v[0] for k, v in _qs.items()})))

    # UniSQ: international-student fees, IELTS, campus, and intakes are only
    # visible with ?studentType=international on each course detail page.
    _parsed_url = urlparse(url)
    if _parsed_url.netloc in ("www.unisq.edu.au", "unisq.edu.au") and "/degrees-and-courses/" in _parsed_url.path:
        _qs = parse_qs(_parsed_url.query)
        if "studentType" not in _qs:
            _qs["studentType"] = ["international"]
            url = urlunparse(_parsed_url._replace(query=urlencode({k: v[0] for k, v in _qs.items()})))

    # ACU: Australian Catholic University serves a Domestic / International tab
    # toggle on every course detail page.  The international fees, IELTS score,
    # and campus details only appear when ?type=International is appended to the
    # URL.  Without this rewrite the scraper gets CSP/domestic values (~$5–8 k)
    # instead of the real international tuition (~$25–35 k).
    # Auth-subdomain guard: ACU occasionally redirects to auth.acu.edu.au — strip
    # back to www.acu.edu.au so the fetch doesn't follow the login redirect.
    _parsed_url = urlparse(url)
    if _parsed_url.netloc in ("auth.acu.edu.au",):
        url = urlunparse(_parsed_url._replace(netloc="www.acu.edu.au"))
        _parsed_url = urlparse(url)
    if _parsed_url.netloc in ("www.acu.edu.au", "acu.edu.au"):
        _qs = parse_qs(_parsed_url.query)
        if "type" not in _qs:
            _qs["type"] = ["International"]
            url = urlunparse(_parsed_url._replace(query=urlencode({k: v[0] for k, v in _qs.items()})))

    # UOW: international-student fees, IELTS, intakes, and campus are only
    # visible with ?students=international on each course detail page.
    # Also pass the current year so UOW returns the correct session dates.
    _parsed_url = urlparse(url)
    if _parsed_url.netloc in ("www.uow.edu.au", "uow.edu.au") and "/courses/" in _parsed_url.path:
        from datetime import datetime as _dt
        _qs = parse_qs(_parsed_url.query)
        changed = False
        if "students" not in _qs:
            _qs["students"] = ["international"]
            changed = True
        if "year" not in _qs:
            _qs["year"] = [str(_dt.now().year)]
            changed = True
        if changed:
            url = urlunparse(_parsed_url._replace(query=urlencode({k: v[0] for k, v in _qs.items()})))

    # VU (Victoria University): every course detail page under /courses/<slug>/
    # ships a Domestic tab by default.  The international tab — which carries
    # the international fee, IELTS scores, intake months, and full campus list
    # — lives at /courses/<slug>/international (path suffix, NOT a query
    # parameter).  Without this rewrite the regex extractor reads the prominent
    # domestic VET fee (e.g. $1,337/year for the Cert IV in Tertiary
    # Preparation) and stamps it into international_fee.  Append "/international"
    # to the path when missing; idempotent.  Excludes /vu-sydney/ paths because
    # the VU Sydney sub-site uses a different URL shape.
    _parsed_url = urlparse(url)
    if (
        _parsed_url.netloc in ("www.vu.edu.au", "vu.edu.au")
        and _parsed_url.path.startswith("/courses/")
        and not _parsed_url.path.rstrip("/").endswith("/international")
        and not _parsed_url.path.rstrip("/").endswith("/domestic")
    ):
        _new_path = _parsed_url.path.rstrip("/") + "/international"
        url = urlunparse(_parsed_url._replace(path=_new_path))

    # UTAS: course listing pages sometimes link to the domestic-tab anchor
    # (``#tabDomestic``).  URL fragments are stripped by every HTTP client
    # before sending the request so the server always returns the full-page
    # HTML regardless of the fragment — BUT Playwright respects the fragment
    # and activates the domestic tab via JavaScript, hiding the international
    # section.  When the domestic tab is active the page body contains
    # "may not be available to international students" and the domestic-only
    # filter incorrectly rejects the course.  We strip the fragment so
    # Playwright lands on the default (combined) view and both tabs are
    # visible in the rendered DOM.
    _parsed_url = urlparse(url)
    if _parsed_url.netloc in ("www.utas.edu.au", "utas.edu.au"):
        if (_parsed_url.fragment or "").lower() in ("tabdomestic", "tab-domestic"):
            url = urlunparse(_parsed_url._replace(fragment=""))

    # ── per-call performance flags ────────────────────────────────────────────
    # Mutable dict accumulated as the call progresses; included in the returned
    # result dict so the orchestrator can aggregate savings across the run.
    _perf_flags: dict = {
        "http_skipped": False,
        "vision_skipped": False,
        "empty_text_static": False,
        "ai_skipped_empty_text": False,      # Gemini+AI skipped: static text was 0
        "browser_retry_empty_text": False,   # browser retried but ALSO returned 0 text
        "skipped_empty_text": False,         # course bailed: no text from any source
        "fallback_skipped_empty_text": False, # fee/IELTS defaults suppressed (no text)
    }
    # Set to True when text_len=0 after both static and browser attempts, causing
    # the course to be skipped without applying fee/IELTS defaults.
    _bail_empty_text: bool = False

    # Task #233: track whether the initial HTTP fetch actually ran, and the host
    # of the (finalised) URL.  Used below to (a) skip the reliably-wasted HTTP
    # attempt on confirmed browser-only hosts and (b) only count a browser
    # *rescue* when HTTP was genuinely attempted and failed.
    _http_attempted = False
    _fetch_host = (urlparse(url).netloc or "").lower()
    if html is None:
        # ── skip_initial_http_fetch gate ──────────────────────────────────────
        # For 100%-Cloudflare-protected universities (e.g. UEL), every plain
        # HTTP attempt returns a 403/challenge before the browser fallback fires.
        # Setting skip_initial_http_fetch=true in the YAML bypasses the wasted
        # round-trip and jumps straight to the browser path below.
        _uc_http = get_uni_config()
        from app.services.scraper.per_course_browser import (
            is_confirmed_browser_only as _is_confirmed_browser_only,
        )
        from app.services.skip_counters import note_skip as _note_skip
        _yaml_skip_http = (
            _uc_http is not None
            and getattr(_uc_http.extraction, "skip_initial_http_fetch", False)
        )
        # Task #233: once a host has been browser-rescued enough times this run,
        # the initial HTTP fetch is reliably wasted — skip straight to browser.
        _confirmed_browser_only = _is_confirmed_browser_only(_fetch_host)
        _skip_http = _yaml_skip_http or _confirmed_browser_only
        if _skip_http:
            _perf_flags["http_skipped"] = True
            if _confirmed_browser_only:
                _note_skip("browser_http_skipped")
            if emit:
                _skip_reason = (
                    "skip_initial_http_fetch=true" if _yaml_skip_http
                    else "confirmed browser-only host"
                )
                await emit(
                    "status",
                    f"[BROWSER-FIRST] {_skip_reason} — skipping HTTP, going straight to browser for {url[:70]}",
                    phase="extract", kind="skip_initial_http", url=url,
                )
        else:
            _http_attempted = True
            if _use_scrape_do_render:
                with scrape_do_render_scope():
                    from app.services.scraper.extractors import (
                        latrobe_json as _latrobe_fetch,
                    )
                    if _latrobe_fetch.is_latrobe_host(url):
                        (
                            html,
                            _latrobe_prefetched_doc,
                            _latrobe_prefetched_url,
                        ) = (
                            await _latrobe_fetch.fetch_course_bundle(
                                url,
                                wait_for_ms=_extr_wait_ms,
                                local_concurrency_limit=_scrape_do_local_concurrency,
                            )
                        )
                        if not html:
                            html = await fetch_html(
                                url,
                                wait_for_ms=_extr_wait_ms,
                            )
                    else:
                        html = await fetch_html(url, wait_for_ms=_extr_wait_ms)
                # Partial-hydration guard: React SPAs can return a page where
                # SSR'd body text (e.g. IELTS paragraphs) is present but the
                # H1 component hasn't finished rendering yet.  fetch_html
                # accepts any non-None response, so a partial render slips
                # through as valid HTML and produces course_name = domain.
                # If H1 is absent/empty after the initial render, retry once
                # with 2× the configured wait (capped at 12 000 ms).
                if (
                    html
                    and _use_scrape_do_render
                    and not _skip_render_hydration_retry
                ):
                    # Strip inner tags from every H1 and check for visible text.
                    # A bare r"<h1[^>]*>\s*\S" would match the opening `<` of a
                    # child <span> (e.g. <h1><span></span></h1>) and incorrectly
                    # report H1 as populated.
                    _h1_raw = _re.findall(r"<h1[^>]*>(.*?)</h1>", html, _re.S | _re.I)
                    _h1_texts = [_re.sub(r"<[^>]+>", "", m).strip() for m in _h1_raw]
                    _h1_present = any(_h1_texts)
                    if not _h1_present:
                        _retry_wait_ms = min(_extr_wait_ms * 2, 12000)
                        log.info(
                            "[RENDER-HYDRATION-RETRY] %s: H1 absent/empty after"
                            " %dms — retrying with %dms",
                            url, _extr_wait_ms, _retry_wait_ms,
                        )
                        with scrape_do_render_scope():
                            _html_retry = await fetch_html(
                                url, wait_for_ms=_retry_wait_ms
                            )
                        if _html_retry:
                            html = _html_retry
                            log.info(
                                "[RENDER-HYDRATION-RETRY] %s: retry got %d chars",
                                url, len(_html_retry),
                            )
                # Secondary tab fetch for hash-routed SPAs (e.g. La Trobe).
                # Renders the entry-requirements (IELTS) tab separately and
                # stores it in _secondary_html for merging into the Gemini
                # context below.  Only runs when the primary fetch succeeds.
                if _secondary_fragment and html:
                    _sec_url = url.split("#")[0] + _secondary_fragment
                    try:
                        with scrape_do_render_scope():
                            _secondary_html = await asyncio.wait_for(
                                fetch_html(_sec_url, wait_for_ms=_extr_wait_ms),
                                timeout=90.0,
                            )
                        if emit and _secondary_html:
                            await emit(
                                "status",
                                f"[SECONDARY-FETCH] +{len(_secondary_html)}B from {_sec_url[-70:]}",
                                phase="extract",
                                kind="secondary_tab_fetched",
                                url=_sec_url,
                            )
                    except Exception as _sec_exc:
                        log.debug(
                            "[SECONDARY-FETCH] %s failed: %s", _sec_url, _sec_exc
                        )
            elif _use_scrape_do_static:
                with scrape_do_static_scope():
                    html = await fetch_html(url)
            else:
                html = await fetch_html(url)
    # ── 404 + query-string domestic-skip ────────────────────────────────────
    # When a URL rewrite (e.g. ?audience=INTERNATIONAL) appends a query param
    # and the fetch returns nothing (includes HTTP 404), retry the bare URL
    # BEFORE falling through to browser rescue.
    #
    # CQU pattern: /courses/<code>/<slug>?audience=INTERNATIONAL returns 404
    # for domestic-only combined-degree programs (LLB+B.Acc etc.) while the
    # bare URL also returns nothing — there is simply no international page.
    # Previously these 89 courses all triggered browser rescue (browser also
    # fails → 89 wasted browser slots, each ~30 s) and then became fetch_failed,
    # pushing the failure-guard rate to 48 % and marking every run
    # completed_with_warnings at the very last step.
    #
    # With this block:
    #  • bare URL succeeds → use it (domestic-range fees blanked later via
    #    _stripped_audience_retry, same as the broken-CMS retry path)
    #  • bare URL also fails → return skipped:domestic_only_404 (counted as
    #    summary["skipped"] by the orchestrator, not fetch_failed)
    #
    # Guarded by broken_cms_retry_strip_query (same per-uni opt-in already set
    # for CQU; no new YAML knob needed).
    _http_404_bare_retry: bool = False  # propagated to _stripped_audience_retry
    if not html and "?" in url:
        _uc_404q = get_uni_config()
        if (
            _uc_404q is not None
            and getattr(
                getattr(_uc_404q.extraction, "filters", None),
                "broken_cms_retry_strip_query",
                False,
            )
        ):
            _pu_404q = urlparse(url)
            _bare_404q = urlunparse(_pu_404q._replace(query=""))
            try:
                _bare_html_404 = await fetch_html(_bare_404q)
            except Exception:
                _bare_html_404 = None
            if _bare_html_404:
                # Bare URL returned content — continue extraction with it.
                # Mark the flag so any domestic-range fees get blanked later.
                html = _bare_html_404
                _http_404_bare_retry = True
                if emit:
                    await emit(
                        "status",
                        f"[404→BARE] {url[:70]} — bare URL ok; extracting without audience param",
                        phase="extract",
                        kind="fetch_404_bare_ok",
                        url=url,
                    )
            else:
                # Bare URL also failed → no international page exists.
                # Count as domestic skip, not fetch_failed — this is intentional
                # domestic filtering, not a network/bot-protection failure.
                if emit:
                    await emit(
                        "status",
                        f"[404 SKIP] {url[:70]} — no international page; skipped as domestic-only",
                        phase="extract",
                        kind="fetch_404_domestic_skip",
                        url=url,
                    )
                return {
                    "url": url,
                    "error": "skipped:domestic_only_404",
                    "payload": {},
                    "evidence": [],
                }
    if not html:
        # HTTP fetch failed (Cloudflare, bot-protection, JS-gate, etc.).
        # Try a real Playwright browser before giving up — this handles any
        # site where plain httpx gets a 403/challenge/empty body.
        #
        # Use the per-host browser config (_browser_config_for) so Cloudflare-
        # protected hosts like UTAS get networkidle + 5s settle + 60s budget
        # rather than the previous hardcoded domcontentloaded + 2s / 35s,
        # which left UTAS at 116/120 fetch_failed in prod (job_..._utas)
        # because the Cloudflare interstitial hadn't cleared yet.
        #
        # skip_per_course_browser=true in YAML short-circuits here, before any
        # browser imports or emit calls.  This is the fix for Ulster (and any
        # CF-Enterprise-blocked site) where Playwright is also IP-blocked and
        # the 186 browser fallbacks wasted ~60 min returning rendered=0B.
        _uc_skip_pcb = get_uni_config()
        # skip_browser_rescue: true — wires the existing YAML flag into this path.
        # This is the fix for Ulster: the flag was set but only guarded the
        # *sparse-static rescue* (post-Gemini), not the initial HTTP-failure
        # browser fallback.  Now it gates BOTH paths.
        # skip_per_course_browser: true — broader YAML flag; also gates this path
        # as a belt-and-suspenders for 100%-CF-blocked universities.
        _skip_rescue = (
            _uc_skip_pcb is not None
            and getattr(_uc_skip_pcb.extraction, "skip_browser_rescue", False)
        )
        _skip_per_course = (
            _uc_skip_pcb is not None
            and getattr(_uc_skip_pcb.extraction, "skip_per_course_browser", False)
        )
        _skip_all_browser = _skip_rescue or _skip_per_course
        if _skip_all_browser:
            _skip_flag_name = (
                "skip_browser_rescue=true"
                if _skip_rescue
                else "skip_per_course_browser=true"
            )
            log.info(
                "[BROWSER↑ SKIPPED] %s — skipping browser fallback for %s",
                _skip_flag_name, url,
            )
            if emit:
                await emit(
                    "status",
                    f"[BROWSER↑ SKIPPED] {_skip_flag_name} — no browser fallback for {url[:70]}",
                    phase="extract", kind="browser_skipped_yaml", url=url,
                )
        else:
          try:
            from app.services.scraper.browser_pool import (
                BROWSER_RATE_LIMITED, pool as _bp,
            )
            from app.services.scraper.per_course_browser import (
                _browser_config_for, is_challenge_shell, note_browser_rescue, should_retry_browser,
            )
            from app.services.skip_counters import note_skip as _note_skip
            if emit:
                await emit(
                    "status",
                    f"[BROWSER↑] HTTP blocked for {url[:70]} — retrying via browser",
                    phase="extract", kind="browser_http_fallback", url=url,
                )
            wait_until, settle_ms, _outer_sec, goto_ms = _browser_config_for(url)
            html = await _bp.fetch_html(
                url, wait_until=wait_until, timeout=goto_ms, settle_ms=settle_ms,
            )
            # ── 429 rate-limit cooldown ──────────────────────────────────────
            # When Cloudflare issues a hard 429 (not a timeout / partial page),
            # short retries just burn through the rate-limit budget.  Instead
            # we detect the BROWSER_RATE_LIMITED sentinel and sleep 600 s
            # (10 min) so the Cloudflare counter resets before we try once more.
            # Belt-and-suspenders for arts-soc UTAS pages that are 429'd when
            # the extraction pass starts but become accessible ~10 min in once
            # the discovery-phase browser session cools down.
            _is_rate_limited = html is BROWSER_RATE_LIMITED
            if _is_rate_limited:
                html = None
                _rl_wait = 600
                log.warning(
                    "[BROWSER↑] 429 rate-limit for %s — returning retry_after=%ds "
                    "(orchestrator will sleep outside semaphore so other courses proceed)",
                    url, _rl_wait,
                )
                if emit:
                    await emit(
                        "status",
                        f"[BROWSER↑⏳] 429 rate-limited — releasing slot, retrying {url[:60]} after {_rl_wait}s",
                        phase="extract",
                        kind="browser_rate_limit_cooldown",
                        url=url,
                        wait_seconds=_rl_wait,
                    )
                # Return a retry sentinel instead of sleeping in-place.
                # The orchestrator's _bounded wrapper will exit the semaphore
                # (releasing the slot to other courses), sleep _rl_wait seconds
                # outside the sem, then re-run full extraction for this URL.
                # Previously this asyncio.sleep(600) held the semaphore slot,
                # freezing all N concurrent slots simultaneously on a 429 storm.
                return {
                    "_retry_after": _rl_wait,
                    "payload": {},
                    "evidence": [],
                    "error": "browser_rate_limit_retry",
                }
            # Up to 2 additional retries with exponential backoff for known
            # Cloudflare/anti-bot hosts.  The first attempt routinely fails on
            # UTAS because cf_clearance has not yet been set; the second
            # usually passes once the failed attempt's session cookie is
            # cached.  Bumped from 1 → 2 retries (2026-05-11) after a UTAS
            # run still showed 69/120 fetch_failed even with the single
            # 2.0s retry — the third attempt with a longer 4.5s settle
            # rescues most of the residual Cloudflare-throttled URLs.
            # (Skip short retries if we just did a 429-cooldown retry above.)
            if not html and not _is_rate_limited and should_retry_browser(url):
                for _attempt, _backoff in enumerate((2.0, 4.5), start=1):
                    await asyncio.sleep(_backoff)
                    if emit:
                        await emit(
                            "status",
                            f"[BROWSER↑↻{_attempt}] retry for {url[:70]}",
                            phase="extract",
                            kind="browser_http_fallback_retry",
                            url=url,
                            attempt=_attempt,
                        )
                    html = await _bp.fetch_html(
                        url, wait_until=wait_until, timeout=goto_ms, settle_ms=settle_ms,
                    )
                    if html and html is not BROWSER_RATE_LIMITED:
                        break
                    if html is BROWSER_RATE_LIMITED:
                        html = None
                        break
            # Task #233: count a genuine browser *rescue* only when the initial
            # HTTP fetch was actually attempted and failed AND the browser then
            # returned *substantive* HTML.  The BROWSER_RATE_LIMITED sentinel is
            # already nulled+returned above, but a Cloudflare/anti-bot challenge
            # interstitial ("Just a moment…") can still come back truthy-but-tiny.
            # Counting those would let 3 challenge shells wrongly mark a host
            # browser-only and skip HTTP for courses HTTP actually serves, so we
            # require a minimum body length (a fully-rendered course page is
            # never this small; an interstitial/empty shell is).
            if html and _http_attempted and len(html) >= _BROWSER_RESCUE_MIN_HTML_LEN:
                if is_challenge_shell(html):
                    _note_skip("challenge_shell")
                else:
                    note_browser_rescue(_fetch_host)
          except Exception as _exc:
            log.warning("browser fallback failed for %s: %s", url, _exc)

    if not html:
        return {"url": url, "error": "fetch_failed", "payload": {}, "evidence": []}

    # ── Federation stub-page guard (2026-05-10) ──────────────────────────
    # Federation pages such as Cert II in Furniture Making are content
    # stubs (nav + footer chrome only — no Duration JSON, no
    # StudentTypeBlock anywhere on the page). Feeding these to the rest
    # of the pipeline triggers Gemini fee/IELTS/duration hallucinations
    # because the model has no real data to ground against. Skip them
    # before any extractor / Gemini call fires.
    from app.services.scraper.extractors import federation_json as _fed_json
    if _fed_json.is_federation_host(url) and _fed_json.is_stub_page(html):
        log.info(
            "[FED STUB] %s — page lacks Duration JSON and StudentTypeBlock; "
            "skipping (would otherwise produce hallucinated AI fields)",
            url,
        )
        if emit:
            await emit(
                "status",
                f"[FED STUB] skipping content-stub page: {url[:80]}",
                phase="extract",
                kind="federation_stub_skip",
                url=url,
            )
        return {
            "url": url,
            "error": "federation_stub_page",
            "payload": {},
            "evidence": [],
        }

    # ── VU brand-chrome scrub (2026-05-14) ────────────────────────────────
    # VU pages ship two footer chunks on EVERY page that list "Sydney",
    # "Melbourne", and "Brisbane" as VU brand-chrome (Indigenous
    # acknowledgement of country anchored on "Kulin Nation", and the CRICOS
    # registration line "00124K (Melbourne), 02475D (Sydney and Brisbane)").
    # The structured "Course essentials" panel that vu_course_card.py
    # parses is hydrated by JS and ABSENT from the static HTML the pipeline
    # actually sees, so the bag-of-text location fallback was reading the
    # brand-chrome chunks and stamping "Sydney, Melbourne, Brisbane" as the
    # campus list on courses delivered exclusively at Footscray Park (e.g.
    # NMPM Master of Project Management — user-reported 2026-05-14).
    # Scrubbing both chunks before any extractor runs lets regex bag-of-text
    # AND Gemini see only real course content; for Footscray-only courses
    # this means location stays NULL (acceptable — better than wrong data),
    # and for genuine multi-city VU courses the Sydney/Melbourne/Brisbane
    # references in real course content (e.g. "Sydney Campus") are
    # untouched.  Hostname-gated; pure parse, no extra HTTP request.
    from app.services.scraper.extractors import vu_course_card as _vu_cc_pre
    if _vu_cc_pre.is_vu_host(url):
        html = _vu_cc_pre.scrub_brand_chrome_html(html)

    # ── University of Newcastle: strip the "related courses" tile block ──
    # www.newcastle.edu.au course pages render a "More degrees you may like"
    # / related-courses carousel at the bottom of every page.  Each tile
    # contains the SIBLING course's own admission summary, e.g.
    #   "Bachelor of Medical Radiation Science (Honours) (Diagnostic
    #    Radiography)  Selection Rank: 94.00 (Newcastle)
    #    Indicative Fee1: AUD 43,250  Full-time duration: 4 years
    #    Locations: Newcastle  Learn more"
    # The fee extractor's currency-amount scan walks the flattened text
    # left-to-right and the first amount it scores often comes from one of
    # those tiles instead of the page's own "Indicative fee1 AUD 48,535"
    # admission-info sidebar — every UoN course was therefore staged with
    # the SAME tile fee (A$43,250) regardless of its real international
    # tuition.  The tile block also contributes stray month names to
    # intake_months ("February" was bleeding in fleet-wide).
    #
    # The tiles are deterministic: each one contains "Selection Rank:" —
    # a phrase that does not appear anywhere in the main course content
    # on UoN /degrees/<slug> pages.  Strip from the first occurrence of
    # "Selection Rank" through to the end of the document so the fee /
    # intake / english extractors only see the course's own fields.
    try:
        from urllib.parse import urlparse as _up_n
        _n_host = (_up_n(url).hostname or "").lower()
        if _n_host == "newcastle.edu.au" or _n_host.endswith(".newcastle.edu.au"):
            _sr_idx = _re.search(
                r"selection\s+rank\s*[:\-]", html, _re.IGNORECASE,
            )
            if _sr_idx is not None:
                # Walk back to the start of the enclosing tile so we don't
                # leave a dangling "Bachelor of X (...)" course-name fragment
                # in the truncated HTML (which would otherwise feed the
                # course_name fallback).  Cap the rewind at 4 KB so we never
                # over-strip on pathological pages.
                _cut = _sr_idx.start()
                _rewind_floor = max(0, _cut - 4096)
                _tile_open = html.rfind("<", _rewind_floor, _cut)
                if _tile_open != -1:
                    _cut = _tile_open
                html = html[:_cut]
    except Exception as _ncl_exc:  # noqa: BLE001
        log.debug("newcastle related-tile scrub skipped on %s: %s", url, _ncl_exc)

    payload: dict[str, Any] = {"course_website": url}
    evidence: list[dict[str, Any]] = []
    _gemini_primary_cost: float = 0.0
    _is_csu_page: bool = False  # set True by the CSU pre-seed; gates Gemini Primary

    # Reset per-coroutine Gemini call log accumulator so this course starts fresh.
    from app.services.ai.gemini_client import get_call_log as _gcl_get, reset_call_log as _gcl_reset
    _gcl_reset()

    # ── Domestic-only early exit ──────────────────────────────────────────────
    # If the page text explicitly states the course is not available to
    # international students, flag it immediately.  The staging guard will
    # reject it with reason "domestic_only" without running any more extractors.
    # Phase 3: gated on extraction.filters.domestic_only.enabled (fail-open).
    if _domestic_only_filter_enabled() and _is_domestic_only_page(html):
        payload["domestic_only"] = True
        await emit(
            "status",
            f"[DOMESTIC ONLY] {url} — course page states domestic-students-only; skipping",
            phase="extract",
            kind="domestic_only_skip",
            url=url,
        )
        return {"url": url, "payload": payload, "evidence": evidence}

    # ── Part-time-only early exit ─────────────────────────────────────────────
    # Some universities (e.g. WLV) offer certain courses as Part-time only with
    # no Full-time option.  International students on a student visa must
    # typically enrol full-time, so these courses are not applicable and should
    # be rejected at the extraction stage rather than staged for review.
    # Gated on extraction.filters.reject_parttime_only: true in per-uni YAML;
    # fail-closed (never fires for universities that haven't opted in).
    # Reuses the existing domestic_only payload key so guards.py rejects with
    # reason "domestic_only"; parttime_only=True is set for metrics/logging.
    if _parttime_only_filter_enabled() and _is_parttime_only_page(html):
        payload["domestic_only"] = True
        payload["parttime_only"] = True
        await emit(
            "status",
            f"[PART-TIME ONLY] {url} — no full-time option found in course-length cell; skipping",
            phase="extract",
            kind="parttime_only_skip",
            url=url,
        )
        return {"url": url, "payload": payload, "evidence": evidence}

    # ── Not-currently-accepting / rested-program early exit ──────────────────
    # Newcastle (and similar): pages whose program status is "rested" or
    # whose sidebar shows "<h5>No current intake</h5> This program is not
    # currently accepting new applications." MUST be rejected before the
    # extractors run, otherwise stale fee/duration/campus data gets staged
    # for a course that no longer accepts students. Reuses the same
    # ``domestic_only`` payload key so the existing staging guard
    # (guards.py:394) rejects the row with reason "domestic_only".
    if _is_not_accepting_page(html):
        payload["domestic_only"] = True
        payload["not_accepting"] = True
        await emit(
            "status",
            f"[NOT ACCEPTING] {url} — course page states program is closed / not currently accepting applications; skipping",
            phase="extract",
            kind="not_accepting_skip",
            url=url,
        )
        return {"url": url, "payload": payload, "evidence": evidence}

    # ── Newcastle online-only early exit ─────────────────────────────────────
    # Newcastle pages with <meta name="UON.Degree.Location"
    # content="location_online"> are delivered exclusively online (single
    # Online toggle, no physical campus). The pipeline should not stage
    # them — without this guard Gemini misreads the auxiliary
    # ShortCourses.ModeOfDelivery meta and stamps study_mode="On Campus",
    # bypassing the generic online_only guard in guards.py and producing
    # a row with course_location BLANK and the wrong mode. Reuses the
    # existing domestic_only payload key so guards.py:394 rejects with
    # reason "domestic_only"; also sets online_only_uon=True for metrics.
    if _is_uon_online_only_page(html):
        payload["domestic_only"] = True
        payload["online_only_uon"] = True
        await emit(
            "status",
            f"[ONLINE ONLY] {url} — Newcastle UON.Degree.Location=location_online; skipping",
            phase="extract",
            kind="online_only_skip",
            url=url,
        )
        return {"url": url, "payload": payload, "evidence": evidence}

    # UniSQ (unisq.edu.au) pure-online courses — primary Location <ul>
    # contains only Online/External/Distance with no physical campus.
    # See _is_unisq_online_only_page docstring for full rationale.
    # Reuses domestic_only payload key so guards.py:394 rejects with
    # reason "domestic_only"; also sets online_only_unisq=True for metrics.
    if (
        "unisq.edu.au" in (url or "").lower()
        and _is_unisq_online_only_page(html)
    ):
        payload["domestic_only"] = True
        payload["online_only_unisq"] = True
        await emit(
            "status",
            f"[ONLINE ONLY] {url} — UniSQ primary Location list is all-virtual; skipping",
            phase="extract",
            kind="online_only_skip",
            url=url,
        )
        return {"url": url, "payload": payload, "evidence": evidence}

    # ── Broken-CMS-page short-circuit ─────────────────────────────────────────
    # Some universities (notably CQU's `?audience=INTERNATIONAL` rewrite on
    # certain course shapes) return a 200-OK branded error template instead
    # of real content.  The body text is a single short error sentence, e.g.
    #   "The server encountered an error and cannot process your request."
    # If we let the rest of the pipeline run on these pages, Gemini fires
    # against ~70 chars of text and either returns all-nulls (wasted budget)
    # or hallucinates a course named "There Was A Problem on Our End" that
    # then pollutes the staging table.
    #
    # Detection has two tiers:
    #   1. **Tiny page**: <400 chars of compacted body text AND known error
    #      sentence — original CQU 137-char branded error template shape.
    #   2. **Visible-body marker** (2026-05-12 fix): error sentence appears
    #      within the FIRST 500 chars of compacted text. This catches CQU's
    #      Next.js variant where the error UI renders at the top followed by
    #      a multi-MB embedded `__NEXT_DATA__` JSON dump (e.g.
    #      `cm17/bachelor-of-medical-science-pathway-to-medicine?audience=
    #      INTERNATIONAL` returned 594 KB / 468 KB compact text starting
    #      with "There was a problem on our end The server encountered an
    #      error..." — the length-only gate let it slip through and the
    #      page-regex extractor mined a $33,300 fee from the JSON dump,
    #      polluting staging).  Real course pages start with the course
    #      title ("<Course Name> - CQUniversity ..."), never with the
    #      error-template phrases — verified across the 5 working bare URLs.
    try:
        from app.services.scraper.extractors._text import (
            html_to_text as _html_to_text,
            compact as _compact,
        )
        _broken_text = _compact(_html_to_text(html or ""))
        _lower = _broken_text.lower()
        _BROKEN_MARKERS = (
            "server encountered an error and cannot process",
            "there was a problem on our end",
            "the server encountered an error",
            # VU (Victoria University) soft-404: dead /courses/<slug> URLs
            # return HTTP 200 with a branded "Page not found" template.
            # The page title is "Page not found | Victoria University" and
            # the body text starts "An error occurred Sorry, we can't find
            # the page you were looking for. It may have been moved or
            # deleted." (2026-05-13 — fleet-wide soft-404 bug: the regex
            # extractor pulled "May" out of "It MAY have been moved" into
            # intake_months and Gemini hallucinated "Sydney, Melbourne,
            # Brisbane" from VU brand chrome on the error page, producing
            # ghost rows like "Bachelor of Laws (Honours)/Bachelor of
            # Criminology" with bogus values.)
            "an error occurred sorry, we can",
            "we can't find the page you were looking for",
            "we cannot find the page you were looking for",
            # 2026-05-14 — VU's soft-404 template's body text is 3,249
            # characters (over the 400-char ``broken_short`` threshold)
            # AND the historic ``"an error occurred…"`` marker first
            # appears at offset ~700 (well past the [:500] window the
            # ``broken_visible`` check uses).  Result: every dead VU
            # /courses/<slug>/international URL silently slipped past
            # the gate, was treated as a real course, and produced a
            # ghost row with intake "May" (regex pulled out of "It MAY
            # have been moved") and location "Sydney, Melbourne,
            # Brisbane" (extracted from VU's Indigenous acknowledgement
            # of country footer + CRICOS registration line).  The page
            # title — "Page not found | Victoria University" — is the
            # very first thing in the visible body, so adding it here
            # is enough to catch the entire family.
            "page not found | victoria university",
            # ── Generic soft-404 / wrong-page markers ──────────────────
            # Bath Spa University (www.bathspa.ac.uk) returns a branded
            # 200-OK page whose h1 is literally "Course Not Found" for
            # dead or moved course slugs.  The course-name extractor then
            # returns "Course Not Found" as the course name, producing
            # junk review rows.  "course not found" appears within the
            # first few hundred chars of compacted body text so the
            # _broken_visible tier catches it reliably.
            "course not found",
            # General "page not found" soft-404 title used by many CMS
            # platforms (WordPress, Drupal, Umbraco, etc.) where the
            # <h1> or <title> is "Page Not Found" and the rest of the
            # body is brand chrome.
            "page not found",
            # Variations used by some Australian unis and UK providers.
            "sorry, the page you were looking for",
            "the page you requested could not be found",
            "page could not be found",
            "this page does not exist",
            "this page couldn't be found",
            "oops! that page can",
        )
        _broken_visible = any(m in _lower[:500] for m in _BROKEN_MARKERS)
        _broken_short = (
            len(_broken_text) < 400
            and any(m in _lower for m in _BROKEN_MARKERS)
        )
        # Tracks when a broken-CMS retry (or a 404-bare-URL retry above)
        # stripped an audience-related query string (e.g. ?audience=INTERNATIONAL)
        # so the post-extractor gate can blank domestic-range fees extracted from
        # the bare URL.  Seeded from _http_404_bare_retry so the two retry paths
        # share a single fee-blanking gate downstream.
        _stripped_audience_retry: bool = _http_404_bare_retry
        if _broken_visible or _broken_short:
            # Per-uni recovery: when ``broken_cms_retry_strip_query``
            # is enabled AND the URL carries a query string, refetch
            # the bare URL once. CQU's ?audience=INTERNATIONAL rewrite
            # serves a 200-OK 137-char branded error template on most
            # Bachelors / Masters even though the bare URL returns a
            # full ~18-20 KB international-eligible page. Without this
            # retry the broken-CMS guard silently dropped ~40+ real
            # CQU programs (Bachelor of Health Science, Bachelor of
            # Paramedicine, Bachelor of Education Secondary, every
            # coursework Master's, etc.).
            _retry_strip = False
            try:
                _retry_strip = bool(
                    _uc.extraction.filters.broken_cms_retry_strip_query
                )
            except Exception:  # noqa: BLE001
                _retry_strip = False
            _retry_html: str | None = None
            # Diagnostic: when the retry path is enabled but does NOT
            # recover the page, capture the precise reason so the next
            # scrape's logs are actionable instead of silently emitting
            # the same generic [BROKEN CMS] skip line.  Without this,
            # CQU's 137 broken-CMS skips were indistinguishable from the
            # 34 successful retries — we couldn't tell whether the bare
            # URL also returned the CMS error template, returned <400
            # chars, or whether fetch_html itself raised.
            _retry_attempted = False
            _retry_fail_reason: str | None = None
            _retry_fail_detail: str | None = None
            if _retry_strip:
                _pu = urlparse(url)
                if _pu.query:
                    _retry_attempted = True
                    _bare_url = urlunparse(_pu._replace(query=""))
                    try:
                        if _use_scrape_do_render:
                            with scrape_do_render_scope():
                                _retry_html = await fetch_html(_bare_url)
                        else:
                            _retry_html = await fetch_html(_bare_url)
                    except Exception as _exc:  # noqa: BLE001
                        log.warning(
                            "broken-cms retry-without-query failed for %s: %s",
                            _bare_url,
                            _exc,
                        )
                        _retry_html = None
                        _retry_fail_reason = "fetch_exception"
                        _retry_fail_detail = type(_exc).__name__
                    if _retry_html:
                        _retry_text = _compact(_html_to_text(_retry_html))
                        _retry_lower = _retry_text.lower()
                        # Mirror the two-tier gate above so a bare URL
                        # that returns a multi-MB Next.js error variant
                        # (error UI at top + JSON dump) is also rejected.
                        _retry_broken = (
                            any(m in _retry_lower[:500] for m in _BROKEN_MARKERS)
                            or (
                                len(_retry_text) < 400
                                and any(m in _retry_lower for m in _BROKEN_MARKERS)
                            )
                        )
                        if not _retry_broken and len(_retry_text) >= 400:
                            await emit(
                                "status",
                                f"[BROKEN CMS RETRY] {url[:80]} — "
                                f"bare URL returned {len(_retry_text)} chars; "
                                f"continuing extraction",
                                phase="extract",
                                kind="broken_cms_retry_ok",
                                url=url,
                            )
                            html = _retry_html
                            # Mark that the audience-rewrite query was stripped
                            # so the post-extractor gate can blank CSP-range fees.
                            # The international URL returned broken CMS, meaning
                            # no dedicated international page exists — any fee
                            # found on the bare URL is likely the domestic rate.
                            if "audience" in (_pu.query or "").lower():
                                _stripped_audience_retry = True
                            # Fall through to the rest of extraction.
                        else:
                            _retry_html = None
                            if _retry_broken:
                                _retry_fail_reason = "still_broken"
                                _retry_fail_detail = f"{len(_retry_text)} chars"
                            else:
                                _retry_fail_reason = "too_short"
                                _retry_fail_detail = f"{len(_retry_text)} chars"
                    elif _retry_fail_reason is None:
                        # fetch_html returned empty/None without raising.
                        _retry_fail_reason = "empty_response"
                        _retry_fail_detail = "fetch_html returned no body"
            # ── VU /international path-suffix retry (2026-05-13) ──────────
            # Victoria University's URL rewriter (above, ~L815) blindly
            # appends ``/international`` to every /courses/<slug>/ URL so
            # extractors see the international fee/IELTS/intake/campus
            # tab.  For ~50+ valid courses the /international variant
            # returns VU's branded "An error occurred / Page not found"
            # template (489 KB), even though the bare URL returns the
            # full real page (~970 KB) with all the data inline.
            # Symptom: Master of Enterprise Resource Planning, Master
            # of Research, Master of Applied Research, Graduate Diploma
            # in Business / Financial Planning / Migration Law, etc.
            # all logged "[WARN] An Error Occurred — Fee section
            # detected but fee is blank" and either dropped from
            # staging or staged with bogus location ("Sydney, Melbourne,
            # Brisbane" hallucinated by AI from VU brand chrome on the
            # error page) and intake "May" (regex pulled out of "It MAY
            # have been moved").
            # Recovery: when broken-CMS fires AND the URL is a VU
            # /courses/<slug>/international and the strip-query retry
            # didn't recover, refetch the bare URL once.  Hostname-
            # gated → no behaviour change for any other uni.
            if not _retry_html:
                _vu_pu = urlparse(url)
                _vu_path = _vu_pu.path.rstrip("/")
                if (
                    _vu_pu.netloc in ("www.vu.edu.au", "vu.edu.au")
                    and _vu_path.startswith("/courses/")
                    and _vu_path.endswith("/international")
                ):
                    _retry_attempted = True
                    _bare_path = _vu_path[: -len("/international")]
                    _bare_url = urlunparse(_vu_pu._replace(path=_bare_path))
                    try:
                        if _use_scrape_do_render:
                            with scrape_do_render_scope():
                                _retry_html = await fetch_html(_bare_url)
                        else:
                            _retry_html = await fetch_html(_bare_url)
                    except Exception as _exc:  # noqa: BLE001
                        log.warning(
                            "VU /international strip retry failed for %s: %s",
                            _bare_url,
                            _exc,
                        )
                        _retry_html = None
                        _retry_fail_reason = "vu_strip_intl_fetch_exception"
                        _retry_fail_detail = type(_exc).__name__
                    if _retry_html:
                        _retry_text = _compact(_html_to_text(_retry_html))
                        _retry_lower = _retry_text.lower()
                        _retry_broken = (
                            any(m in _retry_lower[:500] for m in _BROKEN_MARKERS)
                            or (
                                len(_retry_text) < 400
                                and any(m in _retry_lower for m in _BROKEN_MARKERS)
                            )
                        )
                        # Defensive guard (code-review): the bare URL
                        # serves the DOMESTIC tab by default — accepting
                        # it without an international-signal check would
                        # re-introduce the very domestic-fee leak the
                        # /international rewriter was added to prevent
                        # (e.g. $1,337/yr Cert IV in Tertiary Prep).
                        # Require the bare HTML to contain at least one
                        # unambiguous international marker (CRICOS code,
                        # "international student(s)", or an explicit
                        # international-fee/tuition phrase) before
                        # adopting it as the extraction source.  The
                        # real Master-of-ERP page contains all three;
                        # a true domestic-only landing page contains
                        # none.
                        _intl_signals = (
                            "cricos",
                            "international student",
                            "international fee",
                            "international tuition",
                            "for international",
                        )
                        _has_intl_signal = any(
                            s in _retry_lower for s in _intl_signals
                        )
                        if (
                            not _retry_broken
                            and len(_retry_text) >= 400
                            and _has_intl_signal
                        ):
                            await emit(
                                "status",
                                f"[VU /INTL STRIP] {url[:80]} — bare URL "
                                f"returned {len(_retry_text)} chars w/ "
                                f"intl signals; continuing extraction "
                                f"from {_bare_url[:80]}",
                                phase="extract",
                                kind="vu_intl_strip_retry_ok",
                                url=url,
                            )
                            html = _retry_html
                            url = _bare_url
                            # Keep payload provenance consistent with
                            # the URL we're actually extracting from —
                            # course_website was set to the original
                            # /international URL earlier in the pipeline
                            # and would otherwise diverge from the
                            # source_url stamped on every evidence row.
                            payload["course_website"] = _bare_url
                        else:
                            _retry_html = None
                            if _retry_broken:
                                _retry_fail_reason = "vu_strip_intl_still_broken"
                            elif not _has_intl_signal:
                                _retry_fail_reason = "vu_strip_intl_no_intl_signal"
                            else:
                                _retry_fail_reason = "vu_strip_intl_too_short"
                            _retry_fail_detail = f"{len(_retry_text)} chars"
                    elif _retry_fail_reason is None:
                        _retry_fail_reason = "vu_strip_intl_empty_response"
                        _retry_fail_detail = "fetch_html returned no body"
            if _retry_attempted and _retry_fail_reason:
                await emit(
                    "status",
                    f"[BROKEN CMS RETRY-FAIL] {url[:80]} — "
                    f"reason={_retry_fail_reason} ({_retry_fail_detail})",
                    phase="extract",
                    kind="broken_cms_retry_fail",
                    url=url,
                )
            if not _retry_html:
                payload["_rejection_reason"] = "broken_cms_page"
                await emit(
                    "status",
                    f"[BROKEN CMS] {url[:80]} — page returned CMS error template "
                    f"({len(_broken_text)} chars); skipping",
                    phase="extract",
                    kind="broken_cms_skip",
                    url=url,
                )
                return {"url": url, "payload": payload, "evidence": evidence}
    except Exception as exc:  # noqa: BLE001 — never abort extraction
        log.warning("broken-cms-page check errored on %s: %s", url, exc)

    # ── Stage 0: AI-generated extraction rules (Phase 2 autonomous pipeline) ──
    # If probe_and_configure produced CSS/XPath/regex rules for this site, apply
    # them FIRST — before any regex heuristic and before any per-course Gemini
    # call.  When rules cover ≥ 85% of review fields, Gemini is skipped entirely
    # for this course (per should_skip_gemini() below), reducing per-course cost
    # to zero.  Results written with method "ai_rule:css/xpath/regex" so the
    # Evidence panel tracks provenance correctly.
    _stage0_covered: set[str] = set()
    if extraction_rules:
        try:
            from app.services.scraper.ai_extractor_run import (
                apply_extraction_rules as _apply_rules,
                should_skip_gemini as _skip_gemini_check,
            )
            # A manual per-uni YAML override for a field's extraction strategy
            # (e.g. course_name.h1_css_selector, set by an operator debugging a
            # specific site) must always win over an auto-generated Stage-0 CSS
            # rule for that SAME field. Auto-rules are regenerated by CASCADE
            # recovery after every poor-quality run and can silently reintroduce
            # the exact bug the manual override was written to fix (University
            # of Hull London, 2026-07-09: manual h1_css_selector fix for
            # course_name kept getting clobbered by a freshly-regenerated
            # extraction_rules["course_name"]["css"]="h2" rule that matched the
            # wrong heading — Stage-0 wrote "payload[field]" first, so the
            # correct h1 extractor's later setdefault() was a no-op).
            _s0_manual_override_fields: set[str] = set()
            try:
                _s0_uc = get_uni_config()
                if _s0_uc is not None and _s0_uc.extraction is not None:
                    if getattr(_s0_uc.extraction.course_name, "h1_css_selector", None):
                        _s0_manual_override_fields.add("course_name")
            except Exception:  # noqa: BLE001
                pass
            _stage0_results = _apply_rules(html or "", extraction_rules)
            for _s0_field, (_s0_value, _s0_method) in _stage0_results.items():
                if _s0_field in _s0_manual_override_fields:
                    continue
                if _s0_value is not None and _s0_field not in payload:
                    payload[_s0_field] = _s0_value
                    _stage0_covered.add(_s0_field)
                    evidence.append({
                        "field_key": _s0_field,
                        "value": _s0_value,      # _persist_evidence reads "value" not "candidate_value"
                        "method": _s0_method,    # _persist_evidence reads "method" not "extraction_method"
                        "confidence": 0.80,
                        "snippet": f"{_s0_method}: {_s0_value}",
                        "source_url": url,
                        # Do NOT pre-mark selected — _finalize_evidence_selection decides
                    })
            if _stage0_covered:
                log.debug(
                    "[STAGE0] AI rules filled %d fields: %s",
                    len(_stage0_covered), sorted(_stage0_covered),
                )
            # Check if coverage is high enough to skip per-course Gemini
            _REVIEW_FIELDS_13 = [
                "course_name", "degree_level", "category", "study_mode",
                "course_location", "duration", "intake_months",
                "international_fee", "description", "academic_level",
                "academic_score", "english_test", "other_requirement",
            ]
            if _skip_gemini_check(_stage0_results, _REVIEW_FIELDS_13):
                use_ai_fallback = False
                log.info(
                    "[STAGE0] Rule coverage ≥ 85%% — Gemini disabled for %s", url
                )
        except Exception as _s0_exc:
            log.debug("[STAGE0] Rule application failed (non-fatal): %s", _s0_exc)

    # ── CSU pre-seed: runs BEFORE _EXTRACTORS ────────────────────────────────
    # CSU pages embed all course data as inline JS (fees, ocb_metadata,
    # session_data).  Standard regex extractors reliably mis-fire on the
    # 1.3 MB HTML:
    #   course_location → "test"        (JS string fragment)
    #   duration        → 1.0           (one-year subject in a table)
    #   intake_months   → ["February"]  (stale date in body HTML)
    #   study_mode      → "Blended"     (CSU marketing copy)
    # We pre-seed the payload with authoritative CSU values using direct
    # assignment so that ``payload.setdefault(k, v)`` in the extractor loop
    # is a no-op for every key we've already filled.
    # Three keys (course_location, intake_months, study_mode) are ALWAYS
    # written — even when None — so that the garbage regex results can never
    # win via setdefault.
    try:
        from app.services.scraper.csu_static_extract import (
            apply_csu_static_extraction as _csu_apply,
            is_csu_url as _is_csu,
        )
        if _is_csu(url):
            _csu_pre = _csu_apply(url, html)
            for _k, _v in _csu_pre.items():
                payload[_k] = _v  # direct write — extractors use setdefault
                if _v not in (None, "", 0, []):
                    # source_url + snippet are REQUIRED by enforce_source_evidence
                    # (guards.py) for the critical-field set: international_fee,
                    # ielts_overall, pte_overall, toefl_overall, study_mode,
                    # location_text, duration_text. Without proof the field is
                    # nullified at staging time. Bond and ECU pre-seeds set the
                    # same shape; CSU was historically missing it, which silently
                    # blanked every CSU fee + English-test column on the review
                    # page. Mirror the Bond/ECU snippet shape exactly.
                    evidence.append(
                        {
                            "field_key": _k,
                            "value": _v,
                            "confidence": 0.9,
                            "method": "csu_static",
                            "source_url": url,
                            "snippet": f"CSU pre-seed: {_k}={_v}",
                        }
                    )
            if emit:
                _csu_parts: list[str] = []
                if _csu_pre.get("domestic_fee"):
                    _csu_parts.append(f"dom={_csu_pre['domestic_fee']}")
                if _csu_pre.get("international_fee"):
                    _csu_parts.append(f"int={_csu_pre['international_fee']}")
                if _csu_pre.get("ielts_overall"):
                    _csu_parts.append(f"ielts={_csu_pre['ielts_overall']}")
                if _csu_pre.get("pte_overall"):
                    _csu_parts.append(f"pte={_csu_pre['pte_overall']}")
                if _csu_pre.get("duration"):
                    _csu_parts.append(
                        f"dur={_csu_pre['duration']}"
                        f"{_csu_pre.get('duration_term', '')}"
                    )
                if _csu_pre.get("intake_months"):
                    _csu_parts.append(
                        f"intakes={','.join(_csu_pre['intake_months'])}"
                    )
                if _csu_pre.get("course_location"):
                    _csu_parts.append(
                        f"loc={(_csu_pre['course_location'] or '')[:30]}"
                    )
                if _csu_parts:
                    await emit(
                        "status",
                        f"[CSU ✓] {url.split('/')[-1][:40]} — "
                        f"{', '.join(_csu_parts)}",
                        phase="extract",
                        kind="csu_static_preseed",
                        url=url,
                        filled=[
                            k for k, v in _csu_pre.items()
                            if v not in (None, "", 0, [])
                        ],
                    )
            # CSU pages embed all data in JS variables — the visible page
            # text the AI sees says "This course has no domestic offering"
            # for every course.  Gemini always returns null for all fields,
            # so every AI call is pure waste.  Skip all Gemini calls.
            use_ai_fallback = False
            _is_csu_page = True
            # The pre-seed only writes ielts_overall/pte_overall when they
            # are non-None (so that tests can assert "not in result").
            # Block the regex extractors from setting false positives on
            # CSU pages by ensuring both keys are in payload now — even as
            # None — so downstream setdefault() calls are no-ops.
            for _guard_k in ("ielts_overall", "pte_overall"):
                if _guard_k not in payload:
                    payload[_guard_k] = None
    except Exception as _csu_exc:  # noqa: BLE001
        log.warning("csu_static_extract pre-seed failed on %s: %s", url, _csu_exc)

    # ── Bond pre-seed: runs BEFORE _EXTRACTORS ───────────────────────────────
    # Bond University (bond.edu.au/program/*) renders all dynamic fields
    # (fees, English scores, intake calendar) via client-side JavaScript.
    # Playwright returns filled=[] even with a real browser because the fee/
    # English XHR round-trips complete after the settle window.  The Bond
    # pre-seed:
    #   1. Sets has_central_fee_page=True  → bypasses no_international_fee
    #      rejection; courses stage for human review instead of being dropped.
    #   2. Sets course_location="Gold Coast, Queensland" directly → prevents
    #      the footer-derived garbage location (e.g. "University Club (Building
    #      6), Bond University") from winning via setdefault.
    #   3. Sets study_mode="On Campus" (default; switches to Blended/Online
    #      when the static HTML has explicit online-delivery keywords).
    #   4. Injects Bond's tri-semester intake calendar (January/May/September)
    #      as the fallback when no real intake months are found.
    # Unlike CSU, we do NOT disable use_ai_fallback — Gemini can still help
    # with course_name, duration, description, and English scores.
    _is_bond_page: bool = False
    try:
        from app.services.scraper.bond_static_extract import (
            apply_bond_extraction as _bond_apply,
            is_bond_program_url as _is_bond,
        )
        if _is_bond(url):
            _bond_pre = _bond_apply(url, html)
            # Direct-write keys must block generic extractor mis-fires.
            # Only the keys explicitly listed here use direct write; all
            # other keys (e.g. international_fee when found in static HTML)
            # use setdefault so the standard extractors can override when
            # they actually find a value on the page.
            _BOND_DIRECT_KEYS = {"has_central_fee_page", "course_location", "study_mode"}
            for _k, _v in _bond_pre.items():
                if _k == "scrape_warnings":
                    # Merge into any existing warnings already set.
                    _existing_w = list(payload.get("scrape_warnings") or [])
                    for _w in (_v or []):
                        if _w not in _existing_w:
                            _existing_w.append(_w)
                    payload["scrape_warnings"] = _existing_w
                    continue
                if _k in _BOND_DIRECT_KEYS:
                    payload[_k] = _v
                else:
                    payload.setdefault(_k, _v)
                if _v not in (None, "", 0, []):
                    evidence.append(
                        {
                            "field_key": _k,
                            "value": _v,
                            "confidence": 0.85,
                            "method": "bond_static",
                            "source_url": url,
                            "snippet": f"Bond pre-seed: {_k}={_v}",
                        }
                    )
            _is_bond_page = True
            if emit:
                _bond_parts: list[str] = []
                if _bond_pre.get("international_fee"):
                    _bond_parts.append(f"fee={_bond_pre['international_fee']:.0f}")
                if _bond_pre.get("intake_months"):
                    _bond_parts.append(f"intakes={','.join(_bond_pre['intake_months'])}")
                if _bond_pre.get("course_location"):
                    _bond_parts.append(f"loc={_bond_pre['course_location'][:30]}")
                if _bond_pre.get("study_mode"):
                    _bond_parts.append(f"mode={_bond_pre['study_mode']}")
                _bond_warns = _bond_pre.get("scrape_warnings") or []
                if _bond_warns:
                    _bond_parts.append(f"warn={','.join(_bond_warns)}")
                await emit(
                    "status",
                    f"[BOND ✓] {url.split('/')[-1][:40]} — "
                    + (", ".join(_bond_parts) if _bond_parts else "pre-seed applied"),
                    phase="extract",
                    kind="bond_static_preseed",
                    url=url,
                    filled=[
                        k for k, v in _bond_pre.items()
                        if v not in (None, "", 0, []) and k != "scrape_warnings"
                    ],
                )
    except Exception as _bond_exc:  # noqa: BLE001
        log.warning("bond_static_extract pre-seed failed on %s: %s", url, _bond_exc)

    # ── ECU pre-seed: runs BEFORE _EXTRACTORS ─────────────────────────────────
    # ECU (Edith Cowan University) course pages at /degrees/courses/<slug>.
    # The pre-seed provides:
    #   1. has_central_fee_page=True  → bypasses no_international_fee rejection
    #   2. course_location            → ECU campus names (Joondalup / Mount Lawley /
    #                                    South West / Perth City) or "Perth, Australia"
    #                                    Uses direct assignment to block footer-derived
    #                                    garbage (Sri Lanka etc.) from winning.
    #   3. scrape_warnings            → "ecu_fee_review" when fee absent from static HTML
    # study_mode is NOT set here — ECU's defaultStudyMode="On Campus" in scrape_config
    # overrides any low-confidence "Online" value inside the orchestrator staging loop.
    _is_ecu_page: bool = False
    try:
        from app.services.scraper.ecu_static_extract import (
            apply_ecu_extraction as _ecu_apply,
            is_ecu_course_url as _is_ecu,
        )
        if _is_ecu(url):
            _ecu_pre = _ecu_apply(url, html)
            # Direct-write keys prevent generic extractor noise from winning.
            # ``intake_months`` is direct-write because the structured
            # availability grid (parse_availability_grid) is authoritative —
            # ties FT/PT presence to (campus, semester) pairs, unlike the
            # text-only intake.ecu_semester extractor which adds both Feb+Jul
            # whenever the page mentions "Semester 1" or "Semester 2" (every
            # ECU course page does, regardless of which semester is offered).
            _ECU_DIRECT_KEYS = {
                "has_central_fee_page",
                "course_location",
                "intake_months",
            }
            for _ek, _ev in _ecu_pre.items():
                if _ek == "scrape_warnings":
                    _ew = list(payload.get("scrape_warnings") or [])
                    for _w in (_ev or []):
                        if _w not in _ew:
                            _ew.append(_w)
                    payload["scrape_warnings"] = _ew
                    continue
                if _ek in _ECU_DIRECT_KEYS:
                    payload[_ek] = _ev
                else:
                    payload.setdefault(_ek, _ev)
                if _ev not in (None, "", 0, []):
                    # intake_months from the grid is structured DOM data —
                    # use 0.95 so it beats intake.ecu_semester (0.85) and any
                    # other text-based intake guess in the merge ranker.
                    _ecu_conf = 0.95 if _ek == "intake_months" else 0.85
                    evidence.append({
                        "field_key": _ek,
                        "value": _ev,
                        "confidence": _ecu_conf,
                        "method": (
                            "ecu_static:availability_grid"
                            if _ek in ("intake_months", "course_location")
                            else "ecu_static"
                        ),
                        "source_url": url,
                        "snippet": f"ECU pre-seed: {_ek}={_ev}",
                    })
            _is_ecu_page = True
            log.info(
                "[ECU ✓] %s — pre-seed applied: loc=%s fee=%s",
                url.split("/")[-1][:40],
                _ecu_pre.get("course_location"),
                _ecu_pre.get("international_fee"),
            )
    except Exception as _ecu_exc:  # noqa: BLE001
        log.warning("ecu_static_extract pre-seed failed on %s: %s", url, _ecu_exc)

    for module, extra_keys in _EXTRACTORS:
        kwargs: dict[str, Any] = {}
        for k in extra_keys:
            if k == "country":
                kwargs["country"] = country
        # degree_level accepts an optional ``course_name`` so it can read
        # the H1-level title without re-parsing <title>. Pass whatever the
        # course_name extractor already produced (it runs first in the
        # tuple, so payload['course_name'] is populated by the time we
        # reach degree_level). Falls through harmlessly when the kwarg
        # isn't supported by this extractor.
        if module is degree_level and payload.get("course_name"):
            kwargs["course_name"] = payload["course_name"]
        try:
            results: list[ExtractionResult] = await module.extract(html, url, **kwargs)
        except Exception as exc:  # one extractor must never break the others
            log.warning("Extractor %s failed on %s: %s", module.__name__, url, exc)
            continue
        for r in results:
            evidence.append(
                {
                    "field_key": r.field_key,
                    "value": r.value,
                    "confidence": r.confidence,
                    "method": r.method,
                    # source_url is required by enforce_source_evidence so that
                    # regex-extracted critical fields (ielts_overall, etc.) are not
                    # silently dropped before the DB insert.
                    "source_url": url,
                    "snippet": r.snippet,
                }
            )
            if r.normalized:
                for k, v in r.normalized.items():
                    if v is None:
                        continue
                    # Structural location extractors (method starts with "location.")
                    # override any Stage-0 ai_rule:css value for the same field.
                    # Stage-0 CSS rules can match testimonial or junk text on some
                    # sites (e.g. BCU person names picked up before the keyfacts
                    # panel extractor runs). Since Stage-0 uses setdefault too, the
                    # structural extractor — which reads from an explicit DOM panel
                    # and has confidence ≥ 0.90 — should always win.
                    # _stage0_covered tracks exactly which fields Stage-0 wrote.
                    if r.method.startswith("location.") and k in _stage0_covered:
                        payload[k] = v  # structural extractor overrides Stage-0 guess
                    else:
                        # First-write-wins so the highest-confidence result (which
                        # the extractor returned first) is preserved.
                        payload.setdefault(k, v)

    # ── Field-level extraction summary log ───────────────────────────────────
    # After all static extractors have run, emit a structured per-field summary
    # for the five critical fields so the live log shows exactly which strategy
    # succeeded and what value it produced. Only fires when an emit handler is
    # registered (i.e. the live WebSocket is open).
    if emit:
        _KEY_FIELDS = ("international_fee", "ielts_overall", "duration", "intake_months", "study_mode")
        _FIELD_LABEL = {
            "international_fee": "Fee",
            "ielts_overall": "IELTS",
            "duration": "Duration",
            "intake_months": "Intake",
            "study_mode": "StudyMode",
        }
        # Build a per-field dict: field_key → first evidence entry that filled it
        _field_summary: dict[str, dict] = {}
        for _ev in evidence:
            _fk = _ev.get("field_key", "")
            if _fk in _KEY_FIELDS and _fk not in _field_summary:
                _field_summary[_fk] = _ev

        _summary_lines: list[str] = []
        for _fk in _KEY_FIELDS:
            _label = _FIELD_LABEL[_fk]
            _ev = _field_summary.get(_fk)
            if _ev:
                _method = (_ev.get("method") or "?")[:30]
                _val = str(_ev.get("value") or "")[:30]
                _conf = _ev.get("confidence") or 0
                _summary_lines.append(f"  {_label}: ✅ {_val!r} [{_method} conf={_conf:.2f}]")
            else:
                _summary_lines.append(f"  {_label}: ❌ not found")

        if _summary_lines:
            _summary_name = (payload.get("course_name") or url.split("/")[-1] or url)[:50]
            await emit(
                "status",
                f"[FIELD SUMMARY — pre-Gemini/vision baseline] {_summary_name}\n" + "\n".join(_summary_lines),
                phase="extract",
                kind="field_extraction_summary",
                url=url,
                fields={
                    _fk: {
                        "found": _fk in _field_summary,
                        "method": (_field_summary[_fk].get("method") or "") if _fk in _field_summary else None,
                        "value": (_field_summary[_fk].get("value")) if _fk in _field_summary else None,
                    }
                    for _fk in _KEY_FIELDS
                },
            )

    # ── CQU bare-URL fee guard ────────────────────────────────────────────────
    # When broken_cms_retry_strip_query fired AND the stripped query contained
    # "audience" (e.g. ?audience=INTERNATIONAL), the HTML we just extracted was
    # fetched from the bare URL — which has no international-specific rendering.
    # Any fee in the domestic/CSP range (≤ AUD 13,000) found on that bare URL
    # is almost certainly the domestic rate, not the international fee.
    # Blank it now so the course either picks up a central-page fee or fails
    # the no-international-fee guard and is rejected rather than staged with
    # a wrong domestic value.
    # Courses where the bare URL genuinely shows an international fee (e.g.
    # ~AUD 20-30K for a bachelor's) are not affected — only CSP-range amounts.
    try:
        if _stripped_audience_retry:
            _gap_fee = payload.get("international_fee")
            if _gap_fee is not None:
                try:
                    _gap_fee_f = float(_gap_fee)
                    _gap_currency = (payload.get("fee_currency") or "AUD").upper()
                    # Convert to AUD for comparison (same logic as data_quality.py)
                    _GAP_AUD_RATES: dict[str, float] = {"GBP": 1.95, "USD": 1.55, "EUR": 1.67, "NZD": 0.92, "CAD": 1.13, "AUD": 1.0}
                    _gap_fee_aud = _gap_fee_f * _GAP_AUD_RATES.get(_gap_currency, 1.0)
                    if _gap_fee_aud <= 13_000.0:
                        log.info(
                            "[BARE_URL_FEE] Blanking domestic-range fee %.0f %s (≈ %.0f AUD) "
                            "extracted from bare URL after audience-query strip on %s — "
                            "no dedicated international page existed.",
                            _gap_fee_f, _gap_currency, _gap_fee_aud, url,
                        )
                        payload.pop("international_fee", None)
                        payload.pop("fee_currency", None)
                        payload.pop("fee_term", None)
                        payload.pop("fee_year", None)
                        evidence[:] = [
                            e for e in evidence
                            if e.get("field_key") not in (
                                "international_fee", "fee_currency", "fee_term", "fee_year"
                            )
                        ]
                        evidence.append({
                            "field_key": "international_fee",
                            "value": None,
                            "confidence": 0.0,
                            "method": "bare_url_fee_blanked",
                            "source_url": url,
                            "snippet": (
                                f"Fee {_gap_fee_f:.0f} {_gap_currency} blanked: "
                                f"international page returned broken CMS; "
                                f"bare URL fee ({_gap_fee_aud:.0f} AUD) is in domestic/CSP range."
                            ),
                        })
                except (TypeError, ValueError):
                    pass
    except Exception:  # noqa: BLE001 — never abort extraction
        pass

    # ── Bug 1 (KBS): location-based mode correction ──────────────────────────
    # The bare `\bonline\b` fallback in study_mode.py fires on marketing copy
    # like "Apply Online" / "Enquire Online" found in footers/navs of pages
    # that have NO structural mode label.  It is assigned confidence=0.5
    # (deliberately low) but still wins when there's no competing signal.
    #
    # Stronger case (ACAP): even a high-confidence "Online" classification
    # (confidence=0.7) can be wrong when the university offers the same
    # course both online AND on-campus. The location extractor already strips
    # virtual/online keywords, so a non-empty course_location = confirmed
    # physical campus exists. When the page says "Online" but the location
    # extractor found real cities/campuses, the true mode is "Blended".
    #
    # NOTE: We deliberately do NOT derive "On Campus" when study_mode is
    # absent. A missing mode means no evidence was found — not that the course
    # is on-campus. Defaulting to "On Campus" from a location value produces
    # misleading data and causes the guard to reject legitimate courses as
    # Blended (when Blended+no-location fires the online_only guard).
    _study_mode_evidence = [e for e in evidence if e["field_key"] == "study_mode"]
    _was_online = payload.get("study_mode") == "Online"
    _has_physical_location = bool((payload.get("course_location") or "").strip())
    # Low-confidence online OR rule-only online with confirmed physical campus:
    # upgrade to "On Campus" rather than letting the online_only guard reject.
    #
    # Case 1 (original): study_mode:rule fired at ≤50% confidence — bare
    #   \bonline\b keyword fallback; a physical campus is a stronger signal.
    # Case 2 (new): study_mode:rule is the ONLY evidence source (no structural
    #   evidence such as span_id_delivery, data_attribute, or gemini_primary)
    #   AND a physical campus is confirmed in course_location. The keyword rule
    #   is routinely fooled by "Study online" / "online delivery" appearing in
    #   university nav bars, footer links, or marketing copy that is flattened
    #   into the page's tag-stripped body (e.g. Flinders). A confirmed physical
    #   campus is architecturally stronger evidence of campus delivery than any
    #   keyword match regardless of that match's confidence level.
    #
    # We do NOT upgrade when any high-authority method (span_id_delivery,
    # data_attribute, gemini_primary, etc.) corroborates Online — those
    # sources are explicitly anchored to the course's own delivery section
    # and are treated as authoritative.
    _rule_only_online = (
        _was_online
        and bool(_study_mode_evidence)
        and all(
            (e.get("method") or "").startswith("study_mode:rule")
            for e in _study_mode_evidence
        )
        and _has_physical_location
    )
    _low_conf_online = _was_online and any(
        e.get("confidence", 1.0) <= 0.5 and e.get("method") == "study_mode:rule"
        for e in _study_mode_evidence
    )
    if _low_conf_online or _rule_only_online:
        from app.services.scraper.extractors.study_mode import derive_mode_from_location

        # Per-uni flag: when the university genuinely offers courses both online
        # AND on-campus (e.g. JCU: "Online: Jan, May" + "Townsville: Jan, May"
        # on the same page), set Blended instead of On Campus.
        try:
            _uc_blended = get_uni_config()
            _prefer_blended_oc = (
                _uc_blended.extraction.prefer_blended_over_on_campus
                if _uc_blended else False
            )
        except Exception:  # noqa: BLE001
            _prefer_blended_oc = False

        if _prefer_blended_oc and _has_physical_location:
            _derived_mode = "Blended"
        else:
            _derived_mode = derive_mode_from_location(payload.get("course_location"))
        if _derived_mode:
            payload["study_mode"] = _derived_mode
            _upgrade_reason = "Low-confidence" if _low_conf_online else "Rule-only"
            evidence.append(
                {
                    "field_key": "study_mode",
                    "value": _derived_mode,
                    "confidence": 0.65,
                    "method": "study_mode:location_derived",
                    "snippet": (
                        f"{_upgrade_reason} online overridden by physical campus: "
                        f"{(payload.get('course_location') or '')[:80]}"
                    ),
                }
            )

    # Belt-and-suspenders guard for Bug 1 (UniSQ "Recently viewed" sidebar):
    # If study_mode was set to "On Campus" EXCLUSIVELY by the keyword-rule
    # extractor (study_mode:rule — the last-resort fallback that scans raw
    # tag-stripped text), AND there is NO location evidence of any kind, the
    # "On Campus" match almost certainly came from sidebar noise (e.g.
    # "Recently viewed" widget listing other courses' campus names).  In that
    # case the course's own Location field either says "Online" or was not
    # captured — neither supports a confident "On Campus" assignment.
    #
    # Action: downgrade to "Online" so the online_only guard in guards.py
    # can evaluate and reject the course when appropriate.  This ONLY fires
    # when there is NO high-authority structural evidence (span_id_delivery,
    # data_attribute, strong_label, gemini_primary, etc.) — those methods
    # would have returned before the keyword fallback and would appear with
    # a different method name in the evidence list.
    #
    # Location evidence hierarchy (any is sufficient to suppress the downgrade):
    #   1. _has_physical_location  — extractor found a campus name in the HTML
    #   2. location_text           — raw text from which the location was parsed
    #   3. course_location preset  — universities with default_course_location set
    #      in their YAML (e.g. Manchester → "Manchester") fill course_location
    #      without setting location_text.  A non-online preset city IS real campus
    #      evidence and must not trigger the sidebar-contamination downgrade.
    # course_location may not yet be filled from default_course_location at
    # this point in the pipeline (that YAML fallback runs ~4 500 lines later
    # at line ~7071).  Read the YAML setting directly so that universities
    # with default_course_location configured (e.g. Manchester → "Manchester")
    # are not incorrectly downgraded to Online just because no per-page
    # location extractor fired.
    _preset_loc = (payload.get("course_location") or "").strip()
    if not _preset_loc:
        try:
            _uc_preset = get_uni_config()
            _preset_loc = (
                getattr(
                    getattr(_uc_preset, "extraction", None),
                    "default_course_location",
                    None,
                ) or ""
            ).strip()
        except Exception:  # noqa: BLE001
            _preset_loc = ""
    _has_preset_physical_location = bool(_preset_loc) and _preset_loc.lower() not in (
        "online", "distance learning", "distance", "virtual"
    )
    _only_rule_based_on_campus = (
        payload.get("study_mode") == "On Campus"
        and not _has_physical_location
        and not (payload.get("location_text") or "").strip()
        and not _has_preset_physical_location
        and bool(_study_mode_evidence)
        and all(
            (e.get("method") or "").startswith("study_mode:rule")
            for e in _study_mode_evidence
        )
    )
    if _only_rule_based_on_campus:
        # Exception: if an international fee was already extracted from the
        # page (via Gemini, regex, or the browser extended-extract pass), the
        # course IS accessible to international students and the fee panel
        # loaded successfully — the blank location is a scrape miss, not
        # evidence of online-only delivery.  Downgrading would incorrectly
        # reject legitimate on-campus courses at the online_only guard.
        _has_extracted_fee = payload.get("international_fee") is not None
        if _has_extracted_fee:
            log.info(
                "[STUDY_MODE OVERRIDE SKIPPED] course=%r — rule-only 'On Campus' "
                "but international_fee=%r already extracted; location miss is a "
                "scrape failure, not online delivery evidence. Keeping On Campus.",
                payload.get("course_name") or url,
                payload["international_fee"],
            )
        else:
            payload["study_mode"] = "Online"
            evidence.append(
                {
                    "field_key": "study_mode",
                    "value": "Online",
                    "confidence": 0.55,
                    "method": "study_mode:no_location_online_override",
                    "snippet": (
                        "study_mode:rule returned 'On Campus' but course_location "
                        "and location_text are both blank — sidebar contamination "
                        "suspected. Downgraded to Online for online_only guard."
                    ),
                }
            )
            log.info(
                "[STUDY_MODE OVERRIDE] course=%r — rule-only 'On Campus' with no "
                "location evidence; downgraded to Online for guard evaluation.",
                payload.get("course_name") or url,
            )

    # suppress_on_campus guard: for UK universities where "On Campus" is the
    # physical delivery location (already captured in course_location) rather
    # than the study mode.  When extraction.study_mode.suppress_on_campus=True
    # in the per-uni YAML, any "On Campus" study_mode — regardless of which
    # extractor set it (rule, Gemini, location_derived) — is cleared to None.
    # The Mode column then shows blank (or Full-time/Part-time if derivable)
    # instead of duplicating the Course Location column.
    if payload.get("study_mode") == "On Campus":
        try:
            _sc_uc = _uni_cfg if "_uni_cfg" in dir() else None
            if _sc_uc is None:
                from app.services.scraper.config.context import get_uni_config as _guc
                _sc_uc = _guc()
            if _sc_uc is not None:
                _sc_sm_opts = getattr(_sc_uc.extraction, "study_mode", None)
                if _sc_sm_opts is not None and getattr(_sc_sm_opts, "suppress_on_campus", False):
                    payload["study_mode"] = None
                    evidence.append({
                        "field_key": "study_mode",
                        "value": None,
                        "confidence": 1.0,
                        "method": "study_mode:suppress_on_campus",
                        "snippet": (
                            "suppress_on_campus=True: 'On Campus' cleared — "
                            "delivery location already captured in course_location."
                        ),
                    })
                    log.info(
                        "[STUDY_MODE] suppress_on_campus: cleared 'On Campus' for %r "
                        "(course_location=%r)",
                        payload.get("course_name") or url,
                        payload.get("course_location"),
                    )
        except Exception:
            pass

    # T002: per-course Bootstrap-modal English-test extractor. Runs BEFORE
    # the per-course browser pass because (a) it's pure-CPU (no Playwright
    # spin-up, no network), (b) the english_test extractor often misses
    # the values when they live ONLY inside a hidden modal, and (c) a
    # successful modal pass populates the english slots so the browser
    # fallback no-ops on its first gate. Only fires when at least one
    # english slot is still empty — paying for BeautifulSoup parse on a
    # page whose IELTS already extracted is wasted work.
    _ENGLISH_SLOTS_FOR_MODAL = (
        "ielts_overall", "pte_overall", "toefl_overall", "cambridge_overall",
    )
    if any(payload.get(k) in (None, "", 0) for k in _ENGLISH_SLOTS_FOR_MODAL):
        try:
            from app.services.scraper.per_course_modal import extract_modal_english

            modal_filled = extract_modal_english(
                html,
                course_name=payload.get("course_name") or "",
                degree_level=payload.get("degree_level") or "",
            )
            modal_summary = modal_filled.pop("__modal_summary", None)
            for k, v in modal_filled.items():
                if v in (None, "", 0):
                    continue
                if k in payload and payload.get(k) not in (None, "", 0):
                    continue
                payload[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": 0.9,
                        "method": "per_course_modal",
                        "snippet": modal_summary,
                    }
                )
            if emit and modal_filled:
                await emit(
                    "status",
                    f"[per-course modal ✓] {payload.get('course_name', url)[:40]} — "
                    f"{modal_summary or ''}",
                    phase="extract",
                    kind="per_course_modal_done",
                    url=url,
                    filled=list(modal_filled.keys()),
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("per_course_modal failed on %s: %s", url, exc)
            if emit:
                await emit(
                    "status",
                    f"[per-course modal ✗] {payload.get('course_name', url)[:40]} — "
                    f"{str(exc)[:80]}",
                    phase="extract",
                    kind="per_course_modal_error",
                    url=url,
                )

    # ── PHASE A — exhaust the course page (no university-wide sources) ──────────
    # All extractors in this phase read exclusively from the course's own page:
    # static HTML, Gemini AI on that HTML, browser-rendered DOM, and vision OCR
    # screenshots.  University-wide sources (PDF backfill, central page) are
    # Phase B — only reached when this phase leaves a required field null.
    #
    # Phase A order (each step in its own try/except):
    #   1. Regex / rule extractors   ← complete above; zero network I/O
    #   2. Gemini PRIMARY            ← AI extraction on static HTML (below)
    #   3. Browser fallback          ← JS-render + fee-toggle clicks (below)
    #   4. Domestic-only re-check    ← uses browser-rendered DOM (below)
    #   5. Vision OCR                ← screenshot / image OCR (below)
    #   6. AI fallback               ← last course-page resort (below)
    #
    # Gemini runs on static HTML before the browser so we avoid paying for a
    # Playwright launch on pages the domestic-only re-check will skip.
    # For SPA-only sites the static HTML is sparse — but those are handled by
    # university-specific pre-seeds; the browser fills any remaining gaps.
    rendered_html: str | None = None

    # ── Gemini Flash PRIMARY (Phase A, step 2) ───────────────────────────────
    # Runs on static HTML; rendered_html is not yet available (browser is
    # step 3).  PRIMARY semantics: Gemini's value always wins over an earlier
    # regex hit for the 16 hard fields.  Evidence entries for those fields are
    # replaced so extraction_method correctly credits gemini_primary.
    # Emit [GEMINI] unconditionally — even when 0 fields filled — so every
    # course has a visible log entry for diagnostics.
    #
    # CSU EXCEPTION: all data is in inline JS; the visible page text the AI
    # sees is "This course has no domestic offering" for every course.
    # Skip the Gemini call and emit a $0 line to keep the log uniform.
    try:
        if _is_csu_page:
            if emit:
                await emit(
                    "status",
                    f"[GEMINI] {url[:60]} → skipped (CSU pre-seed) (cost=$0.000000)",
                    phase="extract",
                    kind="gemini_primary_done",
                    filled=[],
                    cost_usd=0.0,
                    input_tokens=0,
                    output_tokens=0,
                    url=url,
                )
        else:
            from app.services.scraper.extractors import gemini_primary as _gp
            from app.services.scraper.gemini_gate import (
                build_classification_only_prompt as _build_class_prompt,
                should_skip_gemini_primary as _gate_check,
            )
            from app.services.scraper.extractors._text import html_to_text as _h2t_gate
            from app.services.ai import gemini_client as _gc
            import json as _gp_json

            # ── Empty-text guard with browser retry ──────────────────────────────
            # If the fetched HTML (static or scrape.do render) yields zero visible
            # text, calling Gemini/AI is wasteful and produces nothing useful.
            # skip_ai_when_text_empty=true in YAML enables this guard.
            #
            # New behaviour (2026-06-05):
            #   1. text_len=0 after static fetch → skip AI, log, attempt browser retry
            #      (if skip_per_course_browser is not set in YAML).
            #   2. Browser recovers text → clear flags, proceed normally.
            #   3. Browser also empty (or not allowed) → _bail_empty_text=True.
            #      The bail flag propagates to the fee/IELTS defaults blocks (which
            #      are skipped) and triggers an early return with
            #      error="fetch_failed_empty_text" before the final staging step.
            _uc_eat = get_uni_config()
            _skip_ai_on_empty = (
                _uc_eat is not None
                and getattr(_uc_eat.extraction, "skip_ai_when_text_empty", False)
            )
            if _skip_ai_on_empty:
                _eat_text = (_h2t_gate(html or "") or "").strip()
                if not _eat_text:
                    _perf_flags["empty_text_static"] = True
                    _perf_flags["ai_skipped_empty_text"] = True
                    use_ai_fallback = False
                    _gemini_primary_cost = 0.0
                    log.info(
                        "[AI-SKIP] text_len=0 static — skip_ai_when_text_empty=true "
                        "on %s — attempting browser retry",
                        url,
                    )
                    if emit:
                        await emit(
                            "status",
                            f"[AI-SKIP] text_len=0 static — skipping Gemini+AI, "
                            f"retrying with browser for {url[:60]}",
                            phase="extract",
                            kind="ai_skip_empty_text",
                            url=url,
                        )
                    # ── One browser retry ─────────────────────────────────────
                    _allow_empty_browser = not getattr(
                        getattr(_uc_eat, "extraction", None),
                        "skip_per_course_browser",
                        False,
                    )
                    if _allow_empty_browser:
                        try:
                            from app.services.scraper.browser_pool import (
                                pool as _bpet,
                            )
                            from app.services.scraper.per_course_browser import (
                                _browser_config_for as _bcfg_et,
                            )
                            _wait_et, _settle_et, _, _goto_et = _bcfg_et(url)
                            if emit:
                                await emit(
                                    "status",
                                    f"[BROWSER-RETRY] text_len=0 — fetching via browser for {url[:60]}",
                                    phase="extract",
                                    kind="browser_retry_empty_text",
                                    url=url,
                                )
                            _br_html = await _bpet.fetch_html(
                                url,
                                wait_until=_wait_et,
                                timeout=_goto_et,
                                settle_ms=_settle_et,
                            )
                            _br_text = (
                                (_h2t_gate(_br_html or "") or "").strip()
                                if _br_html else ""
                            )
                            if _br_text:
                                # Browser got visible text — use it and proceed.
                                html = _br_html
                                _perf_flags["empty_text_static"] = False
                                _perf_flags["ai_skipped_empty_text"] = False
                                use_ai_fallback = True
                                log.info(
                                    "[BROWSER-RETRY ✓] recovered %d chars from browser for %s",
                                    len(_br_text), url,
                                )
                                if emit:
                                    await emit(
                                        "status",
                                        f"[BROWSER-RETRY ✓] recovered {len(_br_text)} chars — "
                                        f"proceeding for {url[:60]}",
                                        phase="extract",
                                        kind="browser_retry_text_recovered",
                                        url=url,
                                    )
                                _gate_skip, _gate_reason = _gate_check(payload, evidence)
                            else:
                                # Browser also empty — bail.
                                _perf_flags["browser_retry_empty_text"] = True
                                _perf_flags["skipped_empty_text"] = True
                                _bail_empty_text = True
                                log.info(
                                    "[BROWSER-RETRY ✗] browser also empty — bailing for %s", url,
                                )
                                if emit:
                                    await emit(
                                        "status",
                                        f"[BROWSER-RETRY ✗] browser also empty — "
                                        f"skipping {url[:60]} (fetch_failed_empty_text)",
                                        phase="extract",
                                        kind="browser_retry_still_empty",
                                        url=url,
                                    )
                                _gate_skip, _gate_reason = True, "empty_text"
                        except Exception as _exc_et:
                            log.warning(
                                "browser retry on empty text raised for %s: %s", url, _exc_et,
                            )
                            _bail_empty_text = True
                            _perf_flags["skipped_empty_text"] = True
                            _gate_skip, _gate_reason = True, "empty_text"
                    else:
                        # Browser disabled for this uni — bail immediately.
                        _bail_empty_text = True
                        _perf_flags["skipped_empty_text"] = True
                        _gate_skip, _gate_reason = True, "empty_text"
                else:
                    _gate_skip, _gate_reason = _gate_check(payload, evidence)
            else:
                _gate_skip, _gate_reason = _gate_check(payload, evidence)

            # ── Early content-based staging skip (skip_staging_keywords) ──────────
            # Check BEFORE any Gemini call. CPD/short-course pages identified by
            # page text (e.g. Ulster "Short course and CPD") exit here without
            # spending primary-Gemini budget (~$0.000265/call).
            # The late check at ~line 4839 is a safety net for unusual code paths;
            # this early gate handles the normal flow.
            _uc_early_kw = get_uni_config()
            _early_skip_kws: list[str] = (
                list(_uc_early_kw.extraction.skip_staging_keywords)
                if _uc_early_kw else []
            )
            if _early_skip_kws:
                _early_text = (_h2t_gate(rendered_html or html or "") or "").lower()
                for _esk in _early_skip_kws:
                    if _esk.lower() in _early_text:
                        log.info(
                            "[SKIP-STAGING] CPD/short-course detected (%r) — aborting before Gemini on %s",
                            _esk, url,
                        )
                        if emit:
                            await emit(
                                "status",
                                f"[SKIP-STAGING] CPD/short-course detected ({_esk!r}) — not staging",
                                phase="extract", kind="staging_skipped_cpd", url=url,
                            )
                        return {
                            "url": url,
                            "error": "skipped:cpd_short_course",
                            "payload": {},
                            "evidence": [],
                        }

            _gp_filled: dict[str, Any] = {}
            _gp_dbg: dict[str, Any] = {}
            _gp_in_tok: int = 0
            _gp_out_tok: int = 0
            _gp_cost: float = 0.0
            _gp_full_ran: bool = False

            if _gate_skip:
                # All high-value fields already covered — skip Gemini primary.
                # Also skip AI fallback: if regex/defaults already have the
                # core money fields, the AI fallback can't improve on them.
                use_ai_fallback = False
                _gemini_primary_cost = 0.0
                if emit:
                    await emit(
                        "status",
                        f"[GEMINI] {url[:60]} → skipped ({_gate_reason}) (cost=$0.000000)",
                        phase="extract",
                        kind="gemini_primary_done",
                        filled=[],
                        cost_usd=0.0,
                        input_tokens=0,
                        output_tokens=0,
                        gate_reason=_gate_reason,
                        url=url,
                    )

            elif _gate_reason == "classification_only":
                # Only category/sub_category missing — use cheap 100-token prompt.
                _class_text = _h2t_gate(rendered_html or html)
                _class_prompt = _build_class_prompt(
                    payload.get("course_name") or "",
                    _class_text,
                )
                _class_resp = await _gc.generate(
                    _class_prompt,
                    max_output_tokens=120,
                    call_type="classification_only",
                    course_url=url,
                )
                _gemini_primary_cost = _class_resp.cost_usd
                _gp_in_tok = _class_resp.input_tokens
                _gp_out_tok = _class_resp.output_tokens
                if _class_resp.text and not _class_resp.skipped:
                    try:
                        _gp_filled = _gp_json.loads(_class_resp.text)
                    except Exception:
                        pass
                if emit:
                    await emit(
                        "status",
                        f"[GEMINI] {url[:60]} → classification_only "
                        f"cat={_gp_filled.get('category', '?')!r} "
                        f"(cost=${_class_resp.cost_usd:.6f})",
                        phase="extract",
                        kind="gemini_primary_done",
                        filled=list(_gp_filled.keys()),
                        cost_usd=_class_resp.cost_usd,
                        input_tokens=_class_resp.input_tokens,
                        output_tokens=_class_resp.output_tokens,
                        gate_reason=_gate_reason,
                        url=url,
                    )

            else:
                # Full extraction needed — run the complete Gemini primary prompt.
                _gp_html = rendered_html or html

                # ── Secondary tab HTML merge (hash-routed SPA, e.g. La Trobe) ─
                # When secondary_fetch_fragment is configured, the entry-
                # requirements tab was rendered separately and stored in
                # _secondary_html.  Convert it to plain text and append it
                # under a labelled section so Gemini sees IELTS + fee data
                # from both tabs in a single extraction call.
                if _secondary_html:
                    from app.services.scraper.extractors._text import (
                        html_to_text as _h2t_sec,
                    )
                    _sec_text = (_h2t_sec(_secondary_html) or "").strip()
                    if _sec_text:
                        _gp_html = (_gp_html or "") + (
                            "\n\n[Secondary tab content — entry requirements]\n"
                            + _sec_text
                        )

                # ── Admission-only text filter ────────────────────────────────
                # Strip non-admission sections (career outcomes, how-to-apply,
                # open days, student life, course structure/modules) from the
                # HTML copy sent to Gemini.  The original HTML used by regex /
                # CSS / structural extractors above is untouched.
                # Reduces Gemini input by ~30-50 % and prevents marketing copy
                # from being misidentified as fee or IELTS data.
                # Enabled by default; disable per-uni with:
                #   extraction:
                #     strip_non_admission_content: false
                _adm_filter_on = getattr(
                    getattr(_uc, "extraction", None),
                    "strip_non_admission_content",
                    True,
                )
                if _adm_filter_on and _gp_html:
                    from app.services.scraper.admission_text_filter import (
                        filter_admission_html as _adm_filter_html,
                    )
                    _gp_html = _adm_filter_html(_gp_html, url=url)

                # ── Per-course linked admission pages ─────────────────────────
                # When follow_admission_links=true in YAML, detect links to
                # per-course sub-pages (fees, entry requirements, English
                # requirements, international info, intake, scholarships) and
                # fetch them before calling Gemini.  The fetched text is
                # appended to _gp_html so Gemini can extract data that is
                # split across tabs or separate URLs.
                # Off by default; enable per-uni with:
                #   extraction:
                #     follow_admission_links: true
                #     max_admission_linked_pages: 4   # optional, default 4
                _follow_links_on = getattr(
                    getattr(_uc, "extraction", None),
                    "follow_admission_links",
                    False,
                )
                if _follow_links_on:
                    from app.services.scraper.per_course_linked_pages import (
                        fetch_linked_pages_text as _fetch_linked,
                    )
                    _max_linked = int(
                        getattr(
                            getattr(_uc, "extraction", None),
                            "max_admission_linked_pages",
                            4,
                        )
                    )
                    try:
                        _linked_text = await asyncio.wait_for(
                            _fetch_linked(
                                url,
                                html or "",
                                max_pages=_max_linked,
                                emit=emit,
                            ),
                            timeout=60.0,
                        )
                        if _linked_text:
                            _gp_html = (_gp_html or "") + "\n\n" + _linked_text
                    except Exception as _lp_exc:
                        log.debug(
                            "[LINKED-PAGES] skipped for %s: %s", url, _lp_exc
                        )

                _gp_filled, _gp_cost, _gp_in_tok, _gp_out_tok, _gp_dbg = await asyncio.wait_for(
                    _gp.extract_primary(_gp_html, url),
                    timeout=_AI_FALLBACK_TIMEOUT_SEC,
                )
                _gemini_primary_cost = _gp_cost
                _gp_full_ran = True

                # ── DEBUG: emit via the SSE/Celery log path so it appears in journalctl
                if emit and _gp_dbg:
                    await emit(
                        "status",
                        f"[GP-DEBUG] static={len(html) if html else 0}B "
                        f"rendered={len(rendered_html) if rendered_html else 0}B "
                        f"using={'rendered' if rendered_html else 'static'} "
                        f"text_len={_gp_dbg.get('text_len', '?')}",
                        phase="extract",
                        kind="gp_debug_html",
                        url=url,
                    )
                    await emit(
                        "status",
                        f"[GP-DEBUG] text[:500]={_gp_dbg.get('text_snippet', '')!r}",
                        phase="extract",
                        kind="gp_debug_text",
                        url=url,
                    )
                    await emit(
                        "status",
                        f"[GP-DEBUG] raw_response={_gp_dbg.get('raw_response', '')!r}",
                        phase="extract",
                        kind="gp_debug_raw",
                        url=url,
                    )
            # ────────────────────────────────────────────────────────────────────

            # Map duration_value/duration_unit → canonical duration/duration_term
            # unconditionally (PRIMARY means Gemini beats any earlier regex hit).
            if _gp_filled.get("duration_value") is not None:
                try:
                    _gp_filled["duration"] = float(_gp_filled["duration_value"])
                except (TypeError, ValueError):
                    pass
            if _gp_filled.get("duration_unit"):
                from app.services.scraper.extractors.duration import _normalise_unit as _nu
                _gp_term = _nu(str(_gp_filled["duration_unit"]))
                if _gp_term:
                    _gp_filled["duration_term"] = _gp_term

            # Map intake_text → canonical intake_months (JSONB list of month
            # name strings). Gemini returns a comma-separated string like
            # "January, July"; the DB stores a list like ["January", "July"].
            # Without this translation intake_text lands in the payload but
            # is silently dropped by stage_course because intake_text is not
            # a column on ScrapedCourse — causing intakes to always show as "-".
            if _gp_filled.get("intake_text"):
                from app.services.scraper.extractors.intake import (
                    _normalise_month as _nm,
                )
                _months: list[str] = []
                for _part in _re.split(r"[,;/\n]", str(_gp_filled["intake_text"])):
                    _mo = _nm(_part.strip())
                    if _mo and _mo not in _months:
                        _months.append(_mo)
                if _months:
                    _gp_filled["intake_months"] = _months

            # ── UTAS online-only flag from Gemini's `mode` field ───────────────
            # UTAS course pages publish a single "Location" panel which reads
            # exactly "Online" for online-only courses (e.g. Graduate Certificate
            # in Dementia, M5x, https://www.utas.edu.au/courses/health/courses/
            # m5x-graduate-certificate-in-dementia).  The location extractor
            # strips virtual keywords → course_location ends up blank →
            # `utas.yaml`'s `extraction.default_course_location: "Hobart"`
            # fallback fills it with "Hobart" → the existing UTAS blank-location
            # online-only guard in guards.py is masked and the row is staged
            # with the bogus "Hobart" / "On Campus" combo (user-reported
            # 2026-05-17). The YAML default itself is intentional — partial-HTML
            # browser fetches on Cloudflare-protected arts-soc / health pages
            # often omit the Location panel for legitimate Hobart courses and
            # the default keeps those from being rejected as online-only.
            #
            # Gemini's `mode` field reads the Location panel directly (the
            # _HARD_FIELDS prompt explicitly says: "If the page lists
            # 'Location: Online', always use 'Online'"). Surface that as a
            # payload flag so guards.py can reject online-only UTAS rows even
            # when the YAML default has masked the blank-location signal.
            # Pure positive signal: only fires when Gemini explicitly says
            # mode="Online"; never affects courses with any on-campus content.
            try:
                _gp_mode = str(_gp_filled.get("mode") or "").strip().lower()
                if "utas.edu.au" in (url or "").lower() and _gp_mode == "online":
                    payload["online_only_utas"] = True
            except Exception:  # noqa: BLE001 — never break the pipeline
                pass

            # Map location_text → canonical course_location (Text). Gemini
            # returns a string like "Melbourne" or "Ballarat, Gippsland"; the
            # DB column is course_location. The regex extractor only succeeds
            # when the page has a structured DOM label (strong/dt/th), which
            # many modern sites omit — making AI the primary source for
            # location on generic sites like Federation University.
            #
            # Protections (Issue 4):
            # 1. Reject values that are study-mode labels ("On Campus",
            #    "Online", "Blended") — Gemini sometimes confuses mode with
            #    location when the page presents them together.
            # 2. Protect a value already set by the structural location
            #    extractor (method starts with "location.") — it read an
            #    explicit DOM label and is more reliable than Gemini's prose
            #    read.  Generic sites that have NO structural label still get
            #    Gemini's value as before.
            _STUDY_MODE_KEYWORDS = frozenset(
                {"on campus", "online", "blended", "distance", "virtual",
                 "flexible", "on-campus", "face to face", "face-to-face"}
            )
            if _gp_filled.get("location_text"):
                _loc = str(_gp_filled["location_text"]).strip()
                # Discard chrome text before any further processing.
                # UTAS pages have "Key Information Entry requirements Course rules"
                # immediately after the Location heading; Gemini copies it verbatim.
                if _is_location_chrome(_loc):
                    _loc = ""
                # Strip semester/trimester/period labels from Gemini's location_text
                # before storing as course_location.  Gemini often copies the raw
                # "Hobart Semester 1, Semester 2 Launceston Semester 1" panel text
                # verbatim.  _strip_period_labels() normalises it to "Hobart, Launceston"
                # regardless of which extractor ultimately wins the field.
                if _loc:
                    try:
                        from app.services.scraper.extractors.location import (
                            _strip_period_labels as _spl,
                            _sanitise_for_display as _sfd,
                        )
                        _loc_clean = _spl(_loc)
                        if _loc_clean:
                            _loc = _loc_clean
                        # Strip trailing country-name parts (e.g. Gemini returns
                        # "Sydney, Melbourne, Brisbane, Australia" for Torrens —
                        # "Australia" is not a campus, must be removed).  Mirrors
                        # the same _sanitise_for_display call the structural
                        # location extractor cascade already runs.
                        _loc_sane = _sfd(_loc)
                        if _loc_sane:
                            _loc = _loc_sane
                    except Exception:
                        pass  # never block on import/runtime error
                if _loc and _loc.lower() not in _STUDY_MODE_KEYWORDS:
                    _has_structural_loc = any(
                        ev.get("field_key") == "course_location"
                        and str(ev.get("method", "")).startswith("location.")
                        for ev in evidence
                    )
                    # BCU location suppression: BCU pages contain testimonials and
                    # graduate-story sections with person names (Lauren Redfern,
                    # Ben Stones, Danielle, Alhage, etc.) that Gemini mistakes for
                    # campus names when the structured panel has no Location entry.
                    # The structural cascade already scopes BCU to ONLY the
                    # div.course__key-info__inner panel.  When that panel returns
                    # nothing (no Location row), the field must stay blank — never
                    # fall through to AI.  Suppress Gemini PRIMARY location for all
                    # bcu.ac.uk pages unconditionally.
                    _is_bcu_host_gp = "bcu.ac.uk" in (url or "").lower()
                    if not _has_structural_loc and not _is_bcu_host_gp:
                        _gp_filled["course_location"] = _loc

            # Helper: return the method of the current best evidence row for
            # a field, ignoring superseded rows.
            def _best_ev_method(fk: str) -> str | None:
                for _ev in evidence:
                    if _ev.get("field_key") == fk and _ev.get("decision_status") != "superseded":
                        return str(_ev.get("method") or "")
                return None

            for _gp_k, _gp_v in _gp_filled.items():
                if _gp_k in ("duration_value", "duration_unit"):
                    continue  # consumed by the mapped keys above
                # Remap Gemini's "mode" JSON key to the canonical payload key.
                # The Gemini prompt asks for "mode" (shorter, less verbose) but
                # the rest of the pipeline — evidence lookup, FIELD TRACE, staging
                # — all use "study_mode".  Without this remap, Gemini's extracted
                # study mode goes into payload["mode"] and payload["study_mode"]
                # stays None, causing the UI to show "-" even when Gemini clearly
                # returned "On Campus" or "Online".
                if _gp_k == "mode":
                    _gp_k = "study_mode"

                # Bug A.2 (KBS grad certs — atomic duration tuple guard):
                # `duration` and `duration_term` are an atomic pair — they must
                # come from the same extractor.  The general "course page wins"
                # guard at line ~1340 protects `duration` because the duration
                # extractor emits an ExtractionResult with field_key="duration"
                # and method="regex", which IS in _STRUCTURAL_COURSE_PAGE_EXACT.
                # However `duration_term` has NO separate evidence row
                # (field_key="duration_term" is never emitted; it only lives in
                # the `normalized` dict of the `field_key="duration"` result).
                # So _best_ev_method("duration_term") returns None, and the guard
                # below never fires for it — Gemini silently overwrites Month→Year.
                # Result: duration=8.0 (regex, protected) + duration_term=Year
                # (Gemini, unprotected) → 8.0 Year → sanity cap nullifies → drop.
                #
                # Fix: if `duration` is already owned by a structural extractor,
                # treat `duration_term` as atomic with it.  Gemini may not split
                # the pair by supplying only a unit from its own reading.
                if _gp_k == "duration_term":
                    _dur_owner = _best_ev_method("duration")
                    if _dur_owner and _is_structural_course_page_method(_dur_owner):
                        continue  # duration is structural → term is locked too

                # Issue 3: Don't let Gemini PRIMARY override a structured-pass
                # intake_months (e.g. "intake.structural", "intake.start_dates_section",
                # "rule:intake").  Those passes read clearly-labelled DOM sections
                # and are more reliable than Gemini reading prose.
                # EXCEPTION: the plain "regex" method is the intake extractor's
                # lowest-quality fallback — a keyword-window scan that often picks
                # up stray months from application timelines, related-course tiles,
                # or admission-calendar tables unrelated to actual start dates.
                # When only that fallback owns the field, allow Gemini's intake_text
                # conversion to win (it reads the authoritative Start-dates section).
                if _gp_k == "intake_months":
                    _int_method = _best_ev_method("intake_months")
                    if _int_method and _int_method != "regex":
                        continue

                # Issue 5: Don't let Gemini PRIMARY set fee_term when it
                # didn't also find a fee amount.  fee_term without a fee is
                # meaningless and will pollute the payload when the actual fee
                # later comes from the university PDF (e.g. ASAHE courses where
                # Gemini reads "Per Unit" from the page prose but returns
                # international_fee=null, while the PDF provides the total
                # course fee with its own fee_term).
                #
                # Additionally, don't let Gemini PRIMARY override a fee_term
                # that was already set by a uni_pdf extractor.  The PDF fee
                # schedule is more authoritative than Gemini's prose reading.
                if _gp_k == "fee_term":
                    # Allow Gemini PRIMARY's fee_term when:
                    #  (a) Gemini itself found a fee amount (original guard), OR
                    #  (b) the payload already has a fee from regex / structural
                    #      extractor — Gemini's "Annual" should still be able to
                    #      override a regex-produced "Full Course" that arose
                    #      because the fee context window contained "total tuition"
                    #      text even though the captured amount was the annual rate.
                    _gp_has_fee = bool(
                        _gp_filled.get("international_fee") is not None
                        or _gp_filled.get("domestic_fee") is not None
                        or payload.get("international_fee") is not None
                        or payload.get("domestic_fee") is not None
                    )
                    if not _gp_has_fee:
                        continue
                    _ft_method = _best_ev_method("fee_term")
                    if _ft_method and _ft_method.startswith("uni_pdf"):
                        continue

                # Issue 4b: Belt-and-suspenders guard for course_location.
                #
                # The Issue-4 block (~30 lines above) prevents populating
                # _gp_filled["course_location"] when location_text is a
                # study-mode keyword.  However Gemini can still set
                # course_location here via two edge cases:
                #
                #   a) location_text contains extra words that make the
                #      keyword check miss (e.g. "On Campus, Sydney" is not
                #      exactly in _STUDY_MODE_KEYWORDS but looks wrong).
                #   b) The field reaches this loop via another code path
                #      that does not go through the keyword guard above.
                #
                # Guard 1 — reject if the raw value is purely a study-mode
                #            label ("On Campus", "Online", …).
                # Guard 2 — reject if the structural location extractor
                #            (method starts with "location.") already owns
                #            the course_location field.  That extractor reads
                #            an explicit DOM label and is more reliable than
                #            Gemini's prose read.  Generic sites with no
                #            structural label still get Gemini's value.
                if _gp_k == "course_location":
                    # BCU hard block: NEVER allow Gemini PRIMARY to set
                    # course_location for bcu.ac.uk pages.  The structural
                    # cascade (_from_bcu_keyfacts) is the ONLY permitted
                    # source.  BCU pages contain testimonials with person
                    # names that Gemini mistakes for campus names when the
                    # keyfacts panel has no Location row.
                    # Note: _is_bcu_host_gp is computed at ~line 3061.
                    if _is_bcu_host_gp:
                        log.info(
                            "[BCU LOC BLOCK] Gemini PRIMARY course_location=%r "
                            "suppressed (BCU host — panel-only rule) on %s",
                            _gp_v, url,
                        )
                        continue
                    if (isinstance(_gp_v, str)
                            and _gp_v.strip().lower() in _STUDY_MODE_KEYWORDS):
                        continue  # study-mode phrase — not a real location
                    # Guard 3 (2026-05-22 — UniSQ fleet-wide footer leak):
                    # Reject chrome text when Gemini PRIMARY returns the
                    # field directly as `course_location` (not via the
                    # location_text → course_location mapping at line 2429,
                    # which already runs _is_location_chrome at line 2434).
                    # Observed: Gemini fills course_location="Accommodation
                    # UniSQ Events Contributing to our communities" verbatim
                    # on ~40% of UniSQ rows when the structural quickfacts
                    # reader returns None and the per-course page has no
                    # "Location:" label — Gemini then reads the
                    # site-footer quick-links column. The FALLBACK loop at
                    # line ~3696 already covers this for the AI-fallback
                    # path; this guard closes the PRIMARY path.
                    if isinstance(_gp_v, str) and _is_location_chrome(_gp_v):
                        continue  # site-chrome text — not a real location
                    _cl_method = _best_ev_method("course_location")
                    if _cl_method and _cl_method.startswith("location."):
                        continue  # structural extractor already owns this field

                # General "course page wins" guard ───────────────────────────
                # If the current best evidence for this field was written by a
                # structural (non-AI) course-page extractor, Gemini PRIMARY must
                # not overwrite it.  Structural extractors parse explicit DOM
                # labels (e.g. "Course Duration: 2 years Full Time"), meta tags,
                # H1 headings, or compiled regex patterns — all of which are more
                # precise than Gemini reading the same prose.
                #
                # English fields are intentionally excluded: a generic
                # degree-level rule (rule:english) is LESS reliable than Gemini
                # reading the actual page's requirements section, so Gemini is
                # allowed to override rule:english.
                #
                # The specific guards above (intake_months/regex, fee_term/uni_pdf,
                # course_location/location.*) are now redundant but kept for
                # readability / documentation of the original intent.
                if _gp_k not in _ENGLISH_SLOTS:
                    _cur_method = _best_ev_method(_gp_k)
                    if _cur_method and _is_structural_course_page_method(_cur_method):
                        continue  # course page structural extractor owns this field

                # Guard: never overwrite an existing non-null value with None.
                # Gemini returning null means it could not find the value on
                # this page; it should not erase what a prior extractor found.
                # Root cause of Issue 1 (Manchester review): Gemini returns
                # {"international_fee": null, "ielts_overall": null, ...} for
                # fields it can't find, which was silently overwriting regex /
                # structural values already in the payload.
                if _gp_v is None and payload.get(_gp_k) is not None:
                    log.debug(
                        "[GEMINI NULL SKIP] %s: %s already=%r — null from "
                        "Gemini ignored; existing value preserved",
                        url, _gp_k, payload.get(_gp_k),
                    )
                    continue

                # PRIMARY: overwrite payload value (null-overwrite guard above).
                # Keep prior evidence rows so Evidence Review can show every
                # source that found a value — mark them "superseded" so the UI
                # can distinguish them from the winning entry.
                _prior_for_qa = payload.get(_gp_k)
                payload[_gp_k] = _gp_v
                # QA Issue-6: log field overwrites so merge behaviour is auditable.
                if _prior_for_qa is not None and _gp_v is not None and _prior_for_qa != _gp_v:
                    log.info(
                        "[FIELD_OVERWRITE] %s: gemini_primary changed %s "
                        "from %r → %r",
                        url, _gp_k, _prior_for_qa, _gp_v,
                    )
                for _prior_ev in evidence:
                    if _prior_ev.get("field_key") == _gp_k:
                        _prior_ev["decision_status"] = "superseded"
                evidence.append({
                    "field_key": _gp_k,
                    "value": _gp_v,
                    "confidence": 0.75,
                    "method": "gemini_primary",
                    # enforce_source_evidence requires both source_url and snippet
                    # to keep a critical field; without them, fee/IELTS are dropped.
                    "source_url": url,
                    "snippet": f"gemini_primary: {_gp_k}={_gp_v}",
                    "decision_status": "selected",
                })

            # Emit the [GEMINI] line only for the full-extraction path —
            # the skip-gate and classification_only branches above already
            # emitted their own [GEMINI] line, so re-emitting here would be
            # a duplicate and would also reference _gp_cost / _gp_in_tok /
            # _gp_out_tok that those branches do not populate.
            if emit and _gp_full_ran:
                _gp_skip_note = (
                    f" SKIP={_gp_dbg.get('skip_reason', '?')!r}"
                    if _gp_dbg and _gp_dbg.get("skipped")
                    else ""
                )
                await emit(
                    "status",
                    f"[GEMINI] {url[:60]} → {len(_gp_filled)} field(s) "
                    f"(cost=${_gp_cost:.6f}, in={_gp_in_tok} out={_gp_out_tok}){_gp_skip_note}",
                    phase="extract",
                    kind="gemini_primary_done",
                    filled=list(_gp_filled.keys()),
                    cost_usd=_gp_cost,
                    input_tokens=_gp_in_tok,
                    output_tokens=_gp_out_tok,
                    url=url,
                )
    except asyncio.TimeoutError:
        log.warning("gemini_primary: timed out after %ss on %s — continuing without", _AI_FALLBACK_TIMEOUT_SEC, url)
    except Exception as _gp_exc:
        log.warning("gemini_primary: failed on %s — %s", url, _gp_exc)

    # ── Per-course browser fallback (Phase A, step 3) ────────────────────────
    # Renders JS-heavy SPAs and clicks "International students" fee toggles.
    # Runs after Gemini so static-HTML cost is not wasted on domestic-only
    # pages that the re-check below will short-circuit.
    try:
        from app.services.scraper.per_course_browser import (
            _force_browser_for_url,
            maybe_browser_refetch,
        )

        _force = _force_browser_for_url(url)
        # ── Sparse-static rescue ────────────────────────────────────────────
        # When the static HTML was an SPA shell, the regex/structural
        # extractors find nothing and Gemini-primary fills slots with
        # generic defaults (intake="May", location="Melbourne", IELTS=6).
        # The default browser-refetch gate at maybe_browser_refetch()
        # short-circuits whenever any english slot is populated — so the
        # bogus Gemini values prevent the JS-rendered page from ever being
        # fetched, leaving fee/duration blank fleet-wide.
        #
        # Detection signature: BOTH international_fee AND duration are
        # blank after the Gemini-primary pass. Static HTML providing
        # neither of these critical fields is conclusive evidence that
        # the page was an SPA shell. Force a Playwright refetch with
        # override=True so the rendered DOM can replace the bogus
        # Gemini fallback values with the real course data.
        #
        # Verified live (2026-05-14) on VU Bachelor of Dermal Sciences,
        # Diploma of Education Studies, etc. — both staged at 54-62%
        # completeness with intake="May", location="Melbourne", fee NULL,
        # duration NULL. Re-extracting via the browser path recovers
        # full data ($16k-20k fee, real campus, July intake).
        # Per-uni opt-out: skip rescue for Cloudflare-Enterprise-blocked sites
        # where Playwright is also IP-blocked (rendered=0B) — the rescue wastes
        # 10-30 s per course with zero benefit.  Set extraction.skip_browser_rescue:
        # true in the per-uni YAML (e.g. notredame.yaml).
        _uc_rescue = get_uni_config()
        _skip_rescue = (
            _uc_rescue is not None
            and getattr(_uc_rescue.extraction, "skip_browser_rescue", False)
        )
        if (
            not _force
            and not _skip_rescue
            and payload.get("international_fee") in (None, "", 0)
            and payload.get("duration") in (None, "", 0)
        ):
            _force = True
            log.info(
                "[SPARSE STATIC RESCUE] %s — fee+duration both blank after "
                "Gemini-primary; forcing browser refetch with override",
                url,
            )
            if emit:
                await emit(
                    "status",
                    f"[sparse-static rescue] {url} — forcing browser refetch",
                    phase="fallback",
                    kind="sparse_static_rescue",
                    url=url,
                )
        elif _skip_rescue and payload.get("international_fee") in (None, "", 0) and payload.get("duration") in (None, "", 0):
            log.debug(
                "[SPARSE STATIC RESCUE] %s — skipped (skip_browser_rescue=true in YAML)",
                url,
            )
        browser_filled, browser_evidence, rendered_html, _override = (
            await maybe_browser_refetch(url, payload, emit=emit, force=_force)
        )
        for k, v in browser_filled.items():
            if _override:
                payload[k] = v
            else:
                payload.setdefault(k, v)
        evidence.extend(browser_evidence)

        # ── YAML: fee rejection (reject_keywords) ─────────────────────────────
        # Discards international_fee when the winning evidence snippet contains
        # a configured keyword indicating it is a domestic / CSP / HECS fee.
        # Also honours prefer_international by preferring the highest-ranked
        # non-rejected fee evidence row instead of the first one written.
        try:
            _uc_fee_reject = get_uni_config()
            _fee_reject_kws: list[str] = (
                _uc_fee_reject.extraction.fees.reject_keywords if _uc_fee_reject else []
            ) or []
        except Exception:  # noqa: BLE001
            _fee_reject_kws = []
        if _fee_reject_kws and payload.get("international_fee") is not None:
            _fee_ev_snips: list[str] = [
                str(e.get("snippet") or "").lower()
                for e in evidence
                if e.get("field_key") == "international_fee"
            ]
            _kw_hit: str | None = next(
                (kw for kw in _fee_reject_kws if any(kw.lower() in s for s in _fee_ev_snips)),
                None,
            )
            # ── international_fee_keywords guard ─────────────────────────────
            # If the evidence also contains an explicit international-student
            # marker, the international marker wins and the fee is KEPT even
            # when a reject_keyword was also matched.  Handles pages that
            # publish both a domestic fee and an international fee on the same
            # page (e.g. "UK fee: £9,250 / International fee: £15,000").
            if _kw_hit:
                try:
                    _intl_fee_kws: list[str] = (
                        _uc_fee_reject.extraction.fees.international_fee_keywords
                        if _uc_fee_reject else []
                    ) or []
                except Exception:
                    _intl_fee_kws = []
                if _intl_fee_kws:
                    _intl_kw_hit: str | None = next(
                        (
                            kw for kw in _intl_fee_kws
                            if any(kw.lower() in s for s in _fee_ev_snips)
                        ),
                        None,
                    )
                    if _intl_kw_hit:
                        log.info(
                            "[FEE_KEEP] course=%r — international_fee=%r kept;"
                            " international_marker %r overrides reject_keyword %r",
                            payload.get("course_name") or url,
                            payload["international_fee"],
                            _intl_kw_hit,
                            _kw_hit,
                        )
                        if emit:
                            await emit(
                                "status",
                                f"[FEE_KEEP] {str(payload.get('course_name', url))[:40]}"
                                f" — international fee kept (marker: {_intl_kw_hit!r}"
                                f" overrides reject: {_kw_hit!r})",
                                phase="extract",
                                kind="fee_kept",
                                url=url,
                                keyword=_intl_kw_hit,
                            )
                        _kw_hit = None  # clear reject trigger
            if _kw_hit:
                log.info(
                    "[FEE_REJECT] course=%r — international_fee=%r discarded; "
                    "reject_keyword %r matched evidence snippet.",
                    payload.get("course_name") or url,
                    payload["international_fee"],
                    _kw_hit,
                )
                payload["international_fee"] = None
                evidence.append({
                    "field_key": "international_fee",
                    "value": None,
                    "confidence": 0.0,
                    "method": "yaml_reject_keyword",
                    "snippet": (
                        f"Fee discarded: evidence snippet matched reject_keyword "
                        f"'{_kw_hit}' from extraction.fees.reject_keywords"
                    ),
                })
                if emit:
                    await emit(
                        "status",
                        f"[FEE_REJECT] {str(payload.get('course_name', url))[:40]} — "
                        f"domestic fee discarded (keyword: {_kw_hit!r})",
                        phase="extract",
                        kind="fee_rejected",
                        url=url,
                        keyword=_kw_hit,
                    )

        # ── YAML: Fee link-following (fees.follow_links) ──────────────────────
        # When international_fee is still blank after all extractors and after
        # fee rejection, scan the course HTML for <a> elements whose text
        # matches any listed phrase (case-insensitive), fetch the linked page,
        # and re-run the fee extractor.  Mirrors english.follow_links.  Any fee
        # found on the linked page is also filtered through reject_keywords so
        # domestic/CSP amounts on that page are discarded automatically.
        if payload.get("international_fee") is None:
            try:
                _uc_fee_fl = get_uni_config()
                _fee_follow_patterns: list[str] = (
                    _uc_fee_fl.extraction.fees.follow_links if _uc_fee_fl else []
                ) or []
            except Exception:  # noqa: BLE001
                _fee_follow_patterns = []
            if _fee_follow_patterns and html:
                try:
                    from bs4 import BeautifulSoup as _BS4_fee
                    from urllib.parse import urljoin as _urljoin_fee
                    import httpx as _httpx_fee
                    from app.services.scraper.extractors import fee as _fee_extractor
                    _soup_fee_fl = _BS4_fee(html, "html.parser")
                    _fee_followed_urls: list[str] = []
                    for _a_fee in _soup_fee_fl.find_all("a", href=True)[:300]:
                        _lt_fee = (_a_fee.get_text() or "").strip()
                        if any(p.lower() in _lt_fee.lower() for p in _fee_follow_patterns):
                            _href_fee = _urljoin_fee(url, str(_a_fee["href"]))
                            if _href_fee != url and _href_fee not in _fee_followed_urls:
                                _fee_followed_urls.append(_href_fee)
                            if len(_fee_followed_urls) >= 3:
                                break
                    for _fee_fl_url in _fee_followed_urls:
                        if payload.get("international_fee") is not None:
                            break
                        # PDF dedup: skip any URL already seen this run — covers
                        # both .pdf-extension URLs and non-.pdf redirects / query-
                        # string download links (e.g. /download?file=fees) that
                        # were registered after their first fetch revealed a PDF
                        # content-type.
                        if seen_pdf_urls is not None and _fee_fl_url in seen_pdf_urls:
                            log.debug(
                                "[FEE_FOLLOW] PDF %r already fetched this run"
                                " — skipping (seen_pdf_urls guard)",
                                _fee_fl_url,
                            )
                            continue
                        # Pre-register .pdf-extension URLs before the fetch so a
                        # concurrent sibling course skips rather than double-fetches.
                        if seen_pdf_urls is not None and _fee_fl_url.lower().endswith(".pdf"):
                            seen_pdf_urls.add(_fee_fl_url)
                        _fee_fl_html = None
                        try:
                            async with _httpx_fee.AsyncClient(
                                follow_redirects=True, timeout=15
                            ) as _cl_fee:
                                _fee_fl_resp = await _cl_fee.get(
                                    _fee_fl_url,
                                    headers={"User-Agent": "Mozilla/5.0"},
                                )
                            if _fee_fl_resp.status_code == 200:
                                _fee_fl_html = _fee_fl_resp.text
                                # Register non-.pdf URLs that served a PDF by
                                # content-type so sibling courses skip the re-fetch.
                                if (
                                    seen_pdf_urls is not None
                                    and "application/pdf" in _fee_fl_resp.headers.get(
                                        "content-type", ""
                                    ).lower()
                                ):
                                    seen_pdf_urls.add(_fee_fl_url)
                        except Exception:  # noqa: BLE001
                            pass
                        # Browser fallback — Cloudflare/WAF (e.g. JCU) blocks
                        # all datacenter httpx requests with 403.  Retry via
                        # the Playwright browser pool so the follow-link page
                        # can actually be read.
                        if not _fee_fl_html:
                            try:
                                from app.services.scraper.browser_pool import (
                                    pool as _bp_fee_fl,
                                )
                                from app.services.scraper.per_course_browser import (
                                    _browser_config_for as _bcf_fee,
                                )
                                _bwu_f, _bsm_f, _, _bto_f = _bcf_fee(_fee_fl_url)
                                _fee_fl_html = await _bp_fee_fl.fetch_html(
                                    _fee_fl_url,
                                    wait_until=_bwu_f,
                                    timeout=_bto_f,
                                    settle_ms=_bsm_f,
                                ) or None
                            except Exception:  # noqa: BLE001
                                pass
                        if not _fee_fl_html:
                            continue
                        _fee_fl_results = await _fee_extractor.extract(
                            _fee_fl_html, _fee_fl_url
                        )
                        for _fee_fl_res in (_fee_fl_results or []):
                            _fee_fl_val = _fee_fl_res.value
                            if _fee_fl_val in (None, "", 0):
                                continue
                            # Apply reject_keywords to linked-page fee as well
                            _fee_fl_snip = str(_fee_fl_res.snippet or "").lower()
                            if _fee_reject_kws and any(
                                kw.lower() in _fee_fl_snip for kw in _fee_reject_kws
                            ):
                                continue
                            payload["international_fee"] = _fee_fl_val
                            evidence.append({
                                "field_key": "international_fee",
                                "value": _fee_fl_val,
                                "confidence": _fee_fl_res.confidence or 0.70,
                                "method": "yaml_fee_follow_link",
                                "snippet": f"Fee link followed: {_fee_fl_url}",
                                "source_url": _fee_fl_url,
                            })
                            if emit:
                                await emit(
                                    "status",
                                    f"[FEE_FOLLOW] {str(payload.get('course_name', url))[:35]} "
                                    f"→ {_fee_fl_url[:50]}: fee={_fee_fl_val}",
                                    phase="extract",
                                    kind="fee_follow_link",
                                    url=_fee_fl_url,
                                    fee=_fee_fl_val,
                                )
                            break
                except Exception as _fee_fl_exc:  # noqa: BLE001
                    log.warning("fees follow_links error on %s: %s", url, _fee_fl_exc)

        # ── YAML: English link-following (follow_links) ───────────────────────
        # When IELTS / PTE / TOEFL are still missing after all course-page
        # extraction AND extraction.english.follow_links is configured, find
        # <a> elements in the course HTML whose text matches any listed phrase,
        # fetch the linked page, and re-run the English extractor.
        if any(payload.get(k) in (None, "", 0) for k in (
            "ielts_overall", "pte_overall", "toefl_overall",
        )):
            try:
                _uc_en_follow = get_uni_config()
                _follow_patterns: list[str] = (
                    _uc_en_follow.extraction.english.follow_links if _uc_en_follow else []
                ) or []
            except Exception:  # noqa: BLE001
                _follow_patterns = []
            if _follow_patterns and html:
                try:
                    from bs4 import BeautifulSoup as _BS4
                    from urllib.parse import urljoin as _urljoin2
                    import httpx as _httpx_en
                    from app.services.scraper.extractors import english_test as _et2
                    _soup_en = _BS4(html, "html.parser")
                    _followed_urls: list[str] = []
                    for _a_tag in _soup_en.find_all("a", href=True)[:300]:
                        _lt = (_a_tag.get_text() or "").strip()
                        if any(p.lower() in _lt.lower() for p in _follow_patterns):
                            _href = _urljoin2(url, str(_a_tag["href"]))
                            if _href != url and _href not in _followed_urls:
                                _followed_urls.append(_href)
                            if len(_followed_urls) >= 3:
                                break
                    for _fl_url in _followed_urls:
                        # PDF dedup: skip any URL already seen this run — covers
                        # both .pdf-extension URLs and non-.pdf redirects / query-
                        # string download links that were registered after their
                        # first fetch revealed a PDF content-type.
                        if seen_pdf_urls is not None and _fl_url in seen_pdf_urls:
                            log.debug(
                                "[FOLLOW_LINK] PDF %r already fetched this run"
                                " — skipping (seen_pdf_urls guard)",
                                _fl_url,
                            )
                            continue
                        # Pre-register .pdf-extension URLs before the fetch so a
                        # concurrent sibling course skips rather than double-fetches.
                        if seen_pdf_urls is not None and _fl_url.lower().endswith(".pdf"):
                            seen_pdf_urls.add(_fl_url)
                        _fl_html = None
                        try:
                            async with _httpx_en.AsyncClient(
                                follow_redirects=True, timeout=15
                            ) as _cl:
                                _fl_resp = await _cl.get(
                                    _fl_url,
                                    headers={"User-Agent": "Mozilla/5.0"},
                                )
                            if _fl_resp.status_code == 200:
                                _fl_html = _fl_resp.text
                                # Register non-.pdf URLs that served a PDF by
                                # content-type so sibling courses skip the re-fetch.
                                if (
                                    seen_pdf_urls is not None
                                    and "application/pdf" in _fl_resp.headers.get(
                                        "content-type", ""
                                    ).lower()
                                ):
                                    seen_pdf_urls.add(_fl_url)
                        except Exception:  # noqa: BLE001
                            pass
                        # Browser fallback — same reason as fee follow_links:
                        # Cloudflare-protected hosts (e.g. JCU) block httpx
                        # with 403; the IELTS/English requirements page is only
                        # reachable via a real browser.
                        if not _fl_html:
                            try:
                                from app.services.scraper.browser_pool import (
                                    pool as _bp_en_fl,
                                )
                                from app.services.scraper.per_course_browser import (
                                    _browser_config_for as _bcf_en,
                                )
                                _bwu_e, _bsm_e, _, _bto_e = _bcf_en(_fl_url)
                                _fl_html = await _bp_en_fl.fetch_html(
                                    _fl_url,
                                    wait_until=_bwu_e,
                                    timeout=_bto_e,
                                    settle_ms=_bsm_e,
                                ) or None
                            except Exception:  # noqa: BLE001
                                pass
                        if not _fl_html:
                            continue
                        from app.services.scraper.extractors._text import html_to_text as _h2t_fl  # noqa: PLC0415
                        _fl_text = _h2t_fl(_fl_html)
                        _fl_result = _et2.extract(_fl_text, url=_fl_url)
                        _fl_fields = getattr(_fl_result, "fields", {}) or {}
                        _fl_filled: list[str] = []
                        for _fk, _fv in _fl_fields.items():
                            if _fv in (None, "", 0):
                                continue
                            if _fk not in ("ielts_overall", "pte_overall", "toefl_overall",
                                           "cambridge_overall", "duolingo_overall"):
                                continue
                            if payload.get(_fk) not in (None, "", 0):
                                continue
                            payload[_fk] = _fv
                            evidence.append({
                                "field_key": _fk,
                                "value": _fv,
                                "confidence": 0.78,
                                "method": "yaml_follow_link",
                                "snippet": f"Followed link: {_fl_url}",
                                "source_url": _fl_url,
                            })
                            _fl_filled.append(_fk)
                        if emit and _fl_filled:
                            await emit(
                                "status",
                                f"[FOLLOW_LINK] {str(payload.get('course_name', url))[:35]} "
                                f"→ {_fl_url[:55]}: filled {_fl_filled}",
                                phase="extract",
                                kind="english_follow_link",
                                url=_fl_url,
                                filled=_fl_filled,
                            )
                except Exception as _flen_exc:  # noqa: BLE001
                    log.warning("english follow_links error on %s: %s", url, _flen_exc)

        # ── YAML: English band mapping ─────────────────────────────────────────
        # For universities that publish English requirements as named bands
        # (e.g. JCU "Band 2" = IELTS 6.5 overall, 6.0 each component), search
        # the course HTML for a recognised band label and apply the mapped scores.
        # The lookup runs even when central_page has already filled ielts_overall
        # because the per-course band label is more specific than the institution
        # default (which is always the lowest band on the policy page).
        try:
            _uc_bm = get_uni_config()
            _band_map: dict = (
                {
                    k: v if isinstance(v, dict) else (v.model_dump() if hasattr(v, "model_dump") else {})
                    for k, v in _uc_bm.extraction.english.band_mapping.items()
                }
                if _uc_bm and _uc_bm.extraction.english.band_mapping
                else {}
            )
            _band_ref_url: str | None = (
                _uc_bm.extraction.english.band_reference_url if _uc_bm else None
            )
        except Exception:  # noqa: BLE001
            _band_map = {}
            _band_ref_url = None
        if not _band_map:
            log.debug(
                "[BAND_MAP] no band_mapping config for %s — skipping band label lookup",
                url,
            )
        if _band_map and html:
            import re as _re_bm

            def _bm_can_override(_fkey: str) -> bool:
                """True when the field is blank OR was set only by central_page/sibling cache.

                Band label on the course page is per-course authoritative — it must
                override the institution-wide central_page default (which is always
                the lowest band on the policy schedule).
                """
                _cur = payload.get(_fkey)
                if _cur in (None, "", 0):
                    return True
                _field_ev = [e for e in evidence if e.get("field_key") == _fkey]
                return bool(_field_ev) and all(
                    "central_page" in e.get("method", "")
                    or e.get("method", "").startswith("yaml_default")
                    for e in _field_ev
                )

            from app.services.scraper.extractors._text import html_to_text as _h2t_bm  # noqa: PLC0415
            _bm_text = _h2t_bm(html)
            _matched_band: str | None = None
            _matched_spec: dict | None = None
            for _bname, _bspec in _band_map.items():
                if _re_bm.search(
                    r"\b" + _re_bm.escape(_bname) + r"\b",
                    _bm_text,
                    _re_bm.IGNORECASE,
                ):
                    _matched_band = _bname
                    _matched_spec = _bspec if isinstance(_bspec, dict) else {}
                    break
            if not _matched_band:
                if _re_bm.search(r"\bBand\s+[1-9P]", _bm_text, _re_bm.IGNORECASE):
                    log.info(
                        "[BAND_MAP_MISS] %s — 'Band N' found in page text but no "
                        "band_mapping config entry matched; check "
                        "extraction.english.band_mapping in the university YAML",
                        url,
                    )
            if _matched_band and _matched_spec is not None:
                _bm_filled: list[str] = []
                _bm_snippet = (
                    f"Band mapping: {_matched_band} → "
                    f"IELTS {_matched_spec.get('ielts_overall')} "
                    f"(each ≥{_matched_spec.get('ielts_each')})"
                )
                _bm_src = _band_ref_url or url
                # Apply IELTS overall
                _bm_ielts_overall = _matched_spec.get("ielts_overall")
                if _bm_ielts_overall is not None and _bm_can_override("ielts_overall"):
                    payload["ielts_overall"] = float(_bm_ielts_overall)
                    evidence[:] = [
                        e for e in evidence
                        if not (e.get("field_key") == "ielts_overall" and "central_page" in e.get("method", ""))
                    ]
                    evidence.append({
                        "field_key": "ielts_overall",
                        "value": float(_bm_ielts_overall),
                        "confidence": 0.90,
                        "method": "yaml_band_mapping",
                        "snippet": _bm_snippet,
                        "source_url": _bm_src,
                    })
                    _bm_filled.append("ielts_overall")
                # Apply IELTS per-component (listening, reading, speaking, writing)
                _bm_ielts_each = _matched_spec.get("ielts_each")
                if _bm_ielts_each is not None:
                    for _bm_field in (
                        "ielts_listening", "ielts_reading",
                        "ielts_speaking", "ielts_writing",
                    ):
                        if _bm_can_override(_bm_field):
                            payload[_bm_field] = float(_bm_ielts_each)
                            evidence[:] = [
                                e for e in evidence
                                if not (e.get("field_key") == _bm_field and "central_page" in e.get("method", ""))
                            ]
                            evidence.append({
                                "field_key": _bm_field,
                                "value": float(_bm_ielts_each),
                                "confidence": 0.90,
                                "method": "yaml_band_mapping",
                                "snippet": _bm_snippet,
                                "source_url": _bm_src,
                            })
                            _bm_filled.append(_bm_field)
                # Apply PTE overall
                _bm_pte = _matched_spec.get("pte_overall")
                if _bm_pte is not None and _bm_can_override("pte_overall"):
                    payload["pte_overall"] = int(_bm_pte)
                    evidence[:] = [
                        e for e in evidence
                        if not (e.get("field_key") == "pte_overall" and "central_page" in e.get("method", ""))
                    ]
                    evidence.append({
                        "field_key": "pte_overall",
                        "value": int(_bm_pte),
                        "confidence": 0.90,
                        "method": "yaml_band_mapping",
                        "snippet": _bm_snippet,
                        "source_url": _bm_src,
                    })
                    _bm_filled.append("pte_overall")
                # Apply TOEFL overall
                _bm_toefl = _matched_spec.get("toefl_overall")
                if _bm_toefl is not None and _bm_can_override("toefl_overall"):
                    payload["toefl_overall"] = int(_bm_toefl)
                    evidence[:] = [
                        e for e in evidence
                        if not (e.get("field_key") == "toefl_overall" and "central_page" in e.get("method", ""))
                    ]
                    evidence.append({
                        "field_key": "toefl_overall",
                        "value": int(_bm_toefl),
                        "confidence": 0.90,
                        "method": "yaml_band_mapping",
                        "snippet": _bm_snippet,
                        "source_url": _bm_src,
                    })
                    _bm_filled.append("toefl_overall")
                if emit and _bm_filled:
                    await emit(
                        "status",
                        f"[BAND_MAP] {str(payload.get('course_name', url))[:35]} "
                        f"→ {_matched_band}: filled {_bm_filled}",
                        phase="extract",
                        kind="english_band_mapping",
                        url=url,
                        band=_matched_band,
                        filled=_bm_filled,
                    )

        # ── Reverse no_location_online_override when browser fills location ──
        # The override fired at line 901 when course_location was blank
        # (SPA static shell returns the same content for every URL — the
        # location extractor found nothing).  The browser pass now has
        # JS-rendered HTML and may have filled course_location with real
        # physical campuses.  If so, the override was a false positive:
        # revert study_mode to "On Campus" so the correct mode is stored.
        _was_no_loc_override = any(
            e.get("method") == "study_mode:no_location_online_override"
            for e in evidence
        )
        if (
            _was_no_loc_override
            and payload.get("study_mode") == "Online"
            and bool((payload.get("course_location") or "").strip())
        ):
            payload["study_mode"] = "On Campus"
            evidence.append(
                {
                    "field_key": "study_mode",
                    "value": "On Campus",
                    "confidence": 0.65,
                    "method": "study_mode:browser_location_restore",
                    "snippet": (
                        "no_location_online_override reversed — browser pass "
                        "found physical location: "
                        f"{(payload.get('course_location') or '')[:80]}"
                    ),
                }
            )
            log.info(
                "[STUDY_MODE RESTORE] course=%r — browser filled location=%r; "
                "reversed no_location_online_override back to 'On Campus'.",
                payload.get("course_name") or url,
                payload.get("course_location"),
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("per-course browser fallback errored on %s: %s", url, exc)

    # ── Domestic-only re-check on rendered HTML ───────────────────────────────
    # Some sites (e.g. Federation) show "Not available to international
    # students" only in JS-rendered content (a disabled tab, a warning
    # banner loaded via XHR).  The static-HTML check above misses these.
    # Re-run the same test against the rendered HTML when we have it.
    #
    # Skip this check when the evidence already proves the page is
    # international — two independent signals are sufficient:
    #
    #  (A) URL contains an explicit international-student query parameter
    #      (e.g. UOW ?students=international, Monash ?intlFees=1).  The
    #      site's own URL routing is a stronger signal than any phrase in
    #      the rendered DOM, which may contain inactive domestic-tab markup.
    #
    #  (B) The per-course browser just extracted BOTH a fee AND an English
    #      score — this is only possible on a page that actually displays
    #      international student data, so a domestic-only flag would be a
    #      false positive.
    _url_signals_international = bool(
        _re.search(
            r"[?&](students|studenttype|student_type|intlfees|international)=international"
            r"|[?&]type=international",
            url,
            _re.IGNORECASE,
        )
    )
    _browser_confirmed_intl = bool(
        payload.get("international_fee") and (
            payload.get("ielts_overall")
            or payload.get("pte_overall")
            or payload.get("toefl_overall")
        )
    )
    # Phase 3: gated on extraction.filters.domestic_only.enabled (fail-open).
    #
    # Two-tier check on the rendered HTML:
    #
    #   1. HARD signal (`_DOMESTIC_ONLY_RE` direct hit) — unambiguous
    #      attribute or JSON markers like Torrens
    #      ``data-studenttypes="Domestic"`` or Federation's
    #      ``"StudentTypeBlock" … "hasInternational": false``. These
    #      MUST flip the flag even when fee+English data is present in
    #      the payload, because that data may have leaked in from
    #      (a) per-course PDF matching against an unrelated row, or
    #      (b) the central English-requirements page applied to every
    #      course on the site. The ``_browser_confirmed_intl`` exemption
    #      was designed to protect SOFT markers from false positives,
    #      not to override an unambiguous CMS-level domestic-only flag.
    #      Verified live (job_7f369…) on Bachelor of Media Production
    #      & Communication: PDF matched a $37,500 row → IELTS came from
    #      central English page → guard incorrectly bypassed.
    #
    #   2. SOFT/full check (``_is_domestic_only_page``, which also
    #      runs the soft "may not be available" pattern) keeps the
    #      ``_browser_confirmed_intl`` + ``_url_signals_international``
    #      exemptions to avoid false positives on pages that genuinely
    #      do display international data.
    if (
        not payload.get("domestic_only")
        and rendered_html
        and _domestic_only_filter_enabled()
        and not _url_signals_international
    ):
        _hard_marker_hit = bool(_DOMESTIC_ONLY_RE.search(rendered_html))
        # Federation-scoped supplement: the React app disables the
        # International tab (and shows a CSP-only fee) via component state
        # rather than emitting the `"hasInternational": false` JSON string
        # that _DOMESTIC_ONLY_RE looks for, so the hard marker above misses
        # courses like the Bachelor of Exercise and Sport Science.  Treat a
        # disabled-tab / CSP-only signal as a HARD marker too: it must
        # override _browser_confirmed_intl, because the international_fee in
        # the payload was leaked by the central fee-schedule PDF matching a
        # row for a course the live page does not offer to international
        # students.  Host-gated, so no effect on other universities.
        _fed_signal = _federation_domestic_only_signal(rendered_html, url)
        # PDF-fee override (2026-05-28): Federation's HE international fee
        # schedule PDF, by definition, only contains courses offered to
        # international students.  If the per-course PDF matcher populated
        # `payload["international_fee"]`, then this course IS offered to
        # internationals — so a `federation_intl_tab_disabled` verdict on
        # the rendered DOM (which can be empty, broken, or contain
        # unrelated disabled UI elements that fool the tab-state matcher)
        # must be wrong.  Drop the federation tab-disabled signal in that
        # case.  The `federation_csp_domestic_only` signal is NOT
        # overridden because it already requires no international dollar
        # amount on the page and is therefore self-protecting.
        if _fed_signal == "federation_intl_tab_disabled" and payload.get(
            "international_fee"
        ):
            log.info(
                "[FED OVERRIDE] %s — ignoring federation_intl_tab_disabled "
                "because per-course PDF supplied international_fee=%s",
                url, payload.get("international_fee"),
            )
            _fed_signal = None
        if _fed_signal:
            _hard_marker_hit = True
        if _hard_marker_hit or (
            _is_domestic_only_page(rendered_html) and not _browser_confirmed_intl
        ):
            payload["domestic_only"] = True
            await emit(
                "status",
                f"[DOMESTIC ONLY] {url} — rendered page states domestic-students-only"
                + (f" ({_fed_signal})" if _fed_signal else "")
                + "; skipping",
                phase="extract",
                kind="domestic_only_skip",
                url=url,
            )
            return {"url": url, "payload": payload, "evidence": evidence}

    try:
        if not _vision_ocr_trusted():
            # trust_vision_ocr: false in per-uni YAML — skip all vision OCR for
            # this university.  Stub empty containers so the downstream merge /
            # suppression logic in this try-block runs harmlessly (empty-dict
            # iterations and falsy guards all short-circuit correctly).
            log.info(
                "[VISION SKIP] trust_vision_ocr=false for this uni — "
                "skipping vision OCR pass on %s",
                url,
            )
            vision_filled, vision_evidence = {}, []
        elif (
            getattr(get_uni_config(), "extraction", None) is not None
            and getattr(get_uni_config().extraction.english, "skip_vision_when_core_found", False)
            and payload.get("ielts_overall")
            and payload.get("international_fee")
        ):
            # skip_vision_when_core_found=true + both core fields populated:
            # vision OCR cannot improve on pre-filled IELTS default + fee default.
            # Avoids scanning 6 candidate images + Gemini API call per course.
            _ielts_v = payload.get("ielts_overall")
            _fee_v = payload.get("international_fee")
            _perf_flags["vision_skipped"] = True
            log.info(
                "[VISION SKIP] skip_vision_when_core_found — ielts=%s fee=%s already set on %s",
                _ielts_v, _fee_v, url,
            )
            if emit:
                await emit(
                    "status",
                    f"[VISION SKIP] Core fields already set (IELTS={_ielts_v} fee={_fee_v}) — skipping vision OCR",
                    phase="extract", kind="vision_skip_core_found", url=url,
                )
            vision_filled, vision_evidence = {}, []
        else:
            from app.services.scraper.per_course_vision import maybe_vision_refetch

            # Determine whether to skip tier-1 images before calling Gemini.
            # This saves the API call entirely rather than calling then discarding.
            #   • skip_tier1_english=True when the uni YAML sets
            #     trust_tier1_vision_ocr_english=false (Flinders and any future
            #     uni where tier-1 images are known to hallucinate).
            #   • The function also skips tier-1 images automatically when
            #     payload already has ielts_overall from regex (global cost-saver
            #     for every uni — regex found it in text, vision adds nothing).
            _uc_pre = get_uni_config()
            _skip_tier1 = not (
                _uc_pre is None
                or _uc_pre.extraction.english.trust_tier1_vision_ocr_english
            )
            vision_filled, vision_evidence = await maybe_vision_refetch(
                url, rendered_html or html, payload, emit=emit,
                image_cache=vision_image_cache,
                degree_level=payload.get("degree_level"),
                skip_tier1_english=_skip_tier1,
            )
        # Authority-aware merge: per_course_vision (tier 4) overrides any
        # tier-3 text extraction (regex, Gemini, browser, AI fallback).
        # This is the key ASAHE fix: the image is the authoritative source
        # even when text extraction happened to fill the slot with a value.
        # Pre-seeds (tier 5) are NOT overridden — they are site-specific
        # hard-coded values that should always win.
        #
        # TIER GUARD (Fix 3): only tier-0 vision images (those found inside
        # the "English Requirements" / "Entry Requirements" DOM section) are
        # allowed to *override* an existing tier-3 page-text value.  Tier-1/2
        # images can FILL empty slots but must not supersede regex /
        # equivalence_table results — they may have slipped through the
        # decorative filter and be hallucinating plausible-looking scores.

        # ── Tier-1 IELTS-coherence gate ───────────────────────────────────
        # Problem: a tier-1 AEM/hero image on a Flinders page may show a
        # *generic* English requirements table (IELTS=6.5 for the faculty)
        # rather than this specific course's requirements (IELTS=6.0 per
        # regex).  The image passes the decorative filter (it IS a table) but
        # is not course-specific.  It then silently fills empty TOEFL/PTE
        # slots with values from the wrong table.
        #
        # Guard: if page-text (regex/structural) already established
        # ielts_overall=X and a tier-1 vision image returned ielts_overall=Y
        # where |X-Y| > 0.1, that image is reading a different course's table.
        # Discard ALL fields sourced from that image — including TOEFL/PTE
        # that would otherwise fill empty slots unchallenged.
        _regex_ielts: float | None = None
        _regex_ielts_method = next(
            (
                ev.get("method", "")
                for ev in reversed(evidence)
                if ev.get("field_key") == "ielts_overall"
                and ev.get("decision_status") != "superseded"
            ),
            "",
        )
        if (
            _regex_ielts_method
            and not _regex_ielts_method.startswith("per_course_vision")
            and not _regex_ielts_method.startswith("uni_pdf")
        ):
            try:
                _regex_ielts = float(payload.get("ielts_overall") or 0) or None
            except (TypeError, ValueError):
                _regex_ielts = None

        # English overall slots used by both sub-gates below.
        _ENGLISH_OVERALL_SLOTS: frozenset[str] = frozenset({
            "ielts_overall", "pte_overall", "toefl_overall",
            "cambridge_overall", "duolingo_overall",
        })

        _incoherent_img_urls: set[str] = set()

        # ── Per-uni tier-1 English OCR opt-out (safety net) ─────────────
        # maybe_vision_refetch already skips tier-1 images before calling
        # Gemini when skip_tier1_english=True or when payload has ielts_overall.
        # This block is a belt-and-suspenders guard: any tier-1 evidence that
        # somehow reached vision_evidence despite those pre-filters is added to
        # _incoherent_img_urls so the merge loop discards it.  In normal
        # operation this loop runs but adds nothing (vision_evidence has no
        # tier-1 entries for skipped images).
        _uc = get_uni_config()
        _tier1_english_trusted = (
            _uc is None
            or _uc.extraction.english.trust_tier1_vision_ocr_english
        )
        if not _tier1_english_trusted:
            for _vev in vision_evidence:
                if _vev.get("source_tier", 1) != 0:
                    _src = _vev.get("source_url", "")
                    if _src:
                        _incoherent_img_urls.add(_src)
            if _incoherent_img_urls:
                log.info(
                    "[VISION TIER1 ENGLISH DISABLED] %s: %d tier-1 image(s) "
                    "blocked for English test fields (trust_tier1_vision_ocr_english=false)",
                    url,
                    len(_incoherent_img_urls),
                )

        # ── Sub-gate A: IELTS-anchor mismatch (regex IELTS known) ────────
        if _regex_ielts is not None:
            for _vev in vision_evidence:
                if (
                    _vev.get("field_key") == "ielts_overall"
                    and _vev.get("source_tier", 1) != 0
                ):
                    try:
                        if abs(float(_vev["value"]) - _regex_ielts) > 0.1:
                            _incoherent_img_urls.add(_vev.get("source_url", ""))
                            log.info(
                                "[VISION IELTS INCOHERENT] %s: img %r returned "
                                "ielts_overall=%.1f but regex established %.1f — "
                                "discarding ALL fields from this tier-1 image",
                                url,
                                (_vev.get("source_url") or "")[-80:],
                                float(_vev["value"]),
                                _regex_ielts,
                            )
                    except (TypeError, ValueError):
                        pass

        # ── Sub-gate B: single-test tier-1 images when no regex anchor ───
        # When regex found no IELTS at all (_regex_ielts is None), sub-gate A
        # cannot fire.  A real requirements table always lists ≥2 English tests
        # (IELTS + TOEFL, or IELTS + PTE, etc.).  A hero image or AEM content
        # fragment that merely mentions "IELTS 6.5" in a caption will provide
        # only one overall slot.  Reject tier-1 images that provide fewer than
        # 2 distinct English overalls — they are almost certainly not the
        # course's requirements table.
        else:
            # Count distinct English overalls per tier-1 image URL.
            from collections import Counter as _Counter
            _t1_overall_count: dict[str, int] = _Counter(
                _vev["source_url"]
                for _vev in vision_evidence
                if (
                    _vev.get("source_tier", 1) != 0  # tier-1/2 only
                    and _vev.get("field_key") in _ENGLISH_OVERALL_SLOTS
                    and _vev.get("source_url")
                )
            )
            for _img_url, _cnt in _t1_overall_count.items():
                if _cnt < 2:
                    _incoherent_img_urls.add(_img_url)
                    log.info(
                        "[VISION SINGLE-TEST REJECT] %s: tier-1 img %r "
                        "supplied only %d English overall slot(s) — "
                        "requires ≥2 to be trusted without regex IELTS anchor",
                        url,
                        _img_url[-80:],
                        _cnt,
                    )

        for k, v in vision_filled.items():
            _prior_method = ""
            for _ev in reversed(evidence):
                if _ev.get("field_key") == k and _ev.get("decision_status") != "superseded":
                    _prior_method = _ev.get("method", "")
                    break
            # Look up which tier this vision evidence came from.
            _vision_ev = next(
                (ev for ev in vision_evidence if ev.get("field_key") == k), None
            )
            _vision_is_tier0 = _vision_ev is not None and _vision_ev.get("source_tier", 1) == 0

            # Reject all fields from images flagged by the IELTS coherence gate.
            if _vision_ev and _vision_ev.get("source_url") in _incoherent_img_urls:
                continue

            # ── IELTS sub-band coherence guard (Fix: vision portrait bug) ─
            # Sub-bands from a non-requirements-section (tier-1/2) image are
            # rejected when the page text already established a higher
            # ielts_overall.  Root cause: Gemini halluccinates plausible-
            # looking IELTS bands (e.g. 6.0 L / 6.5 R) from an image of a
            # student portrait that contains no IELTS data at all.  The
            # hallucinated values are below the overall (7.0) that regex found
            # on the same page — a reliable coherence signal.
            #
            # Guard fires when ALL of:
            #   1. k is an IELTS sub-band slot
            #   2. The image is NOT tier-0 (not from the English requirements
            #      DOM section) — tier-0 images are trusted unconditionally
            #   3. ielts_overall is already set in the payload from a
            #      page-text method (regex, structural — NOT vision or pdf)
            #   4. The vision sub-band value is strictly less than the
            #      established overall — physically impossible for a real table
            #      whose "no band below X" floor equals the overall
            _IELTS_SUBBAND_SET = frozenset({
                "ielts_listening", "ielts_reading",
                "ielts_speaking", "ielts_writing",
            })
            if k in _IELTS_SUBBAND_SET and not _vision_is_tier0 and v is not None:
                _est_overall = payload.get("ielts_overall")
                if _est_overall is not None:
                    _overall_method = next(
                        (
                            ev.get("method", "")
                            for ev in reversed(evidence)
                            if ev.get("field_key") == "ielts_overall"
                            and ev.get("decision_status") != "superseded"
                        ),
                        "",
                    )
                    _overall_from_text = bool(
                        _overall_method
                        and not _overall_method.startswith("per_course_vision")
                        and not _overall_method.startswith("uni_pdf")
                    )
                    if _overall_from_text:
                        try:
                            if float(v) < float(_est_overall):
                                log.info(
                                    "[VISION SUBBAND REJECT] %s: %s=%.1f from "
                                    "tier-1/2 image rejected — below "
                                    "ielts_overall=%.1f established by %r",
                                    url, k, float(v), float(_est_overall),
                                    _overall_method,
                                )
                                continue
                        except (TypeError, ValueError):
                            pass

            if payload.get(k) in (None, "", 0):
                # Week 2 P7 audit: log when vision fills a null slot AND the
                # value is corroborated by static page text.  This is the
                # spec's "preserve vision when other extractor returned null
                # and value appears in page text" path — we always preserve
                # (existing behaviour) but now leave a trail so reviewers
                # can distinguish "vision rescued a real value" from
                # "vision hallucinated into an empty slot".
                try:
                    _pt = (rendered_html or html or "").lower()
                    if _pt and v is not None and str(v).lower() in _pt:
                        log.info(
                            "[VISION CORROBORATED] %s: %s=%s filled by vision "
                            "and confirmed verbatim in page text",
                            url, k, v,
                        )
                except Exception:  # noqa: BLE001
                    pass
                payload[k] = v  # fill null slot — always safe regardless of tier
            elif _vision_is_tier0 and can_override(_prior_method, "per_course_vision"):
                # Tier-0 image (from English requirements DOM section) beats
                # tier-3 text — supersede the existing value.
                # QA Issue-6: log when OCR overrides a structured/Gemini value.
                _vision_prior_val = payload.get(k)
                if (
                    _vision_prior_val is not None
                    and _prior_method
                    and not _prior_method.startswith("per_course_vision")
                ):
                    log.info(
                        "[OCR_CONFLICT] %s: tier-0 vision %s=%r overrides "
                        "%s=%r — structured/Gemini value superseded by OCR",
                        url, k, v, _prior_method, _vision_prior_val,
                    )
                for _ev in evidence:
                    if _ev.get("field_key") == k and _ev.get("decision_status") != "superseded":
                        _ev["decision_status"] = "superseded"
                payload[k] = v
            # else: either non-tier0 vision (must not override page-text) or
            #       existing value already has tier ≥ 4 authority — keep it.
        evidence.extend(vision_evidence)

        # ── Vision negative-suppression ───────────────────────────────────
        # When vision processed a comprehensive English-requirements image
        # (evidenced by ≥ 2 distinct English overalls found) but did NOT
        # find a specific slot (e.g. DET, CAE), any university-wide PDF value
        # for that slot should be nulled.  The course-specific image is the
        # ground truth: its ABSENCE of DET/CAE means those tests don't apply
        # here.  Without this suppression, the PDF's generic Duolingo or
        # Cambridge row bleeds into every course even when the course page
        # explicitly shows no requirement.
        #
        # Only fires when:
        #  1. vision_filled has ≥ 2 English overall slots (comprehensive table)
        #  2. The slot to null has ONLY uni-wide evidence (max authority < 3)
        #  3. The slot is not present in vision_filled (image lacks that test)
        _ENGLISH_OVERALL_VISION = (
            "ielts_overall", "pte_overall", "toefl_overall",
            "cambridge_overall", "duolingo_overall",
        )
        _vision_overalls_found = sum(
            1 for s in _ENGLISH_OVERALL_VISION if s in vision_filled
        )
        if _vision_overalls_found >= 2:
            for _vs in _ENGLISH_OVERALL_VISION:
                if _vs in vision_filled:
                    continue  # vision found it — not absent
                if payload.get(_vs) in (None, "", 0):
                    continue  # nothing to suppress
                _vs_max_auth = max(
                    (_method_authority(ev.get("method", ""))
                     for ev in evidence if ev.get("field_key") == _vs),
                    default=0,
                )
                if _vs_max_auth >= _AUTHORITY_COURSE_SPECIFIC:
                    continue  # course-specific evidence — don't null it
                # Null the uni-wide-only value and mark evidence superseded
                payload[_vs] = None
                for _ev in evidence:
                    if _ev.get("field_key") == _vs and _ev.get("decision_status") != "superseded":
                        _ev["decision_status"] = "superseded"
                log.info(
                    "[VISION NEG-SUPPRESS] %s: nulled %s (image absent; "
                    "was %s from uni-wide source)",
                    url, _vs, _vs_max_auth,
                )
                if emit:
                    await emit(
                        "status",
                        f"[VISION NEG-SUPPRESS] {payload.get('course_name', url)[:40]} — "
                        f"nulled {_vs} (course image has no {_vs} row; "
                        f"uni-wide PDF value suppressed)",
                        phase="fallback",
                        kind="vision_neg_suppress",
                        url=url,
                        slot=_vs,
                    )

        # ── Vision sub-band suppression ───────────────────────────────────
        # When vision found `ielts_overall` (or pte/toefl) from a course
        # image but the OCR was incomplete and missed some sub-bands (e.g.
        # reading/speaking/writing), those slots may still hold a stale
        # uni-wide PDF value (e.g. 5.5).  The inference in per_course_vision
        # normally fills them, but if that didn't fire (e.g. the cached
        # result predates the fix), null out any sub-band whose ONLY source
        # is a uni-wide PDF and whose corresponding overall came from vision.
        _SUBBAND_SUPPRESSION_GROUPS: dict[str, tuple[str, ...]] = {
            "ielts_overall": (
                "ielts_listening", "ielts_reading", "ielts_speaking", "ielts_writing",
            ),
            "pte_overall": (
                "pte_listening", "pte_reading", "pte_speaking", "pte_writing",
            ),
            "toefl_overall": (
                "toefl_listening", "toefl_reading", "toefl_speaking", "toefl_writing",
            ),
        }
        for _overall_slot, _sbands in _SUBBAND_SUPPRESSION_GROUPS.items():
            if _overall_slot not in vision_filled:
                continue  # vision didn't find this test — nothing to do
            for _sb in _sbands:
                if _sb in vision_filled:
                    continue  # vision already provided this sub-band — ok
                if payload.get(_sb) in (None, "", 0):
                    continue  # slot empty — nothing to suppress
                _sb_max_auth = max(
                    (_method_authority(ev.get("method", ""))
                     for ev in evidence if ev.get("field_key") == _sb),
                    default=0,
                )
                if _sb_max_auth >= _AUTHORITY_COURSE_SPECIFIC:
                    continue  # protected by course-specific text — don't null
                # Null the uni-wide-only sub-band value so downstream
                # sibling-cache and staging don't propagate wrong scores.
                payload[_sb] = None
                for _ev in evidence:
                    if _ev.get("field_key") == _sb and _ev.get("decision_status") != "superseded":
                        _ev["decision_status"] = "superseded"
                log.info(
                    "[VISION NEG-SUPPRESS] %s: nulled sub-band %s (vision "
                    "found %s from image but sub-band was uni-wide PDF only)",
                    url, _sb, _overall_slot,
                )

        # ── Vision sanity check ───────────────────────────────────────────
        # When a per-course vision OCR reading for an English slot diverges
        # too far from the university-wide central-page value, the course
        # page always wins.  The central-page value is stored as a superseded
        # evidence row so reviewers can see both readings in Evidence Review.
        #
        # Part 4 — corroboration: if the vision value also appears verbatim
        # in the static page text (keyword + value within 100 chars), the
        # reading is confirmed as real, not a hallucination.  The corroboration
        # result is surfaced in the emit message and evidence snippet so
        # reviewers know whether to trust a low value (e.g. IELTS 4.5 on an
        # ELICOS page).  The keep-vs-revert decision is unaffected — the
        # course page already wins unconditionally — but the corroboration
        # flag is useful for distinguishing "genuinely low" from "misread".
        if central_data and vision_filled:
            _central_eng: dict = central_data.get("english") or {}
            _central_eng_url: str | None = central_data.get("english_page_url")
            # Import corroboration helper once for this block
            try:
                from app.services.scraper.pathway_detection import (
                    vision_value_appears_in_page_text as _vision_corroborated,
                )
                from app.services.scraper.extractors._text import (
                    compact as _compact_text,
                    html_to_text as _html_to_text,
                )
                _page_text_for_corroboration = _compact_text(_html_to_text(html or ""))
            except Exception:  # noqa: BLE001
                _vision_corroborated = None  # type: ignore[assignment]
                _page_text_for_corroboration = ""
            for _slot, _max_delta in _VISION_SANITY_THRESHOLDS.items():
                if _slot not in vision_filled:
                    continue
                _v_val = payload.get(_slot)
                _c_val = _central_eng.get(_slot)
                if _v_val is None or _c_val is None:
                    continue
                try:
                    _delta = abs(float(_v_val) - float(_c_val))
                except (TypeError, ValueError):
                    continue
                if _delta <= _max_delta:
                    continue
                # Check whether the vision value is corroborated in static HTML
                _corroborated = bool(
                    _vision_corroborated is not None
                    and _vision_corroborated(
                        _v_val, _slot, _page_text_for_corroboration
                    )
                )
                # Course page always wins: do NOT revert to central-page value
                # even when vision and central diverge.  Instead, store the
                # central-page value as a superseded evidence row so the reviewer
                # can see both readings side-by-side in Evidence Review.
                _corr_note = " [corroborated by page text]" if _corroborated else " [not found in page text — review recommended]"
                evidence.append({
                    "field_key": _slot,
                    "value": _c_val,
                    "confidence": 0.50,
                    "method": "central_page:english",
                    "source_url": _central_eng_url or url,
                    "snippet": (
                        f"central_page:english {_slot}={_c_val} (diverges from course vision "
                        f"by {_delta:.1f}; course page value kept{_corr_note})"
                    ),
                    "decision_status": "superseded",
                })
                if emit:
                    await emit(
                        "status",
                        f"[VISION vs CENTRAL] {payload.get('course_name', url)[:40]} — "
                        f"{_slot}: vision={_v_val} vs central={_c_val} "
                        f"(delta={_delta:.1f} > {_max_delta}) — course page value kept"
                        f"{_corr_note}",
                        phase="extract",
                        kind="vision_sanity_note",
                        url=url,
                        slot=_slot,
                        vision_val=_v_val,
                        central_val=_c_val,
                        corroborated=_corroborated,
                    )
    except Exception as exc:  # noqa: BLE001
        log.warning("per-course vision fallback errored on %s: %s", url, exc)

    # T003: VIT-specific static fallback for duration / intake / location.
    # The per-course browser pass clicks the "International students"
    # toggle which strips the static narrative paragraph (`<p><strong>
    # Duration:</strong> Usually a 3 year course...</p>`) from the
    # rendered DOM. We re-parse the original static HTML to recover
    # those fields. Only fires when at least one of the three slots is
    # still missing AND the URL is a vit.edu.au page.
    try:
        from app.services.scraper.vit_static_extract import (
            apply_vit_summary_extraction,
            is_vit_url,
        )
        if is_vit_url(url):
            need_dur = payload.get("duration") in (None, "", 0) or not payload.get("duration_term")
            need_int = payload.get("intake_text") in (None, "")
            need_loc = payload.get("location_text") in (None, "")
            if need_dur or need_int or need_loc:
                vit_filled = apply_vit_summary_extraction(url, html, payload)
                for k, v in vit_filled.items():
                    if v in (None, "", 0):
                        continue
                    if payload.get(k) not in (None, "", 0):
                        continue
                    payload[k] = v
                    # source_url + non-empty snippet are required by
                    # enforce_source_evidence (guards.py) for the critical
                    # field set (location_text, duration_text, study_mode).
                    # Mirrors the Bond/ECU/CSU pre-seed shape so the recovered
                    # values aren't silently nulled at staging time.
                    evidence.append(
                        {
                            "field_key": k,
                            "value": v,
                            "confidence": 0.85,
                            "method": "vit_static_fallback",
                            "source_url": url,
                            "snippet": f"VIT fallback: {k}={v}",
                        }
                    )
                if emit and vit_filled:
                    parts = []
                    if vit_filled.get("duration") is not None:
                        parts.append(
                            f"duration={vit_filled.get('duration')}"
                            f"{vit_filled.get('duration_term', '')}"
                        )
                    if vit_filled.get("intake_text"):
                        parts.append(f"intakes={vit_filled['intake_text']}")
                    if vit_filled.get("location_text"):
                        parts.append(f"location={vit_filled['location_text']}")
                    await emit(
                        "status",
                        f"[VIT static fallback ✓] "
                        f"{payload.get('course_name', url)[:40]} — "
                        f"recovered {', '.join(parts)}",
                        phase="fallback",
                        kind="vit_static_done",
                        url=url,
                        filled=list(vit_filled.keys()),
                    )
    except Exception as exc:  # noqa: BLE001
        log.warning("vit_static_extract failed on %s: %s", url, exc)

    # ── Content-based staging skip (skip_staging_keywords) ──────────────────
    # If the YAML lists keyword phrases and ANY appears in the page text, abort
    # staging entirely — the course is never written to scraped_courses.
    # This runs BEFORE any Gemini calls so CPD/short-course pages that slipped
    # past the URL block_url_patterns filter are silently dropped without
    # wasting API budget.
    # Ulster use case: CPD pages carry "Short course and CPD" in their <title>
    # but may have IDs outside the blocked 50xxx/46xxx/47xxx ranges.
    _uc_stage_skip = get_uni_config()
    _skip_stage_kws: list[str] = (
        list(_uc_stage_skip.extraction.skip_staging_keywords)
        if _uc_stage_skip else []
    )
    if _skip_stage_kws:
        _page_text_for_stage_kw = (_h2t_gate(rendered_html or html or "") or "").lower()
        for _skw in _skip_stage_kws:
            if _skw.lower() in _page_text_for_stage_kw:
                log.info(
                    "[SKIP-STAGING] CPD/short-course detected (%r) — aborting staging on %s",
                    _skw, url,
                )
                if emit:
                    await emit(
                        "status",
                        f"[SKIP-STAGING] CPD/short-course detected ({_skw!r}) — not staging",
                        phase="extract", kind="staging_skipped_cpd", url=url,
                    )
                return {
                    "url": url,
                    "error": "skipped:cpd_short_course",
                    "payload": {},
                    "evidence": [],
                }

    # ── Content-based AI-fallback skip (skip_ai_fallback_keywords) ───────────
    # If the YAML lists keyword phrases and ANY appears in the page text, skip
    # the fallback Gemini enrichment (ai_fallback.fill_missing).  The pre-
    # baseline Gemini (category / study_load) is NOT affected — only this late
    # enrichment pass is suppressed.
    # Ulster use case: ~250-300 CPD "Short course and CPD" pages have no
    # international fee, IELTS, or intake data — both Gemini calls return
    # all-nulls and waste ~45s/course × 300 courses ≈ 3.75 hours.
    if use_ai_fallback:
        _uc_kw = get_uni_config()
        _skip_kws: list[str] = (
            list(_uc_kw.extraction.skip_ai_fallback_keywords)
            if _uc_kw else []
        )
        if _skip_kws:
            _page_text_for_kw = (_h2t_gate(rendered_html or html or "") or "").lower()
            for _kw in _skip_kws:
                if _kw.lower() in _page_text_for_kw:
                    use_ai_fallback = False
                    log.info(
                        "[SKIP-FALLBACK] CPD short course detected (%r) — skipping AI fallback on %s",
                        _kw, url,
                    )
                    if emit:
                        await emit(
                            "status",
                            f"[SKIP-FALLBACK] CPD short course detected ({_kw!r}) — skipping AI fallback",
                            phase="extract", kind="ai_fallback_skipped_cpd", url=url,
                        )
                    break

    if use_ai_fallback:
        # ── English-section gate for IELTS ai_fallback (Bug 7 fix) ──────────
        # ai_fallback.fill_missing was hallucinating ielts_overall (e.g.
        # IELTS 3.0 for a bachelor's) when the course page has no English
        # requirements section — Gemini returns a plausible-sounding score
        # instead of null. Fix: detect whether any English requirement heading
        # appears in the page HTML BEFORE the ai_fallback call; if none is
        # found, exclude ielts_overall from the fields passed to fill_missing.
        # This prevents the model from being asked to invent a score from a
        # page that makes no mention of IELTS. Missing-IELTS warnings are
        # acceptable; invented scores are not.
        _af_check_html_lower = (rendered_html or html or "").lower()
        _AF_ENGLISH_HEADING_PATTERNS = (
            "english language requirement",
            "english requirement",
            "english proficiency",
            "ielts requirement",
            "language requirement",
            "english language proficiency",
            "ielts:",
            "ielts ",
            "pte:",
            "pte ",
        )
        _af_english_section_present = any(
            p in _af_check_html_lower for p in _AF_ENGLISH_HEADING_PATTERNS
        )
        # Build an explicit fields override when no English section found.
        # fill_missing defaults to all _FIELD_HINTS keys; passing an explicit
        # list lets the caller exclude sensitive fields without touching the module.
        _af_fields_override: list[str] | None = None
        if not _af_english_section_present:
            _af_fields_override = [
                f for f in ai_fallback._FIELD_HINTS
                if f != "ielts_overall"
            ]
            log.info(
                "[AI_FALLBACK] no English section detected — "
                "excluding ielts_overall from fallback fill on %s",
                url,
            )

        # Note which slots are still empty so the UI can show *what* the AI
        # is being asked to fill (helpful when diagnosing weak per-page
        # extraction on a new university template).
        _ai_target_keys = (
            "international_fee", "domestic_fee", "ielts_overall",
            "duration_text", "intake_text", "location_text",
        )
        missing = [k for k in _ai_target_keys if k not in payload or payload.get(k) is None]

        # UOW / UniSQ: explicit parser-failure logging and parser_error flag.
        # Both universities publish fee + IELTS on every course page. By this
        # point the browser pass has run the full extractor suite against the
        # JS-rendered DOM, so a still-empty slot indicates a genuine extractor
        # miss or a page that hides data behind a login wall.
        #
        # UOW rule (per-spec): if the browser timed out AND any field in the
        # "must-not-be-guessed" set is still blank (would be AI-filled), mark
        # parser_error so the row is not staged as review-ready.  This prevents
        # rows with AI-hallucinated duration / intake / fee from polluting the
        # review queue.  For UniSQ only the render-success path applies.
        _ext_critical = {"international_fee", "ielts_overall"}
        # Fields that require rendered HTML for UOW — if these are still
        # missing after static-HTML extraction and browser timed out, the
        # values would be AI-guessed and must NOT be trusted.
        _uow_render_required: set[str] = {
            "duration_text", "intake_text", "study_mode",
        }
        _parsed_host = (urlparse(url).netloc or "").lower()
        _is_uow_host = _parsed_host in ("www.uow.edu.au", "uow.edu.au")
        if _is_uow_host or _parsed_host in ("www.unisq.edu.au", "unisq.edu.au"):
            _had_render = rendered_html is not None  # type: ignore[possibly-undefined]

            # ── Critical-field check (both UOW and UniSQ) ─────────────────
            _still_missing = [f for f in _ext_critical if f in missing]
            if _still_missing:
                _reason = (
                    "not found in static HTML OR rendered DOM — data may be behind login"
                    if _had_render
                    else "browser render unavailable (timeout) — static HTML only"
                )
                for _fld in _still_missing:
                    log.warning("[UOW PARSER MISSING] %s — %s — %s", _fld, url, _reason)
                    if emit:
                        await emit(
                            "status",
                            f"[UOW PARSER MISSING] {_fld}: {_reason}",
                            phase="extract",
                            kind="parser_missing",
                            field=_fld,
                            url=url,
                            had_render=_had_render,
                        )
                # Mark as parser_error when the browser DID render the page but
                # extractors still could not fill the field — this prevents a
                # row with blank fee/IELTS from being staged as review-ready and
                # polluting the review queue with obviously incomplete data.
                if _had_render:
                    payload["parser_error"] = True
                    payload["parser_error_fields"] = _still_missing

            # ── UOW browser-timeout guard ──────────────────────────────────
            # UOW requires rendered HTML to fill duration / intake / mode.
            # When the browser timed out, fields in _uow_render_required that
            # are still blank will be filled by the AI fallback below — those
            # values cannot be trusted.  Mark parser_error so the staging gate
            # withholds the row from the review queue rather than showing
            # incorrect data.
            if _is_uow_host and not _had_render:
                # CANONICAL-SLOT BYPASS (2026-05-17): the prior version only
                # checked the *text-shape* slots (duration_text, intake_text)
                # against `missing`.  But the static-HTML regex pass on UOW
                # pages reliably fills the *canonical* slots — `duration` via
                # the generic regex cascade and `intake_months` via
                # `intake.session_names` ("Autumn Session" → March, "Spring
                # Session" → July).  When those are already populated, the
                # downstream AI fallback won't synthesise new values — yet
                # the prior guard still flipped parser_error=True and the
                # orchestrator (orchestrator.py:1996) silently dropped the
                # row from the staging queue.  Net effect: hundreds of
                # legitimate UOW rows were withheld on every scrape where
                # the browser refetch hit its outer timeout on the JS-only
                # shell pages.  Delegated to module-private helper so the
                # regression test can import it directly.
                _uow_guessed = _uow_timeout_guessed_fields(payload, missing)
                if _uow_guessed:
                    payload["parser_error"] = True
                    payload["parser_error_fields"] = (
                        payload.get("parser_error_fields") or []
                    ) + _uow_guessed
                    _uow_reason = (
                        f"browser timed out — {', '.join(_uow_guessed)} "
                        f"would be AI-guessed; row withheld from review queue"
                    )
                    log.warning("[UOW TIMEOUT GUARD] %s — %s", url, _uow_reason)
                    if emit:
                        await emit(
                            "status",
                            f"[UOW TIMEOUT GUARD] {_uow_reason}",
                            phase="extract",
                            kind="uow_timeout_parser_error",
                            url=url,
                            guessed_fields=_uow_guessed,
                        )

        if emit:
            await emit(
                "status",
                f"[FALLBACK] AI enriching {url} (missing: {', '.join(missing) if missing else 'none'})",
                phase="extract",
                kind="ai_fallback_start",
                missing=missing,
            )
        try:
            # Hard ceiling so a hung Gemini call cannot wedge a worker
            # the same way the Playwright incident did. On timeout the
            # underlying SDK call is cancelled and we fall through to
            # the existing "AI failure" path — extraction proceeds
            # without AI fill, which is the same UX as a model error.
            ai_filled = await asyncio.wait_for(
                ai_fallback.fill_missing(payload, html=html, url=url, fields=_af_fields_override),
                timeout=_AI_FALLBACK_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            log.warning(
                "AI fallback exceeded %ss on %s — aborting this course's AI pass",
                _AI_FALLBACK_TIMEOUT_SEC,
                url,
            )
            if emit:
                await emit(
                    "status",
                    f"[FALLBACK] AI fallback exceeded "
                    f"{_AI_FALLBACK_TIMEOUT_SEC}s on {url} — moving on without AI fill",
                    phase="extract",
                    kind="ai_fallback_timeout",
                    timeout_seconds=_AI_FALLBACK_TIMEOUT_SEC,
                    level="warn",
                )
            ai_filled = {}
        except Exception as exc:  # never break extraction on AI failure
            log.warning("AI fallback errored on %s: %s", url, exc)
            if emit:
                await emit(
                    "status",
                    f"[FALLBACK] AI fallback errored on {url}: {exc}",
                    phase="extract",
                    kind="ai_fallback_error",
                )
            ai_filled = {}
        if emit and ai_filled:
            await emit(
                "status",
                f"[FALLBACK] AI filled {len(ai_filled)} field(s) on {url}: "
                f"{', '.join(ai_filled.keys())}",
                phase="extract",
                kind="ai_fallback_done",
                filled=list(ai_filled.keys()),
            )
        # AI returns duration as `duration_value` + `duration_unit` (kept
        # separate so the prompt can constrain each field independently).
        # The staged-course schema uses `duration` (real) +
        # `duration_term` (Year/Month/Week/...). Translate before merging
        # so AI-filled units don't silently drop on the floor. See B20.
        _apply_ai_duration_mapping(payload, ai_filled)

        # ── Restore YAML default_currency after AI fallback ───────────────
        # AI fallback (fill_missing) may fill fee_currency='AUD' for
        # non-AUD universities (GBP, NZD, etc.) because AUD is the model's
        # training-data default for "international tuition fee".
        # data_quality.py reads payload["fee_currency"] first when deciding
        # whether a fee is a domestic/CSP amount, so an AI-set 'AUD' on a
        # GBP university produces false-positive possible_domestic_fee and
        # annual_fee_too_low_warning alerts.
        # Guard: only override when YAML has an EXPLICIT non-AUD currency
        # (i.e. the operator intentionally configured it) AND the payload
        # currently holds 'AUD' (from AI) or nothing.
        # Both payload["fee_currency"] (AI-filled) AND payload["currency"]
        # (staged to DB and read by the review UI) are corrected so neither
        # the data-quality check nor the review table shows the wrong symbol.
        try:
            _af_fees_cfg = getattr(
                getattr(get_uni_config(), "extraction", None), "fees", None
            )
            _af_yaml_cur: str = (
                getattr(_af_fees_cfg, "default_currency", "AUD") or "AUD"
            )
            if _af_yaml_cur != "AUD":
                _ai_set_cur = payload.get("fee_currency") or "AUD"
                if _ai_set_cur == "AUD":
                    payload["fee_currency"] = _af_yaml_cur
                    # Also sync the "currency" key — this is what the review
                    # table reads (c.currency) and what the degree_level_defaults
                    # block later uses via setdefault().  Without this sync the
                    # review table still shows "A$" even when fee_currency=MYR.
                    if (payload.get("currency") or "AUD") == "AUD":
                        payload["currency"] = _af_yaml_cur
                    log.debug(
                        "[CURRENCY-FIX] %s: fee_currency/currency AUD → %s "
                        "(YAML default_currency override after AI fallback)",
                        url, _af_yaml_cur,
                    )
        except Exception:  # noqa: BLE001
            pass  # never break extraction on config-access error

        # ── Universal currency sync ────────────────────────────────────────
        # The CURRENCY-FIX guard above only fires when fee_currency was 'AUD'
        # (AI default). When AI correctly extracts MYR/GBP/NZD directly,
        # payload["currency"] (the DB column the review table reads) is never
        # set because only the degree_level_defaults fallback block
        # (setdefault) writes it — and that block only runs when no fee was
        # found on the page. Sync unconditionally here so both columns agree.
        _fc_sync = payload.get("fee_currency")
        if _fc_sync and not payload.get("currency"):
            payload["currency"] = _fc_sync

        # ── Post-ai_fallback study_mode correction (2026-05-31) ──────────
        # The first _rule_only_online / _low_conf_online correction earlier
        # in this function runs BEFORE ai_fallback fills course_location, so
        # _has_physical_location=False there even when a physical campus
        # exists. Run a second pass now that ai_fallback has had a chance to
        # populate course_location.  Uses the same logic but only fires when
        # study_mode is still 'Online' from rule-only evidence AND
        # ai_fallback has now confirmed a physical campus location.
        _post_ai_study_mode_evidence = [
            e for e in evidence if e["field_key"] == "study_mode"
        ]
        _post_ai_still_online = payload.get("study_mode") == "Online"
        _post_ai_has_location = bool((payload.get("course_location") or "").strip())
        _post_ai_rule_only = (
            _post_ai_still_online
            and bool(_post_ai_study_mode_evidence)
            and all(
                (e.get("method") or "").startswith("study_mode:rule")
                for e in _post_ai_study_mode_evidence
            )
            and _post_ai_has_location
        )
        if _post_ai_rule_only:
            from app.services.scraper.extractors.study_mode import derive_mode_from_location
            _derived = derive_mode_from_location(payload.get("course_location"))
            if _derived:
                payload["study_mode"] = _derived
                evidence.append({
                    "field_key": "study_mode",
                    "value": _derived,
                    "confidence": 0.65,
                    "method": "study_mode:location_derived",
                    "snippet": (
                        "Post-ai_fallback correction: rule-only 'Online' overridden "
                        f"by ai_fallback-confirmed physical campus: "
                        f"{(payload.get('course_location') or '')[:80]}"
                    ),
                })
                log.info(
                    "[STUDY_MODE POST-AI] course=%r — rule-only 'Online' corrected "
                    "to %r after ai_fallback filled course_location=%r",
                    payload.get("course_name") or url,
                    _derived,
                    payload.get("course_location"),
                )

        # ── UEL: on-campus study_mode override (2026-05-31) ─────────────────
        # UEL (www.uel.ac.uk) is a physical campus university (Docklands /
        # Stratford campuses). Every course page contains "apply online" /
        # "online student services" which leads Gemini (ai_fallback) to return
        # study_mode="Online". The study_mode:rule extractor is already
        # suppressed for UEL (no rule evidence), so the _post_ai_rule_only
        # gate above never fires — ALL evidence is ai_fallback, not rule-only.
        # Override to "On Campus" when:
        #   (a) host is UEL, AND
        #   (b) study_mode is still "Online", AND
        #   (c) no high-authority structural evidence confirms Online delivery
        #       (span_id_delivery / data_attribute / gemini_primary are the only
        #       methods trusted to identify genuinely online-only courses).
        _uel_host = urlparse(url).netloc.lower()
        if _uel_host in {"www.uel.ac.uk", "uel.ac.uk"}:
            _uel_high_auth = {"span_id_delivery", "data_attribute", "gemini_primary"}
            _uel_study_ev = [e for e in evidence if e.get("field_key") == "study_mode"]
            if (
                payload.get("study_mode") == "Online"
                and not any(e.get("method", "") in _uel_high_auth for e in _uel_study_ev)
            ):
                payload["study_mode"] = "On Campus"
                evidence.append({
                    "field_key": "study_mode",
                    "value": "On Campus",
                    "confidence": 0.70,
                    "method": "study_mode:host_default",
                    "snippet": (
                        "UEL on-campus override: 'Online' from ai_fallback with no "
                        "structural delivery evidence overridden to 'On Campus'"
                    ),
                })
                log.info(
                    "[UEL] study_mode 'Online' → 'On Campus' (no structural delivery evidence) for %s",
                    url,
                )

            # ── UEL: campus location normalisation (2026-05-31) ──────────────
            # Extractor often picks up "London" from generic page copy (footer,
            # "Study in London" marketing). UEL campuses are:
            #   • Docklands (Royal Docks, E16)
            #   • Stratford / University Square Stratford (E20)
            # Map extracted values to canonical names; blank out bare "London"
            # so auto-publish doesn't stage ambiguous location data.
            _uel_raw_loc = (payload.get("course_location") or "").strip()
            if _uel_raw_loc:
                _uel_loc_lc = _uel_raw_loc.lower()
                if "docklands" in _uel_loc_lc or "royal docks" in _uel_loc_lc:
                    _uel_norm_loc: str | None = "Docklands"
                elif "university square" in _uel_loc_lc or "uss" in _uel_loc_lc:
                    _uel_norm_loc = "University Square Stratford"
                elif "stratford" in _uel_loc_lc:
                    _uel_norm_loc = "Stratford"
                elif _uel_loc_lc.strip(" ,") in {
                    "london", "london uk", "london, uk",
                    "united kingdom", "uk", "east london",
                }:
                    _uel_norm_loc = None
                else:
                    _uel_norm_loc = _uel_raw_loc
                if _uel_norm_loc != _uel_raw_loc:
                    payload["course_location"] = _uel_norm_loc
                    evidence.append({
                        "field_key": "course_location",
                        "value": _uel_norm_loc or "",
                        "confidence": 0.80,
                        "method": "course_location:uel_campus_normalise",
                        "snippet": (
                            f"UEL campus normalisation: {_uel_raw_loc!r} → {_uel_norm_loc!r}"
                        ),
                    })
                    log.info(
                        "[UEL] course_location %r → %r for %s",
                        _uel_raw_loc, _uel_norm_loc, url,
                    )

        # ── Title-based "Online Learning" belt-and-suspenders (Bug 2 fix) ──────
        # When a course title explicitly contains "Online Learning" or "Fully
        # Online", the course is unambiguously online regardless of what the
        # study_mode rule, location_derived heuristic, or ai_fallback said.
        # This fires AFTER all other study_mode logic so it overrides even
        # rule:study_mode structural protection (the course's own name is the
        # most authoritative signal available).
        #
        # Affected cases: Malaysian university fee tables whose "Campus" column
        # header triggers study_mode:rule → "On Campus", then structural
        # protection blocks Gemini from overriding with the correct "Online"
        # value it reads from the course title ("MBA (Online Learning)").
        _cn_for_online = (payload.get("course_name") or "").lower()
        _ONLINE_TITLE_SIGNALS = (
            "online learning",
            "fully online",
            "100% online",
            "online only",
            "distance learning",
            "distance education",
        )
        if any(sig in _cn_for_online for sig in _ONLINE_TITLE_SIGNALS):
            _prev_mode = payload.get("study_mode")
            if _prev_mode != "Online":
                payload["study_mode"] = "Online"
                evidence.append({
                    "field_key": "study_mode",
                    "value": "Online",
                    "confidence": 0.90,
                    "method": "study_mode:title_keyword",
                    "snippet": (
                        f"Course title contains online keyword — overriding "
                        f"{_prev_mode!r}: {(payload.get('course_name') or '')[:80]}"
                    ),
                })
                log.info(
                    "[STUDY_MODE TITLE] course=%r — title keyword → 'Online' "
                    "(was %r) for %s",
                    payload.get("course_name") or url,
                    _prev_mode,
                    url,
                )

        # ── Federation JSON-block authoritative override (2026-05-10) ──
        # Federation embeds the canonical course summary as a JSON tree
        # inside <script>; the standard text-strip wipes it, so the
        # downstream regex extractors never see "4 years full-time" /
        # "Berwick (on campus)<br>Gippsland (on campus)<br>Mt Helen ...".
        # Net effect upstream: NULL durations on B Occupational Therapy
        # (Honours) / M Data Science / M Social Work, plus Gemini-
        # hallucinated locations ("Sydney" appearing on Berwick-only
        # programmes). Run AFTER ai_fallback / _apply_ai_duration_mapping
        # so the JSON value REPLACES whatever Gemini guessed (gates the
        # PDF-merge condition at L2742 are then irrelevant for these
        # rows; the JSON is strictly more reliable than either source).
        # Hostname-gated → no-op for every other uni.
        if _fed_json.is_federation_host(url):
            try:
                _fed_json.apply_overrides(
                    payload, html, url=url, rendered_html=rendered_html
                )
            except Exception as _fed_exc:  # noqa: BLE001 — never break a scrape
                log.warning("federation_json override failed on %s: %s", url, _fed_exc)
        # ── CQU JSON-block authoritative override (2026-05-11) ──
        # CQU is a NextJS / Sitecore site that ships every course's
        # canonical AIMSData inside __NEXT_DATA__. The text-strip wipes
        # it (page text drops to ~57 chars), so the regex extractors
        # pull intake-panel UI fragments ("2026", "Next start term
        # Anytime", "& 3 more") into course_location, IELTS goes 99%
        # NULL, and the fee extractor picks up the domestic CSP rate.
        # Hostname-gated → no-op for every other uni.
        from app.services.scraper.extractors import cqu_json as _cqu_json
        if _cqu_json.is_cqu_host(url):
            try:
                _cqu_json.apply_overrides(payload, html, url=url, evidence=evidence)
            except Exception as _cqu_exc:  # noqa: BLE001 — never break a scrape
                log.warning("cqu_json override failed on %s: %s", url, _cqu_exc)
        # ── La Trobe per-course JSON authoritative override (2026-05-13) ──
        # La Trobe per-course pages are SPA shells — fees, duration,
        # intake months, and per-course campus are loaded client-side
        # via a separate JSON document at
        #   /courses/data/{year}/{locale}/{campus}/{slug}?v=...
        # The static HTML lists every variant in an inline
        # ``allDetailUrls`` block but contains none of the actual
        # values, so the regex extractors return NULL on
        # international_fee / duration / intake_months for ~all 219
        # La Trobe rows and the central fee page also yields 0 records.
        # This override fetches the international JSON and replaces
        # those fields with the canonical values.
        # Hostname-gated → no-op for every other uni. Async because
        # it issues one extra HTTP request per course page.
        from app.services.scraper.extractors import latrobe_json as _latrobe_json
        if _latrobe_json.is_latrobe_host(url):
            try:
                await _latrobe_json.apply_overrides(
                    payload,
                    html,
                    url=url,
                    evidence=evidence,
                    prefetched_doc=_latrobe_prefetched_doc,
                    prefetched_url=_latrobe_prefetched_url,
                    local_concurrency_limit=_scrape_do_local_concurrency,
                )
            except Exception as _ltu_exc:  # noqa: BLE001 — never break a scrape
                log.warning("latrobe_json override failed on %s: %s", url, _ltu_exc)
        # MIT (Melbourne Institute of Technology) per-course fee table
        # override.  MIT's per-course page genuinely contains no
        # international fee — Gemini sees the "DomesticInternational"
        # toggle text and returns null.  The complete fee schedule
        # lives in a single HTML table at /study-with-us/tuition-fees
        # (international accordion section).  Without this override,
        # the central-page generic parser broadcast a single wrong fee
        # (A$13,320 = 2027 Master of ICT Research per-trimester)
        # onto every course as "Full Course" — confirmed by the
        # 2026-05-13 user-reported staged data showing 22 wrong rows.
        # Hostname-gated; one cached fetch per worker.
        # ── MIT title-major course-name override (2026-05-13) ──
        # MIT publishes one URL per major specialisation
        # (e.g. master-networking-project-management) but the on-page <h1>
        # always reads the bare program name ("Master of Networking"),
        # so the standard h1-based course_name extractor produces 6
        # identical staged rows for the 6 Master-of-Networking variants.
        # The page <title> tag carries the major
        # ("Master of Networking | major in Project Management"), so this
        # extractor pulls the major from the title and rewrites
        # course_name to the canonical "<base> - Major in <Major>" form
        # so each variant has a unique, parseable name.
        # MUST run BEFORE mit_fees so the central fee-table lookup can
        # exact-match on the now-fully-qualified course_name.
        from app.services.scraper.extractors import mit_course_name as _mit_cn
        if _mit_cn.is_mit_host(url):
            try:
                _mit_cn.apply_overrides(
                    payload, html, url=url, evidence=evidence
                )
            except Exception as _mit_cn_exc:  # noqa: BLE001 — never break a scrape
                log.warning("mit_course_name override failed on %s: %s", url, _mit_cn_exc)
        from app.services.scraper.extractors import mit_fees as _mit_fees
        if _mit_fees.is_mit_host(url):
            try:
                await _mit_fees.apply_overrides(
                    payload, url=url, evidence=evidence
                )
            except Exception as _mit_exc:  # noqa: BLE001 — never break a scrape
                log.warning("mit_fees override failed on %s: %s", url, _mit_exc)
        # ── Torrens per-course JSON-LD location override (2026-05-13) ──
        # Torrens course pages publish a marketing-style "Campus locations"
        # header listing ALL Torrens-network campuses
        # ("Sydney, Melbourne, Brisbane, Adelaide, Online") on EVERY course,
        # even when the course is only delivered at 1-2 of them.  The
        # visible-text extractor was picking up that brand statement
        # verbatim and stamping all 4 cities onto every staged row,
        # producing the user-reported 2026-05-13 bug where Education and
        # Cybersecurity courses (only at 2 campuses) were shown as offered
        # at all 4.  The page also carries a JSON-LD <Course> block whose
        # hasCourseInstance[] entries give the actual per-campus
        # availability — that's what we use to REPLACE course_location.
        # Hostname-gated; pure parse, no extra HTTP request.
        from app.services.scraper.extractors import torrens_json as _torrens_json
        if _torrens_json.is_torrens_host(url):
            try:
                _torrens_json.apply_overrides(
                    payload, html, url=url, evidence=evidence
                )
            except Exception as _tor_exc:  # noqa: BLE001 — never break a scrape
                log.warning("torrens_json override failed on %s: %s", url, _tor_exc)
        # ── VU per-course course-card location override (2026-05-14) ──
        # VU course pages contain a footer Indigenous-acknowledgement
        # of country plus a CRICOS registration line that both list
        # "Sydney", "Melbourne", and "Brisbane" as brand-chrome.  The
        # bag-of-text location fallback was picking those up and
        # stamping them as the course location, even on courses
        # delivered at a single campus (user-reported: SIT50422
        # Diploma of Hospitality Management staged with location
        # "Sydney, Melbourne, Brisbane" instead of "Footscray
        # Nicholson Campus").  This override REPLACE-writes
        # course_location with the value from VU's structured
        # "Course essentials" panel
        # (.vu-course-essentials-content-label "Location" → matching
        # .vu-course-essentials-content-value).  Hostname-gated;
        # pure parse, no extra HTTP request.
        from app.services.scraper.extractors import vu_course_card as _vu_cc
        if _vu_cc.is_vu_host(url):
            try:
                _vu_cc.apply_overrides(
                    payload, html, url=url, evidence=evidence
                )
            except Exception as _vu_exc:  # noqa: BLE001 — never break a scrape
                log.warning("vu_course_card override failed on %s: %s", url, _vu_exc)
        # ── London Met brand-chrome scrub (2026-05-14) ─────────────
        # London Met staged 5 ghost rows out of 322 discovered, all
        # carrying international_fee=£10,000/Annual sourced from a
        # "Postgraduate Loan of over £10,000" advert in the page
        # chrome (NOT a real fee), and 2 of those 5 had
        # course_location set to the literal string "London
        # Metropolitan University" from the brand chrome.  This
        # scrub NULLs the loan-banner false-positive fee triple
        # AND the brand-name location.  After scrubbing, those
        # ghost rows fail the existing should_stage_course
        # ``no_international_fee`` gate and are dropped — the
        # correct outcome until a separate Fees & Funding sub-page
        # fetcher is built (real London Met fees do not live on
        # the per-course page; that is follow-up work).
        # Hostname-gated; pure parse, no extra HTTP request.
        from app.services.scraper.extractors import londonmet_chrome_scrub as _lm_scrub
        if _lm_scrub.is_londonmet_host(url):
            try:
                _lm_applied = _lm_scrub.apply_overrides(
                    payload, html, url=url, evidence=evidence
                )
                if _lm_applied.get("is_domestic_only"):
                    # The page has the entry-point selector but no
                    # data-fee-type="International" option — this course is
                    # UK-only.  Reject it the same way as _DOMESTIC_ONLY_RE.
                    payload["domestic_only"] = True
                    await emit(
                        "status",
                        f"[LM DOMESTIC ONLY] {url} — no International entry-point option; skipping",
                        phase="extract",
                        kind="domestic_only_skip",
                        url=url,
                    )
                    return {"url": url, "payload": payload, "evidence": evidence}
            except Exception as _lm_exc:  # noqa: BLE001 — never break a scrape
                log.warning("londonmet_chrome_scrub failed on %s: %s", url, _lm_exc)
        # Build a lookup of which fields already have evidence from a
        # non-ai_fallback method so the guard below can log drop attempts.
        # First-write-wins: the earliest non-ai_fallback entry for each field
        # is the authoritative source method for that field.
        _prior_method: dict[str, str] = {}
        for _ev in evidence:
            _fk = _ev.get("field_key", "")
            _m = _ev.get("method", "")
            if _fk and _m and _m not in ("ai_fallback",) and _fk not in _prior_method:
                _prior_method[_fk] = _m
        # Week 1 Prompt 8 — page-text snapshot used to validate every
        # AI fallback value below.  Computed once per course rather than
        # per field.  Falls back to the raw HTML lower-cased when the
        # html_to_text helper is unavailable (defensive — should never
        # happen in production but keeps unit tests cheap).
        try:
            from app.services.scraper.extractors._text import html_to_text as _html_to_text
            _ai_page_text = _html_to_text(html or "")
        except Exception:  # noqa: BLE001
            _ai_page_text = (html or "")
        from app.services.scraper.extractors.ai_fallback import (
            validate_ai_fallback_value as _validate_ai_fallback_value,
        )
        # BCU FALLBACK AI location suppression — must be computed once before the loop.
        # BCU testimonial / graduate-story sections contain person names that
        # the FALLBACK AI (Gemini) mistakes for campus names when the structured
        # panel has no Location row.  If the structural cascade filled the field
        # (method starts with "location."), we preserve it; if the cascade found
        # nothing, the field stays blank — no AI fallback for location on BCU.
        _is_bcu_host_fb = "bcu.ac.uk" in (url or "").lower()
        for k, v in ai_filled.items():
            # Discard chrome text returned by the FALLBACK AI for location fields.
            # UTAS pages have "Key Information Entry requirements Course rules"
            # immediately after the Location heading; the AI sometimes copies it
            # verbatim.  Dropping it keeps course_location=None so the online-only
            # rejection filter can fire correctly.
            if k in ("location_text", "course_location") and isinstance(v, str) and _is_location_chrome(v):
                continue
            # BCU: suppress FALLBACK AI from filling location — person names in
            # testimonials pollute the value when the keyfacts panel has no Location.
            if k in ("location_text", "course_location") and _is_bcu_host_fb:
                continue
            # Virtual-only location guard.  Newcastle Master of Nursing and
            # similar multi-campus pages render "Online | Newcastle" as a
            # JS-injected radio-toggle group; when the browser pass times
            # out before the toggles hydrate, the AI fallback sees only the
            # default-selected "Online" pill and returns course_location=
            # "Online" (or "Online (Newcastle)").  `_sanitise_for_display`
            # would later strip the entire value via `_REMOVE_VIRTUAL` and
            # the dashboard column ends up blank.  Reject the AI value here
            # so the structural toggle reader (run on the next scrape, or
            # on a retry with a longer browser settle) can fill it cleanly.
            # Only fires when EVERY comma-split part is a virtual token; a
            # mixed value like "Newcastle, Online" is kept and downstream
            # sanitise drops the "Online" part, preserving "Newcastle".
            if (
                k in ("location_text", "course_location")
                and isinstance(v, str)
                and v.strip()
            ):
                _parts = [p.strip() for p in v.split(",") if p.strip()]
                from app.services.scraper.extractors.location import (
                    _REMOVE_VIRTUAL as _LOC_REMOVE_VIRTUAL,
                )
                if _parts and all(_LOC_REMOVE_VIRTUAL.fullmatch(p) for p in _parts):
                    log.info(
                        "[AI_FALLBACK REJECT] %s=%r — virtual-only "
                        "(every comma part matches _REMOVE_VIRTUAL); "
                        "rejecting on %s", k, v, url,
                    )
                    continue
            # Belt-and-braces override block: AI fallback can only fill fields
            # that have no prior evidence from a higher-authority method.
            # payload.setdefault() already prevents payload overwrite, but this
            # guard also logs the attempt so it is auditable in the Celery log.
            existing_method = _prior_method.get(k)
            if existing_method and existing_method not in ("ai_fallback", None):
                log.info(
                    "[AI_FALLBACK] dropping %s=%r — already set by %s on %s",
                    k, v, existing_method, url,
                )
                continue
            # Week 1 Prompt 8 — verbatim page-text validation.  Drop any
            # AI fallback value whose digits/score/tokens cannot be located
            # in the rendered page text.  Catches model hallucinations the
            # Prompt-5 rules block would otherwise let through.
            if not _validate_ai_fallback_value(k, v, _ai_page_text):
                log.info(
                    "[AI_FALLBACK REJECT] %s=%r — not found in page text: %s",
                    k, v, url,
                )
                continue
            payload.setdefault(k, v)
            evidence.append(
                {
                    "field_key": k,
                    "value": v,
                    "confidence": 0.5,
                    "method": "ai_fallback",
                    # enforce_source_evidence requires both source_url and snippet
                    # to preserve a critical field before staging.
                    "source_url": url,
                    "snippet": f"ai_fallback: {k}={v}",
                }
            )

    # Post-AI mode derivation deliberately removed.
    # Inferring "On Campus" from course_location alone produces misleading data:
    # a location field is evidence of WHERE the course runs, not HOW it is
    # delivered. Pages that never mention a delivery mode should stage with an
    # empty study_mode rather than a fabricated "On Campus" value.
    # The Review UI will surface these as a completeness gap for human review.

    # ── Study-mode field trace ────────────────────────────────────────────────
    # Emits a single diagnostic event so operators can follow the mode value
    # through the full pipeline without trawling the evidence table.
    if emit:
        _mode_ev = [e for e in evidence if e.get("field_key") == "study_mode"]
        _mode_method = _mode_ev[-1].get("method", "none") if _mode_ev else "none"
        await emit(
            "status",
            f"[FIELD TRACE] study_mode={payload.get('study_mode')!r} "
            f"location={payload.get('course_location')!r} "
            f"method={_mode_method} url={url}",
            phase="extract",
            kind="field_trace_study_mode",
            url=url,
            extracted_study_mode=payload.get("study_mode"),
            payload_study_mode=payload.get("study_mode"),
            method=_mode_method,
        )

    # ── prefer_location_over_online_keyword safety gate ──────────────────────
    # When YAML sets extraction.study_mode.prefer_location_over_online_keyword:
    # true, a bare-keyword "Online" result is suppressed if a non-empty
    # course_location was also found (implying the course has a physical campus).
    # This prevents courses whose page incidentally says "available online"
    # in a side-bar from being staged with study_mode=Online while also
    # carrying a concrete campus location.
    if payload.get("study_mode") == "Online" and (payload.get("course_location") or "").strip():
        try:
            from app.services.scraper.config.context import get_uni_config as _get_polok
            _polok_uc = _get_polok()
            if _polok_uc is not None:
                _polok_sm = getattr(
                    getattr(_polok_uc, "extraction", None), "study_mode", None
                )
                if _polok_sm is not None and getattr(
                    _polok_sm, "prefer_location_over_online_keyword", False
                ):
                    # Only suppress low-confidence / bare-keyword Online results.
                    _sm_ev = [e for e in evidence if e.get("field_key") == "study_mode"]
                    _sm_conf = _sm_ev[-1].get("confidence") if _sm_ev else None
                    if _sm_conf is None or _sm_conf < 0.7:
                        _old_mode = payload.pop("study_mode", None)
                        log.info(
                            "[STUDY_MODE] prefer_location_over_online_keyword: "
                            "suppressed %r → None (location=%r, confidence=%s) %s",
                            _old_mode,
                            payload.get("course_location"),
                            _sm_conf,
                            url,
                        )
                        if emit:
                            await emit(
                                "status",
                                f"[STUDY_MODE] prefer_location_over_online_keyword: "
                                f"suppressed bare Online (location={payload.get('course_location')!r}) {url}",
                                phase="extract",
                                kind="study_mode_location_override",
                                url=url,
                            )
        except Exception:
            pass

    # ── distance_learning_with_campus_is_blended upgrade ─────────────────────
    # When YAML sets extraction.study_mode.distance_learning_with_campus_is_blended:
    # true, a study_mode='Online' result derived from a 'distance learning'
    # pattern is upgraded to 'Blended' when a physical campus location was
    # also extracted.  Oxford Brookes lists "Distance learning" alongside
    # physical campuses in the same Location section; the course is available
    # both online and in-person, so the correct mode is 'Blended'.
    if payload.get("study_mode") == "Online" and (payload.get("course_location") or "").strip():
        try:
            from app.services.scraper.config.context import get_uni_config as _get_dlcb
            _dlcb_uc = _get_dlcb()
            if _dlcb_uc is not None:
                _dlcb_sm = getattr(
                    getattr(_dlcb_uc, "extraction", None), "study_mode", None
                )
                if _dlcb_sm is not None and getattr(
                    _dlcb_sm, "distance_learning_with_campus_is_blended", False
                ):
                    payload["study_mode"] = "Blended"
                    evidence.append({
                        "field_key": "study_mode",
                        "value": "Blended",
                        "confidence": 0.8,
                        "method": "study_mode:distance_learning_with_campus_is_blended",
                        "snippet": (
                            "distance_learning_with_campus_is_blended=True: "
                            "Online upgraded to Blended — physical campus also "
                            f"extracted: {(payload.get('course_location') or '')[:80]}"
                        ),
                    })
                    log.info(
                        "[STUDY_MODE] distance_learning_with_campus_is_blended: "
                        "upgraded Online → Blended (location=%r) %s",
                        payload.get("course_location"),
                        url,
                    )
        except Exception:
            pass

    # ── CRICOS code extraction from the course page ──────────────────────────
    # Extract CRICOS code early so it is available during PDF row matching.
    # ``cricos_code`` is stored in the payload and mapped to the DB column by
    # the staging layer automatically (hasattr(ScrapedCourse, "cricos_code")).
    # Only runs for AU country scrapes; harmless but no-op for non-AU pages.
    if "cricos_code" not in payload or not payload.get("cricos_code"):
        try:
            from app.services.scraper.extractors.cricos_code import (
                extract_cricos_code,
                extract_cricos_code_from_html_structured,
            )

            _cricos_html = html or ""
            _cricos_text = ""
            try:
                from bs4 import BeautifulSoup as _BS

                _cricos_text = _BS(_cricos_html, "html.parser").get_text(" ", strip=True)
            except Exception:  # noqa: BLE001
                pass

            _cricos_val = extract_cricos_code_from_html_structured(
                _cricos_html
            ) or extract_cricos_code(_cricos_html, _cricos_text)

            if _cricos_val:
                payload["cricos_code"] = _cricos_val
                evidence.append(
                    {
                        "field_key": "cricos_code",
                        "value": _cricos_val,
                        "confidence": 0.95,
                        "method": "regex:cricos",
                        "snippet": f"CRICOS code extracted from course page: {_cricos_val}",
                    }
                )
                log.info("[CRICOS] extracted %s from %s", _cricos_val, url)
        except Exception as _cricos_exc:  # noqa: BLE001
            log.debug("cricos_code extraction failed on %s: %s", url, _cricos_exc)

    # Last-resort: backfill from university-level PDFs (fee schedule,
    # admissions/IELTS policy). Only fills keys still missing after
    # page extractors + AI. Each filled key emits a provenance row that
    # credits the source PDF URL.
    if uni_pdf_data:
        fee_block = uni_pdf_data.get("fee") or {}
        english_block = uni_pdf_data.get("english") or {}
        fees_pdf_url = uni_pdf_data.get("fees_pdf_url")
        reqs_pdf_url = uni_pdf_data.get("requirements_pdf_url")

        # NEW: prefer the per-course row from the fee schedule PDF over
        # the uni-wide value. ``fee_by_course`` is populated when the
        # PDF was a multi-row schedule (ASA, Torrens, …). Matching is
        # done by CRICOS-first lookup (when the course page exposes a
        # CRICOS code) then distinctive course-name token overlap — see
        # :func:`match_course_in_pdf_table`. When a row matches, it
        # *replaces* ``fee_block`` for this course and is tagged with a
        # different provenance method so reviewers can tell per-course
        # rows apart from the old uni-wide stamp.
        fee_by_course = uni_pdf_data.get("fee_by_course") or {}
        fee_method = "uni_pdf:fees"
        # When the schedule PDF parses to a real per-course table (≥2
        # rows — same threshold ``_pick_per_course_amounts`` uses to
        # consider a table "real"), the per-course path becomes the
        # source of truth for this university's fees. Falling back to
        # the uni-wide stamp for unmatched courses re-creates the
        # original failure mode this PR was built to fix (every course
        # gets the same number) — Torrens v1 symptom. We instead leave
        # the fee NULL so the dashboard surfaces it as missing rather
        # than silently wrong.
        per_course_table_active = len(fee_by_course) >= 2
        if fee_by_course:
            from app.services.scraper.pipelines.university_pdfs import (
                match_course_in_pdf_table,
            )

            # Per-uni PDF course-name aliases (YAML: extraction.fees.
            # course_pdf_aliases). Lets an operator map a DB course name
            # to its real PDF row title when the row carries a qualifier
            # the DB name lacks (e.g. Torrens "Master of Design" →
            # "Master of Design (Non-Cognate)"). Empty for every uni
            # that has not opted in, so the global behaviour is unchanged.
            # NOTE: do NOT re-import get_uni_config here — the module-level
            # import at the top of this file already binds the name. A local
            # ``from ... import get_uni_config`` would mark the symbol as a
            # function-local for the ENTIRE extract_course() body, and the
            # earlier call at line ~1750 would then raise UnboundLocalError
            # ("cannot access local variable 'get_uni_config' …").
            _pdf_aliases: dict[str, str] = {}
            try:
                _cfg = get_uni_config()
                if _cfg is not None:
                    _pdf_aliases = dict(
                        getattr(_cfg.extraction.fees, "course_pdf_aliases", {}) or {}
                    )
            except Exception:  # noqa: BLE001
                _pdf_aliases = {}

            matched_row, _match_suffix = match_course_in_pdf_table(
                payload.get("course_name") or "",
                fee_by_course,
                cricos_code=payload.get("cricos_code"),
                course_pdf_aliases=_pdf_aliases,
            )
            if matched_row:
                log.info(
                    "[FEE] per-course PDF row matched for %r via %s: $%s (%s)",
                    payload.get("course_name"),
                    _match_suffix,
                    matched_row.get("international_fee"),
                    matched_row.get("fee_term"),
                )
                fee_block = matched_row
                fee_method = (
                    "uni_pdf:cricos_match:fees"
                    if _match_suffix == "cricos_match"
                    else "uni_pdf:fees:per_course"
                )
                # 2026-05-10: when the matched PDF row also carries a
                # trailing duration column (per-uni regex extension —
                # currently Federation only), use it as a last-resort
                # fallback for payload duration. We only fill when the
                # per-course HTML extractor came back NULL — never
                # overwrite a real page-derived value. Empty / None for
                # every uni whose pdf_row_pattern has no duration group,
                # so this branch is a no-op there.
                _pdf_dur = matched_row.get("duration_pdf")
                _pdf_dur_term = matched_row.get("duration_term_pdf")
                if (
                    _pdf_dur is not None
                    and _pdf_dur_term
                    and payload.get("duration") in (None, "")
                ):
                    payload["duration"] = _pdf_dur
                    payload["duration_term"] = _pdf_dur_term
                    log.info(
                        "[DURATION] filled from PDF row for %r: %s %s",
                        payload.get("course_name"),
                        _pdf_dur,
                        _pdf_dur_term,
                    )
            elif per_course_table_active:
                # No per-course row matched, but the schedule itself
                # parses cleanly. Suppress the uni-wide stamp so we
                # don't pollute every unmatched course with the same
                # (likely-wrong) number. Leave the rest of the PDF
                # block (english requirements, etc.) intact.
                log.info(
                    "[FEE] no per-course PDF row for %r — leaving fee NULL "
                    "(schedule has %d rows; uni-wide stamp suppressed)",
                    payload.get("course_name"),
                    len(fee_by_course),
                )
                fee_block = {}

        # Diff item H (MIGRATION_AUDIT.md §6): gate the uni-wide fee PDF
        # fallback on course-specific evidence. Without this, every
        # Bachelor on the catalogue inherits the same single dollar
        # amount from the generic /international-fees page (Torrens v1
        # symptom).
        #
        # The guard is text-based, so we can only run it when the loader
        # surfaces ``fee_text`` (the raw extracted PDF text we'd grep for
        # course-name tokens). Today ``load_university_pdf_data`` only
        # returns the parsed numbers, not the source text — wiring that
        # through is a follow-up. Until then, fail-OPEN when no text is
        # available (preserves v1 behavior) and fail-CLOSED only when the
        # caller has supplied text we can actually evaluate against.
        # (Code-review feedback on PR-1: avoid silently dropping every
        # uni-PDF fallback now that we lack the text channel.)
        # The guard is intentionally bypassed when we have a per-course
        # row — that row IS the course-specific evidence the guard is
        # asking for, so applying the guard a second time would be
        # double-jeopardy.
        fee_search_text = uni_pdf_data.get("fee_text") or ""
        fee_amount = fee_block.get("international_fee")
        unique_amounts = (
            [int(fee_amount)] if isinstance(fee_amount, (int, float)) else []
        )
        trust_fee_fallback = True
        if (
            fee_block
            and fees_pdf_url
            and fee_search_text
            and fee_method == "uni_pdf:fees"  # only guard the uni-wide stamp
        ):
            try:
                trust_fee_fallback = should_trust_generic_university_fee_fallback(
                    fees_pdf_url,
                    payload.get("course_name") or "",
                    fee_search_text,
                    unique_amounts,
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("fee-guard failed for %s: %s", fees_pdf_url, exc)
                trust_fee_fallback = False
            if not trust_fee_fallback:
                log.info(
                    "[FEE] uni-PDF fallback skipped for %s — no course-specific evidence",
                    payload.get("course_name"),
                )

        # Per-uni knob ``pdf_overrides_page_regex``: when True and we have
        # a per-course PDF row match, treat the PDF as authoritative for
        # the four fee slots (international_fee / currency / fee_term /
        # fee_year). Overwrites whatever a page regex / Gemini prose grab
        # wrote earlier — fixes Torrens-style marketing copy like
        # "will cost an international student $82,800" overriding the
        # official $31,600/Annual figure from the 2026 fee schedule PDF.
        # Default False preserves course-page-wins for every other uni.
        _pdf_overrides_regex = False
        if fee_method.startswith("uni_pdf:") and fee_method != "uni_pdf:fees":
            try:
                _cfg = get_uni_config()
                if _cfg is not None:
                    _pdf_overrides_regex = bool(
                        getattr(
                            _cfg.extraction.fees,
                            "pdf_overrides_page_regex",
                            False,
                        )
                    )
            except Exception:  # noqa: BLE001
                _pdf_overrides_regex = False
        _PDF_AUTHORITATIVE_FEE_KEYS = {
            "international_fee",
            "currency",
            "fee_term",
            "fee_year",
        }

        if trust_fee_fallback:
            for k, v in fee_block.items():
                if v is None:
                    continue
                # Empty-aware: only skip when the page already extracted
                # a *real* value (matches per-course modal / VIT static /
                # sibling cache). Treats None / "" / 0 as "still empty"
                # so a stray placeholder from any upstream merge site
                # never blocks the PDF backfill. Course-page-wins still
                # holds — see step-1 extractors which strip Nones before
                # setdefault, so a real extraction is always truthy.
                #
                # Per-uni override: when ``pdf_overrides_page_regex`` is
                # enabled AND we matched a real per-course PDF row, the
                # four fee slots are overwritten unconditionally — the
                # PDF schedule wins over any page-derived value.
                _override = (
                    _pdf_overrides_regex
                    and k in _PDF_AUTHORITATIVE_FEE_KEYS
                )
                if not _override and payload.get(k) not in (None, "", 0):
                    continue
                # When overriding, mark the prior evidence row as
                # superseded so the Evidence Review still shows the
                # page-regex / Gemini value but flags it as not winning.
                if _override and payload.get(k) not in (None, "", 0):
                    for _prior_ev in evidence:
                        if _prior_ev.get("field_key") == k:
                            _prior_ev["decision_status"] = "superseded"
                payload[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": 0.7,
                        "method": fee_method,
                        # source_url: PDF URL when known; course-page URL as
                        # provenance fallback so enforce_source_evidence never
                        # drops a proven field just because the PDF URL wasn't
                        # recorded (Bug: snippet was the URL → double-URL in UI).
                        "source_url": fees_pdf_url or url,
                        "snippet": f"uni_pdf fee: {k}={v}",
                    }
                )
        # Course-page-wins: only fill empty english slots from the
        # uni-PDF backfill. The PDF's value gets stored verbatim — no
        # bump or other heuristic.
        #
        # Tier-mismatch guard (2026-05-10 — ASA Masters CAE/DET bug):
        # The PDF row is internally consistent — IELTS, PTE, TOEFL, CAE
        # and DET all describe ONE entry tier. ASA's central admissions
        # PDF only publishes the bachelor tier (IELTS 6.0 / CAE 169 /
        # DET 100). When a master course already has a higher IELTS
        # from per_course_vision (e.g. 6.5 from MaSTER.png), filling
        # its empty CAE/DET slots from this PDF row imports
        # bachelor-tier scores onto a master course (CAE 169 / DET 100
        # instead of ~176 / ~110). Skip the entire english block when
        # the PDF's IELTS tier disagrees with the course's IELTS — better
        # to leave CAE/DET null than to write wrong values that look
        # authoritative.
        _pdf_ielts = english_block.get("ielts_overall")
        _payload_ielts = payload.get("ielts_overall")
        _tier_mismatch = (
            _pdf_ielts is not None
            and _payload_ielts not in (None, "", 0)
            and abs(float(_payload_ielts) - float(_pdf_ielts)) > 0.25
        )
        if _tier_mismatch:
            log.info(
                "[UNI_PDF SKIP english] %s: course IELTS=%s != PDF IELTS=%s "
                "— skipping english backfill (tier mismatch); leaving "
                "non-IELTS slots null rather than importing wrong-tier "
                "CAE/DET/PTE/TOEFL from %s",
                url, _payload_ielts, _pdf_ielts, reqs_pdf_url or "(unknown PDF)",
            )
        else:
            for k, v in english_block.items():
                if v is None:
                    continue
                # Empty-aware: see fee-block comment above.
                if payload.get(k) not in (None, "", 0):
                    continue
                payload[k] = v
                evidence.append(
                    {
                        "field_key": k,
                        "value": v,
                        "confidence": 0.7,
                        "method": "uni_pdf:requirements",
                        # source_url: PDF URL when known; course-page URL as
                        # provenance fallback so enforce_source_evidence never
                        # drops english fields just because reqs_pdf_url is
                        # absent (fixes MIT SW missing-english bug).
                        # snippet is always descriptive text — never the URL —
                        # so it doesn't duplicate the source link in Evidence Review.
                        "source_url": reqs_pdf_url or url,
                        "snippet": f"uni_pdf english: {k}={v}",
                    }
                )

        # Phase 6: merge academic entry requirements (ATAR, GPA, prior degree,
        # work experience, portfolio, interview) from the requirements PDF into
        # the other_requirement field when it is currently empty.
        # Provenance is tagged as "uni_pdf:entry_requirements" so reviewers can
        # distinguish it from page-extracted or Gemini-extracted values.
        _p6_er_data = uni_pdf_data.get("entry_requirements") or {}
        if _p6_er_data and not (payload.get("other_requirement") or "").strip():
            try:
                from app.services.scraper.entry_req_extractor import EntryRequirement as _P6ER
                _p6_er = _P6ER.from_dict(_p6_er_data)
                _p6_summary = _p6_er.to_summary_text()
                if _p6_summary:
                    payload["other_requirement"] = _p6_summary
                    evidence.append(
                        {
                            "field_key": "other_requirement",
                            "value": _p6_summary,
                            "confidence": min(0.80, _p6_er_data.get("confidence", 0.5)),
                            "method": "uni_pdf:entry_requirements",
                            "source_url": uni_pdf_data.get("requirements_pdf_url") or url,
                            "snippet": f"entry_req PDF: {_p6_summary[:80]}",
                        }
                    )
            except Exception as _p6_exc:  # noqa: BLE001
                log.debug("[P6] entry_req merge failed: %s", _p6_exc)

    # ── Pathway program detection ─────────────────────────────────────────────
    # Pathway / preparatory programs (Foundation Studies, ELICOS, UniPrep,
    # bridging courses) have lower English admission requirements than standard
    # academic degrees.  They must NOT inherit the university's main IELTS from
    # the central English requirements page.
    # Detection runs here — after course_name and degree_level are in the
    # payload — so both signals are available, and before the central-data
    # fallback that would wrongly apply the university-wide IELTS.
    try:
        from app.services.scraper.pathway_detection import is_pathway_program as _is_pathway
        _pathway_flag = _is_pathway(
            payload.get("course_name"),
            degree_level=payload.get("degree_level"),
        )
    except Exception:  # noqa: BLE001
        _pathway_flag = False
    if _pathway_flag and not payload.get("is_pathway"):
        payload["is_pathway"] = True
        if emit:
            await emit(
                "status",
                f"[PATHWAY] {payload.get('course_name', url)[:50]} — "
                f"detected as pathway program; central English requirements will be skipped",
                phase="extract",
                kind="pathway_detected",
                url=url,
            )

    # Bug 2: central-pages fallback — applies fees and English requirements
    # pre-fetched from a university's central fee/admissions page when
    # per-course extractors, AI, and PDF backfill all left these slots empty.
    # This is the absolute last-resort path: confidence ceiling is 0.45 for
    # fees (lower than every earlier stage) and 0.50 for English requirements
    # (central admissions pages are authoritative for English policy, but we
    # still want course-page data and sibling cache to win when present).
    if central_data:
        try:
            from app.services.scraper.central_pages import match_central_fee

            _central_fees: list = central_data.get("fees") or []
            _central_english: dict = central_data.get("english") or {}
            _central_fee_url: str | None = central_data.get("fee_page_url")
            _central_eng_url: str | None = central_data.get("english_page_url")
            # Per-level source URLs (populated when separate UG/PG pages are configured).
            # Used in evidence snippets so reviewers can navigate to the exact page.
            _central_eng_url_ug: str | None = central_data.get("english_page_url_ug")
            _central_eng_url_pg: str | None = central_data.get("english_page_url_pg")

            # ── Fee fallback ─────────────────────────────────────────────
            _fee_slots = ("international_fee", "domestic_fee", "currency", "fee_term", "fee_year")
            _fee_missing = any(payload.get(k) in (None, "", 0) for k in ("international_fee",))
            if _fee_missing and _central_fees:
                _course_name_for_fee = payload.get("course_name") or ""
                matched, _fee_confidence = match_central_fee(
                    _course_name_for_fee,
                    _central_fees,
                    degree_level=payload.get("degree_level"),
                )
                if matched and _fee_confidence != "none":
                    _prog = matched.get("program_pattern", "?")
                    if _fee_confidence == "bucket":
                        # Check per-uni YAML opt-in: allow_bucket_match
                        _allow_bucket = False
                        try:
                            _bucket_cfg = get_uni_config()
                            if _bucket_cfg is not None:
                                _allow_bucket = bool(
                                    _bucket_cfg.extraction.fees.allow_bucket_match
                                )
                        except Exception:  # noqa: BLE001
                            pass
                        if _allow_bucket:
                            # Bucket match applied — set fee with low confidence
                            # and a scrape warning so reviewers know it's imprecise.
                            _bv = matched.get("international_fee")
                            if _bv not in (None, "", 0):
                                payload["international_fee"] = _bv
                                for _bk, _bsk in (
                                    ("international_fee", "international_fee"),
                                    ("currency", "currency"),
                                    ("fee_term", "per"),
                                ):
                                    _bval = matched.get(_bsk)
                                    if _bval not in (None, "", 0) and payload.get(_bk) in (None, "", 0):
                                        payload[_bk] = _bval
                                evidence.append({
                                    "field_key": "international_fee",
                                    "value": _bv,
                                    "confidence": 0.30,
                                    "method": "central_page:fees:bucket",
                                    "source_url": _central_fee_url or url,
                                    "snippet": f"central_page bucket fee (degree-level only): {_prog}",
                                })
                                _bucket_applied = (
                                    f"[FEE bucket] course={_course_name_for_fee!r} — "
                                    f"bucket fee applied (row={_prog!r}, fee={_bv}); "
                                    f"allow_bucket_match=true in YAML"
                                )
                                payload.setdefault("scrape_warnings", [])
                                payload["scrape_warnings"].append(_bucket_applied)
                                if emit:
                                    await emit(
                                        "status",
                                        _bucket_applied,
                                        phase="fallback",
                                        kind="central_fee_bucket_applied",
                                        url=url,
                                        matched_program=_prog,
                                    )
                        else:
                            # Bucket fallback: degree-level match only — too imprecise
                            # to apply silently.  Log a scrape warning and leave fee blank.
                            _bucket_warn = (
                                f"[FEE skip] course={_course_name_for_fee!r} — "
                                f"only bucket match available (row={_prog!r}, "
                                f"fee={matched.get('international_fee')}); "
                                f"fee left blank to avoid wrong data"
                            )
                            payload.setdefault("scrape_warnings", [])
                            payload["scrape_warnings"].append(_bucket_warn)
                            if emit:
                                await emit(
                                    "status",
                                    _bucket_warn,
                                    phase="fallback",
                                    kind="central_fee_bucket_skip",
                                    url=url,
                                    matched_program=_prog,
                                )
                    else:
                        # Confident name match (exact / high / medium) — apply fee.
                        _confidence_numeric = (
                            0.70 if _fee_confidence == "exact" else
                            0.55 if _fee_confidence == "high" else
                            0.45  # medium
                        )
                        _filled_fee_keys: list[str] = []
                        for _k, _src_k in (
                            ("international_fee", "international_fee"),
                            ("domestic_fee", "domestic_fee"),
                            ("currency", "currency"),
                            ("fee_term", "per"),
                        ):
                            _v = matched.get(_src_k)
                            if _v in (None, "", 0):
                                continue
                            if payload.get(_k) not in (None, "", 0):
                                continue
                            payload[_k] = _v
                            evidence.append({
                                "field_key": _k,
                                "value": _v,
                                "confidence": _confidence_numeric,
                                "method": f"central_page:fees:{_fee_confidence}",
                                "source_url": _central_fee_url or url,
                                "snippet": f"central_page fee: {_k}={_v}",
                            })
                            _filled_fee_keys.append(_k)
                        if emit and _filled_fee_keys:
                            await emit(
                                "status",
                                f"[FEE match] course={_course_name_for_fee!r} "
                                f"matched_row={_prog!r} "
                                f"fee={matched.get('international_fee')} "
                                f"confidence={_fee_confidence}",
                                phase="fallback",
                                kind="central_fee_applied",
                                url=url,
                                matched_program=_prog,
                                fee_confidence=_fee_confidence,
                                filled=_filled_fee_keys,
                            )

            # ── English-requirements fallback ────────────────────────────
            # Two data paths, in priority order:
            #
            # Path 1 — level-keyed data (``english_by_level``): populated when
            #   ``central_english_pg_skip`` is True and the English page was
            #   browser-rendered.  Contains separate dicts for "undergraduate"
            #   and "postgraduate".  Apply the bucket that matches this course's
            #   degree_level.  No skip needed — the values are already correct.
            #
            # Path 2 — flat data (``english``): populated for all universities.
            #   For universities where the central page is level-uniform (KBS,
            #   most others) this is the right value for every course.
            #   For ASA-style pages it reflects UG-only values (6.0/50/60/169);
            #   applying them to PG courses is wrong, hence the pg_skip flag.
            _course_dl = (payload.get("degree_level") or "").strip()
            _english_by_level: dict = central_data.get("english_by_level") or {}
            # Re-infer degree_level from the AI-enriched course_name when the
            # raw HTML extractor missed it.  JS-rendered sites (e.g. law.ac.uk)
            # return a minimal shell on a plain HTTP fetch so the degree_level
            # extractor fires on content-free HTML and returns no match.  The AI
            # fallback then enriches course_name (e.g. "LLM Journalism and the
            # Law") but does not back-fill degree_level, leaving _course_dl
            # empty and causing Path 1 to fall back to the "undergraduate" bucket
            # — applying UG IELTS 6.0 to every LLM / MBA / MSc course.
            # Guard: only re-infer when a separate "postgraduate" bucket exists
            # in english_by_level.  If the central page is level-uniform (single
            # bucket only) the bucket choice doesn't affect the outcome anyway.
            if not _course_dl and _english_by_level.get("postgraduate"):
                _cn_for_dl = (payload.get("course_name") or "").strip()
                try:
                    from app.services.scraper.extractors.degree_level import (
                        classify_degree_level as _classify_dl,
                    )
                    _inferred_dl, _, _ = _classify_dl(_cn_for_dl, url)
                    if _inferred_dl:
                        _course_dl = _inferred_dl
                        # Also back-fill payload so staging sees the correct
                        # degree_level without waiting for sanitize_degree_level.
                        if not payload.get("degree_level"):
                            payload["degree_level"] = _inferred_dl
                except Exception:  # noqa: BLE001
                    pass
                # URL-path fallback: classify_degree_level scans for structured
                # label patterns ("degree level: …", "award: …") in page_text,
                # so passing a bare URL as page_text returns None when the URL
                # contains only a path segment like "/postgraduate/".  This is
                # the common failure mode for JS-rendered sites (e.g. law.ac.uk)
                # where course_name is not yet populated at this pipeline stage
                # (AI enrichment runs later) but the URL already encodes the
                # study tier:
                #   /study/postgraduate/law/llm-… → "Master's" bucket
                #   /study/undergraduate/law/llb-… → "Bachelor's" (default)
                # Without this fallback every PG course inherits the UG central
                # IELTS (e.g. 6.0 instead of 7.5) when course_name is empty.
                if not _course_dl:
                    _url_lower = url.lower()
                    if "/postgraduate/" in _url_lower or "/postgrad/" in _url_lower:
                        _course_dl = "Master's"
                        if not payload.get("degree_level"):
                            payload["degree_level"] = "Master's"
            # Diploma/Advanced Diploma programs sit between pathway programs
            # and bachelor-level courses in the KBS column-keyed table.  They
            # have a separate "diploma" by_level key populated by the
            # _parse_column_keyed_english_table Diploma column (e.g. IELTS 5.5
            # at KBS).  Without this bucket, the Diploma column value would be
            # overwritten by the higher-priority Bachelor+PG column (6.0)
            # because both previously shared the "undergraduate" key.
            _DIPLOMA_LEVELS: frozenset[str] = frozenset(
                {"Diploma", "Advanced Diploma", "Associate Diploma"}
            )
            _level_bucket = (
                "postgraduate"
                if _course_dl in _CENTRAL_ENGLISH_PG_LEVELS
                else "diploma"
                if _course_dl in _DIPLOMA_LEVELS
                else "undergraduate"
            )
            _level_english: dict = _english_by_level.get(_level_bucket) or {}

            # Pathway guard: pathway programs (Foundation Studies, ELICOS,
            # UniPrep, bridging courses) must not inherit the university-wide
            # IELTS from the central English page.  Their own pages may state
            # a lower requirement (e.g. IELTS 4.5 for ELICOS) and wrongly
            # applying the central 6.5 would block those values from ever
            # surfacing.  NULL is correct until the course page itself provides
            # a value; null is reviewable; a silently wrong 6.5 is not.
            _is_pathway_course = bool(payload.get("is_pathway"))

            # Methods whose values the verified central English page may
            # supersede.  AI guesses (hallucinated) and Gemini primary
            # (university-generic, not course-specific) lose to an
            # explicitly configured central-page URL.  Course-specific
            # sources (browser, vision, per-course Gemini) keep their
            # values.  Defined here so both Path 1 and Path 2 share it.
            _CENTRAL_ENGLISH_OVERRIDABLE: frozenset[str] = frozenset(
                {"", "ai_fallback", "gemini_primary"}
            )
            try:
                _cfg_course_english_priority: bool = bool(
                    getattr(_uc.extraction.english, "course_english_priority", False)
                )
            except Exception:  # noqa: BLE001
                _cfg_course_english_priority = False

            # ── Week 2 P5: SKIP_CENTRAL_ENGLISH_PROPAGATION ──────────────
            # Operators can disable propagation entirely while keeping the
            # diagnostic extraction.  When enabled, neither Path 1 nor Path 2
            # writes evidence; we emit a single status line so the dashboard
            # can show that the central page WAS fetched (cost tracking)
            # but its values were intentionally not propagated.
            if _skip_central_english_propagation() and (_level_english or _central_english):
                if emit:
                    await emit(
                        "status",
                        f"[CENTRAL —] {payload.get('course_name', url)[:40]} — "
                        f"propagation disabled (SKIP_CENTRAL_ENGLISH_PROPAGATION=true)",
                        phase="fallback",
                        kind="central_english_propagation_skipped",
                        url=url,
                        had_level_data=bool(_level_english),
                        had_flat_data=bool(_central_english),
                    )
                # Skip both Path 1 and Path 2 — drop into safety-net block below.
                _level_english = {}
                _central_english = {}

            # Path 1: level-specific values available — use them unconditionally.
            if _level_english and not _is_pathway_course:
                _eng_filled: list[str] = []
                for _k, _v in _level_english.items():
                    if _v in (None, "", 0):
                        continue
                    _curr = payload.get(_k)
                    if _curr not in (None, "", 0):
                        # Allow override when existing value came from a
                        # low-authority source (AI guess, Gemini primary).
                        if _cfg_course_english_priority:
                            continue  # course page English always wins
                        _existing_method = next(
                            (
                                ev.get("method", "")
                                for ev in reversed(evidence)
                                if ev.get("field_key") == _k
                            ),
                            "",
                        )
                        if _existing_method not in _CENTRAL_ENGLISH_OVERRIDABLE:
                            continue
                        # Drop stale low-authority evidence for this slot so
                        # extraction_method reflects the central page source.
                        evidence[:] = [
                            ev for ev in evidence if ev.get("field_key") != _k
                        ]
                    payload[_k] = _v
                    # Prefer the level-specific source URL when separate UG/PG pages
                    # are configured — gives reviewers a direct link to the right page.
                    _level_src_url = (
                        _central_eng_url_pg if _level_bucket == "postgraduate" else _central_eng_url_ug
                    ) or _central_eng_url or url
                    evidence.append({
                        "field_key": _k,
                        "value": _v,
                        "confidence": 0.55,
                        "method": "central_page:english_level",
                        "source_url": _level_src_url,
                        "snippet": f"central_page english_level ({_level_bucket}): {_k}={_v}",
                    })
                    _eng_filled.append(_k)
                if emit and _eng_filled:
                    _scores = " ".join(
                        f"{k.replace('_overall', '')}={payload.get(k)}"
                        for k in _eng_filled
                    )
                    await emit(
                        "status",
                        f"[CENTRAL ✓] {payload.get('course_name', url)[:40]} — "
                        f"english ({_level_bucket}) from central page: {_scores}",
                        phase="fallback",
                        kind="central_english_level_applied",
                        url=url,
                        bucket=_level_bucket,
                        filled=_eng_filled,
                    )

            # Path 2: fall back to flat values when no level-keyed data exists.
            else:
                _pg_skip_configured = bool(
                    central_data.get("central_english_pg_skip", False)
                )
                # Also skip flat central English for PG courses when the YAML
                # has per-level defaults configured.  In that case the central
                # page is often UG-specific (e.g. Waikato's undergrad-only page
                # at /study/apply/undergraduate-international/...) and the flat
                # values it returns (e.g. IELTS 6.0) are wrong for PG courses.
                # Skipping here lets the degree_level_defaults block below apply
                # the correct tier-specific values (e.g. IELTS 6.5 for PG).
                if not _pg_skip_configured and _course_dl in _CENTRAL_ENGLISH_PG_LEVELS:
                    try:
                        _yaml_uc2 = get_uni_config()
                        _yaml_eng2 = getattr(getattr(_yaml_uc2, "extraction", None), "english", None)
                        _yaml_dl2: dict = getattr(_yaml_eng2, "degree_level_defaults", {}) or {}
                        if _yaml_dl2.get("postgraduate"):
                            _pg_skip_configured = True
                    except Exception:
                        pass
                _skip_central_english = (
                    _pg_skip_configured and _course_dl in _CENTRAL_ENGLISH_PG_LEVELS
                ) or _is_pathway_course  # pathway: skip central English entirely
                if _central_english and not _skip_central_english:
                    _eng_filled = []
                    for _k, _v in _central_english.items():
                        if _v in (None, "", 0):
                            continue
                        _curr = payload.get(_k)
                        if _curr not in (None, "", 0):
                            if _cfg_course_english_priority:
                                continue  # course page English always wins
                            _existing_method = next(
                                (
                                    ev.get("method", "")
                                    for ev in reversed(evidence)
                                    if ev.get("field_key") == _k
                                ),
                                "",
                            )
                            if _existing_method not in _CENTRAL_ENGLISH_OVERRIDABLE:
                                continue
                            evidence[:] = [
                                ev for ev in evidence if ev.get("field_key") != _k
                            ]
                        payload[_k] = _v
                        evidence.append({
                            "field_key": _k,
                            "value": _v,
                            "confidence": 0.50,
                            "method": "central_page:english",
                            "source_url": _central_eng_url or url,
                            "snippet": f"central_page english: {_k}={_v}",
                        })
                        _eng_filled.append(_k)
                    if emit and _eng_filled:
                        _scores = " ".join(
                            f"{k.replace('_overall', '')}={payload.get(k)}"
                            for k in _eng_filled
                        )
                        await emit(
                            "status",
                            f"[CENTRAL ✓] {payload.get('course_name', url)[:40]} — "
                            f"english from central page: {_scores}",
                            phase="fallback",
                            kind="central_english_applied",
                            url=url,
                            filled=_eng_filled,
                        )
                elif _central_english and _skip_central_english and emit:
                    _skip_reason = (
                        "pathway program — central English not applicable"
                        if _is_pathway_course
                        else f"PG level ({_course_dl or 'unknown'}): no level-keyed data, pg_skip=true"
                    )
                    await emit(
                        "status",
                        f"[CENTRAL —] {payload.get('course_name', url)[:40]} — "
                        f"central english skipped: {_skip_reason}",
                        phase="fallback",
                        kind="central_english_skipped_pathway" if _is_pathway_course else "central_english_skipped_pg",
                        url=url,
                        degree_level=_course_dl,
                        is_pathway=_is_pathway_course,
                    )

            # ── Band Correction: align PTE / TOEFL / CAE to match per-course IELTS ──
            # When a university uses an English band system (e.g. JCU Bands P→3c),
            # the central page cache stores the institution minimum (typically Band P).
            # A course page may extract a higher IELTS (e.g. 7.0 for Medicine) but
            # TOEFL/PTE/CAE remain at the Band P values from the cache, triggering
            # false "IELTS/TOEFL Mismatch" warnings.  This block reads the per-uni
            # YAML band_mapping, finds the band whose ielts_overall matches the
            # current payload value, then overwrites any central-page-sourced
            # TOEFL/PTE/CAE with the correct band-specific equivalents.
            # Only central_page and ai_fallback method fields are overridden —
            # course-specific OCR / regex values are left untouched.
            try:
                _band_map: dict = getattr(_uc.extraction.english, "band_mapping", {}) or {}
                _band_ref_url: str = (
                    getattr(_uc.extraction.english, "band_reference_url", None) or url
                )
                _cur_ielts: float = float(payload.get("ielts_overall") or 0.0)
                if _band_map and _cur_ielts > 0:
                    _hit_band_name: str | None = None
                    _hit_band_spec: dict | None = None
                    for _bn, _bs in _band_map.items():
                        _bsd = _bs.model_dump() if hasattr(_bs, "model_dump") else dict(_bs)
                        _bi = float(_bsd.get("ielts_overall") or 0.0)
                        if abs(_bi - _cur_ielts) < 0.05:
                            _hit_band_name = _bn
                            _hit_band_spec = _bsd
                            break
                    if _hit_band_name and _hit_band_spec:
                        _BAND_FIELDS = ("pte_overall", "toefl_overall", "cambridge_overall", "duolingo_overall")
                        _BAND_OVERRIDE_METHODS = frozenset({
                            "central_page:english", "central_page:english_level",
                            "ai_fallback", "gemini_primary", "",
                        })
                        _band_applied: list[str] = []
                        for _bfk in _BAND_FIELDS:
                            _bfv = _hit_band_spec.get(_bfk)
                            if not _bfv:
                                continue
                            _curr_bfv = payload.get(_bfk)
                            if _curr_bfv not in (None, 0, ""):
                                _curr_bfm = next(
                                    (ev.get("method", "") for ev in reversed(evidence)
                                     if ev.get("field_key") == _bfk),
                                    "",
                                )
                                if _curr_bfm not in _BAND_OVERRIDE_METHODS:
                                    continue  # Course-specific OCR/regex — don't touch
                                if payload.get(_bfk) == _bfv:
                                    continue  # Already at the correct value
                                evidence[:] = [
                                    ev for ev in evidence if ev.get("field_key") != _bfk
                                ]
                            payload[_bfk] = _bfv
                            evidence.append({
                                "field_key": _bfk,
                                "value": _bfv,
                                "confidence": 0.80,
                                "method": "yaml_band_mapping",
                                "source_url": _band_ref_url,
                                "snippet": f"band_mapping {_hit_band_name}: {_bfk}={_bfv}",
                            })
                            _band_applied.append(_bfk)
                        if emit and _band_applied:
                            await emit(
                                "status",
                                f"[BAND ✓] {payload.get('course_name', url)[:40]} — "
                                f"IELTS={_cur_ielts} → {_hit_band_name}: "
                                + " ".join(
                                    f"{k.replace('_overall','')}={payload.get(k)}"
                                    for k in _band_applied
                                ),
                                phase="fallback",
                                kind="band_mapping_applied",
                                url=url,
                                band=_hit_band_name,
                                fields_applied=_band_applied,
                            )
            except Exception as _band_exc:
                log.warning("[BAND] band_mapping correction failed on %s: %s", url, _band_exc)

        except Exception as exc:  # noqa: BLE001 — never abort extraction
            log.warning("central_pages fallback errored on %s: %s", url, exc)

        # ── PG English clear-out (safety net) ────────────────────────────────
        # When ``central_english_pg_skip`` is True AND the browser fetch did
        # not return reliable level-keyed PG data (``english_by_level``
        # missing or has no "postgraduate" entry), English scores that came
        # from the central page or sibling-cache (UG-only values) must be
        # cleared.  NULL is honest and recoverable; a silently-wrong 6.0 for
        # a Master's that requires 6.5 is neither.
        #
        # EXCEPTION: if a slot was filled by per-course vision OCR
        # (``per_course_vision`` / ``per_course_vision_cached``), it was
        # read directly from the course's own page and is per-course
        # reliable.  Those values must survive the clear-out even when the
        # browser-rendered central page had no level headings.
        #
        # When the browser DID return level-keyed data and Path 1 applied
        # the correct PG values above, this block is skipped — the values
        # are already right and should not be cleared.
        #
        # This runs AFTER all extractors have settled (including vision OCR
        # and sibling-cache backfill) so it is the definitive last word.
        _pg_skip_final = bool(central_data.get("central_english_pg_skip", False))
        _pg_dl_final = (payload.get("degree_level") or "").strip()
        _pg_has_level_data = bool(
            (central_data.get("english_by_level") or {}).get("postgraduate")
        )
        if (
            _pg_skip_final
            and not _pg_has_level_data
            and _pg_dl_final in _CENTRAL_ENGLISH_PG_LEVELS
        ):
            # Build a quick index: slot → set of methods that filled it
            _slot_methods: dict[str, set[str]] = {}
            for _ev in evidence:
                _fk = _ev.get("field_key", "")
                _meth = _ev.get("method", "")
                if _fk and _meth:
                    _slot_methods.setdefault(_fk, set()).add(_meth)

            _cleared: list[str] = []
            for _slot in ("ielts_overall", "pte_overall", "toefl_overall", "cambridge_overall", "duolingo_overall"):
                if payload.get(_slot) not in (None, "", 0):
                    # Keep the value if any evidence for this slot has course-specific
                    # authority (≥ _AUTHORITY_COURSE_SPECIFIC = 3).  This replaces the
                    # old _PER_COURSE_VISION_METHODS frozenset with a numeric model so
                    # new extractors automatically get the right treatment without needing
                    # a hand-written exemption here.
                    _slot_max_auth = max(
                        (_method_authority(m) for m in _slot_methods.get(_slot, set())),
                        default=0,
                    )
                    if _slot_max_auth >= _AUTHORITY_COURSE_SPECIFIC:
                        continue
                    payload[_slot] = None
                    _cleared.append(_slot)
            if _cleared and emit:
                await emit(
                    "status",
                    f"[PG-SKIP ✗] {payload.get('course_name', url)[:40]} — "
                    f"nulled english for PG ({_pg_dl_final}): "
                    f"{', '.join(_cleared)} (no level-keyed data from browser)",
                    phase="fallback",
                    kind="pg_english_cleared",
                    url=url,
                    degree_level=_pg_dl_final,
                    cleared=_cleared,
                )

        # Signal to the staging gate that this university has a centralized fee
        # page.  Even if this specific course wasn't listed in the table, the
        # course may still be open to international students — the staging gate
        # should stage it for human review rather than auto-rejecting it.
        #
        # IMPORTANT: the flag is only set when the central page actually PARSED
        # fee records.  Previously this was set whenever a fee page URL was
        # merely *discovered*, even if the page yielded zero records.  At
        # Federation, auto-discovery picked /apply/ as the "fee page" and parsed
        # it to 0 program records — but the flag still flipped True, so the
        # no_international_fee staging gate (guards.py:531) was bypassed for
        # EVERY blank-fee course, flooding the review queue with domestic-only
        # courses (Cert/Diploma/Health/Education programs not offered to
        # international students).  A discovered-but-empty fee page is no
        # evidence that international fees exist elsewhere, so it must NOT grant
        # the escape hatch.
        _central_fee_records = central_data.get("fees") or []
        if central_data.get("fee_page_url") and _central_fee_records:
            payload["has_central_fee_page"] = True
        elif central_data.get("fee_page_url"):
            log.info(
                "[CENTRAL FEE] fee page %s discovered but parsed 0 records — "
                "NOT setting has_central_fee_page (no escape hatch) for %r",
                central_data.get("fee_page_url"),
                payload.get("course_name") or url,
            )

    # ── Institutional English defaults (last-resort fallback) ─────────────────
    # When a university publishes a single institutional minimum English score
    # for international entry (e.g. CQU's "IELTS 6.5 / PTE 58 / TOEFL 79"),
    # the YAML can declare it via extraction.english.default_ielts /
    # default_pte / default_toefl.  These fill English slots only when EVERY
    # earlier path returned null — per-course HTML, browser, vision OCR,
    # central page, sibling cache, all came up empty.
    #
    # Confidence 0.40 is intentionally lower than central_page:english (0.50)
    # so a real central page always wins.  Pathway / ELICOS courses are
    # exempt for the same reason as central English: their own pages may
    # state a lower requirement that would be wrongly overridden.
    try:
        _eng_cfg = getattr(getattr(get_uni_config(), "extraction", None), "english", None)
        if _eng_cfg is not None and not bool(payload.get("is_pathway")):
            # Resolve degree-level tier for per-level defaults (e.g. UG 6.0 / PG 6.5).
            _dl_raw = (payload.get("degree_level") or "").lower().strip()
            _dl_tier: str | None = None
            if _dl_raw:
                if any(k in _dl_raw for k in ("bachelor", "honours", "honor")):
                    _dl_tier = "undergraduate"
                elif any(k in _dl_raw for k in ("master",)):
                    _dl_tier = "postgraduate"
                elif any(k in _dl_raw for k in ("doctor", "phd", "dphil")):
                    _dl_tier = "doctorate"
                elif _dl_raw.startswith("graduate") or "postgraduate" in _dl_raw:
                    # "Graduate Diploma", "Graduate Certificate", "Postgraduate Diploma" → PG
                    _dl_tier = "postgraduate"
                elif any(k in _dl_raw for k in ("diploma", "certificate")):
                    # Plain diploma/cert without graduate/postgraduate prefix → UG tier
                    _dl_tier = "undergraduate"
            # Look up per-tier config; fall back to flat defaults if tier not found.
            _dl_defaults_map: dict = getattr(_eng_cfg, "degree_level_defaults", {}) or {}
            _tier_cfg = None
            if _dl_tier and _dl_defaults_map:
                _tier_cfg = _dl_defaults_map.get(_dl_tier)
                # "doctorate" key optional — fall back to "postgraduate" if missing
                if _tier_cfg is None and _dl_tier == "doctorate":
                    _tier_cfg = _dl_defaults_map.get("postgraduate")
            def _pick(tier_attr: str, flat_attr: str):
                """Return tier value if set, else flat default, else None."""
                if _tier_cfg is not None:
                    v = getattr(_tier_cfg, tier_attr, None)
                    if v not in (None, 0):
                        return v
                return getattr(_eng_cfg, flat_attr, None)
            _defaults = (
                ("ielts_overall",     _pick("ielts",  "default_ielts")),
                ("pte_overall",       _pick("pte",    "default_pte")),
                ("toefl_overall",     _pick("toefl",  "default_toefl")),
            )
            # Build a set of slots that have at least one "proven" evidence row
            # (both source_url AND snippet populated).  guards.enforce_source_evidence
            # will null any critical english slot lacking such proof at staging time.
            # If an upstream extractor wrote a value WITHOUT proof, the value is doomed
            # to become NULL anyway — so we should treat the slot as empty here and let
            # the institutional default (which always carries proof) win.  This was the
            # 2026-05-13 IELTS-blank bug: per-course extractor wrote ielts_overall
            # without source_url+snippet, the default-fill saw a non-null value and
            # skipped, then enforce_source_evidence dropped the value, leaving IELTS
            # NULL while PTE/TOEFL got the default (they were null at default-fill
            # because no extractor wrote them).
            _proven_slots: set[str] = set()
            for _ev in evidence or []:
                if not isinstance(_ev, dict):
                    continue
                _fk = _ev.get("field_key")
                if not _fk:
                    continue
                _src = (_ev.get("source_url") or "").strip()
                _snip = (_ev.get("snippet") or "").strip()
                if _src and _snip:
                    _proven_slots.add(str(_fk))
            _eng_default_filled: list[str] = []
            _eng_default_replaced: list[str] = []
            for _slot, _default_val in _defaults:
                if _default_val in (None, "", 0):
                    continue
                _existing = payload.get(_slot)
                _has_value = _existing not in (None, "", 0)
                _is_proven = _slot in _proven_slots
                # Skip when there's already a value AND it has supporting proof:
                # a real extractor reading won.  Replace when there's a value but
                # no proof: enforce_source_evidence would null it anyway, so swap
                # in the institutional default (which carries proof) now.
                if _has_value and _is_proven:
                    continue
                payload[_slot] = _default_val
                evidence.append({
                    "field_key": _slot,
                    "value": _default_val,
                    "confidence": 0.40,
                    "method": "uni_config:english_default",
                    "source_url": url,
                    "snippet": (
                        f"institutional default from per-uni YAML: {_slot}={_default_val}"
                    ),
                })
                if _has_value:
                    _eng_default_replaced.append(_slot)
                else:
                    _eng_default_filled.append(_slot)
            if _eng_default_replaced:
                log.info(
                    "[ENG-DEFAULT REPLACE] %s — replaced unproven values with "
                    "institutional defaults (would have been nulled by "
                    "enforce_source_evidence): %s",
                    url, _eng_default_replaced,
                )
            # Combined log message uses both buckets so the existing emit logic
            # (and downstream test expectations) keep seeing the same shape.
            _eng_default_filled = _eng_default_filled + _eng_default_replaced
            if emit and _eng_default_filled:
                _scores = " ".join(
                    f"{k.replace('_overall', '')}={payload.get(k)}"
                    for k in _eng_default_filled
                )
                await emit(
                    "status",
                    f"[ENG-DEFAULT] {payload.get('course_name', url)[:40]} — "
                    f"institutional defaults applied: {_scores}",
                    phase="fallback",
                    kind="english_default_applied",
                    url=url,
                    filled=_eng_default_filled,
                )
    except Exception as exc:  # noqa: BLE001 — never abort extraction
        log.warning("english institutional-defaults fallback errored on %s: %s", url, exc)

    # ── Fee degree_level_defaults fallback ──────────────────────────────────
    # When international_fee is still null after all extractors, check whether
    # the per-uni YAML defines a degree_level_defaults fee for this tier.
    # Mirrors the pattern used above for English requirements.
    try:
        _fee_dl_cfg = None
        try:
            _fee_uc = get_uni_config()
            if _fee_uc is not None:
                _fee_dl_cfg = _fee_uc.extraction.fees
        except Exception:
            pass
        _fee_defaults_map: dict = getattr(_fee_dl_cfg, "degree_level_defaults", {}) or {}
        # Definitive per-course signal from fee.py: a structured fee table was
        # found on this exact course page, but it has ONLY Home/Part-time rows
        # and no International + Full-time row at all (e.g. HNC Building
        # Studies at Wolverhampton). That is stronger evidence than "we found
        # no fee data" — it means the university itself does not offer this
        # specific course to international students. Applying the flat
        # institutional degree_level_defaults fee here would fabricate a price
        # for a course nobody can actually pay as an international student, so
        # skip the fallback and let it stay null (→ rejected downstream by the
        # normal no_international_fee gate).
        _fee_table_confirmed_no_intl = bool(
            payload.get("fee_table_confirmed_no_international")
        )
        if (
            not _bail_empty_text
            and _fee_defaults_map
            and not _fee_table_confirmed_no_intl
            and payload.get("international_fee") in (None, "", 0)
        ):
            _fdl_raw = (payload.get("degree_level") or "").lower().strip()
            _fdl_tier: str | None = None
            if _fdl_raw:
                import re as _re_local
                if any(k in _fdl_raw for k in ("bachelor", "honours", "honor", "hons", "associate", "bsc", "ba ", "beng", "bbus", "bcom")):
                    _fdl_tier = "undergraduate"
                elif any(k in _fdl_raw for k in ("master",)) or _re_local.search(r"\b(msc|ma|meng|mba|mres|mphil|llm|mpa|mfa|mmus|mus\.m)\b", _fdl_raw):
                    _fdl_tier = "postgraduate"
                elif any(k in _fdl_raw for k in ("doctor", "phd", "dphil")) or _re_local.search(r"\b(dba|edd|dsc|phd)\b", _fdl_raw):
                    _fdl_tier = "doctorate"
                elif _fdl_raw.startswith("graduate") or "postgraduate" in _fdl_raw or _re_local.search(r"\bpg(dip|cert|diploma|certificate)\b", _fdl_raw):
                    _fdl_tier = "postgraduate"
                elif any(k in _fdl_raw for k in ("diploma", "certificate")):
                    _fdl_tier = "undergraduate"
            _fdl_default: int | None = None
            if _fdl_tier:
                _fdl_default = _fee_defaults_map.get(_fdl_tier)
                if _fdl_default is None and _fdl_tier == "doctorate":
                    _fdl_default = _fee_defaults_map.get("postgraduate")
            if _fdl_default and isinstance(_fdl_default, int):
                _fee_currency = getattr(_fee_dl_cfg, "default_currency", "AUD") or "AUD"
                _fee_term = getattr(_fee_dl_cfg, "fee_term", "Annual") or "Annual"
                payload["international_fee"] = _fdl_default
                payload.setdefault("currency", _fee_currency)
                payload.setdefault("fee_term", _fee_term)
                evidence.append({
                    "field_key": "international_fee",
                    "value": _fdl_default,
                    "confidence": 0.35,
                    "method": "uni_config:fee_default",
                    "source_url": url,
                    "snippet": (
                        f"institutional fee default from per-uni YAML: "
                        f"{_fdl_tier}={_fdl_default} {_fee_currency}"
                    ),
                })
                if emit:
                    await emit(
                        "status",
                        f"[FEE-DEFAULT] {payload.get('course_name', url)[:40]} — "
                        f"YAML default applied: {_fee_currency} {_fdl_default:,} "
                        f"({_fdl_tier})",
                        phase="fallback",
                        kind="fee_default_applied",
                        url=url,
                        filled=["international_fee"],
                    )
    except Exception as exc:  # noqa: BLE001
        log.warning("fee degree_level_defaults fallback errored on %s: %s", url, exc)

    # Rule-based category classifier — runs after every other slot is
    # populated so we can use the (possibly AI-filled) course_name. The
    # Review table's Category column reads scraped_courses.category; without
    # this step every row showed NULL. Skip if an extractor already produced
    # a category (none currently do, but keeps the pipeline future-proof).
    cname = payload.get("course_name") or ""
    # T204: keyword-based pre-map sets BOTH category and sub_category from
    # well-known compound titles ("Hospitality Management" → Tourism &
    # Hospitality / Hospitality Management). Runs first; the body-text
    # classify_category fallback only fires when no pre-map keyword hit.
    #
    # **Rule wins over Gemini**: when the rule-based map fires, OVERWRITE
    # any prior AI / extractor value. The rule map is hand-curated against
    # the live DB taxonomy (`course_sub_categories`), so its output is
    # guaranteed to match an existing canonical row. Gemini's free-text
    # output (e.g. "Applied Cyber Security") otherwise gets inserted as a
    # new auto-added sub-category, fragmenting the taxonomy. Gemini's value
    # only survives when the rule map returns no hit AND no other extractor
    # has populated category/sub_category — i.e. last-resort fallback.
    det = map_course_to_category(cname)
    if det:
        if payload.get("category") != det["category"]:
            payload["category"] = det["category"]
            evidence.append(
                {
                    "field_key": "category",
                    "value": det["category"],
                    "confidence": 0.9,
                    "method": "category:det",
                    "snippet": cname,
                }
            )
        if payload.get("sub_category") != det["sub_category"]:
            payload["sub_category"] = det["sub_category"]
            evidence.append(
                {
                    "field_key": "sub_category",
                    "value": det["sub_category"],
                    "confidence": 0.9,
                    "method": "category:det",
                    "snippet": cname,
                }
            )
        if emit:
            await emit(
                "status",
                f"[CATEGORY det] {cname[:40]} → {det['category']} / {det['sub_category']}",
                phase="classify",
            )
    if not payload.get("category"):
        cat = classify_category(cname)
        if cat:
            payload["category"] = cat
            evidence.append(
                {
                    "field_key": "category",
                    "value": cat,
                    "confidence": 0.6,
                    "method": "category:rule",
                    "snippet": cname,
                }
            )

    # ── Study load (Full Time / Part Time) ───────────────────────────────────
    # Only runs when no extractor (including Gemini primary) has set it yet.
    # Checks duration_text first (most reliable signal: "2 years full-time"),
    # then scans the first 3 KB of page text for explicit phrases.
    if not payload.get("study_load"):
        _sl_sources = [
            (payload.get("duration_text") or "").lower(),
            (rendered_html or html or "")[:3000].lower(),
        ]
        _sl_text = " ".join(_sl_sources)
        if _re.search(r"\bpart[- ]time\b", _sl_text):
            payload["study_load"] = "Part Time"
            evidence.append({
                "field_key": "study_load",
                "value": "Part Time",
                "confidence": 0.75,
                "method": "regex:study_load",
                "snippet": next(
                    (s for s in _sl_sources if _re.search(r"\bpart[- ]time\b", s)), ""
                )[:120],
            })
        elif _re.search(r"\bfull[- ]time\b", _sl_text):
            payload["study_load"] = "Full Time"
            evidence.append({
                "field_key": "study_load",
                "value": "Full Time",
                "confidence": 0.70,
                "method": "regex:study_load",
                "snippet": next(
                    (s for s in _sl_sources if _re.search(r"\bfull[- ]time\b", s)), ""
                )[:120],
            })

    # ── Host-specific fee_term correction ────────────────────────────────────
    # Some universities publish a FULL COURSE total on their course pages
    # without any "per year" / "per annum" qualifier in the surrounding text.
    # _normalize_fee_term (fee.py) therefore defaults to "Annual", which is
    # wrong: showing "A$48,000/Annual" for a 2-year MITS implies $96,000 total
    # when the actual cost is $48,000 total.
    #
    # VIT: charges per-unit fees and lists the total programme cost (e.g.
    # $48,000 for MITS = 24 units × $2,000/unit).  No "per year" text
    # appears near the figure on course pages.  Override to "Full Course"
    # after all extractors have settled so the correction applies regardless
    # of whether the fee came from the static pass, the browser extended
    # extraction, or the PDF backfill.
    _FULL_COURSE_FEE_HOSTS: frozenset[str] = frozenset({
        "vit.edu.au",
        "www.vit.edu.au",
    })
    _sc_host = (urlparse(url).hostname or "").lower()
    if _sc_host in _FULL_COURSE_FEE_HOSTS and payload.get("fee_term") == "Annual":
        payload["fee_term"] = "Full Course"

    # ── MYR total-fee normalizer ──────────────────────────────────────────────
    # INTI and similar Malaysian universities quote TOTAL programme fees with
    # no "per year" qualifier, e.g. "From RM46,588 — Programme can be completed
    # in 2 Years". Gemini labels these "Annual" or "Full Course" nondeterministically.
    #
    # Rule: if fee_currency=="MYR" AND fee_term=="Annual" AND duration>1yr AND
    #   the page text contains no per-year/per-annum/per-semester signal →
    #   relabel fee_term="Full Course" so the fee_calculation_mode:
    #   full_course_to_annual recipe (set in YAML) divides by duration.
    # This runs BEFORE recipe rules (which run in orchestrator.py after this
    # function returns) so the conversion sees the corrected "Full Course" term.
    _myr_fee = payload.get("international_fee")
    _myr_term = payload.get("fee_term")
    _myr_cur = (payload.get("fee_currency") or payload.get("currency") or "").upper()
    _myr_dur = payload.get("duration")
    _myr_dur_term_raw = (payload.get("duration_term") or "").lower()
    if (
        _myr_cur == "MYR"
        and _myr_term == "Annual"
        and _myr_fee and isinstance(_myr_fee, (int, float)) and _myr_fee > 0
        and _myr_dur and isinstance(_myr_dur, (int, float))
    ):
        # Compute duration in years (need > 1 yr to bother dividing)
        _myr_years: float | None = None
        if "year" in _myr_dur_term_raw:
            _myr_years = float(_myr_dur)
        elif "month" in _myr_dur_term_raw:
            _myr_years = float(_myr_dur) / 12.0
        elif "week" in _myr_dur_term_raw:
            _myr_years = float(_myr_dur) / 52.0
        if _myr_years and _myr_years > 1.0:
            _PER_YEAR_SIGNAL_RE = _re.compile(
                r"\b(per\s+year|per\s+annum|p\.a\.|annually|per\s+semester"
                r"|per\s+trimester|per\s+term|per\s+intake|each\s+year)\b",
                _re.IGNORECASE,
            )
            # Use raw HTML — per-year signals appear in visible text which is
            # present in the HTML source regardless of rendering.
            _myr_page_check = (rendered_html or html or "")[:10000]
            if not _PER_YEAR_SIGNAL_RE.search(_myr_page_check):
                payload["fee_term"] = "Full Course"
                evidence.append({
                    "field_key": "fee_term",
                    "value": "Full Course",
                    "confidence": 0.78,
                    "method": "fee_term:myr_total_normalizer",
                    "snippet": (
                        f"MYR site ({_sc_host}): no per-year/per-annum language found "
                        f"in page — Annual relabelled Full Course for recipe division "
                        f"(fee={_myr_fee:.0f}, dur={_myr_dur} {_myr_dur_term_raw})"
                    ),
                })
                log.info(
                    "[MYR FEE NORMALIZER] fee_term Annual → Full Course on %s "
                    "(fee=%.0f MYR, dur=%s %s, no per-year signal in page text)",
                    url, _myr_fee, _myr_dur, _myr_dur_term_raw,
                )

    # ── Graduate Diploma name-based degree_level correction ───────────────────
    # When the course name contains "Graduate Diploma" or "Postgraduate Diploma"
    # the degree level is definitively known from the title and must not be
    # overridden by Gemini's AQF-8 heuristic ("AQF Level 8" = Graduate
    # Certificate in Gemini's mapping, but AQF 8 covers BOTH Graduate
    # Certificate AND Graduate Diploma). Apply this correction AFTER all
    # extractors (including Gemini primary) have settled so it always wins.
    _course_name_for_dl = payload.get("course_name") or ""
    if _re.search(r"\b(?:graduate|postgraduate)\s+diploma\b", _course_name_for_dl, _re.I):
        payload["degree_level"] = "Graduate Diploma"

    # ── Scrape-quality warning detection ─────────────────────────────────────
    # After ALL extractors have settled, audit the final payload for cases
    # where the course page clearly contained a data section but the pipeline
    # failed to extract a value.  These warnings surface in the review UI as
    # amber badges so operators know why a row needs manual verification.
    # They are stored in payload["scrape_warnings"] (JSONB list of codes) and
    # persist to the scraped_courses.scrape_warnings column via stage_course.
    #
    # WARNING CODES:
    #   english_section_detected_scores_blank — "English Language Requirements"
    #     heading found in page HTML but every IELTS/PTE/TOEFL/CAE/DET slot
    #     is still NULL after all extractors including vision and AI fallback.
    #     Most common cause: Gemini not configured on the production host, or
    #     scores are in an image that vision couldn't decode.
    #   fee_section_detected_fee_blank — fee-related heading found in HTML but
    #     international_fee is NULL.  Usually means the page shows fee info in
    #     a JavaScript-rendered table that the browser pass missed.
    #   suspicious_duration — duration value looks wrong for the degree level:
    #     >7 years for Bachelor/Master, or <0.25 years (3 months) for any
    #     course. Catches semester-to-year misconversions and AI hallucinations.
    #   no_intake_months — intake_months list is empty after extraction. Flags
    #     courses where the page shows intake info but none was captured.
    _scrape_warnings: list[str] = list(payload.get("scrape_warnings") or [])

    _check_html = rendered_html or html or ""
    _check_lower = _check_html.lower()

    # ── English section detected but no scores ──────────────────────────────
    _ENGLISH_HEADING_PATTERNS = (
        "english language requirement",
        "english requirement",
        "english proficiency",
        "ielts requirement",
        "language requirement",
        "english language proficiency",
    )
    _english_heading_found = any(p in _check_lower for p in _ENGLISH_HEADING_PATTERNS)
    _english_slots_all_blank = all(
        payload.get(k) in (None, "", 0)
        for k in ("ielts_overall", "pte_overall", "toefl_overall", "cambridge_overall", "duolingo_overall")
    )
    if _english_heading_found and _english_slots_all_blank:
        if "english_section_detected_scores_blank" not in _scrape_warnings:
            _scrape_warnings.append("english_section_detected_scores_blank")
        if emit:
            await emit(
                "status",
                f"[WARN] {payload.get('course_name','?')[:40]} — English section detected in HTML but all scores blank",
                phase="extract",
                kind="scrape_warning",
                warning="english_section_detected_scores_blank",
                url=url,
            )

    # ── Fee section detected but fee is blank ───────────────────────────────
    _FEE_HEADING_PATTERNS = (
        "international tuition",
        "course fee",
        "fees and scholarship",
        "tuition fee",
        "fee summary",
        "international student fee",
        "fees schedule",
    )
    _fee_heading_found = any(p in _check_lower for p in _FEE_HEADING_PATTERNS)
    _fee_blank = payload.get("international_fee") in (None, "", 0)
    if _fee_heading_found and _fee_blank:
        if "fee_section_detected_fee_blank" not in _scrape_warnings:
            _scrape_warnings.append("fee_section_detected_fee_blank")
        # QA Issue-6: detect whether a fee value was extracted at some point
        # during the pipeline but then discarded (e.g. by a validation step
        # or a null-overwrite from a later extractor).
        _ev_had_fee = any(
            ev.get("field_key") == "international_fee"
            and ev.get("value") not in (None, "", 0)
            for ev in evidence
        )
        if _ev_had_fee:
            log.info(
                "[FEE_DISAPPEARED] %s — fee was extracted (see evidence log) "
                "but is blank in final payload; check merge/validation steps",
                payload.get("course_name") or url,
            )
        if emit:
            await emit(
                "status",
                f"[WARN] {payload.get('course_name','?')[:40]} — Fee section detected but fee is blank",
                phase="extract",
                kind="scrape_warning",
                warning="fee_section_detected_fee_blank",
                url=url,
            )

    # ── Suspicious duration ─────────────────────────────────────────────────
    _dur_val = payload.get("duration")
    if _dur_val is not None:
        try:
            _dur_f = float(_dur_val)
            _dur_term = (payload.get("duration_term") or "Year").lower()
            # Normalise to years for the sanity check
            if "month" in _dur_term:
                _dur_years = _dur_f / 12
            elif "semester" in _dur_term:
                _dur_years = _dur_f / 2
            elif "trimester" in _dur_term:
                _dur_years = _dur_f / 3
            elif "week" in _dur_term:
                _dur_years = _dur_f / 52
            else:
                _dur_years = _dur_f  # assume years
            _degree_l = (payload.get("degree_level") or "").lower()
            _is_bachelor_master = any(x in _degree_l for x in ("bachelor", "master", "honours"))
            # Graduate certificates and diplomas are short courses (≤ 1 year typically,
            # absolute max ~2 years).  Any "Year" value ≥ 4 is certainly a scrape error
            # (e.g. a candidature-deadline number that slipped through the extractor).
            # Cap at 4.0 so these are nullified rather than stored as plausible data.
            _is_grad_short = any(x in _degree_l for x in (
                "graduate certificate", "graduate diploma",
                "postgraduate certificate", "postgraduate diploma",
            ))
            _SUSPICIOUS_MAX = (
                7.0 if _is_bachelor_master
                else 4.0 if _is_grad_short
                else 12.0
            )
            # UTAS bachelor-floor guard: UTAS flexible-enrolment pages show
            # "Duration Minimum 1 Semester, up to a maximum of 4 years." for
            # bachelor degrees where the 1-Semester (or similar short) value
            # is the cross-institutional / exchange enrolment floor — NOT the
            # real 3-year program duration.  Any bachelor-level course with
            # duration < 2.0 years is almost certainly a scrape error of this
            # type; a null is far safer to display than "1 Semester".
            # (Australian bachelor degrees are never shorter than 2 years.)
            _is_bachelor_only = "bachelor" in _degree_l and not _is_grad_short
            # Honours exception: a Bachelor (Honours) is a 1-year top-up
            # degree taken AFTER a 3-year bachelor — the floor of 2.0 is
            # legitimately violated. Federation's
            # /courses/dsz8-bachelor-of-science-honours/ publishes
            # "Duration: 1 year full-time" in its JSON; nullifying it
            # here loses real data.
            #
            # Detection: honours signal in course_name OR degree_level
            # (covers cases where the canonical-name override hasn't
            # fired yet and degree_level still carries "Bachelor (Honours)").
            #
            # Tightened range: only exempt 0.9..1.25 years — the genuine
            # 1-year top-up. Values like 0.5 year (1-semester scrape noise
            # that happens to land on an honours page) or 1.7 years
            # (regex picking up an "accelerated" sub-clause) still get
            # nullified so bad data never reaches staging.
            _course_name_l = (payload.get("course_name") or "").lower()
            _honours_signal = (
                "honours" in _course_name_l
                or "(hons)" in _course_name_l
                or "hons)" in _course_name_l
                or "honours" in _degree_l
                or "(hons)" in _degree_l
            )
            _is_honours_one_year = (
                _honours_signal
                and _dur_term == "year"
                and 0.9 <= _dur_years <= 1.25
            )
            # Final-year-entry / top-up bachelor degrees are also legitimately
            # 1 year long.  "Final year entry" means the student enters the last
            # year of a bachelor program (having completed the earlier years via
            # HNC/HND or another qualification), so 1 Year is correct data.
            # Detect via course name keywords or the URL slug.
            _url_slug_l = (url or "").lower()
            _topup_signal = (
                "final year entry" in _course_name_l
                or "final-year-entry" in _course_name_l
                or "final-year entry" in _course_name_l
                or "top-up" in _course_name_l
                or "top up" in _course_name_l
                or "final-year-entry" in _url_slug_l
                or "final-year-entry" in _url_slug_l
                or "top-up" in _url_slug_l
            )
            _is_topup_one_year = (
                _topup_signal
                and _dur_term == "year"
                and 0.9 <= _dur_years <= 1.25
            )
            _bachelor_floor_breach = (
                _is_bachelor_only
                and not _is_honours_one_year
                and not _is_topup_one_year
                and 0 < _dur_years < 2.0
            )
            if _dur_years > _SUSPICIOUS_MAX or _dur_years < 0.25 or _bachelor_floor_breach:
                # Nullify the value so bad data never reaches staging.
                # A missing duration is better than a wrong one — operators
                # can fill it via the review UI; a wrong value propagates silently.
                payload["duration"] = None
                payload["duration_term"] = None
                if "suspicious_duration" not in _scrape_warnings:
                    _scrape_warnings.append("suspicious_duration")
                if emit:
                    _reason = (
                        "bachelor degree floor"
                        if _bachelor_floor_breach
                        else "sanity limit"
                    )
                    await emit(
                        "status",
                        f"[NULLIFIED] {payload.get('course_name','?')[:40]} — duration {_dur_val} {_dur_term} ({_dur_years:.1f} yrs) exceeds {_reason}; cleared",
                        phase="extract",
                        kind="scrape_warning",
                        warning="suspicious_duration",
                        url=url,
                    )
        except (TypeError, ValueError):
            pass

    # ── Duration degree_level_defaults fallback ──────────────────────────────
    # When duration is still None after all extractors (including the sanity
    # nullification above), apply the YAML-configured default years for this
    # degree level.  Mirrors the fee degree_level_defaults pattern.
    # Use for universities where duration lives in a JS-only widget that
    # Scrape.do static HTML cannot reach (e.g. Ulster Cloudflare-gated widget).
    try:
        _dur_dl_cfg = None
        try:
            _dur_uc = get_uni_config()
            if _dur_uc is not None:
                _dur_dl_cfg = _dur_uc.extraction.text_cleaning.duration
        except Exception:
            pass
        _dur_defaults_map: dict = getattr(_dur_dl_cfg, "degree_level_defaults", {}) or {}
        if _dur_defaults_map and payload.get("duration") is None:
            _ddl_raw = (payload.get("degree_level") or "").lower().strip()
            _ddl_tier: str | None = None
            if _ddl_raw:
                import re as _re_dur
                if any(k in _ddl_raw for k in ("bachelor", "honours", "honor", "hons", "associate", "bsc", "ba ", "beng", "bbus", "bcom")):
                    _ddl_tier = "undergraduate"
                elif any(k in _ddl_raw for k in ("master",)) or _re_dur.search(r"\b(msc|ma|meng|mba|mres|mphil|llm|mpa|mfa|mmus|mus\.m)\b", _ddl_raw):
                    _ddl_tier = "postgraduate"
                elif any(k in _ddl_raw for k in ("doctor", "phd", "dphil")) or _re_dur.search(r"\b(dba|edd|dsc)\b", _ddl_raw):
                    _ddl_tier = "doctorate"
                elif "postgraduate" in _ddl_raw or _re_dur.search(r"\bpg(dip|cert|diploma|certificate)\b", _ddl_raw):
                    _ddl_tier = "postgraduate"
                elif any(k in _ddl_raw for k in ("diploma", "certificate")):
                    _ddl_tier = "undergraduate"
            _ddl_years: float | None = None
            if _ddl_tier:
                _ddl_years = _dur_defaults_map.get(_ddl_tier)
                if _ddl_years is None and _ddl_tier == "doctorate":
                    _ddl_years = _dur_defaults_map.get("postgraduate")
            if _ddl_years is not None and isinstance(_ddl_years, (int, float)):
                _ddl_years_f = float(_ddl_years)
                _ddl_term = "year" if _ddl_years_f == 1.0 else "years"
                payload["duration"] = _ddl_years_f
                payload.setdefault("duration_term", _ddl_term)
                evidence.append({
                    "field_key": "duration",
                    "value": _ddl_years_f,
                    "confidence": 0.30,
                    "method": "uni_config:duration_default",
                    "source_url": url,
                    "snippet": (
                        f"institutional duration default from per-uni YAML: "
                        f"{_ddl_tier}={_ddl_years_f} {_ddl_term}"
                    ),
                })
                if emit:
                    await emit(
                        "status",
                        f"[DUR-DEFAULT] {payload.get('course_name', url)[:40]} — "
                        f"YAML default: {_ddl_years_f} {_ddl_term} ({_ddl_tier})",
                        phase="fallback",
                        kind="duration_default_applied",
                        url=url,
                        filled=["duration"],
                    )
    except Exception as exc:  # noqa: BLE001 — never abort extraction
        log.warning("duration degree_level_defaults fallback errored on %s: %s", url, exc)

    # ── Normalise any abbreviated intake months to full names ────────────────
    # Belt-and-suspenders: ai_extractor_run month_list transform, federation_json,
    # and any other extractor that slips through should already emit full names
    # after the 2026-07 fix, but normalise here as a fleet-wide safety net so
    # abbreviated months ("Mar","Jul","Nov") never reach the scraped_courses table
    # and trigger data_quality `invalid_intake_months` warnings on every re-scrape.
    _raw_im = payload.get("intake_months")
    if _raw_im:
        try:
            from app.services.scraper.extractors.intake import (
                _normalise_month as _nm_sc,
            )
            if isinstance(_raw_im, str):
                # Comma-separated string that wasn't converted to a list upstream
                import re as _re_sc
                _raw_im = [p.strip() for p in _re_sc.split(r"[,;/\n]", _raw_im) if p.strip()]
            _normed = [_nm_sc(m) for m in _raw_im if m]
            _normed = [m for m in _normed if m]
            if _normed:
                payload["intake_months"] = _normed
        except Exception:
            pass

    # ── No intake months ────────────────────────────────────────────────────
    _intake_months = payload.get("intake_months") or []
    if not _intake_months:
        # Per-uni rolling-enrollment fallback (research degrees).
        # When the page describes continuous / rolling enrolment AND the
        # uni opted in via YAML, surface that in the catalogue instead
        # of leaving the column blank. Curtin PhD / MPhil pages are the
        # canonical case ("Enrolment shall be continuous").
        _rolling_label: Optional[str] = None
        _rolling_markers: list[str] = []
        try:
            _ic = get_uni_config()
            if _ic is not None:
                _rolling_label = _ic.extraction.intake.rolling_enrollment_label
                _rolling_markers = list(
                    _ic.extraction.intake.rolling_enrollment_markers or []
                )
        except Exception:
            _rolling_label = None
            _rolling_markers = []
        if _rolling_label and _rolling_markers and any(
            m.lower() in _check_lower for m in _rolling_markers
        ):
            payload["intake_months"] = [_rolling_label]
            _intake_months = payload["intake_months"]

    # ── YAML default_by_level intake fallback ────────────────────────────────
    # When intake_months is still empty after all extractors (including the
    # rolling-enrollment fallback above), apply the YAML-configured default
    # month(s) for the course's degree level.  The synthetic evidence row is
    # marked with the configured default_source_note so reviewers can see the
    # intake was not extracted from the course page.
    if not _intake_months:
        try:
            _uc_intk = get_uni_config()
            _intk_cfg = _uc_intk.extraction.intake if _uc_intk else None
        except Exception:
            _intk_cfg = None
        if (
            _intk_cfg is not None
            and getattr(_intk_cfg, "use_default_when_missing", False)
            and getattr(_intk_cfg, "default_by_level", None)
        ):
            _dl_map: dict = dict(_intk_cfg.default_by_level or {})
            _course_dl = (payload.get("degree_level") or "").lower()
            # Normalise degree_level to the three recognised tiers.
            # ORDERING MATTERS: postgraduate is checked FIRST because its
            # keyword set contains "certificate" as a substring via
            # "graduate certificate" / "pgcert", and the undergraduate set
            # also contains the bare word "certificate".  Checking UG first
            # would misclassify "Postgraduate Certificate in Education" as
            # undergraduate.  Doctorate before undergraduate for same reason
            # ("edd" < "diploma" conflict is unlikely but order is explicit).
            _dl_tier = (
                "postgraduate" if any(
                    x in _course_dl for x in (
                        "master", "postgraduate", "graduate certificate",
                        "graduate diploma", "mba", "msc",
                        # NOTE: "ma " intentionally omitted — it is a substring
                        # of "diploma " and "pharmacy " which are UG.  "Master"
                        # catches MA/MArch/etc by their full title.
                        # Teaching qualifications: PGCE/PGDE/PGCert/PGDip do
                        # not contain "master" or "postgraduate" verbatim but
                        # are postgraduate level → September+January default.
                        "pgce", "pgde", "pgcert", "pgdip",
                    )
                )
                else "doctorate" if any(
                    x in _course_dl for x in ("doctor", "phd", "doctorate", "edd")
                )
                else "undergraduate" if any(
                    x in _course_dl for x in ("bachelor", "undergraduate", "diploma", "certificate")
                )
                else ""
            )
            _default_months: list[str] = (
                list(_dl_map.get(_dl_tier) or [])
                if _dl_tier else
                list(_dl_map.get("undergraduate") or [])  # safe global fallback
            )
            if _default_months:
                payload["intake_months"] = _default_months
                _intake_months = _default_months
                _src_note = getattr(_intk_cfg, "default_source_note", "YAML default intake")
                _intk_conf = float(getattr(_intk_cfg, "default_confidence", 0.4))
                evidence.append({
                    "field_key": "intake_months",
                    "value": _default_months,
                    "confidence": _intk_conf,
                    "method": "yaml_default_intake",
                    "snippet": (
                        f"{_src_note}: degree_level={_course_dl!r} → tier={_dl_tier!r} "
                        f"→ months={_default_months}"
                    ),
                })
                log.info(
                    "[INTAKE_DEFAULT] %r — intake_months filled with YAML default %r"
                    " (tier=%r, note=%r)",
                    payload.get("course_name") or url,
                    _default_months,
                    _dl_tier,
                    _src_note,
                )

    if not _intake_months:
        # Only warn if page had explicit intake-related text (avoid false
        # positives for universities that don't publish intake schedules).
        _INTAKE_HEADING_PATTERNS = (
            "intake", "start date", "commencement", "enrolment period",
            "semester start", "trimester start",
        )
        if any(p in _check_lower for p in _INTAKE_HEADING_PATTERNS):
            if "no_intake_months" not in _scrape_warnings:
                _scrape_warnings.append("no_intake_months")

    if _scrape_warnings:
        payload["scrape_warnings"] = _scrape_warnings

    footer = build_course_page_provenance_footer(payload)

    # Build extraction_method provenance map.
    #
    # For each field that appeared in evidence, record the method that produced
    # the value.  Two sentinel suffixes distinguish outcome:
    #
    #   "regex_fee"        — method produced a non-null/non-empty value
    #   "regex_fee:null"   — method was attempted but returned null/empty
    #
    # The :null sentinel is critical for regression detection: if a field is null
    # in both the before- and after-baseline that is only a true no-regression if
    # the *same* method was attempted.  "null because regex didn't fire" vs
    # "null because Gemini returned nothing" are different failure modes even
    # though both produce IELTS=None in the staged row.
    #
    # first-write-wins for successful values (mirrors setdefault throughout the
    # pipeline).  The :null sentinel is overwritten if a later evidence entry
    # produces a real value — we do not add null-fields to _seen_em so the
    # overwrite can happen.
    _extraction_method: dict[str, str] = {}
    _seen_em: set[str] = set()
    for _ev in evidence:
        _fk = _ev.get("field_key", "")
        if not _fk or _fk in _seen_em:
            continue
        _method = _ev.get("method") or "unknown"
        if payload.get(_fk) not in (None, "", 0, []):
            # Successful extraction — credit this method, lock the field.
            _extraction_method[_fk] = _method
            _seen_em.add(_fk)
        elif _fk not in _extraction_method:
            # Attempted but returned null/empty — record with :null suffix.
            # A later evidence entry that produces a value will overwrite this
            # (field not added to _seen_em, so the loop continues for _fk).
            _extraction_method[_fk] = f"{_method}:null"
    # Persist in payload so stage_course can store it without schema changes to
    # extract_course's callers (it is stripped in stage_course before DB write).
    if _extraction_method:
        payload["extraction_method"] = _extraction_method

    # ── Confidence scoring ─────────────────────────────────────────────────
    # Compute a 0-100 aggregate confidence score for this course payload based
    # on the presence of the five critical fields.  Low scores are surfaced as
    # scrape_warnings so the review UI can filter/flag them; we do NOT hard-
    # reject here because some universities have central fee pages (ECU, Bond)
    # where missing fee data is expected and handled separately.
    try:
        from app.services.scraper.confidence import (
            CONFIDENCE_WARN,
            format_confidence_log_line,
            score_payload as _score_payload,
        )

        _conf_result = _score_payload(payload)
        _conf_score = _conf_result["score"]
        _conf_level = _conf_result["level"]

        # Store the score in the payload so orchestrator/staging can gate on it.
        payload["_confidence_score"] = _conf_score
        payload["_confidence_level"] = _conf_level

        # Attach scrape warning for low-confidence courses so the review UI
        # can surface them prominently.
        if _conf_level in ("warn", "low"):
            _conf_warn_tag = f"confidence_{_conf_level}:{_conf_score}"
            _sw = list(payload.get("scrape_warnings") or [])
            if not any(w.startswith("confidence_") for w in _sw):
                _sw.append(_conf_warn_tag)
                payload["scrape_warnings"] = _sw

        # Emit to the live scrape log
        if emit:
            _log_line = format_confidence_log_line(
                payload.get("course_name") or "",
                _conf_result,
                url=url,
            )
            await emit(
                "status",
                _log_line,
                phase="extract",
                kind="confidence_score",
                url=url,
                score=_conf_score,
                level=_conf_level,
                missing=_conf_result.get("missing", []),
            )
    except Exception as _conf_exc:  # never break the pipeline
        log.warning("Confidence scoring failed on %s: %s", url, _conf_exc)

    # ── Per-band IELTS floor backfill (global, fail-safe) ─────────────────
    # When ielts_overall is set (from any source — regex, Gemini, vision,
    # central PDF, sibling cache, etc.) but the four sub-band slots are
    # empty, scan the full page text for "no band less than X" /
    # "no band below X" / "no individual band below X" and apply the floor
    # value to all four sub-bands. This catches the common phrasing used by
    # Federation, UNE, ECU and others that the per_course_modal extractor
    # only handles when an actual modal element is present on the page.
    # _PER_BAND_FLOOR_RE already covers all wording variants ("less than",
    # "below", "lower than", "under") and is the same regex the english_test
    # extractor's existing fallback paths use, so behaviour is consistent.
    try:
        if payload.get("ielts_overall") is not None and not any(
            payload.get(_k) is not None for _k in (
                "ielts_listening", "ielts_reading",
                "ielts_writing", "ielts_speaking",
            )
        ):
            from app.services.scraper.extractors.english_test import _PER_BAND_FLOOR_RE
            from app.services.scraper.extractors._text import html_to_text as _h2t_band  # noqa: PLC0415
            _band_text = _h2t_band(rendered_html or html or "")
            _band_match = _PER_BAND_FLOOR_RE.search(_band_text)
            if _band_match:
                _floor = float(_band_match.group(1))
                if 4.0 <= _floor <= 9.0:
                    for _slot in (
                        "ielts_listening", "ielts_reading",
                        "ielts_writing", "ielts_speaking",
                    ):
                        payload[_slot] = _floor
                    log.info(
                        "[IELTS BANDS] backfilled L/R/W/S=%s on %s from per-band floor clause",
                        _floor, url,
                    )
    except Exception as _band_exc:  # noqa: BLE001 — never break the pipeline
        log.warning("IELTS per-band floor backfill failed on %s: %s", url, _band_exc)

    # ── Per-uni default location fallback ──────────────────────────────────
    # When every extractor (regex, browser, Gemini, PDF) returns an empty
    # course_location, apply the YAML-configured default (if any).  The
    # canonical use-case is UTAS: Cloudflare-protected arts-soc and health
    # pages are often fetched via browser with partial HTML that omits the
    # Location panel entirely.  Without a fallback these real on-campus
    # Hobart courses fail the UTAS-specific blank-location guard in guards.py
    # and are rejected as online-only — even though they ARE on campus.
    # Safety: only fires when course_location is genuinely empty; never
    # overwrites a value set by a real extractor.
    try:
        # NOTE: get_uni_config is already imported at module level from
        # app.services.scraper.config.context — use it directly rather than
        # re-importing inside this try block (see note at ~L2839 for why
        # local re-imports of get_uni_config cause UnboundLocalError).
        _uc = get_uni_config()
        _default_loc = (
            getattr(getattr(_uc, "extraction", None), "default_course_location", None)
            or None
        )
        if _default_loc and not (payload.get("course_location") or "").strip():
            payload["course_location"] = _default_loc
            evidence.append({
                "field_key": "course_location",
                "value": _default_loc,
                "normalized": _default_loc,
                "method": "yaml:default_course_location",
                "confidence": 0.4,
                "snippet": f"YAML default_course_location='{_default_loc}' applied (no extractor found location)",
            })
            log.info(
                "[LOCATION DEFAULT] course=%r — applied YAML default '%s' "
                "(all extractors returned empty course_location)",
                payload.get("course_name") or url,
                _default_loc,
            )
    except Exception as _dloc_exc:  # noqa: BLE001 — never break the pipeline
        log.warning("default_course_location fallback failed on %s: %s", url, _dloc_exc)

    # ── YAML force_central_fee_stage flag ───────────────────────────────────
    # When extraction.fees.force_central_fee_stage=true in the per-uni YAML,
    # mark every course payload as has_central_fee_page=True so the
    # no_international_fee staging gate is bypassed for universities that
    # publish fees on a separate central schedule rather than per course page
    # (e.g. UTAS).  Only sets the flag; never clears an individual fee already
    # extracted by a real extractor.
    try:
        _uc_fees = getattr(get_uni_config(), "extraction", None)
        _uc_fees = getattr(_uc_fees, "fees", None)
        if getattr(_uc_fees, "force_central_fee_stage", False):
            payload.setdefault("has_central_fee_page", True)
            log.info(
                "[CENTRAL FEE] force_central_fee_stage=true — marking "
                "has_central_fee_page=True for %r",
                payload.get("course_name") or url,
            )
    except Exception as _fcfs_exc:  # noqa: BLE001 — never break the pipeline
        log.warning("force_central_fee_stage check failed on %s: %s", url, _fcfs_exc)

    # ── BCU location hard guard ──────────────────────────────────────────────
    # Belt-and-suspenders check that runs AFTER all extractors, AI passes,
    # repair loops, and defaults have had their chance to set course_location.
    # For bcu.ac.uk pages:
    #   • The ONLY valid source is location.bcu_keyfacts (div.course__key-info__inner).
    #   • Any value NOT in the BCU campus allowlist is cleared to None.
    #   • Covers Gemini PRIMARY / FALLBACK / repair_extractor paths that may
    #     have returned person names or testimonial text before being blocked.
    # Logging fields: location_source, original_location, final_location, course_url.
    if "bcu.ac.uk" in (url or "").lower():
        _BCU_LOCATION_ALLOWLIST = [
            "city centre", "city south", "margaret street",
            "royal birmingham conservatoire", "birmingham",
            "online", "distance learning", "uk campus",
        ]
        _bcu_raw_loc = (payload.get("course_location") or "").strip()
        _bcu_loc_source = "none"
        try:
            _bcu_loc_source = _best_ev_method("course_location") or "none"
        except Exception:  # noqa: BLE001
            pass
        if _bcu_raw_loc:
            _bcu_loc_ok = any(
                av in _bcu_raw_loc.lower() for av in _BCU_LOCATION_ALLOWLIST
            )
            _bcu_final = _bcu_raw_loc if _bcu_loc_ok else ""
            log.info(
                "[BCU LOCATION] course_url=%s location_source=%s "
                "original_location=%r final_location=%r allowlist_pass=%s",
                url, _bcu_loc_source, _bcu_raw_loc, _bcu_final, _bcu_loc_ok,
            )
            if not _bcu_loc_ok:
                log.warning(
                    "[BCU LOCATION CLEARED] %r is not a recognised BCU campus "
                    "(source=%s) — cleared to blank on %s",
                    _bcu_raw_loc, _bcu_loc_source, url,
                )
                payload["course_location"] = None
                # Also reset any stale "selected" evidence rows so the UI shows no
                # winner for course_location (prevents snippet from being rendered
                # as the display value when candidate_value is null).
                for _bcu_ev in evidence:
                    if _bcu_ev.get("field_key") == "course_location" and _bcu_ev.get("decision_status") == "selected":
                        _bcu_ev["decision_status"] = "needs_review"
        else:
            log.info(
                "[BCU LOCATION] course_url=%s location_source=%s "
                "original_location='' final_location='' allowlist_pass=n/a "
                "(blank — keyfacts panel had no Location row)",
                url, _bcu_loc_source,
            )

    # ── Generic YAML campus_allowlist enforcement ─────────────────────────────
    # Mirrors the BCU hard guard above but is driven by the per-university
    # YAML config (extraction.location.campus_allowlist) rather than a
    # hardcoded hostname check.  The allowlist is a list of canonical campus
    # names / city tokens.  Any course_location that shares NO token (case-
    # insensitive substring) with any allowlist entry is cleared to None.
    #
    # Use-case: INTI (newinti.edu.my) — fee tables expose "Sydney" (a Sydney
    # partner-campus column header) and bare "University" as campus labels.
    # The allowlist restricts staging to confirmed INTI campuses only.
    try:
        _gal_cfg = getattr(get_uni_config(), "extraction", None)
        _gal_allowlist: list[str] = list(getattr(_gal_cfg, "campus_allowlist", None) or [])
        if _gal_allowlist:
            _gal_raw = (payload.get("course_location") or "").strip()
            if _gal_raw:
                _gal_raw_lower = _gal_raw.lower()
                _gal_ok = any(av.lower() in _gal_raw_lower for av in _gal_allowlist)
                if not _gal_ok:
                    log.warning(
                        "[CAMPUS ALLOWLIST] cleared course_location=%r on %s "
                        "(not in campus_allowlist=%r)",
                        _gal_raw, url, _gal_allowlist,
                    )
                    payload["course_location"] = None
                    for _gal_ev in evidence:
                        if (
                            _gal_ev.get("field_key") == "course_location"
                            and _gal_ev.get("decision_status") == "selected"
                        ):
                            _gal_ev["decision_status"] = "needs_review"
    except Exception as _gal_exc:  # noqa: BLE001 — never break the pipeline
        log.warning("campus_allowlist check failed on %s: %s", url, _gal_exc)

    # ── Evidence selection finalisation ────────────────────────────────────
    # Mark the winning evidence row for each field as decision_status="selected"
    # so that scraped_field_evidence.selected mirrors the actual column values
    # written to scraped_courses. The Evidence Review panel relies on selected=True
    # to identify the authoritative source for each value.
    try:
        _finalize_evidence_selection(payload, evidence)
    except Exception as _ev_exc:  # never break the pipeline
        log.warning("_finalize_evidence_selection failed on %s: %s", url, _ev_exc)

    # ── Empty-text early exit ──────────────────────────────────────────────────
    # If text_len=0 from both static fetch AND browser retry, we never had
    # enough content to extract meaningful data.  Fee/IELTS defaults were
    # already suppressed above.  Return a skip sentinel so the orchestrator
    # counts this as fetch_failed_empty_text rather than staging a hollow record.
    if _bail_empty_text:
        _perf_flags["fallback_skipped_empty_text"] = True
        return {
            "url": url,
            "error": "fetch_failed_empty_text",
            "payload": {},
            "evidence": [],
            "_perf": _perf_flags,
        }

    return {
        "url": url,
        "payload": payload,
        "evidence": evidence,
        "provenance_footer": footer,
        "gemini_primary_cost_usd": _gemini_primary_cost,
        "gemini_calls": _gcl_get(),
        "_perf": _perf_flags,
    }
