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

import logging
import re
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

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
# Category page titles that start with a degree-level keyword but are NOT
# individual courses — e.g. "Diploma Programs", "Bachelor Degrees", etc.
# Without this check, "Diploma Programs" passes the degree-qualifier guard
# because "Diploma" is a recognised qualifier, so the name_has_degree_qualifier
# test returns True even though the full title is clearly a category header.
_RE_LEVEL_CATEGORY = re.compile(
    r"^(?:diploma|certificate|bachelor|master(?:s)?|graduate|postgraduate|"
    r"undergraduate|doctoral?|phd)\s+"
    r"(?:programs?|degrees?|courses?|pathways?|studies|qualifications?|"
    r"offerings?|options?)$",
    re.IGNORECASE,
)
# Navigation / promotional page titles — these are site navigation or
# marketing pages, not individual course detail pages.  Catches Teesside's
# "Study here" / "Study At Teesside", generic "Find a Course" listings,
# "Why Study With Us" promotional pages, etc.
_RE_NAV_PAGE_TITLE = re.compile(
    r"^(?:"
    # "Study here", "Study online", "Study abroad"
    r"study\s+(?:here|online|abroad)"
    # "Study At Teesside", "Study In London", "Study With Us" — any trailing words
    r"|study\s+(?:at|in|with)\b.*"
    r"|why\s+study(?:\s+with\s+us)?"
    r"|find\s+a\s+course"
    r"|browse\s+(?:our\s+)?courses?"
    r"|all\s+(?:our\s+)?courses?"
    r"|our\s+courses?"
    r"|explore\s+(?:our\s+)?courses?"
    r"|courses?\s+(?:listing|search|overview|finder)"
    r"|(?:undergraduate|postgraduate)\s+courses?"
    r")$",
    re.IGNORECASE,
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
        # ACU / generic: category hub pages that contain a degree keyword but
        # are programme-listing pages, not individual course detail pages.
        "diploma programs",
        "diploma programme",
        "admission pathways",
        "admission pathway",
        "pathway programs",
        "pathway programme",
        # Generic research / supervisor hub pages discovered via nav crawl.
        "join a research project",
        "meet our supervisors",
        "meet our supervisor",
        "research supervisors",
        "graduate research supervisors",
        "industry opportunities",
        "industry engagement",
        "joint phds",
        "joint phd",
        "research degrees",
        "research degree",
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
    # Catch "Diploma Programs", "Bachelor Degrees", "Graduate Pathways" etc.
    # These start with a degree-level keyword so they pass _name_has_degree_qualifier
    # and slip through the should_stage_course name check — this guard catches them
    # before they reach the staging decision.
    if _RE_LEVEL_CATEGORY.match(raw):
        return True
    # Catch navigation / promotional page titles: "Study here", "Study At Teesside",
    # "Why Study With Us", "Find a Course", "Browse Courses", etc.
    # These are site navigation or marketing pages found by the discovery crawler
    # when URL patterns are too broad — they reach extraction before the degree-
    # qualifier check can catch them.
    if _RE_NAV_PAGE_TITLE.match(raw.strip()):
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
    r"postgraduate\s+(?:certificate|diploma)|"  # UK PgCert / PgDip (Huddersfield)
    r"pgce\b|"                                    # Postgraduate Certificate of Education (UK)
    r"pgcert\b|"                                  # PgCert (abbreviated)
    r"pgdip\b|"                                   # PgDip (abbreviated)
    r"foundation\s+degree|"                      # UK Foundation Degree (full phrase)
    r"fda\b|"                                     # Foundation Degree Arts (UK)
    r"fdsc\b|"                                    # Foundation Degree Science (UK)
    r"fd\b|"                                      # Foundation Degree (bare abbrev, UK)
    r"certhe\b|"                                  # Certificate of Higher Education (UK)
    r"diphe\b|"                                   # Diploma of Higher Education (UK)
    r"university\s+certificate|"                  # University Certificate in ...
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
    r"msci\b|"          # Master of Science (integrated, UK)
    r"mcom\b|"          # Master of Commerce
    r"mres\b|"          # Master of Research
    r"mfin\b|"          # Master of Finance
    r"meng\b|"          # Master of Engineering (integrated, UK)
    r"mbiol\b|"         # Master of Biology (integrated, UK)
    r"mchem\b|"         # Master of Chemistry (integrated, UK)
    r"mphys\b|"         # Master of Physics (integrated, UK)
    r"mmath\b|"         # Master of Mathematics (integrated, UK)
    r"mds\b|"           # Master of Data Science
    r"ma\b|"            # Master of Arts
    r"mba\s*\(|"        # MBA (Specialisation)
    r"phd\b|"           # Doctor of Philosophy (abbrev.)
    r"ph\.d\b|"
    r"dba\b|"           # Doctor of Business Admin
    r"dclinpsychol\b|"  # Doctor of Clinical Psychology (UK)
    r"edd\b|"           # Doctor of Education (UK)
    r"llb\b|"           # Bachelor of Laws (UK)
    r"ll\.b\b|"         # LLB with dots (LL.B.)
    r"llm\b|"           # Master of Laws (UK)
    r"bba\b|"           # Bachelor of Business Admin
    r"bbs\b|"           # Bachelor of Business Science
    r"bcom\b|"          # Bachelor of Commerce
    r"bbus\b|"          # Bachelor of Business
    r"bit\b|"           # Bachelor of IT
    r"bsw\b|"           # Bachelor of Social Work
    r"bsc\b|"           # Bachelor of Science
    r"beng\b|"          # Bachelor of Engineering
    r"gdip\b|"          # Graduate Diploma (abbreviated, UK)
    r"pdip\b|"          # Postgraduate Diploma (abbreviated, UK)
    r"pcert\b|"         # Postgraduate/Professional Certificate (abbreviated, UK)
    r"iqts\b|"          # International Qualified Teacher Status (UK)
    r"qts\b|"           # Qualified Teacher Status (UK)
    r"bnurs\b|"         # Bachelor of Nursing (UK)
    r"bmid\b|"          # Bachelor of Midwifery (UK)
    r"mphil\b|"         # Master of Philosophy (distinct from mph = Public Health)
    r"mpharm\b|"        # Master of Pharmacy (UK; also in trailing list)
    r"march\b|"         # Master of Architecture (UK, e.g. "MArch / march")
    r"gdl\b|"           # Graduate Diploma in Law (UK conversion course)
    r"hnc\b|"           # Higher National Certificate (UK vocational)
    r"hnd\b|"           # Higher National Diploma (UK vocational)
    r"prof\s+doc\b|"               # Professional Doctorate (abbreviated, UK)
    r"prof\s+gradcert\b|"          # Professional Graduate Certificate
    r"professional\s+doctorate|"   # Professional Doctorate (full phrase)
    r"advanced\s+university\s+diploma|"   # Advanced University Diploma
    r"university\s+statement\s+of\s+credit|"  # University Statement of Credit
    r"international\s+mba\b|"      # International MBA (subject-first form)
    r"ba\b(?:\s|$)"     # Bachelor of Arts (must be word-bounded)
    r")",
    re.IGNORECASE,
)


# Australian / UK national qualification codes that sometimes prefix a
# course title, e.g. "ICT50220 Diploma of Information Technology" or
# "BSB40120 Certificate IV in Business". The code is NOT a degree
# qualifier but the text after it is — strip the code before matching.
# Pattern: 2-6 uppercase letters followed by 4-6 digits (e.g. ICT50220,
# BSB40120, CHC33015). Case-insensitive so mixed-case entries like
# "Ict50220" also match.
_QUAL_CODE_PREFIX_RE = re.compile(r"^[A-Za-z]{2,6}\d{4,6}\s+", re.I)

# ---------------------------------------------------------------------------
# Punctuation / spacing normalisers applied before qualifier matching so that
# universities that write the same award in different ways all resolve to the
# canonical abbreviation recognised by the regexes above.
#
# Canonical forms targeted:
#   LL.B. → LLB     M.Phil → MPhil     Ph.D → PhD   (inter-letter dots)
#   PG Cert → PGCert    PG Dip → PGDip              (space after "PG")
# ---------------------------------------------------------------------------
# Strip a dot that sits *between* two letters (abbreviation dots).
# Anchored with lookbehind/lookahead so only inter-letter dots are removed;
# sentence-ending dots are left alone.
_QUAL_STRIP_DOTS_RE = re.compile(r"(?<=[A-Za-z])\.(?=[A-Za-z])", re.I)

# Collapse the space between "PG" and the next token when that token looks
# like a qualification keyword (Cert, Dip, Cert(s) etc.).  Case-insensitive.
# Lookahead keeps the following word intact: "PG Cert" → "PGCert".
_QUAL_PG_SPACE_RE = re.compile(r"\bPG\s+(?=(?:cert|dip)\w*\b)", re.I)


def _normalise_for_qualifier_match(text: str) -> str:
    """Return *text* with punctuation/spacing collapsed to canonical form.

    Applied before the degree-qualifier regex so that all typography
    variants of the same award are recognised:
      - ``LL.B.``  → ``LLB``
      - ``M.Phil`` → ``MPhil``
      - ``Ph.D``   → ``PhD``
      - ``PG Cert`` / ``Pg Cert`` → ``PGCert``
      - ``PG Dip``  / ``Pg Dip``  → ``PGDip``
    """
    t = _QUAL_STRIP_DOTS_RE.sub("", text)
    t = _QUAL_PG_SPACE_RE.sub("PG", t)
    return t


# ---------------------------------------------------------------------------
# Anywhere-in-title qualifier check
# ---------------------------------------------------------------------------
# The leading ``_DEGREE_QUALIFIER_RE`` (anchored to ``^``) and trailing
# ``_TRAILING_QUALIFIER_RE`` (anchored to ``$``) already cover most forms.
# This regex catches qualifiers that appear in the *middle* of a title, e.g.:
#   "Full-time MBA Programme"          → MBA (middle)
#   "QTS Primary Education pathway"    → QTS (middle)
#   "Translation (MA) - Full time"     → MA  (bracketed, mid-title)
#   "Architecture RIBA 2 March"        → MArch via march token (mid/end)
#   "Crime, Policy and Security Prof Doc" → Prof Doc (end with words before)
#
# Uses \b word boundaries throughout so "Drama" never matches "MA",
# "management" never matches "MA", etc.
#
# NOTE: ``\bmarch\b`` will match the month name "March" with IGNORECASE.
# This is an accepted trade-off: in practice, course H1 titles from
# university pages do not contain the month name as a standalone word, and
# the common false-positive form ("courses starting in March") is blocked
# upstream by ``is_blocked_page`` before ``_name_has_degree_qualifier`` runs.
_ANYWHERE_QUALIFIER_RE = re.compile(
    r"\b(?:"
    # ── Masters ────────────────────────────────────────────────────────────
    r"ma|msc|msci|mba|march|meng|mphil|mpharm|mres|mfin|mds|"
    r"mbs|mpa|mph|med|mit|mcom|mbiol|mchem|mphys|mmath|llm|"
    # ── Doctorates ─────────────────────────────────────────────────────────
    r"phd|dba|dclinpsychol|edd|"
    r"prof\s+doc|"        # Professional Doctorate (abbreviated)
    # ── Bachelors ──────────────────────────────────────────────────────────
    r"ba|bsc|beng|llb|bnurs|bmid|bba|bbs|bcom|bbus|bit|bsw|mbbs|bds|"
    # ── PG awards ──────────────────────────────────────────────────────────
    r"pgce|pgcert|pgdip|gdl|"
    # ── Foundation / vocational ────────────────────────────────────────────
    r"fda|fdsc|fd|certhe|diphe|hnc|hnd|"
    # ── Teacher status ─────────────────────────────────────────────────────
    r"qts|iqts"
    r")"
    r"(?:\s*\(\s*hons\.?\s*\))?"    # optional (Hons) immediately after
    r"(?!\w)",                       # must NOT be followed by a word char
    re.IGNORECASE,
)


# Some universities name courses with the degree abbreviation at the END
# rather than the start, e.g. UEL's "Primary with Early Years (3-7) Pgce"
# or "Business Management MBA". These are genuine degree pages whose H1
# puts the subject first and the award last. Match trailing abbreviations
# to prevent false category_landing_page rejections.
#
# UK universities (e.g. Coventry) commonly append "(Hons)" after the award:
#   "Acting for Stage and Screen BA (Hons)"
#   "Aerospace Engineering MEng/BEng (Hons)"
#   "Computer Games Programming MSci/BSc (Hons)"
# The optional `(?:\(\s*hons\s*\))?` group handles that suffix so the
# abbreviation before it is still recognised as a degree qualifier.
#
# Requires the abbreviation (+ optional Hons) to be the last word so that
# "Learn about MBA programmes" doesn't match — it has words after.
_TRAILING_QUALIFIER_RE = re.compile(
    r"(?:"
    # Full trailing phrases (no abbreviation form).
    r"\bfoundation\s+degree"
    r"|\bprof\s+gradcert\b"              # Professional Graduate Certificate
    r"|\bprofessional\s+doctorate\b"     # Professional Doctorate (full phrase trailing)
    r"|"
    # Abbreviations that may be followed by optional "(Hons)" and/or "Top-up".
    # Examples (all real Coventry / UK course names):
    #   "Acting for Stage and Screen BA (Hons)"
    #   "Aerospace Engineering MEng/BEng (Hons)"
    #   "International Business BA (Hons) Top-up"
    #   "Applied Mechanical Engineering BEng (Hons) Top-up"
    #   "Computing QTS" / "Primary Education iQTS"
    #   "Early Childhood Studies FdA"
    #   "Education Studies CertHE"
    r"\b(?:"
    r"pgce|pgcert|pgdip|"
    r"mba|mbs|mpa|mph|med|mit|msc|msci|meng|mcom|mres|mfin|"
    r"mphil|mpharm|march|"               # MPhil / MPharm / MArch (trailing forms)
    r"phd|ph\.d|dba|dclinpsychol|edd|"
    r"bba|bbs|bcom|bbus|bit|bsw|bsc|beng|"
    r"bnurs|bmid|"                        # BNurs / BMid (trailing forms)
    r"ba|llb|ll\.b|llm|mbbs|bds|"
    r"gdl|hnc|hnd|"                       # GDL / HNC / HND (trailing forms)
    r"fda|fdsc|fd|certhe|diphe|"          # UK Foundation Degree / CertHE / DipHE
    r"iqts|qts"                           # Qualified Teacher Status variants
    r")\s*(?:\(\s*hons\.?\s*\))?\s*(?:top[\s\-]up)?"
    r")\s*[\)\]]*\s*$",
    re.IGNORECASE,
)


def _name_has_degree_qualifier(name: str) -> bool:
    """True when *name* (or *name* stripped of a leading qualification code)
    starts with a recognised degree-level prefix, OR ends with a well-known
    degree abbreviation (e.g. "Primary with Early Years (3-7) Pgce").

    Handles entries like "ICT50220 Diploma of Information Technology" where
    an Australian/UK national qualification code precedes the degree title.

    Normalises punctuation/spacing before matching so that all typography
    variants of the same award are detected:
      ``LL.B.`` == ``LLB``,  ``M.Phil`` == ``MPhil``,
      ``PG Cert`` == ``Pg Cert`` == ``PGCert``,
      ``PG Dip``  == ``Pg Dip``  == ``PGDip``.
    """
    raw = (name or "").strip()
    normed = _normalise_for_qualifier_match(raw)

    # 1. Leading check — qualifier at the START of the title (fastest path,
    #    catches the majority of courses, e.g. "MSc Computer Science").
    if _DEGREE_QUALIFIER_RE.match(normed):
        return True
    # 2. Leading check after stripping a national qualification code prefix
    #    (e.g. "ICT50220 Diploma of Information Technology").
    stripped = _QUAL_CODE_PREFIX_RE.sub("", normed)
    if stripped != normed and _DEGREE_QUALIFIER_RE.match(stripped):
        return True
    # 3. Trailing check — qualifier at the END of the title, with optional
    #    "(Hons)" / "Top-up" suffix (e.g. "Business Management MBA (Hons)").
    if _TRAILING_QUALIFIER_RE.search(normed):
        return True
    # 4. Anywhere check — qualifier appears in the MIDDLE of the title using
    #    word-boundary matching.  Catches:
    #      "Full-time MBA programme"
    #      "QTS Primary Education pathway"
    #      "Translation (MA) - Full time"
    #      "Architecture RIBA 2 March"
    #      "Crime, Policy and Security Prof Doc"
    if _ANYWHERE_QUALIFIER_RE.search(normed):
        return True
    return False


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

    * ``"category_landing_page_url_suffix"`` — URL ends with a known category suffix
                                              (e.g. ``/two-specialisations``)
    * ``"category_landing_page_missing_degree_qualifier"`` — H1/course-name extracted
                                              from the page lacks a degree-level word.
                                              Suppress per-uni via
                                              ``extraction.staging.skip_degree_qualifier_check: true``.
    All ``category_landing_page_*`` reasons are bucketed as ``"category_landing_page"``
    in run-summary stats and the public UI.
    * ``"no_international_fee"`` — international_fee is None after full extraction
    * ``"online_only"``           — study_mode is exactly "Online" (case-insensitive).
                                     Only on-campus or blended courses are ingested.
                                     Marked transient so courses re-stage if campus
                                     options are later added by the institution.

    Callers must invoke this AFTER all extractors + AI fallback have run (i.e.
    just before the DB write in ``stage_course``) so Bug B and C have settled
    payloads to inspect.
    """
    # ── Online-filter opt-out: resolve ONCE before any online-related check ──
    # Distance-education-heavy universities (e.g. CSU, OUA) set
    # ``extraction.filters.online_only.enabled: false`` in their per-uni YAML.
    # We must compute this BEFORE the URL-slug "-online" check below AND before
    # the study_mode check further down, so that both checks honour the override.
    # Fail-safe: if the config is missing/broken we keep the historical reject.
    try:
        from app.services.scraper.config.context import (  # noqa: PLC0415
            get_uni_config as _get_uni_config,
        )
        _oc_cfg = _get_uni_config()
        if (
            _oc_cfg is None
            or _oc_cfg.extraction is None
            or _oc_cfg.extraction.filters is None
            or _oc_cfg.extraction.filters.online_only is None
        ):
            _online_filter_enabled = True
        else:
            _online_filter_enabled = bool(
                _oc_cfg.extraction.filters.online_only.enabled
            )
    except Exception:  # noqa: BLE001 — never crash on config lookup
        log.warning(
            "online_only YAML lookup raised; falling back to historical "
            "reject behaviour",
            exc_info=True,
        )
        _online_filter_enabled = True  # fail safe: keep historical behaviour

    # URL-based category-page rejection — runs FIRST so that pages whose
    # title accidentally gains a degree-level prefix (e.g. "MBA – Two
    # Specialisations" from the MBA title extractor) are still rejected.
    # Name-matching alone cannot catch these because the prefix makes the
    # name pass the degree-qualifier check.
    if source_url:
        _url_path = source_url.lower().split("?")[0]  # strip query string
        if any(_url_path.endswith(sfx) for sfx in _CATEGORY_URL_SUFFIXES):
            return (False, "category_landing_page_url_suffix")

        # URL-slug online detection: if the last path segment ends with
        # "-online" the university explicitly published this as an online-only
        # course (e.g. /course/graduate-certificate-in-business-administration-online).
        # Gated on _online_filter_enabled so that per-uni YAML overrides
        # (online_only.enabled: false) are respected here too.
        _slug = _url_path.rstrip("/").rsplit("/", 1)[-1]
        if _slug.endswith("-online"):
            if _online_filter_enabled:
                log.info(
                    "[REJECT CHECK] course=%r url_slug=%r "
                    "decision=reject (url_slug_online) yaml_override=none",
                    payload.get("course_name") or course_name,
                    _slug,
                )
                return (False, "online_only")
            log.info(
                "[ONLINE-OK] course=%r url_slug=%r — accepted "
                "(per-uni online_only filter disabled, url_slug_online suppressed)",
                payload.get("course_name") or course_name,
                _slug,
            )

    # Bug A: reject pages whose extracted title has no degree-level qualifier.
    # Prefer payload["course_name"] (from H1 via course_name extractor) over
    # the discovery-link name (passed as course_name param) — the H1 is the
    # canonical page title and the most reliable signal.
    effective_name = (payload.get("course_name") or course_name or "").strip()
    # Per-uni YAML opt-out: skip_degree_qualifier_check=true disables the
    # name-based check for universities (e.g. ARU/Writtle) whose SPA pages
    # surface course_name from JSON metadata without the degree prefix.
    # URL-based block_url_patterns already gates non-course pages for those unis.
    _skip_dq = False
    try:
        from app.services.scraper.config.context import (  # noqa: PLC0415
            get_uni_config as _get_uni_config_dq,
        )
        _dq_cfg = _get_uni_config_dq()
        if _dq_cfg is not None and _dq_cfg.extraction is not None:
            _skip_dq = bool(
                getattr(_dq_cfg.extraction.staging, "skip_degree_qualifier_check", False)
            )
    except Exception:  # noqa: BLE001
        pass
    if not _skip_dq and effective_name and not _name_has_degree_qualifier(effective_name):
        return (False, "category_landing_page_missing_degree_qualifier")

    # Explicit domestic-only flag: set by extractors when the page text
    # states "this course is not available to international students" etc.
    if payload.get("domestic_only"):
        return (False, "domestic_only")

    # Slug-name + empty-data rejection: catches pages that silently redirected
    # to a "course not found" 404 during extraction.  When that happens the
    # page has no useful H1/title, the course_name extractor returns [] and
    # the orchestrator falls back to the URL slug ("ug-computing" →
    # "Ug Computing"), and no other fields are populated.
    # Enabled per-uni via staging.reject_slug_name_with_no_data: true.
    # PGCE / PhD courses whose pages *did* load correctly also have slug-style
    # prefixes but carry real fee/mode/duration data — they are kept because
    # at least one of the four "any-data" fields will be non-null.
    _reject_slug_empty = False
    try:
        from app.services.scraper.config.context import (  # noqa: PLC0415
            get_uni_config as _get_uni_config_se,
        )
        _se_cfg = _get_uni_config_se()
        if _se_cfg is not None and _se_cfg.extraction is not None:
            _reject_slug_empty = bool(
                getattr(_se_cfg.extraction.staging, "reject_slug_name_with_no_data", False)
            )
    except Exception:  # noqa: BLE001
        pass
    if _reject_slug_empty:
        _SLUG_PREFIX_RE = re.compile(
            r"^(?:ug|pg|ify|phd|edd|pgce)\s",
            re.IGNORECASE,
        )
        if _SLUG_PREFIX_RE.match(effective_name):
            _has_any_data = any([
                payload.get("international_fee"),
                payload.get("study_mode"),
                payload.get("duration"),
                payload.get("degree_level"),
            ])
            if not _has_any_data:
                log.info(
                    "[REJECT] course=%r url=%r — rejected (url_redirect_not_found): "
                    "slug-derived name with no fee/mode/duration/degree_level; "
                    "page likely redirected to a 404/course-not-found page.",
                    effective_name, source_url,
                )
                return (False, "url_redirect_not_found")

    # Step 6 — Virtual-delivery location sanitisation.
    # "Online", "Distance Learning", "Remote", "Virtual" are study modes,
    # not physical campuses.  Clear course_location when it is purely a
    # virtual delivery label so it is never stored as a campus name.
    # This also prevents the online_only guard below from being confused by a
    # physical-looking location that turns out to be "Online Study".
    _VIRTUAL_LOCATION_RE = re.compile(
        r"^\s*(?:online(?:\s+only|\s+study|\s+learning)?|"
        r"distance(?:\s+education|\s+learning)?|"
        r"remote(?:\s+learning)?|"
        r"virtual(?:\s+campus)?|"
        r"external)\s*$",
        re.IGNORECASE,
    )
    _raw_loc = payload.get("course_location") or payload.get("location_text") or ""
    if _VIRTUAL_LOCATION_RE.match(_raw_loc.strip()):
        log.info(
            "[LOCATION-SANITISE] course=%r — clearing virtual-only location %r "
            "(value is a delivery mode, not a physical campus)",
            effective_name, _raw_loc,
        )
        payload["course_location"] = None
        payload["location_text"] = None

    # Online-only filter: study_mode is the authoritative signal.
    # Rule: if study_mode contains "online" but NO campus/blended keyword →
    # reject regardless of whether a city/campus name appears in
    # course_location.  Many universities list their physical campus name in
    # the location field even for courses delivered entirely online (e.g. ACAP
    # lists all 5 cities for both Online and On Campus courses) — that location
    # text does NOT mean the course is available on campus.
    # Courses that are genuinely mixed-mode have study_mode = "Blended" or
    # "On Campus, Online" and pass through because they contain campus keywords.
    # _online_filter_enabled was computed above — reuse it here.
    _study_mode = (payload.get("study_mode") or "").strip().lower()
    _has_campus_component = any(
        kw in _study_mode
        for kw in (
            "on campus", "on-campus", "campus", "on site", "on-site",
            "face-to-face", "blended", "in-person", "in person",
        )
    )

    _physical_location = (
        payload.get("course_location") or payload.get("location_text") or ""
    ).strip()

    if "online" in _study_mode and not _has_campus_component:
        if _online_filter_enabled:
            log.info(
                "[REJECT CHECK] course=%r detected_modes=[%s] detected_locations=[%s] "
                "decision=reject (online_only — study_mode is authoritative) "
                "yaml_override=none",
                effective_name,
                payload.get("study_mode", "Online"),
                _physical_location or "none",
            )
            return (False, "online_only")
        log.info(
            "[ONLINE-OK] course=%r study_mode=%r — accepted (per-uni "
            "online_only filter disabled)",
            effective_name,
            payload.get("study_mode", "Online"),
        )

    # UTAS-specific online_only: utas.edu.au pages always declare a physical
    # campus name in their "Location" panel when the course has any on-campus
    # component.  The location extractor strips virtual keywords ("Online",
    # "Distance", etc.) so a blank course_location means the panel contained
    # only "Online" — i.e. the course is online-only for international
    # students and cannot be studied on a student visa.
    # This catches cases where the study_mode extractor returned "Blended" or
    # "On Campus" by picking up the domestic-tab campus reference while the
    # international-tab Location field said "Online" only — the general
    # online_only guard above requires "online" in study_mode and misses these.
    #
    # IMPORTANT: use course_location exclusively here, NOT location_text.
    # course_location has already had virtual keywords ("Online", "Distance",
    # "Internet", etc.) stripped by the location extractor, so it contains
    # only confirmed physical campuses.  location_text is the raw value from
    # the page and may be "Online" — using it here would make the guard treat
    # "Online" as a physical campus and silently let the course through.
    # UTAS rejection fires on EITHER of two signals:
    #   (a) `course_location` is blank — the historical signal; the location
    #       extractor stripped virtual keywords ("Online", "Distance", ...) so
    #       blank means the panel contained ONLY a virtual value.
    #   (b) `payload["online_only_utas"]` is True — set by single_course.py
    #       when Gemini's `mode` field returned exactly "Online" for a UTAS
    #       page. Needed because utas.yaml's `default_course_location: "Hobart"`
    #       fallback fills course_location with "Hobart" on partial-HTML
    #       fetches, masking signal (a) for real online-only pages like the
    #       Graduate Certificate in Dementia (M5x, 2026-05-17 report).
    _utas_physical_location = (payload.get("course_location") or "").strip()
    _is_utas = "utas.edu.au" in (source_url or "").lower()
    _utas_online_via_gemini = bool(payload.get("online_only_utas"))
    if _is_utas and (not _utas_physical_location or _utas_online_via_gemini):
        _reason = (
            "utas_online_gemini_mode — Gemini detected Location: Online"
            if _utas_online_via_gemini
            else "utas_online_blank_location — Location panel was Online only"
        )
        log.info(
            "[REJECT CHECK] course=%r url=%r decision=reject (%s)",
            effective_name,
            source_url,
            _reason,
        )
        return (False, "online_only")

    # Note: we do NOT reject study_mode="Blended" when location is absent
    # for non-UTAS universities.
    # "Blended" means the extractor found explicit evidence of both online
    # and campus delivery on the course page — by definition a campus
    # component exists.  Some universities (e.g. Torrens) don't embed campus
    # location details on individual course pages, so location=None is
    # expected even for courses genuinely available on campus.
    # The strict "Online → reject" rule above already handles any case where
    # the mode is purely online.  UTAS is handled by the explicit check above.

    # Bug B: no international fee after all extraction is done.
    # If the university has a centralized fee page, the fee may simply not
    # be listed for this specific course yet — stage for human review instead
    # of auto-rejecting.  International fees on a separate page are legitimate.
    if payload.get("international_fee") is None:
        # Escape hatch 1: university has a central fee page — the per-course
        # fee may legitimately not appear on the individual page.
        if payload.get("has_central_fee_page"):
            return (True, "accepted")
        # Escape hatches 2–4 all require reading the per-uni YAML config.
        _req_fee = True
        _has_fee_defaults = False
        _browser_skipped = False
        try:
            from app.services.scraper.config.context import (  # noqa: PLC0415
                get_uni_config as _get_uni_config_fee,
            )
            _fee_cfg = _get_uni_config_fee()
            if _fee_cfg is not None and _fee_cfg.extraction is not None:
                # Escape hatch 2: explicit opt-out (require_international_fee=false).
                # Use case: ARU / sites where fees are on JS tabs the extractor
                # cannot always render — stage for human review rather than reject.
                _req_fee = bool(
                    getattr(
                        _fee_cfg.extraction.staging,
                        "require_international_fee",
                        True,
                    )
                )
                # Escape hatch 3: YAML has degree_level_defaults fees configured.
                # When defaults exist but the course's degree_level couldn't be
                # determined, the default was never applied and the fee is None —
                # but the fee IS known in principle.  Stage for review; a human
                # can confirm the correct tier rather than the course being silently
                # dropped.  This also covers courses with non-standard qualifiers
                # (FdA, QTS, CertHE, etc.) that may be matched to the wrong tier.
                _fee_defaults: dict = (
                    getattr(_fee_cfg.extraction.fees, "degree_level_defaults", None)
                    or {}
                )
                _has_fee_defaults = bool(_fee_defaults)
                # Escape hatch 4: per-course browser was explicitly skipped
                # (skip_per_course_browser=true).  The fee may be behind a JS tab
                # that was never rendered — stage for review rather than auto-reject.
                _browser_skipped = bool(
                    getattr(
                        _fee_cfg.extraction,
                        "skip_per_course_browser",
                        False,
                    )
                )
        except Exception:  # noqa: BLE001
            pass
        if not _req_fee:
            log.info(
                "[STAGE-OK] course=%r — staged without fee "
                "(require_international_fee=false in YAML)",
                effective_name,
            )
            return (True, "accepted")
        if _has_fee_defaults:
            log.info(
                "[STAGE-OK] course=%r — staged without fee "
                "(degree_level_defaults configured; tier may not have matched)",
                effective_name,
            )
            return (True, "accepted")
        if _browser_skipped:
            log.info(
                "[STAGE-OK] course=%r — staged without fee "
                "(skip_per_course_browser=true; fee may be behind JS tab)",
                effective_name,
            )
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
    # Phase A.5 — pre-extraction reinforcements (user-reported leaks):
    # marketing prefixes used to brand whole sub-sites that show every
    # programme but are themselves not a single course.
    ("/study-at-",              "marketing_page"),  # /study-at-uow, /study-at-une-online
    ("/study-online/",          "category_landing_page_url_block"),
    # NOTE: bare "/study-with-us" is INTENTIONALLY narrowed.  MIT
    # (Melbourne Institute of Technology) — and any future university
    # that uses Drupal-style brand-subsite URL hierarchy — publishes
    # real course pages at /study-with-us/programs/<slug> and
    # /study-with-us/our-courses/<slug>.  A bare "/study-with-us"
    # substring rule killed all 27 of MIT's real courses (Bachelor of
    # Business, Master of Networking, etc.) at the staging gate
    # (2026-05-13).  Only block the well-known audience-landing
    # sub-paths; rely on /why-, /scholarships, /how-to-apply, /open-day,
    # /info-night, /webinar etc. (already in this list) to catch every
    # other marketing variant.
    ("/study-with-us/international-students/", "marketing_page"),
    ("/study-with-us/domestic-students/",      "marketing_page"),
    ("/study-with-us/student-support",         "marketing_page"),
    ("/study-with-us/student-life",            "campus_page"),
    # Pathway / preparation hub URLs (these are college pages, not real
    # university degree pages).
    ("/pathways-to-uni",        "pathway_page"),
    ("/pathways-program",       "pathway_page"),
    ("/pathway-program",        "pathway_page"),
    ("/college/",               "pathway_page"),
    # User UI surfaces inside the public site (saved/compare/favourites
    # are session-state pages, never a course detail).
    ("/saved-courses",          "ui_page"),
    ("/favourites",             "ui_page"),
    ("/favorites",              "ui_page"),
    # Marketing widgets
    ("/webinar",                "marketing_page"),
    # Audience hubs (year-12, mature-age, parents — already partially
    # blocked via /information-for/ in discovery; included here so the
    # single source of truth knows about them too).
    ("/year-12-entry",          "info_page"),
    ("/year-12",                "info_page"),
    ("/mature-age-students",    "info_page"),
    ("/high-school-students",   "info_page"),
    ("/parents-and-guardians",  "info_page"),
    # Standalone test-info pages
    ("/stat-test",              "info_page"),
    # Phase A.6 — additional production-confirmed leak URLs.
    # Each pattern was observed in user-reported scrape logs from UniSQ,
    # UOW, or Flinders.  All have leading slashes so they cannot match
    # inside a real course slug.
    ("/career-finder",          "info_page"),       # UniSQ "career-finder/accountant"
    ("/online-study",           "category_landing_page_url_block"),
    ("/pathway-programs",       "pathway_page"),
    ("/pathway-program/",       "pathway_page"),
    ("/short-courses",          "category_landing_page_url_block"),
    ("/short-course/",          "category_landing_page_url_block"),
    ("/english-language-programs", "pathway_page"),
    ("/english-language-program",  "pathway_page"),
    ("/events-key-dates",       "events_page"),
    ("/key-dates",              "events_page"),
    ("/webinars",               "marketing_page"),
    ("/livestream",             "marketing_page"),
    ("/info-session",           "marketing_page"),
    # JCU-style subject-area category hubs: /courses/<level>/linkassets/<subject>
    # e.g. /courses/postgraduate/linkassets/engineering — these pages list many
    # courses under a subject heading but carry no fee, IELTS, or degree-level
    # data for any individual course.  The "/linkassets/" token is unique to
    # JCU's URL scheme; it will never appear inside a real degree-course slug.
    ("/linkassets/",            "category_landing_page_url_block"),
    # Specific Flinders nav paths.  IMPORTANT: each entry uses a path
    # boundary so it cannot accidentally match a real degree slug.
    # `/study/postgrad/` matches Flinders' postgrad landing page and
    # any nested category pages, but NEVER matches
    # `/study/postgraduate-diploma-of-counselling` (that would have
    # been the bug had we used bare `/study/postgrad`).
    ("/study/postgrad/",        "category_landing_page_url_block"),
    ("/study/pathways/",        "pathway_page"),
    ("/study/pathways-to-",     "pathway_page"),
    ("/study/events-key-dates", "events_page"),
    ("/study/courses/saved-courses", "ui_page"),
    # Indigenous / veterans / equity admission landing pages — these
    # describe an alternate-entry pathway, not a single course.
    ("/indigenous-admission-scheme", "info_page"),
    ("/indigenous-pathway",     "info_page"),
    ("/military-veterans",      "info_page"),
    ("/veterans-pathway",       "info_page"),
    # Foundation studies / preparation landing pages
    ("/foundation-studies",     "pathway_page"),
    ("/foundation-program",     "pathway_page"),
    # CDU-specific: /study/redirect/<slug> URLs deep-link from the marketing
    # site to the CDU handbook via an auth/cookie redirect that both HTTP and
    # Playwright cannot follow.  These always produce fetch_failed; blocking
    # them here prevents wasted retries.  Real CDU course pages come from the
    # sitemap supplement applied during discovery.
    ("/study/redirect/",        "redirect_page"),
    # User UI surfaces — compare / favourites action pages.  The bare
    # "/compare" was already captured via _BLOCK_URL_LAST_SEGMENTS but
    # adding it here too means it also catches "/compare-courses/..."
    # style sub-pages used by some sites.
    ("/compare-courses",        "ui_page"),
    ("/compare/",               "ui_page"),
    # UOW uses a legacy "/study/index.php" router for category /
    # action pages.  Anchoring on "/study/index.php" avoids over-blocking
    # other providers that may legitimately use index.php for course
    # detail routes elsewhere in their site (e.g. WordPress-routed
    # /courses/index.php?id=NNN style URLs that we have NOT confirmed
    # as leaks).
    ("/study/index.php",        "category_landing_page_url_block"),
    # AEM (Adobe Experience Manager) digital asset library — marketing
    # brochures, PDF schedules, and image assets published at
    # /content/dam/...  These URLs are discovered via sitemap on AEM-hosted
    # sites (e.g. Flinders) and never contain course detail data.
    # Blocking here removes them from all three filter points (BFS, post-
    # discovery gate, staging gate) and also eliminates ~$0.001/URL wasted
    # on Gemini calls that extract nothing.
    ("/content/dam/",           "asset_library_pdf"),
    # UTAS CMS asset path — PDF course guides / brochures published at
    # /__data/assets/pdf_file/...  Discovered via faculty listing page nav
    # links (e.g. "Download the 2027 Course Guide") but are binary files,
    # not HTML course detail pages.  Triggers 429 + 600 s cooldown when
    # the browser attempts to render them.
    ("/__data/assets/",         "asset_library_pdf"),
    # Generic PDF URL guard — any URL whose path contains ".pdf" is a
    # document file, never a real course detail page.  PDF links appear
    # on many university faculty pages (brochures, handbooks, fee schedules)
    # and should be filtered before they consume a browser slot + cooldown.
    # ".pdf" as a substring safely covers both bare ".pdf" (path ends here)
    # and ".pdf?" query strings; it cannot accidentally match a real course
    # slug since those never contain a dot-extension sequence.
    (".pdf",                    "asset_library_pdf"),
    # UTAS /study/certificates — the certificate study-type category hub.
    # Not a CRICOS course page; real certificate courses appear under
    # /courses/<faculty>/courses/<slug>.  The trailing slash anchors
    # to the hub URL (/study/certificates and /study/certificates/).
    ("/study/certificates",     "category_landing_page_url_block"),
    # UTAS study-info pages that are NOT course detail pages.  These pages
    # appear in BFS because /courses links to them in the site navigation.
    # All real UTAS CRICOS courses live under /courses/<faculty>/courses/<slug>.
    ("/study/learning-abroad",  "category_landing_page_url_block"),
    ("/study/interstate",       "category_landing_page_url_block"),
    ("/study/areas/",           "category_landing_page_url_block"),
    ("/study/international",    "category_landing_page_url_block"),
    ("/study/online",           "category_landing_page_url_block"),
    ("/study/sustainability",   "category_landing_page_url_block"),
    ("/study/parents-and-carers", "category_landing_page_url_block"),
    ("/study/starting-at-the-university", "category_landing_page_url_block"),
    # NOTE: /study/undergraduate (UTAS hub) and /study/postgraduate/ (Flinders
    # hub) were previously here as global substring blocks but caused
    # false-positive rejections on universities (ARU, ULaw) that publish real
    # degree pages under those prefixes.  Moved to per-university YAML
    # block_url_patterns in utas.yaml and flinders.yaml respectively.
)

# URL query-string substring matches.  Real course-detail pages do not
# rely on query parameters to identify the course (they use a path
# segment) so any URL whose query string contains one of these
# patterns is a UI/action page, not a course detail.
#
# Compared against ``urlparse(url).query.lower()``.
_BLOCK_URL_QUERY_SUBSTRINGS: tuple[tuple[str, str], ...] = (
    # UOW "Save this course" action: /study/courses/?addCourse=<id>
    ("addcourse=",              "ui_page"),
    ("removecourse=",           "ui_page"),
    ("favourite=",              "ui_page"),
    ("favorite=",               "ui_page"),
    ("compare=",                "ui_page"),
)

# URL last-segment exact matches.  Used in addition to the substring list
# above when we must NOT accidentally match a course slug containing the
# same word (e.g. "/undergraduate-study" would substring-match both the
# category page and a hypothetical "/courses/undergraduate-study-tips").
# Compared against the path's last segment with trailing slash stripped.
_BLOCK_URL_LAST_SEGMENTS: dict[str, str] = {
    "undergraduate-study":      "category_landing_page_url_block",
    "postgraduate-study":       "category_landing_page_url_block",
    # Bare nav-root last-segments — when /…/undergraduate (or /…/postgraduate)
    # ends the URL with NO course slug after it, the page is the listing
    # root, not a course.  Real course URLs always have the degree-name
    # slug as the last segment (e.g.
    # /find-a-course/undergraduate/employability-initiatives/cooperative-
    # education-program-in-actuarial-studies → last segment is the long
    # course slug, never the bare word "undergraduate").  Safe globally:
    # no university publishes a real course detail page at a URL whose
    # last segment is literally "undergraduate" or "postgraduate".
    "undergraduate":            "category_landing_page_url_block",
    "postgraduate":             "category_landing_page_url_block",
    # Macquarie nav-driven landing pages discovered via browser BFS when
    # the catalogue seeds short-circuit on Cloudflare (2026-05-18 leak).
    "combined-bachelor-master-degrees": "category_landing_page_url_block",
    "double-degree-builder":    "category_landing_page_url_block",
    "browse-all-degrees":       "category_landing_page_url_block",
    "view-degrees":             "category_landing_page_url_block",
    "view-all-degrees":         "category_landing_page_url_block",
    "all-degrees":              "category_landing_page_url_block",
    "all-courses":              "category_landing_page_url_block",
    "find-a-course":            "category_landing_page_url_block",
    "study-online":             "category_landing_page_url_block",
    "online-study":             "category_landing_page_url_block",
    "online-courses":           "category_landing_page_url_block",
    "double-degrees":           "category_landing_page_url_block",
    "new-degrees":              "category_landing_page_url_block",
    "english-language-programs": "pathway_page",
    # NOTE: "english-language" (bare slug) intentionally NOT here.
    # The path-based patterns /english-language-programs and
    # /english-language-program (in _BLOCK_URL_SUBSTRINGS) already catch
    # real English language pathway listing pages.  The bare slug is too
    # broad: Lincoln University (NZ) has a legitimate degree programme at
    # /study/study-programmes/programme-search/english-language/ which was
    # being incorrectly blocked by this entry.  Removed 2026-07-02.
    "saved-courses":            "ui_page",
    "favourites":               "ui_page",
    "favorites":                "ui_page",
    "compare":                  "ui_page",
    "webinars":                 "marketing_page",
    "webinar":                  "marketing_page",
    "stat":                     "info_page",
}

# Title prefix matches.  Lowercased and stripped before comparison, so
# "Fees and Scholarships | UTAS" → "fees and scholarships" matches the
# "fees and " prefix below.  Order doesn't matter — first match wins.
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
    # "events" bare prefix intentionally NOT here — it would block legitimate
    # academic courses such as "Events Management BA" and "International Events
    # Management MSc".  Instead we match specific event-listing phrasings:
    ("upcoming events",                 "events_page"),
    ("events calendar",                 "events_page"),
    ("events and conferences",          "events_page"),
    ("open day events",                 "events_page"),
    ("university events",               "events_page"),
    ("student events",                  "events_page"),
    ("news and events",                 "events_page"),
    ("events at ",                      "events_page"),
    ("events for ",                     "events_page"),
    # A page titled exactly "Events" (no qualifier) is an event listing;
    # that case is handled by _BLOCK_TITLE_EXACT below.
    ("contact us",                      "contact_page"),
    ("contact",                         "contact_page"),
    ("about us",                        "about_page"),
    ("testimonials",                    "testimonials_page"),
    # Phase A.5 — pre-extraction title gate (user-reported leaks).
    # Real degree titles always start with a degree qualifier (Bachelor,
    # Master, MBA, Diploma, Certificate, ...).  None of the patterns
    # below can accidentally match a real course title.
    ("study online",                    "category_landing_page_title_block"),
    ("study at ",                       "marketing_page"),  # "Study at UOW", "Study at UNE"
    # NOTE: do NOT add bare "undergraduate" / "postgraduate" prefixes —
    # those would wrongly block real award titles like "Undergraduate
    # Certificate of Psychology Fundamentals" and "Postgraduate Diploma
    # of Counselling".  We instead match the specific category-landing
    # phrasings and rely on _BLOCK_TITLE_EXACT for the bare nav items
    # ("Undergraduate" / "Postgraduate" alone, with no following word).
    ("undergraduate study",             "category_landing_page_title_block"),  # "Undergraduate study"
    ("undergraduate degrees",           "category_landing_page_title_block"),
    ("undergraduate courses",           "category_landing_page_title_block"),
    ("undergraduate programs",          "category_landing_page_title_block"),
    ("undergraduate programmes",        "category_landing_page_title_block"),
    ("postgraduate study",              "category_landing_page_title_block"),  # "Postgraduate study"
    ("postgraduate degrees",            "category_landing_page_title_block"),
    ("postgraduate courses",            "category_landing_page_title_block"),
    ("postgraduate programs",           "category_landing_page_title_block"),
    ("postgraduate programmes",         "category_landing_page_title_block"),
    ("graduate study",                  "category_landing_page_title_block"),
    ("research study",                  "category_landing_page_title_block"),
    ("higher degree by research",       "category_landing_page_title_block"),
    ("higher degrees by research",      "category_landing_page_title_block"),
    ("pathways to ",                    "pathway_page"),       # "Pathways to uni"
    ("pathway to ",                     "pathway_page"),
    ("pathway program",                 "pathway_page"),
    ("pathways program",                "pathway_page"),
    ("english language program",        "pathway_page"),       # "English Language Programs"
    ("foundation program",              "pathway_page"),
    ("saved course",                    "ui_page"),            # "Saved courses"
    ("favourite",                       "ui_page"),            # "Favourites" / "Favourite course"
    ("favorite",                        "ui_page"),            # US spelling
    ("compare course",                  "ui_page"),
    ("webinar",                         "marketing_page"),     # "Webinar(s)"
    ("year 12",                         "info_page"),          # "Year 12 entry"
    ("year 11",                         "info_page"),
    ("why ",                            "marketing_page"),     # "Why study", "Why choose UNE"
    ("explore courses",                 "category_landing_page_title_block"),
    ("browse courses",                  "category_landing_page_title_block"),
    ("our courses",                     "category_landing_page_title_block"),
    ("study area",                      "category_landing_page_title_block"),  # "Study area" / "Study areas"
    ("subject area",                    "category_landing_page_title_block"),
    ("information for",                 "info_page"),          # "Information for international students"
    ("how it works",                    "info_page"),
    ("open day",                        "marketing_page"),
    ("info night",                      "marketing_page"),
    ("information session",             "marketing_page"),
    # Phase A.6 — UI action verbs as title prefixes.  Real degree titles
    # always start with a qualification word (Bachelor, Master, Diploma,
    # ...).  No legitimate course can start with "Save"/"View"/"Clear".
    ("save bachelor",                   "ui_page"),  # "Save Bachelor of Arts to Course Favourites"
    ("save master",                     "ui_page"),
    ("save diploma",                    "ui_page"),
    ("save graduate",                   "ui_page"),
    ("save certificate",                "ui_page"),
    ("save doctor",                     "ui_page"),
    ("save course",                     "ui_page"),
    ("save this",                       "ui_page"),
    ("view all saved",                  "ui_page"),  # "View all saved courses"
    ("view saved",                      "ui_page"),
    ("clear all",                       "ui_page"),  # "Clear all"
    ("clear saved",                     "ui_page"),
    ("my favourites",                   "ui_page"),  # "0 My favourites"
    ("my favorites",                    "ui_page"),
    ("my saved",                        "ui_page"),
    ("0 my ",                           "ui_page"),  # "0 My favourites"
    # Audience hub copy that always runs as nav text in the side menu.
    # Each is a UI/info page, never a course detail.
    ("a future ",                       "info_page"),  # "a future postgraduate student"
    ("future student",                  "info_page"),
    ("future postgraduate",             "info_page"),
    ("future undergraduate",            "info_page"),
    ("postgraduate students",           "info_page"),  # "Postgraduate students"
    ("undergraduate students",          "info_page"),
    ("postgraduate information",        "marketing_page"),  # "Postgraduate information sessions"
    ("undergraduate information",       "marketing_page"),
    ("livestream",                      "marketing_page"),  # "Livestream Information Sessions"
    ("tafe/vet to uni",                 "pathway_page"),    # "TAFE/VET to uni"
    ("tafe to uni",                     "pathway_page"),
    ("vet to uni",                      "pathway_page"),
)

# Title exact matches (full-string equals).  Use sparingly — only for
# acronyms / short words that would generate too many false positives if
# matched as a prefix (e.g. "stat" must NOT match "Statistics").
_BLOCK_TITLE_EXACT: frozenset[str] = frozenset({
    # Standalone admissions / test acronyms
    "stat",
    "saq",
    "uac",
    "qtac",
    "vtac",
    "tisc",
    "satac",
    "atar",
    # Bare nav labels — full-string equals only so award titles like
    # "Undergraduate Certificate of Psychology Fundamentals" or
    # "Postgraduate Diploma of Counselling" are NEVER blocked.
    # "events" is safe as an exact match (nav link titled just "Events") but
    # must NOT be a prefix — "Events Management BA" is a real course.
    "events",
    "undergraduate",
    "postgraduate",
    "graduate",
    "research",
    "study",
    "courses",
    "programs",
    "programmes",
    "degrees",
    # 2026-05-18 — Macquarie nav-label leaks: anchor-text-derived course
    # names harvested by browser BFS from MQ's mega-menu.  Real degree
    # titles always start with a qualification word (Bachelor, Master,
    # Diploma, Certificate, Doctor, MBA, ...).  None of these can match
    # a real course title.
    "browse all degrees",
    "view degrees",
    "view all degrees",
    "view all courses",
    "all degrees",
    "all courses",
    "combined bachelor master degrees",
    "combined bachelor/master degrees",
    "double degree builder",
    "find a course",
})


def _last_path_segment(path: str) -> str:
    """Return the last non-empty segment of a URL path, lowercased.

    Examples
    --------
    >>> _last_path_segment("/study/degrees-and-courses/undergraduate-study")
    'undergraduate-study'
    >>> _last_path_segment("/saved-courses/")
    'saved-courses'
    """
    p = (path or "").rstrip("/")
    if not p:
        return ""
    return p.rsplit("/", 1)[-1]


# Host-specific exceptions to _BLOCK_URL_SUBSTRINGS Pass 1.
#
# Some universities publish real course detail pages at URL structures that
# are globally blocked as category hubs for *other* universities.  Adding the
# hostname here bypasses the named pattern(s) only for that host.
#
# NOTE: /study/undergraduate (UTAS hub) and /study/postgraduate/ (Flinders hub)
# previously required exceptions here for ARU and ULaw.  Those two global
# patterns have been removed entirely and moved into per-university YAML
# block_url_patterns (utas.yaml, flinders.yaml) so no host exceptions are
# needed for them.  Prefer the YAML approach for any future single-university
# URL blocks rather than adding new global patterns with exception lists.
_BLOCK_URL_SUBSTRINGS_HOST_EXCEPTIONS: dict[str, frozenset[str]] = {}


def is_blocked_page(url: str | None, title: str | None = None) -> tuple[bool, str]:
    """Return ``(True, reason)`` when this URL/title is definitely not a
    course detail or course listing page; ``(False, "")`` otherwise.

    Phase A safety net (extended in A.5 — pre-extraction gate).  Callers:
      * Discovery BFS — skip the URL before enqueuing it.
      * Orchestrator — drop the candidate before extraction starts.
      * Staging gate — refuse to stage a course whose ``source_url`` is
        on the blocklist (defence in depth).

    Three-pass URL check (cheapest first):

      1. Substring match anywhere in path (``_BLOCK_URL_SUBSTRINGS``).
      2. Last-segment exact match (``_BLOCK_URL_LAST_SEGMENTS``).
         Stricter than substring — avoids accidentally matching a real
         course slug that contains the same word elsewhere.

    Two-pass title check:

      3. Exact match on the normalised title (``_BLOCK_TITLE_EXACT``).
      4. Prefix match on the normalised title (``_BLOCK_TITLE_PREFIXES``).

    The function is **conservative**: every pattern is one we have
    confirmed in production logs as a non-course page.  Generic words
    inside a real course slug (e.g. ``/bachelor-of-arts-and-contact-
    with-society``) won't match any substring because every URL pattern
    includes its leading slash.
    """
    if url:
        try:
            parsed = urlparse(url)
            path = parsed.path.lower()
            query = parsed.query.lower()
        except Exception:  # noqa: BLE001 — malformed URL → no URL signal
            path = ""
            query = ""
        if path:
            # Pass 1: substring match
            _host_exceptions = _BLOCK_URL_SUBSTRINGS_HOST_EXCEPTIONS.get(
                parsed.netloc.lower(), frozenset()
            )
            for pat, reason in _BLOCK_URL_SUBSTRINGS:
                if pat in path:
                    if pat in _host_exceptions:
                        continue  # host-specific exception — skip this block
                    return (True, reason)
            # Pass 2: last-segment exact match (stricter than substring)
            last = _last_path_segment(path)
            if last and last in _BLOCK_URL_LAST_SEGMENTS:
                return (True, _BLOCK_URL_LAST_SEGMENTS[last])
        if query:
            # Pass 2b: query-string substring match (e.g. ?addCourse=)
            for pat, reason in _BLOCK_URL_QUERY_SUBSTRINGS:
                if pat in query:
                    return (True, reason)

    if title:
        norm_title = re.sub(r"\s+", " ", title).strip().lower()
        # Strip common "| University Name" suffixes so "Apply Now | UNE"
        # still matches the "apply now" prefix.
        if "|" in norm_title:
            norm_title = norm_title.split("|", 1)[0].strip()
        # Pass 3: exact match
        if norm_title in _BLOCK_TITLE_EXACT:
            return (True, "info_page")
        # Pass 4: prefix match
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
