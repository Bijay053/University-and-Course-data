"""Unit tests for the per-course Bootstrap-modal English-test extractor.

The modal HTML used in these tests is structurally identical to what
VIT publishes on every course page (Bootstrap modal containing a
concordance table with IELTS / PTE / TOEFL / CAE columns).
"""
from __future__ import annotations

import pytest

from app.services.scraper.per_course_modal import (
    _classify_row_numbers,
    _select_target_ielts,
    extract_modal_english,
)


# Representative VIT BBus modal: three rows (Diploma 5.5, Bachelor 6.0,
# Masters 6.5) with the surrounding "no individual band below" sentence
# in the *page body* outside the modal — exercises both the row-picker
# and the sub-band scan-text fallback.
_VIT_MODAL_HTML = """
<html><body>
  <div class="course-summary">
    <h1>Bachelor of Business</h1>
    <p>Entry: IELTS Academic: Overall 6.0, with no individual band below 5.5</p>
    <button data-bs-toggle="modal" data-bs-target="#englishModal">English requirements</button>
  </div>
  <div class="modal" id="englishModal">
    <div class="modal-content">
      <h3>English language requirements — IELTS, PTE, TOEFL equivalencies</h3>
      <table>
        <thead>
          <tr><th>Level</th><th>IELTS</th><th>PTE</th><th>TOEFL</th><th>CAE</th></tr>
        </thead>
        <tbody>
          <tr><td>Diploma / Certificate</td><td>5.5</td><td>42</td><td>59</td><td>162</td></tr>
          <tr><td>Bachelor</td><td>6.0</td><td>50</td><td>72</td><td>169</td></tr>
          <tr><td>Master / MBA</td><td>6.5</td><td>58</td><td>79</td><td>176</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</body></html>
"""


def test_select_target_ielts_picks_per_degree() -> None:
    assert _select_target_ielts("Master of Business Administration", "Master") == 6.5
    assert _select_target_ielts("MBA Finance", "") == 6.5
    assert _select_target_ielts("Bachelor of Business", "Bachelor") == 6.0
    assert _select_target_ielts("Diploma of IT", "Diploma") == 5.5
    assert _select_target_ielts("Some Course", "") == 6.0


def test_classify_row_numbers_buckets_correctly() -> None:
    # Bachelor row: IELTS 6.0, PTE 50, TOEFL 72, CAE 169.
    out = _classify_row_numbers([6.0, 50.0, 72.0, 169.0])
    assert out["ielts_overall"] == 6.0
    assert out["pte_overall"] == 50
    assert out["toefl_overall"] == 72
    assert out["cambridge_overall"] == 169


def test_classify_row_numbers_handles_pte_toefl_collision() -> None:
    # PTE 50 first, then TOEFL 50 — only the second should land in TOEFL.
    out = _classify_row_numbers([6.0, 50.0])
    assert out.get("pte_overall") == 50
    # No second integer in [30, 120] != 50 — TOEFL should be empty.
    assert "toefl_overall" not in out


def test_extract_modal_english_picks_bachelor_row() -> None:
    """For a Bachelor course, the extractor should pick the row whose
    IELTS is closest to 6.0 — the middle row in the VIT fixture."""
    out = extract_modal_english(
        _VIT_MODAL_HTML, course_name="Bachelor of Business", degree_level="Bachelor"
    )
    assert out["ielts_overall"] == 6.0
    assert out["pte_overall"] == 50
    assert out["toefl_overall"] == 72
    assert out["cambridge_overall"] == 169
    # Sub-bands recovered from the page body's "no individual band below 5.5".
    assert out["ielts_listening"] == 5.5
    assert out["ielts_reading"] == 5.5
    assert out["ielts_writing"] == 5.5
    assert out["ielts_speaking"] == 5.5
    assert "__modal_summary" in out


def test_extract_modal_english_picks_master_row() -> None:
    """For an MBA, the extractor should pick the row whose IELTS is
    closest to 6.5 — the bottom row in the VIT fixture."""
    out = extract_modal_english(
        _VIT_MODAL_HTML, course_name="MBA Finance", degree_level="Master"
    )
    assert out["ielts_overall"] == 6.5
    assert out["pte_overall"] == 58
    assert out["toefl_overall"] == 79
    assert out["cambridge_overall"] == 176


def test_extract_modal_english_picks_diploma_row() -> None:
    """For a Diploma, the extractor should pick the row whose IELTS is
    closest to 5.5 — the top row in the VIT fixture."""
    out = extract_modal_english(
        _VIT_MODAL_HTML, course_name="Diploma of Business", degree_level="Diploma"
    )
    assert out["ielts_overall"] == 5.5
    assert out["pte_overall"] == 42
    assert out["toefl_overall"] == 59
    assert out["cambridge_overall"] == 162


def test_extract_modal_english_returns_empty_when_no_modal() -> None:
    html = "<html><body><p>No modal here.</p></body></html>"
    assert extract_modal_english(html, course_name="X", degree_level="Bachelor") == {}


def test_extract_modal_english_returns_empty_when_modal_lacks_keywords() -> None:
    """A modal that doesn't mention IELTS/PTE/TOEFL is ignored."""
    html = """
    <html><body>
      <div class="modal">
        <h3>Apply now</h3>
        <p>Click here to start your application.</p>
      </div>
    </body></html>
    """
    assert extract_modal_english(html, course_name="X", degree_level="") == {}


def test_extract_modal_english_short_form_subbands() -> None:
    """Pattern C: short-form 'L X.X R X.X W X.X S X.X' wins when no
    Pattern A / A2 / B sentence is present."""
    # Modal text must clear the 80-char minimum gate. We deliberately
    # avoid the words "minimum", "no individual band", and
    # "Listening/Reading" so only Pattern C is in play.
    html = """
    <html><body>
      <p>Required test sub-scores: L 6.0 R 6.0 W 5.5 S 5.5</p>
      <div class="modal">
        <h3>English language equivalency table</h3>
        <p>The following test scores are accepted for entry into this Bachelor
        of Business course at our institution.</p>
        <table>
          <tr><th>IELTS</th><th>PTE</th><th>TOEFL</th></tr>
          <tr><td>6.0</td><td>50</td><td>72</td></tr>
        </table>
      </div>
    </body></html>
    """
    out = extract_modal_english(html, course_name="Bachelor of X", degree_level="Bachelor")
    assert out["ielts_overall"] == 6.0
    assert out["ielts_listening"] == 6.0
    assert out["ielts_reading"] == 6.0
    assert out["ielts_writing"] == 5.5
    assert out["ielts_speaking"] == 5.5
