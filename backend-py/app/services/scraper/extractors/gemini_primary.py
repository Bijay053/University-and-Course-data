"""Gemini Flash primary extractor — hard fields, single call per course.

Replaces the late-running ``ai_fallback`` for fee, duration, English-scores,
mode, and intake fields with a *primary* extraction step that runs BEFORE
the regex extractors.  The regex extractors remain as silent fallbacks: they
only fill in what Gemini left as ``null``.

Design goals
------------
* One Gemini call per course page (cost-efficient, ~$0.00025/course).
* Returns ``{}`` on any error — callers MUST degrade gracefully.
* Hard 30 s timeout, same discipline as the existing AI-fallback block.
* Costs are returned so the orchestrator can accumulate and log a per-scrape
  total at ``[COMPLETE]``.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.ai import gemini_client
from app.services.scraper.extractors._text import html_to_text

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field catalogue
# ---------------------------------------------------------------------------

_HARD_FIELDS: dict[str, str] = {
    "international_fee": (
        "International tuition fee (number in local currency, e.g. 34500 or 125970). "
        "MUST come from the International section of the page (see CRITICAL rule above). "
        "NEVER use a Commonwealth Supported Place / CSP / HECS-HELP fee here — "
        "those are domestic fees. "
        "PRIORITY ORDER — use the first that applies: "
        "(1) 'Full course fee' or 'Total course fee' label → extract that total amount "
        "     and set fee_term='Full Course'. "
        "(2) Annual / per-year fee label → extract the annual amount and set fee_term='Annual'. "
        "(3) Per-semester/trimester only → multiply to annual equivalent. "
        "NEVER extract a 'First year fee' or '1st year fee' when a 'Full course fee' is "
        "also shown on the same page — the full-course total is always preferred. "
        "Null if not explicitly stated in the International section."
    ),
    "domestic_fee": (
        "Annual domestic/local tuition fee (number only). "
        "MUST come from the Domestic section of the page (see CRITICAL rule above). "
        "Commonwealth Supported Place / CSP / HECS fees belong here, not in international_fee. "
        "Null if not stated."
    ),
    "fee_term": (
        "Fee payment period matching the fee you extracted. Pick EXACTLY one: "
        "'Annual', 'Semester', 'Trimester', 'Full Course', 'Per Unit'. "
        "Use 'Full Course' when you extracted a 'Full course fee' or 'Total course fee' label. "
        "Use 'Annual' for per-year fees. Null if ambiguous."
    ),
    "duration_value": (
        "Total duration from enrolment to graduation — FULL-TIME equivalent — "
        "as a NUMBER only (e.g. 2 for a 2-year Masters, 3 for a 3-year Bachelors). "
        "DO NOT report credit-point counts, unit counts, or per-trimester subject loads. "
        "If the page says '5 units of 8 credit points across 2 years' the answer is 2. "
        "If the page gives a range (e.g. 'Minimum 2 years, up to a maximum of 5 years'), "
        "return the MINIMUM (shortest) value — i.e. 2, not 5."
    ),
    "duration_unit": (
        "Unit for duration_value. Use 'years' for year-based programs, "
        "'months' for programs shorter than 1 year. "
        "NEVER use 'units', 'credit points', or 'subjects'."
    ),
    "duration_text": (
        "Raw duration phrase exactly as it appears on the page "
        "(e.g. '2 years full-time', '18 months'). Null if not found."
    ),
    "ielts_overall": (
        "Minimum IELTS *overall* band score required for admission. "
        "Return the OVERALL (total) score ONLY — NOT a sub-band score for "
        "listening, reading, writing, or speaking. "
        "Example: if the page says 'overall score of 7.0, with a minimum of 6.5 "
        "in writing', return 7.0, not 6.5. "
        "CRITICAL: return null if the page does not state an explicit IELTS score "
        "in a number. Do NOT guess or use a default value."
    ),
    "pte_overall": (
        "Minimum PTE Academic overall score required for admission. "
        "Number only. "
        "CRITICAL: return null if the page does not state an explicit PTE score. "
        "Do NOT guess or use a default value."
    ),
    "toefl_overall": (
        "Minimum TOEFL iBT total score required for admission. "
        "Number only. "
        "CRITICAL: return null if the page does not state an explicit TOEFL score. "
        "Do NOT guess or use a default value."
    ),
    "cambridge_overall": (
        "Minimum Cambridge English (CAE/C1/C2) score required for admission (e.g. 169). "
        "Number only. Null if not stated."
    ),
    "duolingo_overall": (
        "Minimum Duolingo English Test score required for admission (e.g. 100). "
        "Number only. Null if not stated."
    ),
    "sub_category": (
        "Academic discipline/specialisation of this course "
        "(e.g. 'Business Administration', 'Computer Science', 'Nursing', "
        "'Hospitality Management'). Be specific. Null if uncertain."
    ),
    "category": (
        "Broad academic field. Pick the BEST match from: "
        "'Business & Management', 'Computer Science & IT', 'Engineering', "
        "'Health Sciences', 'Education', 'Arts & Humanities', 'Law', "
        "'Science', 'Social Sciences', 'Built Environment', "
        "'Hospitality, Tourism & Events', 'Accounting & Finance'. "
        "Null if none fit."
    ),
    "mode": (
        "Primary study mode. Pick EXACTLY one: 'On Campus', 'Online', 'Blended'. "
        "'Online'  — course is taught entirely or primarily online with no required "
        "physical attendance (includes courses whose location is listed as 'Online'). "
        "'Blended' — course explicitly requires BOTH regular on-campus sessions AND "
        "online components (not just optional intensives). "
        "'On Campus' — default when a physical location is mentioned. "
        "If the page lists 'Location: Online', always use 'Online'."
    ),
    "intake_text": (
        "Intake months as a comma-separated list of month names "
        "(e.g. 'January, April, July, October'). "
        "Look for 'Start dates', 'Intake', 'Commencement', or 'Application open' sections. "
        "If specific dates are listed (e.g. '22 June 2026', '20 July 2026', "
        "'25 January 2027'), extract the month names from them (June, July, January). "
        "If only 'Semester 1' / 'Semester 2' labels appear with no explicit month dates, "
        "map them using the standard Australian academic calendar: "
        "Semester 1 = February, Semester 2 = July. "
        "If a 'Start dates and campus' table has column headers like "
        "'Trimester 1 – February 2026' or 'Semester 2 – July 2025', extract "
        "the month from each column header where at least one PHYSICAL campus "
        "(not Online) row shows a checkmark (✓, tick, 'Available'). Exclude "
        "Online-only trimesters/semesters and columns where ALL physical "
        "campus rows show ✗ / cross / 'Not available'. "
        "Return unique months only. Null if not stated."
    ),
    "location_text": (
        "Campus location(s) where this course is physically taught "
        "(e.g. 'Melbourne', 'Armidale', 'Joondalup'). "
        "Use ONLY real city or campus names. "
        "If the page has a pivot table (e.g. 'Availability & Campus', "
        "'Start dates and campus') with period columns (Semester 1/2, "
        "Trimester 1/2/3), the campus names are in the first column data rows "
        "— NOT the column headers. "
        "Include ONLY physical campuses that have a checkmark (✓, tick, "
        "'Available', 'Yes') in at least one period column. "
        "If a row shows ✗ / cross / 'Not available' for ALL period columns, "
        "exclude that campus. "
        "Do NOT include 'Online', 'Virtual', 'Teaching period', 'Term', "
        "'Semester', 'Trimester', or any intake-period / study-period label. "
        "Null if not explicitly stated or if delivery is online-only."
    ),
}

_PROMPT_TEMPLATE = """\
You are a precise data extractor for a university course admission page.
Return ONLY a single JSON object with exactly the keys listed below.
Use JSON null (not the string "null") when a value is not explicitly stated on
the page.  Do NOT invent values.  Do NOT add extra keys.

CRITICAL — DOMESTIC / INTERNATIONAL TAB STRUCTURE:
Many Australian university pages show a "Domestic" section and an
"International" section on the same page (often rendered as tabs labelled
"Domestic students" and "International students", or "For domestic
applicants" and "For international applicants").

Rules you MUST follow when this structure is present:
1. Extract international_fee ONLY from content explicitly labelled for
   international students (e.g. text near "International students",
   "International tuition fee", "International student fee",
   "For international applicants", "Total Tuition Fee (international
   students)", "Tuition fee based on a rate of $X per year").
2. Extract domestic_fee ONLY from content labelled for domestic students
   (e.g. "Domestic students", "Commonwealth Supported Place",
   "CSP fee", "HECS-HELP", "Student Contribution Amount").
3. NEVER put a Commonwealth Supported Place / CSP / HECS / domestic
   contribution amount into international_fee.  These are always
   domestic-only fees and are typically much lower ($5,000–$15,000/yr)
   than the real international tuition ($25,000–$55,000/yr).
4. Extract ielts_overall, pte_overall, toefl_overall, cambridge_overall,
   duolingo_overall from the International section ONLY.  Domestic
   admission requirements (GPA, ATAR, ATARs) must be ignored.
5. If the page says both a "Total course fee (international)" and an
   "Annual rate per year" for international students, prefer the annual
   rate for international_fee and set fee_term='Annual'.

Fields to extract:
{fields_block}

Course URL: {url}

Course page text (may be truncated):
\"\"\"
{text}
\"\"\"
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# Boilerplate element tags and class-name fragments that reliably contain
# navigation, menus, footers, and lead-capture forms rather than
# course-specific content. Removed BEFORE html_to_text so the content
# Gemini receives is course data only.
#
# <form> is included here: actual course data (fees, IELTS, intakes, etc.)
# is never inside a form element — only enquiry / registration / lead-capture
# widgets live in forms. Stripping all forms avoids the common situation where
# UniSQ/other universities embed a large lead-capture form at the TOP of every
# course page, causing Gemini to parse "First Name / Email / I'm in high
# school / I'm looking to do my first degree…" instead of course content.
_BOILERPLATE_TAGS = {"nav", "header", "footer", "aside", "form"}
_BOILERPLATE_CLASS_FRAGS = (
    "nav", "navigation", "menu", "breadcrumb",
    "header", "footer", "sidebar", "widget",
    "cookie", "banner", "alert", "announcement",
    "social", "share", "search-bar",
    # Lead-capture / enquiry form sections that appear before the actual course
    # content on many Australian university pages (UniSQ, etc.).
    "enquir", "enquiry-form", "lead-capture", "contact-form",
    "register-form", "registration-form", "interest-form",
    "apply-now", "application-form", "get-in-touch",
    "chat-widget", "intercom", "hotjar", "drift",
)


def _safe_strip(element: Any, tags: list[str], *, class_frags: tuple[str, ...] = ()) -> None:
    """Collect-then-remove boilerplate from *element* without mutating during iteration.

    The naive ``for tag in element.find_all(True): tag.decompose()`` pattern
    crashes with ``'NoneType' object has no attribute 'get'`` because
    ``find_all`` pre-builds a flat list of *all* descendants; when a parent is
    decomposed its children become invalid but their list slots remain.  This
    helper gathers candidates first, then removes each one with a guarded call
    so that already-invalidated elements are silently skipped.
    """
    to_remove: list[Any] = list(element.find_all(tags))
    if class_frags:
        for tag in element.find_all(True):
            try:
                tag_id = (tag.get("id") or "").lower()
                tag_cls = " ".join(tag.get("class") or []).lower()
                if any(frag in f"{tag_id} {tag_cls}" for frag in class_frags):
                    to_remove.append(tag)
            except Exception:
                pass
    for tag in to_remove:
        try:
            tag.decompose()
        except Exception:
            pass


# Patterns to locate domestic / international tab panels inside the raw DOM.
# Matched against the element's id and class attributes (lowercase, joined).
# Covers UTAS (tabInternational / tabDomestic), ACU, and similarly structured
# Australian university pages that share the same tab idiom.
_INTL_PANEL_RE = re.compile(
    r"\btab[-_]?international\b"
    r"|\binternational[-_]?tab\b"
    r"|\binternational[-_]?(content|panel|section|pane)\b"
    r"|\b(content|panel|section|pane)[-_]?international\b",
    re.IGNORECASE,
)
_DOM_PANEL_RE = re.compile(
    r"\btab[-_]?domestic\b"
    r"|\bdomestic[-_]?tab\b"
    r"|\bdomestic[-_]?(content|panel|section|pane)\b"
    r"|\b(content|panel|section|pane)[-_]?domestic\b",
    re.IGNORECASE,
)


def _promote_international_panel(soup: Any, url: str = "") -> None:
    """Move the international tab/panel element *before* the domestic one.

    Australian university pages (UTAS, ACU, etc.) render a "Domestic" tab
    first in the DOM.  When both panels are serialised to plain text the
    domestic section — including CSP/HECS fees — appears before the
    international section.  Gemini then reads the domestic fee first and
    erroneously emits ``international_fee=null, domestic_fee=<CSP>``.

    We locate both panels by id/class, then use BeautifulSoup's
    ``insert_before`` to reorder them so the international content leads.
    This is a no-op when neither panel is found (safe for any page layout).
    """
    try:
        from bs4.element import Tag

        intl_panel: Any = None
        dom_panel: Any = None

        for el in soup.find_all(True):
            if not isinstance(el, Tag):
                continue
            el_id = (el.get("id") or "").lower()
            el_cls = " ".join(el.get("class") or []).lower()
            combined = f"{el_id} {el_cls}"
            if intl_panel is None and _INTL_PANEL_RE.search(combined):
                intl_panel = el
            if dom_panel is None and _DOM_PANEL_RE.search(combined):
                dom_panel = el
            if intl_panel and dom_panel:
                break

        log.info(
            "gemini_primary[dom_reorder]: url=%s found_intl=%s found_dom=%s applied=%s",
            url,
            intl_panel is not None,
            dom_panel is not None,
            intl_panel is not None and dom_panel is not None and intl_panel is not dom_panel,
        )

        if (
            intl_panel is not None
            and dom_panel is not None
            and intl_panel is not dom_panel
        ):
            dom_panel.insert_before(intl_panel.extract())
    except Exception:
        pass  # never break on reorder failure


# ---------------------------------------------------------------------------
# Text-level international section promotion (fallback for unknown DOM layouts)
# ---------------------------------------------------------------------------
# Text-level markers for identifying section boundaries.
#
# ``_INTL_TEXT_RE`` matches "International students" (or "For international
# students") as a standalone phrase — crucially it requires the word
# "students" to follow so it does NOT fire on compressed nav-bar text like
# "InternationalDomestic" or "International|Domestic".
#
# ``_DOM_ANCHOR_RE`` matches the domestic section by looking for the words
# "Domestic students" OR the distinctive CSP/HECS labels that appear only in
# the domestic-tab content.  This is more robust than a heading-only match on
# pages where the domestic heading is rendered as inline text with no breaks.
_INTL_TEXT_RE = re.compile(
    r"(?:For\s+)?International\s+students?\b",
    re.IGNORECASE,
)
_DOM_ANCHOR_RE = re.compile(
    r"(?:For\s+)?Domestic\s+students?\b"
    r"|Commonwealth\s+Supported\s+Place"
    r"|HECS[- ]HELP"
    r"|Student\s+Contribution\s+Amount",
    re.IGNORECASE,
)


def _promote_international_text(text: str, url: str = "") -> str:
    """Text-level fallback: move the International section before the Domestic one.

    ``html_to_text`` renders most pages as flat single-line strings without
    block separators.  The DOM-level ``_promote_international_panel`` reorder
    therefore cannot rely on newline markers.  This function works by finding
    the first occurrence of an **international-section anchor**
    (``"International students"``  — specifically requiring the word "students"
    so it does not match the compressed nav label ``"InternationalDomestic"``)
    and the first occurrence of a **domestic-section anchor**
    (``"Domestic students"`` or a CSP/HECS label).

    If the domestic anchor appears *before* the international anchor, everything
    from the international anchor onward is extracted and prepended to the
    remaining text so Gemini reads the international fee/IELTS data first.

    Returns ``text`` unchanged when:
    - Either anchor is absent (no tab structure detected — safe no-op).
    - International anchor already precedes the domestic anchor.
    - The anchors are within 50 characters of each other (likely part of the
      same nav-bar label run, not genuine section headings).
    """
    m_intl = _INTL_TEXT_RE.search(text)
    m_dom = _DOM_ANCHOR_RE.search(text)

    if m_intl is None or m_dom is None:
        log.debug("gemini_primary[text_reorder]: no section markers for %s", url)
        return text

    if m_intl.start() <= m_dom.start():
        log.debug(
            "gemini_primary[text_reorder]: international already before domestic "
            "(intl@%d dom@%d) for %s",
            m_intl.start(), m_dom.start(), url,
        )
        return text

    # Extract the international block (from its first anchor to end of text)
    # and prepend it.  We include everything from the anchor onward so we
    # don't risk splitting related content — the pre-anchor domestic block
    # follows as context.
    intl_start = m_intl.start()
    intl_block = text[intl_start:].strip()
    pre_intl = text[:intl_start].strip()

    log.info(
        "gemini_primary[text_reorder]: promoted international section "
        "(was at char %d / %d) for %s",
        intl_start, len(text), url,
    )
    return intl_block + "\n\n" + pre_intl


def _extract_content_html(html: str, url: str = "") -> str:
    """Return the HTML fragment most likely to contain course-specific content.

    Strategy (in order of preference):
    1. ``<main>`` — the semantic landmark; present on KBS, VIT, and most
       modern sites.  Strip boilerplate *within* the extracted element so
       sticky nav bars and course-listing sidebars don't bloat the text.
    2. ``<article>`` — common on blog/CMS layouts.
    3. A ``<div>`` / ``<section>`` whose id/class contains "content"/"main".
    4. Full document stripped of ``<nav>``/``<header>``/``<footer>``/``<aside>``
       and class-matching boilerplate.

    Uses :func:`_safe_strip` (collect-then-remove) throughout to avoid the
    NoneType crash that silently aborted the previous implementation.

    Also calls :func:`_promote_international_panel` before content extraction
    so the international-section text always precedes the domestic-section text
    in the string Gemini receives (fixes UTAS / ACU domestic-tab-first bug when
    tab panels use predictable id/class names).  Text-level reorder in
    :func:`_trim_text` handles unknown DOM layouts as a fallback.
    """
    try:
        from bs4 import BeautifulSoup, Tag

        soup = BeautifulSoup(html, "lxml")

        # Always safe to remove noise tags first — no parent/child ambiguity
        for tag in soup.find_all(["script", "style", "noscript", "template"]):
            try:
                tag.decompose()
            except Exception:
                pass

        # Promote international panel before domestic panel (UTAS / ACU fix).
        _promote_international_panel(soup, url=url)

        # ── Path 1: <main> ────────────────────────────────────────────────
        main = soup.find("main")
        if main and isinstance(main, Tag):
            # Strip nav-like sub-elements from within <main> safely
            _safe_strip(main, list(_BOILERPLATE_TAGS), class_frags=_BOILERPLATE_CLASS_FRAGS)
            return str(main)

        # ── Path 2: <article> ────────────────────────────────────────────
        article = soup.find("article")
        if article and isinstance(article, Tag):
            _safe_strip(article, list(_BOILERPLATE_TAGS), class_frags=_BOILERPLATE_CLASS_FRAGS)
            return str(article)

        # ── Path 3 & 4: full-document strip then find content div ─────────
        _safe_strip(soup, list(_BOILERPLATE_TAGS), class_frags=_BOILERPLATE_CLASS_FRAGS)

        for tag in soup.find_all(["div", "section"]):
            try:
                tag_id = (tag.get("id") or "").lower()
                tag_cls = " ".join(tag.get("class") or []).lower()
                if "content" in tag_id or "content" in tag_cls or "main" in tag_id:
                    return str(tag)
            except Exception:
                pass

        return str(soup)
    except Exception:
        return html  # never break on parse failure


def _trim_text(html: str, *, max_chars: int = 50_000, url: str = "") -> str:
    """Extract main content, convert to plain text, reorder sections, trim.

    Steps:
    1. ``_extract_content_html`` strips nav/header/footer and (where DOM
       structure permits) reorders international/domestic tab panels so the
       international section leads (``_promote_international_panel``).
    2. ``html_to_text`` converts to plain text.
    3. ``_promote_international_text`` is a text-level fallback that reorders
       the 'International students' section before 'Domestic students' when
       step 1's DOM reorder was a no-op (e.g. Cloudflare-protected pages whose
       tab panels use non-standard id/class names like UTAS).
    4. Truncate to max_chars if needed.
    """
    content_html = _extract_content_html(html, url=url)
    text = html_to_text(content_html)
    # Text-level section reorder (fallback for unknown DOM layouts).
    text = _promote_international_text(text, url=url)
    if len(text) <= max_chars:
        return text
    # For very long pages: prefer the first 40 K (intro + fees section) and
    # last 10 K (admission requirements often sit at the bottom of the page).
    return f"{text[:40_000]}\n...\n{text[-10_000:]}"


def _parse_json(raw: str) -> dict[str, Any]:
    """Robustly parse a JSON dict from raw Gemini output (may have fences/noise)."""
    if not raw:
        return {}
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw)
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            pass
    # Best-effort line-by-line fallback for truncated responses
    out: dict[str, Any] = {}
    for line in raw.splitlines():
        m2 = re.match(r'\s*"([^"]+)"\s*:\s*(.+?),?\s*$', line)
        if not m2:
            continue
        key, val_str = m2.group(1), m2.group(2).rstrip(",").strip()
        if val_str == "null":
            out[key] = None
        elif val_str.startswith('"') and val_str.endswith('"'):
            out[key] = val_str[1:-1]
        else:
            try:
                out[key] = float(val_str) if "." in val_str else int(val_str)
            except ValueError:
                out[key] = val_str
    return out


def _coerce(field_key: str, value: Any) -> Any | None:
    """Type-coerce a raw Gemini value to the expected Python type.

    Returns ``None`` when the value is missing, empty, or fails type conversion
    so callers can safely use ``if coerced is not None`` guards.
    """
    if value is None or value == "" or (isinstance(value, list) and not value):
        return None

    _FLOAT_FIELDS = {
        "international_fee", "domestic_fee",
        "duration_value",
        "ielts_overall", "pte_overall", "toefl_overall",
        "cambridge_overall", "duolingo_overall",
    }
    _STR_FIELDS = {
        "fee_term", "duration_unit", "duration_text",
        "sub_category", "category", "mode", "intake_text", "location_text",
    }

    if field_key in _FLOAT_FIELDS:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    if field_key in _STR_FIELDS:
        s = str(value).strip()
        return s or None

    return value


# Controlled-vocabulary validation so Gemini hallucinations on enum fields
# are rejected rather than stored.
_VALID_FEE_TERMS = {"Annual", "Semester", "Trimester", "Full Course", "Per Unit"}
_VALID_DURATION_UNITS = {"years", "months", "weeks"}
_VALID_MODES = {"On Campus", "Online", "Blended"}


def _validate(field_key: str, value: Any) -> Any | None:
    """Return value if it passes field-level validation, else None."""
    if value is None:
        return None
    if field_key == "fee_term" and value not in _VALID_FEE_TERMS:
        log.debug("gemini_primary: invalid fee_term %r — discarding", value)
        return None
    if field_key == "duration_unit" and value not in _VALID_DURATION_UNITS:
        log.debug("gemini_primary: invalid duration_unit %r — discarding", value)
        return None
    if field_key == "mode" and value not in _VALID_MODES:
        log.debug("gemini_primary: invalid mode %r — discarding", value)
        return None
    # Sanity-range checks
    if field_key == "ielts_overall" and not (4.0 <= value <= 9.0):
        log.debug("gemini_primary: ielts_overall %s out of range — discarding", value)
        return None
    if field_key == "pte_overall" and not (30 <= value <= 90):
        log.debug("gemini_primary: pte_overall %s out of range — discarding", value)
        return None
    if field_key == "toefl_overall" and not (30 <= value <= 120):
        log.debug("gemini_primary: toefl_overall %s out of range — discarding", value)
        return None
    if field_key == "duration_value" and not (0.25 <= value <= 10):
        log.debug("gemini_primary: duration_value %s out of range — discarding", value)
        return None
    if field_key == "international_fee" and not (1_000 <= value <= 300_000):
        log.debug("gemini_primary: international_fee %s out of range — discarding", value)
        return None
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def extract_primary(
    html: str,
    url: str,
    *,
    timeout: float = 30.0,
) -> tuple[dict[str, Any], float, int, int, dict]:
    """Run one Gemini Flash call to extract all hard fields.

    Parameters
    ----------
    html:
        Raw HTML of the course page (will be trimmed before sending).
    url:
        Course page URL (included in prompt for context).
    timeout:
        Hard wall-clock ceiling in seconds.  Defaults to 30 s.

    Returns
    -------
    (filled, cost_usd, input_tokens, output_tokens)
        ``filled`` maps field_key → coerced value for every field Gemini
        returned a non-null answer for.  Empty dict on any failure.
        ``cost_usd``, ``input_tokens``, ``output_tokens`` are zero on failure.
    """
    import asyncio as _asyncio

    fields_block = "\n".join(f"- {k}: {hint}" for k, hint in _HARD_FIELDS.items())
    text = _trim_text(html, url=url)
    prompt = _PROMPT_TEMPLATE.format(fields_block=fields_block, url=url, text=text)

    try:
        resp = await _asyncio.wait_for(
            gemini_client.generate(prompt, max_output_tokens=512),
            timeout=timeout,
        )
    except _asyncio.TimeoutError:
        log.warning("gemini_primary: timed out after %ss on %s", timeout, url)
        return {}, 0.0, 0, 0, {"skipped": True, "skip_reason": f"timeout after {timeout}s"}
    except Exception as exc:
        log.warning("gemini_primary: generate failed on %s: %s", url, exc)
        return {}, 0.0, 0, 0, {"skipped": True, "skip_reason": str(exc)}

    if resp.skipped:
        log.warning("gemini_primary: skipped on %s (%s)", url, resp.skip_reason)
        return {}, 0.0, resp.input_tokens, 0, {"skipped": True, "skip_reason": resp.skip_reason}

    raw_data = _parse_json(resp.text)
    filled: dict[str, Any] = {}
    for fk in _HARD_FIELDS:
        coerced = _coerce(fk, raw_data.get(fk))
        validated = _validate(fk, coerced)
        if validated is not None:
            filled[fk] = validated

    log.info(
        "gemini_primary: %s → %d fields filled, cost=$%.6f",
        url,
        len(filled),
        resp.cost_usd,
    )
    # Debug dict — returned to caller so it can emit via the SSE/Celery log path.
    # text_snippet extended to 2000 chars so operators can see whether the
    # international-section reorder fired and where the fee/IELTS data sits.
    _dbg: dict[str, Any] = {
        "html_len": len(html) if html else 0,
        "text_len": len(text),
        "text_snippet": text[:2000],
        "raw_response": (resp.text or "")[:600],
    }
    return filled, resp.cost_usd, resp.input_tokens, resp.output_tokens, _dbg
