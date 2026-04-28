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
        "Annual international tuition fee (number in local currency, e.g. 34500). "
        "If listed as a total course fee, divide by the duration in years to get annual. "
        "If stated per-trimester or per-semester, convert to annual equivalent. "
        "Null if not explicitly stated on this page."
    ),
    "domestic_fee": (
        "Annual domestic/local tuition fee (number only). Null if not stated."
    ),
    "fee_term": (
        "Fee payment period. Pick EXACTLY one: "
        "'Annual', 'Semester', 'Trimester', 'Full Course', 'Per Unit'. "
        "Use 'Full Course' only when the page quotes one total price for the "
        "entire program. Null if ambiguous."
    ),
    "duration_value": (
        "Total duration from enrolment to graduation — FULL-TIME equivalent — "
        "as a NUMBER only (e.g. 2 for a 2-year Masters, 3 for a 3-year Bachelors). "
        "DO NOT report credit-point counts, unit counts, or per-trimester subject loads. "
        "If the page says '5 units of 8 credit points across 2 years' the answer is 2."
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
        "Minimum IELTS overall band score required for admission (e.g. 6.5). "
        "Number only. Null if not stated on this page."
    ),
    "pte_overall": (
        "Minimum PTE Academic overall score required for admission (e.g. 58). "
        "Number only. Null if not stated on this page."
    ),
    "toefl_overall": (
        "Minimum TOEFL iBT total score required for admission (e.g. 85). "
        "Number only. Null if not stated on this page."
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
        "Null if not stated."
    ),
    "location_text": (
        "Campus location(s) where this course is physically taught "
        "(e.g. 'Melbourne', 'Sydney, Brisbane', 'Ballarat'). "
        "Use ONLY real city or campus names. "
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
# navigation, menus, and footers rather than course-specific content.
# Removed BEFORE html_to_text so the content Gemini receives is course data.
_BOILERPLATE_TAGS = {"nav", "header", "footer", "aside"}
_BOILERPLATE_CLASS_FRAGS = (
    "nav", "navigation", "menu", "breadcrumb",
    "header", "footer", "sidebar", "widget",
    "cookie", "banner", "alert", "announcement",
    "social", "share", "search-bar",
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


def _extract_content_html(html: str) -> str:
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


def _trim_text(html: str, *, max_chars: int = 50_000) -> str:
    """Extract main content, convert to plain text, trim to max_chars.

    Uses ``_extract_content_html`` to strip nav/header/footer before running
    ``html_to_text``, so Gemini receives course-specific content rather than
    navigation boilerplate.  Limit raised from 8 K to 50 K chars — Gemini
    Flash's context window is 1 M tokens and the extra input costs < $0.001.
    """
    content_html = _extract_content_html(html)
    text = html_to_text(content_html)
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
    text = _trim_text(html)
    prompt = _PROMPT_TEMPLATE.format(fields_block=fields_block, url=url, text=text)

    try:
        resp = await _asyncio.wait_for(
            gemini_client.generate(prompt, max_output_tokens=512),
            timeout=timeout,
        )
    except _asyncio.TimeoutError:
        log.warning("gemini_primary: timed out after %ss on %s", timeout, url)
        return {}, 0.0, 0, 0, {}
    except Exception as exc:
        log.warning("gemini_primary: generate failed on %s: %s", url, exc)
        return {}, 0.0, 0, 0, {}

    if resp.skipped:
        log.warning("gemini_primary: skipped on %s (%s)", url, resp.skip_reason)
        return {}, 0.0, resp.input_tokens, 0, {}

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
    # Debug dict — returned to caller so it can emit via the SSE/Celery log path
    _dbg: dict[str, Any] = {
        "html_len": len(html) if html else 0,
        "text_len": len(text),
        "text_snippet": text[:500],
        "raw_response": (resp.text or "")[:400],
    }
    return filled, resp.cost_usd, resp.input_tokens, resp.output_tokens, _dbg
