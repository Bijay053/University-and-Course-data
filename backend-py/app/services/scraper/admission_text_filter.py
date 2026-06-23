"""Strip non-admission sections from course page HTML before Gemini sees it.

The filter is applied only to the HTML copy that goes to Gemini — the original
HTML used by regex/CSS/structural extractors is left intact.  Removing career
outcomes, how-to-apply, open-day, student-life, and course-structure/module
blocks typically cuts Gemini input by 30–50 % and prevents hallucinations from
marketing copy being mistaken for fee or IELTS data.

YAML toggle (default ON, set to false to disable per-university):
    extraction:
      strip_non_admission_content: false
"""
from __future__ import annotations

import logging
import re
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Non-admission section heading patterns
# ---------------------------------------------------------------------------
# Matched against stripped heading text.  A match causes the heading AND all
# following siblings up to the next same-or-higher heading to be removed.
# Conservative — only remove content we are certain never contains fee,
# IELTS, intake, or duration data.
_NON_ADMISSION_HEADING_RE = re.compile(
    r"^(?:"
    # ── Career / graduate outcomes ────────────────────────────────────────
    r"career\s+(?:outcomes?|prospects?|opportunities?|pathways?|options?|goals?)"
    r"|graduate\s+(?:outcomes?|prospects?|pathways?|attributes?|destinations?|careers?)"
    r"|employment\s+(?:outcomes?|prospects?|opportunities?|goals?)"
    r"|possible\s+careers?"
    r"|(?:job|role)s?\s+(?:available|you\s+can\s+do|you[''`]?ll?\s+be\s+qualified)"
    r"|where\s+(?:can\s+this|(?:this|your)\s+(?:course|degree|program(?:me)?))\s+"
    r"(?:take\s+you|lead)"
    r"|after\s+(?:you\s+)?graduate"
    r"|industry\s+(?:connections?|partners?|links?|networks?|links)"
    r"|professional\s+outcomes?"
    # ── How to apply / Application process ───────────────────────────────
    r"|how\s+to\s+apply"
    r"|(?:the\s+)?application\s+(?:process|guide|steps?|timeline)"
    r"|apply\s+(?:now|online|today|for\s+this)"
    r"|how\s+do\s+i\s+apply"
    r"|next\s+steps?"
    r"|what\s+(?:happens|to\s+do)\s+next"
    r"|application\s+and\s+(?:selection|admission)\s+process"
    # ── Open days / Campus visits ─────────────────────────────────────────
    r"|open\s+days?"
    r"|campus\s+(?:tours?|visits?|events?|open\s+days?)"
    r"|book\s+(?:a\s+)?(?:tour|visit|open\s+day|your\s+spot|place)"
    r"|visit\s+(?:us|the\s+campus|our\s+campus)"
    # ── Student life / testimonials ───────────────────────────────────────
    r"|student\s+(?:life|testimonials?|stories|experiences?|ambassadors?|voices?)"
    r"|(?:our\s+)?student\s+community"
    r"|hear\s+from\s+(?:our\s+)?(?:students?|alumni|graduates?)"
    r"|life\s+(?:at|on)\s+(?:campus|(?:the\s+)?university|college)"
    r"|campus\s+(?:life|living|amenities)"
    r"|alumni\s+(?:stories?|testimonials?|profiles?|spotlights?)"
    # ── Course structure / module list ───────────────────────────────────
    # Note: "course overview" is intentionally NOT included — it often
    # carries duration, mode, or fee summaries.
    r"|course\s+structure"
    r"|(?:unit|module|subject)\s+(?:list|guide|details?|selection|overview)"
    r"|what\s+(?:units?|modules?|subjects?)\s+(?:you[''`]?ll?\s+)?study"
    r"|curriculum\s+(?:guide|details?)"
    r"|subjects?\s+and\s+(?:units?|modules?|electives?)"
    # ── Brochure / marketing downloads ───────────────────────────────────
    r"|download\s+(?:a\s+)?(?:brochure|prospectus|flyer|guide)"
    r"|(?:course|program(?:me)?)\s+brochure"
    r")\s*$",
    re.IGNORECASE,
)

# CSS id / class fragments that identify non-admission sections.
# Applied against the element's id + class attributes (joined, lowercased).
_NON_ADMISSION_CLASS_FRAGS: tuple[str, ...] = (
    "career-outcome",
    "graduate-outcome",
    "employment-outcome",
    "careers-section",
    "career-prospect",
    "how-to-apply",
    "application-process",
    "apply-now-section",
    "open-day",
    "campus-visit",
    "student-life",
    "student-testimonial",
    "student-story",
    "student-ambassador",
    "alumni-stories",
    "course-structure",
    "modules-section",
    "units-section",
    "brochure-download",
    "download-prospectus",
    "enquiry-form",
    "lead-capture",
    "interest-form",
    "register-form",
    "registration-form",
    "apply-now",
    "application-form",
)

# Admission-relevant heading signal — stop removing siblings when a same-/
# sub-level heading with this text appears inside the stripped section.
_ADMISSION_HEADING_RE = re.compile(
    r"(?:tuition\s+)?fees?|fee\s+(?:schedule|summary|information)"
    r"|entry\s+req|admission\s+req|academic\s+req|entry\s+criteria"
    r"|english\s+(?:language\s+)?req|language\s+req|ielts|toefl|pte"
    r"|international\s+(?:students?|applicants?|req)"
    r"|intake|start\s+date|application\s+date"
    r"|scholarship|financial\s+(?:support|aid|assistance)"
    r"|cricos|course\s+code|program(?:me)?\s+code"
    r"|duration|study\s+mode|delivery\s+(?:mode|method)",
    re.IGNORECASE,
)


def filter_admission_html(html: str, url: str = "") -> str:
    """Remove non-admission sections from course page HTML.

    Returns a modified copy safe to send to Gemini.  If parsing fails for any
    reason the original HTML is returned unchanged — the filter is best-effort.

    Args:
        html:  Raw HTML of the course page.
        url:   Course URL (used only for debug logging).

    Returns:
        Filtered HTML string.
    """
    if not html:
        return html
    try:
        from bs4 import BeautifulSoup, NavigableString, Tag  # type: ignore[import]

        soup = BeautifulSoup(html, "html.parser")
        removed_sections = 0

        # ── Step 1: id/class-based removal ────────────────────────────────
        # Collect first, then decompose — avoids mutating during iteration.
        css_candidates: list[Any] = []
        for el in soup.find_all(True):
            try:
                if not isinstance(el, Tag):
                    continue
                el_id = (el.get("id") or "").lower()
                el_cls = " ".join(el.get("class") or []).lower()
                combined = f"{el_id} {el_cls}"
                if any(frag in combined for frag in _NON_ADMISSION_CLASS_FRAGS):
                    # Belt-and-suspenders: do not strip if element's text
                    # clearly contains admission data (e.g. class="apply-now"
                    # on a div that also holds the IELTS table).
                    preview = el.get_text(" ", strip=True).lower()[:300]
                    if not _ADMISSION_HEADING_RE.search(preview):
                        css_candidates.append(el)
            except Exception:
                continue
        for el in css_candidates:
            try:
                el.decompose()
                removed_sections += 1
            except Exception:
                pass

        # ── Step 2: heading-delimited section removal ─────────────────────
        heading_tags = ("h2", "h3", "h4", "h5")
        for heading in list(soup.find_all(heading_tags)):
            try:
                if not isinstance(heading, Tag):
                    continue
                heading_text = heading.get_text(" ", strip=True)
                if not _NON_ADMISSION_HEADING_RE.search(heading_text):
                    continue

                # This heading starts a non-admission section.
                heading_level = int(heading.name[1])  # "h2" → 2
                to_remove: list[Any] = [heading]

                for sibling in list(heading.next_siblings):
                    if isinstance(sibling, Tag) and sibling.name in heading_tags:
                        sib_level = int(sibling.name[1])
                        if sib_level <= heading_level:
                            break  # same-or-higher-level heading → stop
                        # Sub-heading: stop if it's admission-relevant
                        if _ADMISSION_HEADING_RE.search(
                            sibling.get_text(" ", strip=True)
                        ):
                            break
                    to_remove.append(sibling)

                for el in to_remove:
                    try:
                        if isinstance(el, Tag):
                            el.decompose()
                        elif isinstance(el, NavigableString):
                            el.extract()
                    except Exception:
                        pass
                removed_sections += 1

            except Exception:
                continue

        if removed_sections:
            log.debug(
                "[ADM-FILTER] %s — stripped %d non-admission section(s)",
                url,
                removed_sections,
            )
        return str(soup)

    except ImportError:
        log.warning("[ADM-FILTER] BeautifulSoup unavailable — skipping admission filter")
        return html
    except Exception as exc:
        log.warning(
            "[ADM-FILTER] failed for %s: %s — returning original HTML unchanged",
            url,
            exc,
        )
        return html
