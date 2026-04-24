"""Pure guard functions ported from artifacts/api-server/src/lib/scrape-guards.ts.

Three responsibilities:

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

Implementation mirrors the Node regexes byte-for-byte so the two pipelines
agree on every edge case while both still write to the shared production
``scraped_courses`` table.
"""
from __future__ import annotations

import re
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
