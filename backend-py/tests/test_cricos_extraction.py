"""Tests for Priority 3: CRICOS code extraction and CRICOS-first PDF matching.

Covers:
  - cricos_code.py extractor (format validation, labeled patterns,
    DOM-structured extraction, proximity fallback, noise rejection)
  - match_course_in_pdf_table CRICOS-first branch
  - Authority model: uni_pdf:cricos_match:fees = 2.5
"""
from __future__ import annotations

import pytest

from app.services.scraper.extractors.cricos_code import (
    extract_cricos_code,
    extract_cricos_code_from_html_structured,
    is_valid_cricos,
)
from app.services.scraper.pipelines import university_pdfs
from app.services.scraper.pipelines.single_course import (
    METHOD_AUTHORITY,
    _method_authority,
)

# ---------------------------------------------------------------------------
# is_valid_cricos
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code,expected",
    [
        ("084932E", True),        # 6-digit + letter
        ("1234567A", True),       # 7-digit + letter
        ("0101389", False),       # no letter suffix
        ("ABCDE1A", False),       # letters in numeric part
        ("12345", False),         # too short
        ("12345678A", False),     # too long (9 chars)
        ("", False),              # empty
        (None, False),            # None
        ("084932e", False),       # lowercase letter
        ("0849322E", True),       # 7 digits + letter
    ],
)
def test_is_valid_cricos(code, expected):
    assert is_valid_cricos(code) is expected


# ---------------------------------------------------------------------------
# extract_cricos_code — labeled pattern matching
# ---------------------------------------------------------------------------

LABELED_CASES = [
    # Standard "CRICOS Code: XXXXXXX" form
    ("CRICOS Code: 084932E", "084932E"),
    # Case-insensitive label
    ("cricos code: 084932E", "084932E"),
    # No colon
    ("CRICOS 1234567A", "1234567A"),
    # Hash separator
    ("CRICOS#: 084932E", "084932E"),
    # Hyphen separator
    ("CRICOS code - 084932E", "084932E"),
    # Number label (common AU university page)
    ("CRICOS number: 0101389A", "0101389A"),
    # 7-digit code
    ("CRICOS Code: 1011245B", "1011245B"),
]


@pytest.mark.parametrize("text,expected", LABELED_CASES)
def test_extract_cricos_code_labeled(text, expected):
    result = extract_cricos_code(None, text)
    assert result == expected, f"expected {expected!r}, got {result!r}"


def test_extract_cricos_code_returns_none_when_not_present():
    assert extract_cricos_code(None, "No code here") is None
    assert extract_cricos_code("", "") is None
    assert extract_cricos_code(None, None) is None


def test_extract_cricos_code_uppercase_normalisation():
    """Codes that appear in mixed case in the page text are uppercased."""
    result = extract_cricos_code(None, "CRICOS Code: 084932e")
    assert result == "084932E"


def test_extract_cricos_code_rejects_too_short():
    """A 5-digit code like 12345A should not match."""
    result = extract_cricos_code(None, "CRICOS Code: 12345A")
    assert result is None


def test_extract_cricos_code_rejects_too_long():
    """A 9-digit code should not match."""
    result = extract_cricos_code(None, "CRICOS Code: 123456789A")
    assert result is None


def test_extract_cricos_code_searches_html_when_text_missing():
    """Falls back to searching HTML string when text is None."""
    html = "<p>CRICOS Code: 084932E</p>"
    result = extract_cricos_code(html, None)
    assert result == "084932E"


def test_extract_cricos_code_prefers_text_over_html():
    """When both text and HTML are provided, the code in text wins (first-match)."""
    text = "CRICOS Code: 111111A"
    html = "<p>CRICOS Code: 222222B</p>"
    result = extract_cricos_code(html, text)
    assert result == "111111A"


def test_extract_cricos_code_proximity_fallback():
    """Bare code after 'CRICOS' keyword in text is found via proximity scan."""
    text = (
        "This course is registered with CRICOS under the code "
        "084932E for international students."
    )
    result = extract_cricos_code(None, text)
    assert result == "084932E"


# ---------------------------------------------------------------------------
# extract_cricos_code_from_html_structured — DOM-aware extraction
# ---------------------------------------------------------------------------

STRUCTURED_HTML_CASES = [
    # Definition list <dt>/<dd>
    (
        "<dl><dt>CRICOS Code</dt><dd>084932E</dd></dl>",
        "084932E",
    ),
    # Table <th>/<td>
    (
        "<table><tr><th>CRICOS Code</th><td>1234567A</td></tr></table>",
        "1234567A",
    ),
    # <strong> label followed by text inside parent <p>
    (
        "<p><strong>CRICOS Code:</strong> 084932E</p>",
        "084932E",
    ),
    # <span> label with sibling <span>
    (
        "<div><span>CRICOS</span><span>0101389A</span></div>",
        "0101389A",
    ),
    # Empty — no code present
    ("<p>No code here</p>", None),
]


@pytest.mark.parametrize("html,expected", STRUCTURED_HTML_CASES)
def test_extract_cricos_code_from_html_structured(html, expected):
    result = extract_cricos_code_from_html_structured(html)
    assert result == expected, f"html={html!r} → expected {expected!r}, got {result!r}"


def test_extract_cricos_code_from_html_structured_case_insensitive_label():
    html = "<dl><dt>cricos code</dt><dd>084932E</dd></dl>"
    assert extract_cricos_code_from_html_structured(html) == "084932E"


def test_extract_cricos_code_from_html_structured_none_input():
    assert extract_cricos_code_from_html_structured(None) is None
    assert extract_cricos_code_from_html_structured("") is None


# ---------------------------------------------------------------------------
# CRICOS-first branch in match_course_in_pdf_table
# ---------------------------------------------------------------------------

# Minimal two-row fee_by_course dict keyed by CRICOS (as _pick_per_course_amounts
# returns) — no actual PDF parsing needed.
_FEE_BY_COURSE_FIXTURE: dict[str, dict] = {
    "117606J": {
        "international_fee": 52800,
        "currency": "AUD",
        "fee_term": "Annual",
        "fee_year": 2025,
        "_pdf_primary_name": "Master of Project Management",
        "_pdf_match_text": "Master of Project Management",
        "_cricos": "117606J",
    },
    "117597E": {
        "international_fee": 52800,
        "currency": "AUD",
        "fee_term": "Annual",
        "fee_year": 2025,
        "_pdf_primary_name": "Master of Information Technology (Cyber Security)",
        "_pdf_match_text": "Master of Information Technology (Cyber Security)",
        "_cricos": "117597E",
    },
}


def test_cricos_first_lookup_returns_correct_row():
    """When cricos_code matches a key, the CRICOS path fires."""
    row, suffix = university_pdfs.match_course_in_pdf_table(
        "Master of Project Management",
        _FEE_BY_COURSE_FIXTURE,
        cricos_code="117606J",
    )
    assert row is not None
    assert row["international_fee"] == 52800
    assert suffix == "cricos_match"


def test_cricos_first_lookup_strips_private_fields():
    """CRICOS-path result must not expose internal _xxx keys."""
    row, _ = university_pdfs.match_course_in_pdf_table(
        "Master of Project Management",
        _FEE_BY_COURSE_FIXTURE,
        cricos_code="117606J",
    )
    assert row is not None
    for private_key in ("_pdf_primary_name", "_pdf_match_text", "_cricos"):
        assert private_key not in row, f"CRICOS path leaked {private_key!r}"


def test_cricos_first_lookup_ignores_course_name_mismatch():
    """CRICOS lookup wins even when course name doesn't match the PDF row name."""
    row, suffix = university_pdfs.match_course_in_pdf_table(
        "Some Completely Different Course Name",
        _FEE_BY_COURSE_FIXTURE,
        cricos_code="117597E",
    )
    assert row is not None
    assert row["international_fee"] == 52800
    assert suffix == "cricos_match"


def test_cricos_first_lookup_falls_through_on_miss():
    """When the cricos_code is not in fee_by_course, fuzzy matching is tried."""
    row, suffix = university_pdfs.match_course_in_pdf_table(
        "Master of Project Management",
        _FEE_BY_COURSE_FIXTURE,
        cricos_code="999999Z",  # not in fixture
    )
    # Fuzzy path: "Master of Project Management" should match "117606J" row by name.
    assert row is not None
    assert suffix == "name_match"


def test_no_cricos_code_still_fuzzy_matches():
    """When no cricos_code is supplied, existing fuzzy logic works unchanged."""
    row, suffix = university_pdfs.match_course_in_pdf_table(
        "Master of Project Management",
        _FEE_BY_COURSE_FIXTURE,
    )
    assert row is not None
    assert suffix == "name_match"


def test_cricos_first_no_match_returns_none_suffix():
    """No CRICOS key + no fuzzy match → (None, 'no_match')."""
    row, suffix = university_pdfs.match_course_in_pdf_table(
        "Doctor of Veterinary Medicine",
        _FEE_BY_COURSE_FIXTURE,
        cricos_code="999999Z",
    )
    assert row is None
    assert suffix == "no_match"


# ---------------------------------------------------------------------------
# Authority model — CRICOS match tier
# ---------------------------------------------------------------------------


def test_cricos_match_method_in_authority_dict():
    """uni_pdf:cricos_match:fees must be registered at tier 2.5."""
    assert "uni_pdf:cricos_match:fees" in METHOD_AUTHORITY
    assert METHOD_AUTHORITY["uni_pdf:cricos_match:fees"] == 2.5


def test_cricos_match_requirements_in_authority_dict():
    """uni_pdf:cricos_match:requirements must be registered at tier 2.5."""
    assert "uni_pdf:cricos_match:requirements" in METHOD_AUTHORITY
    assert METHOD_AUTHORITY["uni_pdf:cricos_match:requirements"] == 2.5


def test_cricos_match_authority_beats_fuzzy_pdf():
    """Tier 2.5 must be strictly greater than regular uni_pdf tier 2."""
    assert _method_authority("uni_pdf:cricos_match:fees") > _method_authority(
        "uni_pdf:fees:per_course"
    )


def test_cricos_match_authority_below_course_specific():
    """Tier 2.5 must be strictly less than course-specific regex/Gemini tier 3."""
    assert _method_authority("uni_pdf:cricos_match:fees") < _method_authority(
        "gemini_primary"
    )
    assert _method_authority("uni_pdf:cricos_match:fees") < _method_authority("regex")
