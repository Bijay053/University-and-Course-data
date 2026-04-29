"""CRICOS code extraction from Australian course pages.

CRICOS = Commonwealth Register of Institutions and Courses for Overseas
Students.  Australian government register; unique program identifier per
course version.

Format: 6-7 digits + 1 uppercase letter (e.g., '084932E', '0101389').

Two entry points:
  extract_cricos_code(html, text)               — regex-based, works on raw text
  extract_cricos_code_from_html_structured(html) — DOM-aware, more precise

Confidence: 0.95  |  Method: regex:cricos
"""
from __future__ import annotations

import re

from bs4 import BeautifulSoup

# Regex patterns ordered by specificity
CRICOS_LABEL_PATTERNS: list[re.Pattern[str]] = [
    # "CRICOS Code: 084932E" / "CRICOS code: 084932E" / "CRICOS#: 084932E"
    re.compile(
        r"CRICOS\s*(?:Code|code|number|#)?\s*[:\-]?\s*([0-9]{6,7}[A-Z])\b",
        re.IGNORECASE,
    ),
    # "CRICOS 084932E" (bare label followed by code)
    re.compile(r"\bCRICOS\s+([0-9]{6,7}[A-Z])\b"),
]

# Bare CRICOS code without label — used only as last-resort near a "cricos" keyword
CRICOS_BARE_PATTERN: re.Pattern[str] = re.compile(r"\b([0-9]{6,7}[A-Z])\b")

# Format validation: canonical CRICOS is 6-7 digits + one uppercase letter
CRICOS_FORMAT_RE: re.Pattern[str] = re.compile(r"^\d{6,7}[A-Z]$")

# Noise patterns — prevent room numbers / block codes from matching
_CRICOS_NOISE_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"\bROOM\s+\d+[A-Z]", re.IGNORECASE),
    re.compile(r"\bBLOCK\s+\d+[A-Z]", re.IGNORECASE),
]


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_valid_cricos(code: str | None) -> bool:
    """Return True if *code* matches the CRICOS format ``^\\d{6,7}[A-Z]$``."""
    if not code:
        return False
    return bool(CRICOS_FORMAT_RE.match(code))


def extract_cricos_code(html: str | None, text: str | None) -> str | None:
    """Extract a CRICOS code from a course page.

    Searches *text* and *html* (in that order) for labeled CRICOS patterns,
    then falls back to bare-code proximity matching when the word "cricos"
    appears in the text.

    Returns the code in uppercase canonical form, or ``None`` if none found.
    Only returns codes that pass :func:`is_valid_cricos`.
    """
    if not html and not text:
        return None

    # Strategy 1: labeled patterns (most reliable)
    for source in [text, html]:
        if not source:
            continue
        for pattern in CRICOS_LABEL_PATTERNS:
            match = pattern.search(source)
            if match:
                code = match.group(1).upper()
                if is_valid_cricos(code) and not _is_noise(source, match.start()):
                    return code

    # Strategy 2: bare-code proximity (only when page mentions "cricos")
    if text and "cricos" in text.lower():
        for cricos_match in re.finditer(r"cricos", text, re.IGNORECASE):
            start = cricos_match.end()
            window = text[start : start + 80]
            bare_match = CRICOS_BARE_PATTERN.search(window)
            if bare_match:
                code = bare_match.group(1).upper()
                if is_valid_cricos(code):
                    return code

    return None


def extract_cricos_code_from_html_structured(html: str) -> str | None:
    """Try to find a CRICOS code in structured HTML elements.

    Looks inside ``<dt>``, ``<th>``, ``<strong>``, ``<label>``, and
    ``<span>`` elements whose text says "CRICOS".  When found, reads the
    corresponding sibling or parent node for the code value.

    More precise than regex on pages that use standard definition-list or
    table layouts (``<dt>CRICOS Code</dt><dd>084932E</dd>``).
    """
    if not html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    for elem in soup.find_all(["dt", "th", "strong", "label", "span"]):
        elem_text = elem.get_text(" ", strip=True)
        if "cricos" not in elem_text.lower():
            continue

        # Sibling value cell (dd, td, next span)
        sibling = elem.find_next_sibling(["dd", "td", "span"])
        if sibling:
            sibling_text = sibling.get_text(" ", strip=True)
            bare = CRICOS_BARE_PATTERN.search(sibling_text)
            if bare:
                code = bare.group(1).upper()
                if is_valid_cricos(code):
                    return code

        # Parent's combined text (e.g. <p><strong>CRICOS Code:</strong> 084932E</p>)
        if elem.parent:
            parent_text = elem.parent.get_text(" ", strip=True)
            for pattern in CRICOS_LABEL_PATTERNS:
                match = pattern.search(parent_text)
                if match:
                    code = match.group(1).upper()
                    if is_valid_cricos(code):
                        return code

    return None


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _is_noise(text: str, position: int) -> bool:
    """Return True when the CRICOS-like match is actually a noise pattern."""
    window = text[max(0, position - 40) : position + 40]
    return any(p.search(window) for p in _CRICOS_NOISE_PATTERNS)
