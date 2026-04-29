"""Pathway program detection and pathway-aware sanity utilities.

Pathway programs (Foundation Studies, ELICOS, UniPrep, bridging courses)
have lower English admission requirements than standard academic degrees.
They must not inherit the university's main IELTS value from the central
English requirements page, and the sanity-check floors that nullify
legitimate low values (e.g. IELTS 4.5 for ELICOS) must be relaxed for
these course types.

Public API
----------
is_pathway_program(course_name, degree_level) -> bool
    True when the course is a preparatory / pathway program.

get_english_floor(field, is_pathway) -> float
    Minimum acceptable value for a given English test field.

english_value_passes_sanity(field, value, is_pathway) -> bool
    True when *value* sits between the pathway-adjusted floor and ceiling.

vision_value_appears_in_page_text(value, field, page_text) -> bool
    True when a vision-extracted value is corroborated by the static
    page text (keyword + value appear within 100 chars of each other).
    Used to bypass the sanity floor when the value is demonstrably real.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Pathway name patterns — match against course name (lowercased)
# ---------------------------------------------------------------------------

PATHWAY_NAME_PATTERNS: list[str] = [
    r"\bfoundation\s+(studies|program|year|course|programme)\b",
    r"\bpathway\b",
    r"\belicos\b",
    r"\benglish\s+language\s+(program|course|preparation|centre|center|programme)\b",
    r"\bpre-?university\b",
    r"\bpre-?bachelor\b",
    r"\bbridging\b",
    r"\benabling\b",
    r"\buniprep\b",
    r"\btertiary\s+preparation\b",
    r"\bdirect\s+entry\b",
    r"\bacademic\s+english\b",
    r"\bpre-?sessional\b",
    r"\bpre-?masters?\b",
    r"\bpre-?graduate\b",
    r"\bpre-?degree\b",
    r"\bpreparat(?:ory|ion)\s+(program|course|year|studies|programme)\b",
    r"\bdiploma\s+of\s+english\b",      # English-language diploma is a pathway
    r"\benglish\s+for\s+(academic|university|study)\b",
]

# Hard exclusions — these look like pathway names but are full qualifications
PATHWAY_EXCLUSION_PATTERNS: list[str] = [
    r"\bdiploma\s+of\s+(?!english\b)",  # "Diploma of X" except Diploma of English
    r"\bgraduate\s+diploma\b",
    r"\bgraduate\s+certificate\b",
    r"\bdoctor(?:ate|al)?\b",
]

# Degree-level strings (from the degree_level extractor) that signal a pathway
_PATHWAY_DEGREE_LEVELS: frozenset[str] = frozenset({
    "certificate iv",
    "certificate iii",
    "certificate ii",
    "certificate i",
    "english language",
    "foundation",
    "non-award",
    "pathway",
    "elicos",
    "bridging",
})

_COMPILED_PATHWAY = [re.compile(p, re.I) for p in PATHWAY_NAME_PATTERNS]
_COMPILED_EXCLUSIONS = [re.compile(p, re.I) for p in PATHWAY_EXCLUSION_PATTERNS]


def is_pathway_program(
    course_name: str | None,
    degree_level: str | None = None,
) -> bool:
    """Return True if the course is a pathway / preparatory program.

    Pathway programs typically have lower English requirements than
    standard academic degrees and should not inherit the university's
    main IELTS minimum from the central English requirements page.

    Parameters
    ----------
    course_name:
        The extracted course name string (case-insensitive matching).
    degree_level:
        Optional degree-level string from the degree_level extractor
        (e.g. "Bachelor's", "Foundation", "Certificate IV").
    """
    # Degree-level hint — cheapest check first
    if degree_level and degree_level.strip().lower() in _PATHWAY_DEGREE_LEVELS:
        return True

    if not course_name:
        return False

    name = course_name.strip()

    # Hard exclusions before positive patterns
    for pat in _COMPILED_EXCLUSIONS:
        if pat.search(name):
            return False

    for pat in _COMPILED_PATHWAY:
        if pat.search(name):
            return True

    return False


# ---------------------------------------------------------------------------
# Pathway-aware English sanity floors / ceilings
# ---------------------------------------------------------------------------

ENGLISH_FLOORS_STANDARD: dict[str, float] = {
    "ielts_overall": 5.5,
    "ielts_listening": 5.0,
    "ielts_reading": 5.0,
    "ielts_speaking": 5.0,
    "ielts_writing": 5.0,
    "toefl_overall": 46.0,
    "pte_overall": 36.0,
    "cambridge_overall": 154.0,
    "duolingo_overall": 80.0,
}

ENGLISH_FLOORS_PATHWAY: dict[str, float] = {
    "ielts_overall": 4.5,
    "ielts_listening": 4.0,
    "ielts_reading": 4.0,
    "ielts_speaking": 4.0,
    "ielts_writing": 4.0,
    "toefl_overall": 32.0,
    "pte_overall": 30.0,
    "cambridge_overall": 140.0,
    "duolingo_overall": 65.0,
}

ENGLISH_CEILINGS: dict[str, float] = {
    "ielts_overall": 9.0,
    "ielts_listening": 9.0,
    "ielts_reading": 9.0,
    "ielts_speaking": 9.0,
    "ielts_writing": 9.0,
    "toefl_overall": 120.0,
    "pte_overall": 90.0,
    "cambridge_overall": 230.0,
    "duolingo_overall": 160.0,
}


def get_english_floor(field: str, *, is_pathway: bool) -> float:
    """Return the minimum acceptable value for *field* given course type."""
    floors = ENGLISH_FLOORS_PATHWAY if is_pathway else ENGLISH_FLOORS_STANDARD
    return floors.get(field, 0.0)


def english_value_passes_sanity(
    field: str,
    value: float,
    *,
    is_pathway: bool,
) -> bool:
    """Return True when *value* is within the pathway-adjusted floor/ceiling."""
    floor = get_english_floor(field, is_pathway=is_pathway)
    ceiling = ENGLISH_CEILINGS.get(field, float("inf"))
    try:
        v = float(value)
    except (TypeError, ValueError):
        return False
    return floor <= v <= ceiling


# ---------------------------------------------------------------------------
# Vision-value corroboration: check whether OCR result appears in page text
# ---------------------------------------------------------------------------

_FIELD_KEYWORDS: dict[str, list[str]] = {
    "ielts_overall": ["ielts"],
    "ielts_listening": ["ielts"],
    "ielts_reading": ["ielts"],
    "ielts_speaking": ["ielts"],
    "ielts_writing": ["ielts"],
    "toefl_overall": ["toefl"],
    "toefl_listening": ["toefl"],
    "toefl_reading": ["toefl"],
    "toefl_writing": ["toefl"],
    "toefl_speaking": ["toefl"],
    "pte_overall": ["pte", "pearson"],
    "pte_listening": ["pte", "pearson"],
    "pte_reading": ["pte", "pearson"],
    "pte_writing": ["pte", "pearson"],
    "pte_speaking": ["pte", "pearson"],
    "cambridge_overall": ["cambridge", "cae", "c1 advanced", "c2 proficiency"],
    "duolingo_overall": ["duolingo", "det"],
}

_VISION_CORROBORATION_WINDOW = 100  # characters each side of keyword


def vision_value_appears_in_page_text(
    value: float | None,
    field: str,
    page_text: str,
) -> bool:
    """Return True when *value* is corroborated by the static page text.

    Checks that both a test-name keyword (e.g. "ielts") AND the numeric
    value (e.g. "6.5") appear within :data:`_VISION_CORROBORATION_WINDOW`
    characters of each other in the lowercased page text.  A value that
    passes this check is almost certainly real and should not be reverted
    by the sanity-check floor, even if the value sits below the standard
    floor (e.g. IELTS 4.5 on an ELICOS page).

    Parameters
    ----------
    value:
        Numeric value extracted by vision OCR.
    field:
        Payload field key (e.g. ``"ielts_overall"``).
    page_text:
        Plain-text representation of the course page (from html_to_text).
    """
    if not page_text or value is None:
        return False

    keywords = _FIELD_KEYWORDS.get(field)
    if not keywords:
        return False

    page_lower = page_text.lower()

    # Build value strings to search for: "6.5" and optionally "6" for integers
    try:
        v_float = float(value)
    except (TypeError, ValueError):
        return False

    value_strs: list[str] = [f"{v_float:g}"]
    if v_float == int(v_float):
        int_str = str(int(v_float))
        if int_str not in value_strs:
            value_strs.append(int_str)

    for kw in keywords:
        if kw not in page_lower:
            continue
        for match in re.finditer(re.escape(kw), page_lower):
            start = max(0, match.start() - _VISION_CORROBORATION_WINDOW)
            end = min(len(page_lower), match.end() + _VISION_CORROBORATION_WINDOW)
            context = page_lower[start:end]
            for v_str in value_strs:
                if re.search(rf"\b{re.escape(v_str)}\b", context):
                    return True

    return False
