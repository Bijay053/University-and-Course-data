"""Pure guard functions ported from artifacts/api-server/src/lib/scrape-guards.ts.

Four responsibilities:

1. ``is_generic_course_category_name`` — reject "courses" whose name is just a
   catalogue header ("Business", "Master's Degrees", "Single Subjects"). These
   slip into staging when the discovery crawl walks a category landing page
   and treats every nav item as a real course.

2. ``has_course_specific_fee_evidence`` — given a course name and the text of
   a generic university fee page, decide whether that page actually mentions
   the specific course we're trying to price. Stops the uni-PDF fee fallback
   from cloning the same $30K onto every Bachelor on the site.

3. ``should_trust_generic_university_fee_fallback`` — full guard wrapping (2)
   plus a slug-based shortcut (the URL itself looks course-specific) and a
   FEE-HELP heuristic (loan-limit text without an explicit course-fee phrase
   is almost always a HELP cap, not a course price).

4. ``should_stage_course`` — three Torrens-T007 staging filters that run
   BEFORE any DB write.  Returns ``(accept: bool, reject_reason: str)`` so
   the caller can log the reason and count skipped vs staged.

   Bug A — category landing pages: the extracted course name (H1-based, from
   ``payload["course_name"]``) does not start with a recognised degree-level
   qualifier.  Torrens example: H1 "3D Design and Animation courses" vs
   "Bachelor of 3D Design and Animation" — the latter passes, the former fails.

   Bug B — domestic-only courses: ``international_fee`` is still None after all
   extractors + AI fallback have run.  No international pricing data means the
   course should not be surfaced to international-student audiences.

   Bug C — online-only courses: ``study_mode`` is exactly "Online".  Business
   rule: only on-campus or blended courses are ingested.

Implementation mirrors the Node regexes byte-for-byte so the two pipelines
agree on every edge case while both still write to the shared production
``scraped_courses`` table.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

# Pre-compiled regexes — these run on every staged row in the orchestrator
# loop, so compile-once is worth the few extra lines.
_NORMALIZE_NON_ALNUM = re.compile(r"[^a-z0-9\s]+")
_NORMALIZE_WS = re.compile(r"\s+")
_RE_MASTERS_DEGREES = re.compile(r"^master'?s degrees?$", re.IGNORECASE)
_RE_GRAD_DIPLOMA = re.compile(r"^graduate diploma$", re.IGNORECASE)
_RE_GRAD_CERT = re.compile(r"^graduate certificate$", re.IGNORECASE)
_RE_SHORT_GENERICS = re.compile(
    r"^(design|business|health|hospitality|technology|education)$"
)
_RE_LONG_GENERICS = re.compile(
    r"^(single subjects?|digital badges?|on demand short courses?)$"
)
_RE_FEE_HELP_NEG = re.compile(
    r"\bfee-help\b|\bhelp loan\b|\bvet student loan\b|\bloan limit\b"
)
_RE_FEE_HELP_POS = re.compile(
    r"\b(course fee|tuition fee|international course fee schedule|international tuition)\b"
)
_RE_TOKEN_STOPWORDS = re.compile(
    r"^(bachelor|master|doctor|graduate|diploma|certificate|advanced|"
    r"course|degree|program|online|studies|partnership|with)$"
)


_GENERIC_CATEGORY_NAMES = frozenset(
    {
        "master s degrees",
        "masters degrees",
        "design",
        "business",
        "health",
        "hospitality",
        "technology",
        "education",
        "higher degrees by research",
        "higher degree by research",
        "research",
        "single subjects",
        "digital badges",
        "on demand short courses",
        "short courses",
        # KBS MBA specialisation listing page — not a real course.
        # URL: /courses/mba-master-of-business-administration/two-specialisations
        "two specialisations",
        "two specializations",
    }
)


def _normalize(text: str) -> str:
    """Lowercase, strip non-alphanumerics, collapse whitespace.

    Mirrors Node ``normalize`` (scrape-guards.ts:1) — the apostrophe in
    "Master's Degrees" is dropped here, which is why the lookup set spells
    it ``master s degrees``.
    """
    if not text:
        return ""
    s = text.lower()
    s = _NORMALIZE_NON_ALNUM.sub(" ", s)
    s = _NORMALIZE_WS.sub(" ", s).strip()
    return s


def is_generic_course_category_name(name: str) -> bool:
    """True when ``name`` is a catalogue header (e.g. "Business") rather than
    a real course title (e.g. "Master of Business Administration")."""
    if name is None:
        return True
    raw = name.strip()
    if _RE_MASTERS_DEGREES.match(raw):
        return True
    if _RE_GRAD_DIPLOMA.match(raw):
        return True
    if _RE_GRAD_CERT.match(raw):
        return True
    lower = _normalize(name)
    if not lower:
        return True
    if lower in _GENERIC_CATEGORY_NAMES:
        return True
    if _RE_SHORT_GENERICS.match(lower):
        return True
    if _RE_LONG_GENERICS.match(lower):
        return True
    return False


def _significant_course_tokens(course_name: str) -> list[str]:
    """Tokens from a course name worth using for course-specificity checks.

    Mirrors Node ``significantCourseTokens``: drop tokens shorter than 5
    chars and drop the degree-level stopwords. Leaves the field-of-study
    words ("administration", "psychology", "engineering") that disambiguate
    one course from another on a generic fee page.
    """
    return [
        tok
        for tok in _normalize(course_name).split(" ")
        if len(tok) > 4 and not _RE_TOKEN_STOPWORDS.match(tok)
    ]


def has_course_specific_fee_evidence(course_name: str, search_text: str) -> bool:
    """True when ``search_text`` looks like it's actually about ``course_name``.

    Two acceptance paths (either is enough):
      * The full normalized course name (>=10 chars) appears verbatim.
      * At least min(2, total) significant tokens appear.
    """
    lower_text = _normalize(search_text)
    lower_course = _normalize(course_name)
    if len(lower_course) >= 10 and lower_course in lower_text:
        return True
    tokens = _significant_course_tokens(course_name)
    if not tokens:
        return False
    matched = sum(1 for tok in tokens if tok in lower_text)
    return matched >= min(2, len(tokens))


def should_trust_generic_university_fee_fallback(
    fee_page: str,
    course_name: str,
    search_text: str,
    unique_amounts: list[int] | tuple[int, ...],
) -> bool:
    """Decide whether to clone a uni-wide fee page onto a single course.

    Trust the fallback when:
      1. The fee-page URL slug itself contains a significant course token
         (e.g. ``/business-administration-fees`` for an MBA), OR
      2. The page text mentions the course AND there is exactly one unique
         dollar amount on the page AND the amount isn't obviously a FEE-HELP
         loan-limit number (heuristic: HELP keywords without an explicit
         "course/tuition fee" phrase nearby).
    """
    try:
        slug = urlparse(fee_page).path.lower()
    except Exception:  # noqa: BLE001 — malformed URL → treat as no slug signal
        slug = ""

    tokens = _significant_course_tokens(course_name)
    if tokens and any(tok in slug for tok in tokens):
        return True

    if len(unique_amounts) != 1:
        return False

    lower_text = (search_text or "").lower()
    if _RE_FEE_HELP_NEG.search(lower_text) and not _RE_FEE_HELP_POS.search(lower_text):
        return False

    return has_course_specific_fee_evidence(course_name, search_text)


# ---------------------------------------------------------------------------
# Bug A: degree-qualifier check for category-landing-page rejection
# ---------------------------------------------------------------------------
# Matches the START of a course name. Any course title that begins with one of
# these qualifiers is a real degree-level page; anything else (e.g. "Hotel
# Management", "3D Design and Animation courses", "Faculty of Health") is a
# category-landing page whose H1 just names the subject area.
#
# Note: "Graduate" alone is intentionally NOT in the list — "Graduate" appears
# as a standalone word on Torrens category pages ("Graduate courses"). We
# require it to be followed by "Certificate" or "Diploma" to count.
_DEGREE_QUALIFIER_RE = re.compile(
    r"^(?:"
    r"bachelor|"
    r"master(?:s|'s)?(?!\s+of\s+ceremonies)|"  # reject "Master of Ceremonies"
    r"doctor(?:ate)?|"
    r"graduate\s+(?:certificate|diploma)|"
    r"advanced\s+diploma|"
    r"associate\s+degree|"
    r"diploma(?:\s+of|\s+in)?(?!\s+of\s+(?:ceremonies|honor))|"
    r"certificate\s+(?:i{1,4}v?|iv|iv\+?|\d+)\b|"  # Certificate III/IV/I/II
    r"certificate\s+(?:of|in)\b|"                   # Certificate of ..., Certificate in ...
    # ── Bug 3: well-known degree abbreviations ─────────────────────────────
    # Abbreviation-named courses (e.g. "MBA") must NOT be rejected as category
    # landing pages. Include common postgraduate (M*) and undergraduate (B*)
    # abbreviations plus Ph.D variants.
    r"mba\b|"           # Master of Business Administration
    r"mbs\b|"           # Master of Business Science
    r"mpa\b|"           # Master of Public Admin
    r"mph\b|"           # Master of Public Health
    r"med\b|"           # Master of Education
    r"mit\b|"           # Master of Info Tech
    r"msc\b|"           # Master of Science
    r"mcom\b|"          # Master of Commerce
    r"mres\b|"          # Master of Research
    r"mfin\b|"          # Master of Finance
    r"mba\s*\(|"        # MBA (Specialisation)
    r"phd\b|"           # Doctor of Philosophy (abbrev.)
    r"ph\.d\b|"
    r"dba\b|"           # Doctor of Business Admin
    r"bba\b|"           # Bachelor of Business Admin
    r"bbs\b|"           # Bachelor of Business Science
    r"bcom\b|"          # Bachelor of Commerce
    r"bbus\b|"          # Bachelor of Business
    r"bit\b|"           # Bachelor of IT
    r"bsw\b|"           # Bachelor of Social Work
    r"bsc\b|"           # Bachelor of Science
    r"beng\b|"          # Bachelor of Engineering
    r"ba\b(?:\s|$)"     # Bachelor of Arts (must be word-bounded)
    r")",
    re.IGNORECASE,
)


def _name_has_degree_qualifier(name: str) -> bool:
    """True when *name* starts with a recognised degree-level prefix."""
    return bool(_DEGREE_QUALIFIER_RE.match((name or "").strip()))


# URL path suffixes that always indicate a category listing page rather than
# a real course, regardless of how the page title is rendered.  Checked
# BEFORE the degree-qualifier name check so that pages whose H1 accidentally
# gains a degree prefix (e.g. "MBA – Two Specialisations" from the MBA title
# extractor) are still rejected.
_CATEGORY_URL_SUFFIXES: tuple[str, ...] = (
    "/two-specialisations",
    "/two-specializations",
)


def should_stage_course(
    course_name: str,
    payload: dict[str, Any],
    source_url: str | None = None,
) -> tuple[bool, str]:
    """Three-filter staging gate (Bugs A, B, and C from the T007 sweep).

    Returns ``(True, "accepted")`` when the course passes all filters, or
    ``(False, reject_reason)`` on the first failing check.  Reject reasons
    are designed to be grep-able in production logs:

    * ``"category_landing_page"`` — H1/course-name lacks a degree qualifier
                                     OR URL matches a known category-page suffix
    * ``"no_international_fee"`` — international_fee is None after full extraction
    * ``"online_only"``           — study_mode is exactly "Online" (case-insensitive).
                                     Only on-campus or blended courses are ingested.
                                     Marked transient so courses re-stage if campus
                                     options are later added by the institution.

    Callers must invoke this AFTER all extractors + AI fallback have run (i.e.
    just before the DB write in ``stage_course``) so Bug B and C have settled
    payloads to inspect.
    """
    # URL-based category-page rejection — runs FIRST so that pages whose
    # title accidentally gains a degree-level prefix (e.g. "MBA – Two
    # Specialisations" from the MBA title extractor) are still rejected.
    # Name-matching alone cannot catch these because the prefix makes the
    # name pass the degree-qualifier check.
    if source_url:
        _url_path = source_url.lower().split("?")[0]  # strip query string
        if any(_url_path.endswith(sfx) for sfx in _CATEGORY_URL_SUFFIXES):
            return (False, "category_landing_page")

    # Bug A: reject pages whose extracted title has no degree-level qualifier.
    # Prefer payload["course_name"] (from H1 via course_name extractor) over
    # the discovery-link name (passed as course_name param) — the H1 is the
    # canonical page title and the most reliable signal.
    effective_name = (payload.get("course_name") or course_name or "").strip()
    if effective_name and not _name_has_degree_qualifier(effective_name):
        return (False, "category_landing_page")

    # Explicit domestic-only flag: set by extractors when the page text
    # states "this course is not available to international students" etc.
    if payload.get("domestic_only"):
        return (False, "domestic_only")

    # Bug C: online-only courses with no real physical campus are rejected.
    # The study_mode field is the authoritative signal — location strings like
    # "United Theological College" or "Wagga Wagga Campus" are partner/delivery
    # names and do NOT imply an on-campus offering if the mode is purely Online.
    # Rule: reject when study_mode has "online" but NO campus/blended keyword.
    # Courses that are genuinely blended (study_mode="On Campus, Online") pass.
    # Marked transient so the course re-stages if a campus option is added later.
    _study_mode = (payload.get("study_mode") or "").strip().lower()
    _has_campus_component = any(
        kw in _study_mode
        for kw in (
            "on campus", "on-campus", "campus", "on site", "on-site",
            "face-to-face", "blended", "in-person", "in person",
        )
    )
    if "online" in _study_mode and not _has_campus_component:
        return (False, "online_only")

    # Secondary check: mode is exactly "Blended" but no physical campus
    # was found by any extractor (location_text is null/empty). Courses
    # like "MBA Online" where Gemini still returns "Blended" instead of
    # "Online" are effectively online-only — reject them too.
    if _study_mode == "blended" and not (payload.get("location_text") or "").strip():
        return (False, "online_only")

    # Bug B: no international fee after all extraction is done.
    # If the university has a centralized fee page, the fee may simply not
    # be listed for this specific course yet — stage for human review instead
    # of auto-rejecting.  International fees on a separate page are legitimate.
    if payload.get("international_fee") is None:
        if payload.get("has_central_fee_page"):
            return (True, "accepted")
        return (False, "no_international_fee")

    return (True, "accepted")


# ---------------------------------------------------------------------------
# Phase A — Page blocklist (URL + title)
# ---------------------------------------------------------------------------
# Single source of truth for "this is definitely not a course page".
# Returns (blocked: bool, reason: str). Intentionally narrower and more
# explicit than the larger discovery blocklist so callers (discovery BFS,
# staging gate, future per-provider overrides) can share one rulebook
# and the audit log shows a clean, single reason.
#
# Design rules:
#   * URL match wins over title match (URL is more deterministic).
#   * Reasons are stable string keys so they can be grep'd in logs and
#     counted in metrics.
#   * No regex backtracking risk: every pattern is a literal substring
#     against a lowercased path.
#   * Title matching uses anchored prefixes ("apply", "fees and ...") so
#     titles that happen to mention "apply" mid-sentence don't trip it.

_BLOCK_URL_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    # Application / enrolment funnels — never a course catalogue
    ("/apply",                  "apply_page"),
    ("/application",            "apply_page"),
    ("/how-to-apply",           "apply_page"),
    ("/how-to-enrol",           "apply_page"),
    ("/enrol",                  "apply_page"),
    ("/enrolment",              "apply_page"),
    # Money pages — fees, scholarships, aid (the COURSE page lists fees;
    # the standalone fee page does not list a course).
    ("/fees-and-scholarships",  "fee_page"),
    ("/fees-and-costs",         "fee_page"),
    ("/scholarships",           "scholarship_page"),
    ("/scholarship/",           "scholarship_page"),
    ("/financial-aid",          "scholarship_page"),
    # Calendar / dates
    ("/key-dates",              "key_dates_page"),
    ("/keydates",               "key_dates_page"),
    ("/important-dates",        "key_dates_page"),
    ("/academic-calendar",      "key_dates_page"),
    # News / events / blog — never courses
    ("/news/",                  "news_page"),
    ("/newsroom/",              "news_page"),
    ("/events/",                "events_page"),
    ("/event/",                 "events_page"),
    ("/blog/",                  "blog_page"),
    ("/blogs/",                 "blog_page"),
    ("/stories/",               "blog_page"),
    ("/story/",                 "blog_page"),
    # School / faculty / department landing pages
    ("/schools/",               "faculty_page"),
    ("/school/",                "faculty_page"),
    ("/faculty/",               "faculty_page"),
    ("/faculties/",             "faculty_page"),
    ("/department/",            "faculty_page"),
    ("/departments/",           "faculty_page"),
    # Generic info / about / contact
    ("/contact",                "contact_page"),
    ("/about-us",               "about_page"),
    ("/about/",                 "about_page"),
    ("/testimonials",           "testimonials_page"),
    ("/staff/",                 "staff_page"),
    ("/people/",                "staff_page"),
    # Campus / student-life — not academic catalogues
    ("/campus/",                "campus_page"),
    ("/campus-life",            "campus_page"),
    ("/student-life",           "campus_page"),
    ("/accommodation",          "campus_page"),
    ("/library/",               "campus_page"),
    # Open day / marketing funnels
    ("/open-day",               "marketing_page"),
    ("/info-night",             "marketing_page"),
    ("/why-",                   "marketing_page"),
)

# Title prefix matches.  Lowercased and stripped before comparison, so
# "Fees and Scholarships | UTAS" → "fees and scholarships" matches the
# "fees and " prefix below.
_BLOCK_TITLE_PREFIXES: tuple[tuple[str, str], ...] = (
    ("apply now",                       "apply_page"),
    ("how to apply",                    "apply_page"),
    ("application",                     "apply_page"),
    ("fees and ",                       "fee_page"),
    ("scholarships",                    "scholarship_page"),
    ("key dates",                       "key_dates_page"),
    ("important dates",                 "key_dates_page"),
    ("news",                            "news_page"),
    ("blog",                            "blog_page"),
    ("events",                          "events_page"),
    ("contact us",                      "contact_page"),
    ("contact",                         "contact_page"),
    ("about us",                        "about_page"),
    ("testimonials",                    "testimonials_page"),
)


def is_blocked_page(url: str | None, title: str | None = None) -> tuple[bool, str]:
    """Return ``(True, reason)`` when this URL/title is definitely not a
    course detail or course listing page; ``(False, "")`` otherwise.

    Phase A safety net.  Callers:
      * Discovery BFS — skip the URL before enqueuing it.
      * Staging gate — refuse to stage a course whose ``source_url`` is
        on the blocklist (defence in depth: discovery should have caught
        it, but a regression there must not silently publish bad data).

    The function is **conservative**: only patterns that we know with
    100% confidence are non-course pages are listed.  Generic words
    appearing inside a real course slug (e.g. ``/bachelor-of-arts-and-
    contact-with-society``) will not match any substring here because
    every pattern includes its leading slash.
    """
    if url:
        try:
            path = urlparse(url).path.lower()
        except Exception:  # noqa: BLE001 — malformed URL → no URL signal
            path = ""
        if path:
            for pat, reason in _BLOCK_URL_SUBSTRINGS:
                if pat in path:
                    return (True, reason)

    if title:
        norm_title = re.sub(r"\s+", " ", title).strip().lower()
        # Strip common "| University Name" suffixes so "Apply Now | UNE"
        # still matches the "apply now" prefix.
        if "|" in norm_title:
            norm_title = norm_title.split("|", 1)[0].strip()
        for pfx, reason in _BLOCK_TITLE_PREFIXES:
            if norm_title.startswith(pfx):
                return (True, reason)

    return (False, "")


# ---------------------------------------------------------------------------
# Phase A — Source-evidence enforcement on critical fields
# ---------------------------------------------------------------------------
# Critical fields are the ones we publish to international students and
# whose accuracy materially affects their decisions.  For each of these,
# we require at least one evidence row with BOTH a non-empty source_url
# AND a non-empty snippet (the actual on-page text we extracted from).
# If proof is missing, the field is dropped (set to None) on the staged
# course — better to publish "unknown" than a guess.
_CRITICAL_FIELDS_REQUIRING_PROOF: tuple[str, ...] = (
    "international_fee",
    "ielts_overall",
    "pte_overall",
    "toefl_overall",
    "duolingo_overall",
    "cambridge_overall",
    "location_text",
    "study_mode",
    "duration_text",
)


def enforce_source_evidence(
    payload: dict[str, Any],
    evidence: list[dict[str, Any]] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Drop critical fields from ``payload`` that lack source proof.

    Returns ``(cleaned_payload, dropped_field_keys)``.  A field is kept
    only when at least one evidence row for it has BOTH a non-empty
    ``source_url`` AND a non-empty ``snippet``.  Otherwise the field is
    set to ``None`` in the returned payload so the staging insert writes
    NULL (and the row will fall to review for that field).

    This is intentionally narrow: only the fields in
    ``_CRITICAL_FIELDS_REQUIRING_PROOF`` are checked.  Everything else
    passes through untouched so we don't accidentally null out fields
    whose extractors don't yet emit evidence rows.
    """
    if not isinstance(payload, dict):
        return ({}, [])

    # Build a quick index: field_key -> True if at least one evidence row
    # for it has both a source URL and a snippet.
    proven: set[str] = set()
    for ev in evidence or []:
        if not isinstance(ev, dict):
            continue
        fk = ev.get("field_key")
        if not fk:
            continue
        src = (ev.get("source_url") or "").strip()
        snip = (ev.get("snippet") or "").strip()
        if src and snip:
            proven.add(str(fk))

    cleaned = dict(payload)
    dropped: list[str] = []
    for field_key in _CRITICAL_FIELDS_REQUIRING_PROOF:
        if cleaned.get(field_key) is None:
            continue  # nothing to drop
        if field_key not in proven:
            cleaned[field_key] = None
            dropped.append(field_key)
    return (cleaned, dropped)
