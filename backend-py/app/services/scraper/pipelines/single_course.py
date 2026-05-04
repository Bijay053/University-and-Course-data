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
from app.services.scraper.http_fetcher import fetch_html
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
    r"\b(?:key\s+information|entry\s+requirements?|course\s+rules?)\b",
    _re.IGNORECASE,
)


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
    r"|this\s+(?:program|course|degree)\s+is\s+only\s+for\s+(?:australian|domestic)"
    r"|this\s+(?:program|course|degree)\s+does\s+not\s+accept\s+international"
    # "Sorry, this course is not available to international students"
    r"|sorry[,.]?\s+this\s+(?:course|program)\s+is\s+not\s+available\s+to\s+international"
    # "Open to domestic applicants only" — rare but unambiguous
    r"|open\s+to\s+domestic\s+applicants\s+only"
    # Broader unambiguous negatives — no "this course" qualifier needed.
    # "Not available to international students" is unambiguous on a course
    # detail page.  We do NOT match just "not available to international"
    # (without "students") to avoid false-positives on short snippets.
    r"|not\s+available\s+to\s+international\s+students?"
    # "International applications are not accepted" / "not accepting
    # international student applications" — Federation and similar.
    r"|international\s+(?:student\s+)?applications?\s+(?:are\s+)?not\s+(?:accepted|available|open)"
    r"|not\s+currently\s+accepting\s+international\s+(?:student\s+)?applications?"
    # "your application to study as a domestic student" — Torrens HDR-specific
    # phrasing where the entire admissions section is framed for domestic
    # applicants only (e.g. Doctor of Philosophy by Prior Works).  The phrase
    # is unambiguous on a course-detail page: international-eligible courses
    # have a parallel section framed for international applicants.
    r"|(?:begin\s+your|your)\s+application\s+to\s+study\s+as\s+a\s+domestic\s+student"
    # UTAS distance-courses disclaimer — hard signal even when the page has a
    # structural #tabInternational panel (UTAS includes that tab on every page).
    # The phrase "please see the list of distance courses (i.e. online and
    # taken outside Australia)" always accompanies the soft "may not be
    # available to international students" text and appears exclusively on
    # pages where the course is online-only / not available to student-visa
    # holders.  Treating it as a hard pattern avoids the _has_international_
    # section suppression that would otherwise swallow the soft signal.
    r"|please\s+see\s+the\s+list\s+of\s+distance\s+courses"
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
    text = _re.sub(r"<[^>]+>", " ", html)
    text = _re.sub(r"\s+", " ", text)
    # Hard patterns: unambiguous course-level exclusion statements.
    if _DOMESTIC_ONLY_RE.search(text):
        return True
    # Soft pattern: "may not be available" — only block when there is no
    # structural international section elsewhere on the same page.
    if _DOMESTIC_ONLY_SOFT_RE.search(text) and not _has_international_section(html):
        return True
    return False


def _domestic_only_filter_enabled() -> bool:
    """Phase 3 gate: return True when the domestic-only filter should run.

    Reads ``extraction.filters.domestic_only.enabled`` from the current
    per-university config contextvar.

    Fail-open policy: if the contextvar is not set (no uni context — e.g. a
    direct CLI call or test that hasn't called set_uni_config), the function
    returns True so that _is_domestic_only_page() still runs.  This matches
    current prod behaviour (filter always ran before this gate was added) and
    prevents a missing contextvar from silently bypassing the filter.
    """
    uc = get_uni_config()
    return uc is None or uc.extraction.filters.domestic_only.enabled


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
    "rule:duration",   # rule-based duration inference from degree label
    "rule:intake",     # rule-based intake inference
    "rule:study_mode", # rule-based study-mode inference
    "rule:cricos",     # rule-based CRICOS inference
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
            continue  # field not set — leave evidence as-is

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
    _rescue = _sub_year_regex and _ai_says_years and _ai_plausible

    if ("duration" not in payload or _rescue) and ai_val_raw is not None:
        try:
            ai_filled["duration"] = float(ai_val_raw)
        except (TypeError, ValueError):
            pass
    if ("duration_term" not in payload or _rescue) and ai_unit_raw:
        term = _normalise_unit(ai_unit_raw)
        if term:
            ai_filled["duration_term"] = term


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
    _uc = _ruc()  # noqa: F841

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

    if html is None:
        html = await fetch_html(url)
    if not html:
        # HTTP fetch failed (Cloudflare, bot-protection, JS-gate, etc.).
        # Try a real Playwright browser before giving up — this handles any
        # site where plain httpx gets a 403/challenge/empty body.
        try:
            from app.services.scraper.browser_pool import pool as _bp
            if emit:
                await emit(
                    "status",
                    f"[BROWSER↑] HTTP blocked for {url[:70]} — retrying via browser",
                    phase="extract", kind="browser_http_fallback", url=url,
                )
            html = await _bp.fetch_html(
                url, wait_until="domcontentloaded", timeout=35_000, settle_ms=2000
            )
        except Exception as _exc:
            log.warning("browser fallback failed for %s: %s", url, _exc)
    if not html:
        return {"url": url, "error": "fetch_failed", "payload": {}, "evidence": []}

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
                    evidence.append(
                        {
                            "field_key": _k,
                            "value": _v,
                            "confidence": 0.9,
                            "method": "csu_static",
                            "snippet": None,
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
            _ECU_DIRECT_KEYS = {"has_central_fee_page", "course_location"}
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
                    evidence.append({
                        "field_key": _ek,
                        "value": _ev,
                        "confidence": 0.85,
                        "method": "ecu_static",
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
                f"[FIELD SUMMARY] {_summary_name}\n" + "\n".join(_summary_lines),
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
    # tag-stripped text), AND both course_location AND location_text are
    # blank, the "On Campus" match almost certainly came from sidebar noise
    # (e.g. "Recently viewed" widget listing other courses' campus names).
    # In that case the course's own Location field either says "Online" or
    # was not captured — neither supports a confident "On Campus" assignment.
    #
    # Action: downgrade to "Online" so the online_only guard in guards.py
    # can evaluate and reject the course when appropriate.  This ONLY fires
    # when there is NO high-authority structural evidence (span_id_delivery,
    # data_attribute, strong_label, gemini_primary, etc.) — those methods
    # would have returned before the keyword fallback and would appear with
    # a different method name in the evidence list.
    _only_rule_based_on_campus = (
        payload.get("study_mode") == "On Campus"
        and not _has_physical_location
        and not (payload.get("location_text") or "").strip()
        and bool(_study_mode_evidence)
        and all(
            (e.get("method") or "").startswith("study_mode:rule")
            for e in _study_mode_evidence
        )
    )
    if _only_rule_based_on_campus:
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

            _gate_skip, _gate_reason = _gate_check(payload, evidence)

            _gp_filled: dict[str, Any] = {}
            _gp_dbg: dict[str, Any] = {}
            _gp_in_tok: int = 0
            _gp_out_tok: int = 0

            if _gate_skip:
                # All high-value fields already covered at high confidence — skip.
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
                _gp_filled, _gp_cost, _gp_in_tok, _gp_out_tok, _gp_dbg = await asyncio.wait_for(
                    _gp.extract_primary(_gp_html, url),
                    timeout=_AI_FALLBACK_TIMEOUT_SEC,
                )
                _gemini_primary_cost = _gp_cost

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
                        )
                        _loc_clean = _spl(_loc)
                        if _loc_clean:
                            _loc = _loc_clean
                    except Exception:
                        pass  # never block on import/runtime error
                if _loc and _loc.lower() not in _STUDY_MODE_KEYWORDS:
                    _has_structural_loc = any(
                        ev.get("field_key") == "course_location"
                        and str(ev.get("method", "")).startswith("location.")
                        for ev in evidence
                    )
                    if not _has_structural_loc:
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

                # Issue 3: Don't let Gemini PRIMARY override a regex-extracted
                # intake_months.  The intake regex reads structured DOM text
                # (e.g. "Intake Options: January, April, July, October") and is
                # more reliable than Gemini reading prose where nearby months
                # from other sections may leak in.  If regex already filled
                # intake_months, keep it.
                if _gp_k == "intake_months" and _best_ev_method("intake_months") == "regex":
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
                    if (isinstance(_gp_v, str)
                            and _gp_v.strip().lower() in _STUDY_MODE_KEYWORDS):
                        continue  # study-mode phrase — not a real location
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

                # PRIMARY: always overwrite payload value.
                # Keep prior evidence rows so Evidence Review can show every
                # source that found a value — mark them "superseded" so the UI
                # can distinguish them from the winning entry.
                payload[_gp_k] = _gp_v
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

            # Always emit so every course has a [GEMINI] line in the live log
            if emit:
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
        browser_filled, browser_evidence, rendered_html, _override = (
            await maybe_browser_refetch(url, payload, emit=emit, force=_force)
        )
        for k, v in browser_filled.items():
            if _override:
                payload[k] = v
            else:
                payload.setdefault(k, v)
        evidence.extend(browser_evidence)

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
    if (
        not payload.get("domestic_only")
        and rendered_html
        and _domestic_only_filter_enabled() and _is_domestic_only_page(rendered_html)
        and not _url_signals_international
        and not _browser_confirmed_intl
    ):
        payload["domestic_only"] = True
        await emit(
            "status",
            f"[DOMESTIC ONLY] {url} — rendered page states domestic-students-only; skipping",
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
                payload[k] = v  # fill null slot — always safe regardless of tier
            elif _vision_is_tier0 and can_override(_prior_method, "per_course_vision"):
                # Tier-0 image (from English requirements DOM section) beats
                # tier-3 text — supersede the existing value.
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
                    evidence.append(
                        {
                            "field_key": k,
                            "value": v,
                            "confidence": 0.85,
                            "method": "vit_static_fallback",
                            "snippet": None,
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


    if use_ai_fallback:
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
                # duration_text and intake_text are in _ai_target_keys so they
                # appear in `missing` when blank.  study_mode is NOT in that
                # list, so we check it directly against the payload.
                _uow_guessed = [
                    f for f in ("duration_text", "intake_text")
                    if f in missing
                ] + (
                    ["study_mode"] if not payload.get("study_mode") else []
                )
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
                ai_fallback.fill_missing(payload, html=html, url=url),
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
        for k, v in ai_filled.items():
            # Discard chrome text returned by the FALLBACK AI for location fields.
            # UTAS pages have "Key Information Entry requirements Course rules"
            # immediately after the Location heading; the AI sometimes copies it
            # verbatim.  Dropping it keeps course_location=None so the online-only
            # rejection filter can fire correctly.
            if k in ("location_text", "course_location") and isinstance(v, str) and _is_location_chrome(v):
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

            matched_row, _match_suffix = match_course_in_pdf_table(
                payload.get("course_name") or "",
                fee_by_course,
                cricos_code=payload.get("cricos_code"),
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
                if payload.get(k) not in (None, "", 0):
                    continue
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
        # bump or other heuristic. If the PDF only publishes a bachelor
        # tier, masters courses end up with that tier; that's a known
        # gap to be solved upstream (per-degree-level PDF parsing or
        # OCR of course-page screenshots), NOT by guessing here.
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
                    evidence.append({
                        "field_key": _k,
                        "value": _v,
                        "confidence": 0.55,
                        "method": "central_page:english_level",
                        "source_url": _central_eng_url or url,
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
        if central_data.get("fee_page_url"):
            payload["has_central_fee_page"] = True

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
    det = map_course_to_category(cname)
    if det:
        if not payload.get("category"):
            payload["category"] = det["category"]
            evidence.append(
                {
                    "field_key": "category",
                    "value": det["category"],
                    "confidence": 0.7,
                    "method": "category:det",
                    "snippet": cname,
                }
            )
        if not payload.get("sub_category"):
            payload["sub_category"] = det["sub_category"]
            evidence.append(
                {
                    "field_key": "sub_category",
                    "value": det["sub_category"],
                    "confidence": 0.7,
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
            _bachelor_floor_breach = _is_bachelor_only and 0 < _dur_years < 2.0
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

    # ── No intake months ────────────────────────────────────────────────────
    _intake_months = payload.get("intake_months") or []
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

    # ── Evidence selection finalisation ────────────────────────────────────
    # Mark the winning evidence row for each field as decision_status="selected"
    # so that scraped_field_evidence.selected mirrors the actual column values
    # written to scraped_courses. The Evidence Review panel relies on selected=True
    # to identify the authoritative source for each value.
    try:
        _finalize_evidence_selection(payload, evidence)
    except Exception as _ev_exc:  # never break the pipeline
        log.warning("_finalize_evidence_selection failed on %s: %s", url, _ev_exc)

    return {
        "url": url,
        "payload": payload,
        "evidence": evidence,
        "provenance_footer": footer,
        "gemini_primary_cost_usd": _gemini_primary_cost,
        "gemini_calls": _gcl_get(),
    }
