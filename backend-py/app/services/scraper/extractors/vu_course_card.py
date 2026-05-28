"""Victoria University (vu.edu.au) — per-course course-card overrides.

Background
----------
Even after the broken-CMS gate drops VU's soft-404 pages, REAL VU course
pages still contained two sources of brand-chrome contamination that
leaked into the bag-of-text location extractor:

1. Indigenous acknowledgement of country (every VU page footer):
   "of the Kulin Nation (Melbourne campuses), the Eora Nation
    (Sydney campus) and the Yugara/YUgarapul and Turrbal Nation
    (Brisbane campus)"

2. CRICOS registration line (every VU page footer):
   "Victoria University, CRICOS No. 00124K (Melbourne), 02475D
    (Sydney and Brisbane). RTO 3113."

The bag-of-text fallback was picking up "Sydney", "Melbourne", and
"Brisbane" from those two strings and stamping them as the course
location, even on courses delivered at a single campus (e.g. SIT50422
Diploma of Hospitality Management is offered ONLY at Footscray
Nicholson Campus).

The VU course page header has a structured "Course essentials" panel
with a deterministic, VU-prefixed CSS hook that lists the real campus
location:

    <div class="vu-course-essentials-content-label">Location</div>
    <div class="vu-course-essentials-content-wrap">
      <div class="vu-course-essentials-content-value">
        Footscray Nicholson Campus
      </div>
    </div>

This module reads that panel and REPLACE-overrides ``course_location``
on the payload, mirroring the same pattern Federation / CQU / Torrens
per-uni JSON overrides use.

Hostname-gated → no-op for every other uni.  Pure parse, no extra HTTP.
"""
from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_VU_HOSTS = ("www.vu.edu.au", "vu.edu.au")


def is_vu_host(url: str | None) -> bool:
    """True iff ``url`` is on the Victoria University domain."""
    if not url:
        return False
    try:
        return (urlparse(url).netloc or "").lower() in _VU_HOSTS
    except Exception:  # noqa: BLE001
        return False


_BRAND_CHROME_RES: tuple[re.Pattern[str], ...] = (
    # Indigenous acknowledgement of country — appears in EVERY VU page
    # footer.  Lists "Melbourne campuses", "Sydney campus", and
    # "Brisbane campus" as part of the cultural acknowledgement, NOT
    # as the course's delivery location.  The bag-of-text location
    # extractor was reading these and stamping "Sydney, Melbourne,
    # Brisbane" as the campus list on courses delivered exclusively at
    # Footscray Park (e.g. NMPM Master of Project Management — user-
    # reported 2026-05-14).  Both `/` and `\u002F` (JSON-escaped form)
    # variants are present in the same page (raw HTML + embedded Nuxt
    # state).  Anchored on "Kulin Nation" (uniquely VU brand chrome —
    # no other AU uni acknowledges the Kulin Nation in its course
    # pages) and bounded at "Brisbane campus" + the optional clause
    # tail so we never run away into real course content.
    re.compile(
        r"(?i)(?:the\s+)?Kulin\s+Nation[^<>]{0,400}?Brisbane\s+campus[^<>]{0,200}?"
        r"(?:land\.?|country\.?|owners\.?|university\s+land\.?)",
    ),
    # CRICOS registration line — appears in EVERY VU page footer.
    # Lists Melbourne / Sydney / Brisbane as the registered campuses
    # for the *university*, not for any specific course.  Bounded
    # tightly on "CRICOS No." + a CRICOS code so we don't catch any
    # other CRICOS reference that might appear in a real course
    # description.
    re.compile(
        r"(?i)CRICOS\s+No\.?\s*\d{5}[A-Z]\s*\([^)]*\),?\s*"
        r"\d{5}[A-Z]\s*\([^)]*Brisbane[^)]*\)",
    ),
)


def scrub_brand_chrome_html(html: str | None) -> str | None:
    """Remove VU brand-chrome footer chunks from raw HTML.

    Strips two specific footers that ship on every VU page and contaminate
    the bag-of-text location fallback when the structured "Course
    essentials" panel is unavailable (e.g. the panel is hydrated by JS
    and the static HTML the pipeline sees does not contain it):

      1. Indigenous acknowledgement of country (anchored on "Kulin Nation")
      2. CRICOS registration line (anchored on "CRICOS No." + 5-digit code)

    Both chunks list "Sydney", "Melbourne", and "Brisbane" as VU brand-
    chrome, NOT as the delivery location for the current course.  Stripping
    them in-place lets every downstream extractor (regex bag-of-text +
    Gemini) see only the real course content.

    Idempotent: a second call is a no-op because the patterns are gone.
    Returns ``html`` unchanged when ``html`` is empty/None.
    """
    if not html:
        return html
    out = html
    for pat in _BRAND_CHROME_RES:
        out = pat.sub(" ", out)
    return out


def parse_course_card_location(html: str) -> str | None:
    """Return the campus string from VU's "Course essentials" panel.

    Locates the ``vu-course-essentials-content-label`` div whose visible
    text equals ``"Location"`` and returns the joined, deduplicated text
    of the matching ``vu-course-essentials-content-wrap`` block.

    Multi-campus courses are returned as a comma-separated list (e.g.
    ``"City Campus, Sydney Campus, Brisbane Campus"``).

    Returns ``None`` when the panel is absent (e.g. the page is the VU
    soft-404 — which the broken-CMS gate also catches — or VU restructures
    the markup).
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    for label in soup.find_all(class_="vu-course-essentials-content-label"):
        if label.get_text(strip=True).lower() != "location":
            continue
        wrap = label.find_next_sibling(class_="vu-course-essentials-content-wrap")
        if wrap is None:
            # Some VU page variants put the value as a sibling of the
            # label's parent rather than the label itself.
            parent = label.parent
            wrap = (
                parent.find_next_sibling(class_="vu-course-essentials-content-wrap")
                if parent is not None
                else None
            )
        if wrap is None:
            continue
        seen: list[str] = []
        for v in wrap.find_all(class_="vu-course-essentials-content-value"):
            t = v.get_text(" ", strip=True)
            t = re.sub(r"\s+", " ", t).strip(" ,;|")
            if t and t not in seen:
                seen.append(t)
        if seen:
            return ", ".join(seen)
        # Last resort: take the wrap's own text node, normalised.
        fallback = re.sub(r"\s+", " ", wrap.get_text(" ", strip=True)).strip(" ,;|")
        return fallback or None
    return None


def apply_overrides(
    payload: dict[str, Any],
    html: str,
    *,
    url: str = "",
    evidence: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """REPLACE ``payload['course_location']`` with the VU course-card value.

    Returns a dict describing which overrides fired (empty when nothing
    applied).  Idempotent — calling twice is a no-op on the second call
    because the value already matches.
    """
    applied: dict[str, Any] = {}
    if not html:
        return applied
    loc = parse_course_card_location(html)
    if not loc:
        return applied
    prev = (payload.get("course_location") or "").strip()
    if prev == loc:
        return applied
    payload["course_location"] = loc
    applied["course_location"] = {"old": prev or None, "new": loc}
    if evidence is not None:
        evidence.append({
            "field_key": "course_location",
            "method": "vu_course_card",
            "source_url": url,
            "raw_value": loc,
        })
    log.info(
        "[VU COURSE CARD] %s — course_location override: %r → %r",
        url or "(no url)",
        prev or None,
        loc,
    )
    return applied
