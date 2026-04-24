"""Bug G regression: PDF fee parser must mirror Node's ``pickAmounts``.

Prior to the fix, ``_parse_fee_pdf`` reused the single-page web fee
extractor which scores candidates by proximity to a tuition cue word and
returns the single highest-scoring number. On real fee PDFs (e.g. ASA's
2025 international tuition schedule) every amount sits near the same
cue, so the scorer collapsed to picking a per-trimester instalment as
the international fee. The new helper picks the largest amount in the
1k–200k window and tags the term as Full Course / Per Unit / Annual
based on simple structural cues — the same way Node does.
"""
from __future__ import annotations

from app.services.scraper.pipelines.university_pdfs import (
    _pick_amounts_from_pdf_text,
)


def test_full_course_when_three_or_more_unique_amounts():
    txt = """
    Master of Information Technology
    International Tuition (2025)
    Trimester 1: $9,800
    Trimester 2: $9,800
    Trimester 3: $9,800
    Total Course: $58,800
    """
    out = _pick_amounts_from_pdf_text(txt)
    assert out["international_fee"] == 58800
    assert out["currency"] == "AUD"
    # Two unique values (9800, 58800) BUT 58800 / 9800 = 6.0 ≥ 1.4 →
    # Full Course wins via the jump rule. Either trigger is acceptable;
    # this assertion documents the prod-observed shape.
    assert out["fee_term"] == "Full Course"
    assert out["fee_year"] == 2025


def test_full_course_when_jump_ratio_exceeds_1_4():
    # Two amounts only — but the larger is more than 1.4× the smaller
    # → Full Course tag.
    txt = "Per trimester $5,000. Full course $24,000."
    out = _pick_amounts_from_pdf_text(txt)
    assert out["international_fee"] == 24000
    assert out["fee_term"] == "Full Course"


def test_full_course_keyword_overrides():
    # Two amounts close in size (1.4× rule won't fire) but explicit
    # "full course" wording — still Full Course.
    txt = "Stage A tuition $40,000. Total full course tuition: $42,500"
    out = _pick_amounts_from_pdf_text(txt)
    assert out["fee_term"] == "Full Course"
    assert out["international_fee"] == 42500


def test_per_unit_term():
    # Two close amounts (ratio < 1.4 → Full-Course rule does NOT fire),
    # no "full course" wording, "per unit" cue present → Per Unit term.
    txt = "Per unit cost ranges from $4,500 to $4,800 depending on subject."
    out = _pick_amounts_from_pdf_text(txt)
    assert out["fee_term"] == "Per Unit"
    assert out["international_fee"] == 4800


def test_annual_default():
    # Two close amounts, no full-course or per-unit cue → Annual.
    txt = "2024 international annual tuition $32,400. 2025 indicative $33,000."
    out = _pick_amounts_from_pdf_text(txt)
    assert out["fee_term"] == "Annual"
    assert out["international_fee"] == 33000


def test_returns_empty_when_no_amounts():
    assert _pick_amounts_from_pdf_text("No prices on this page.") == {}


def test_amounts_outside_window_ignored():
    # < 1000 (textbook fee) and > 200000 (page count or year) must not
    # leak into the candidate set. Need ≥2 valid in-range amounts to
    # actually return a result under the new gating rule.
    txt = "Books cost $250. Tuition is $18,500 + $19,200 over two years. Ref 320000."
    out = _pick_amounts_from_pdf_text(txt)
    assert out["international_fee"] == 19200


def test_picks_max_not_first():
    # The first amount mentioned (smaller) must not win — pickAmounts
    # always selects the maximum within range.
    txt = "Deposit $1,500 then $24,000 per year."
    out = _pick_amounts_from_pdf_text(txt)
    assert out["international_fee"] == 24000


def test_single_amount_without_tuition_cue_defers():
    # Architect-flagged regression: a single ``$5,000`` amount with no
    # tuition cue (because real tuition is in ``AUD 25,000`` form which
    # this regex doesn't catch) must NOT short-circuit fee.extract.
    # Empty result here means "fall back to fee.extract" upstream.
    txt = "International tuition fee AUD 25,000. Deposit $5,000 due now."
    out = _pick_amounts_from_pdf_text(txt)
    assert out == {}


def test_single_amount_always_defers():
    # With only one $ amount we cannot tell tuition from deposit/textbook
    # and so always defer to fee.extract upstream — even when the cue
    # word "tuition" appears nearby (it could be describing the OTHER
    # AUD amount). Deferral = empty result here.
    assert _pick_amounts_from_pdf_text("International tuition: $24,500") == {}
    assert _pick_amounts_from_pdf_text("Full course $42,000") == {}


def test_two_amounts_proceeds():
    # Two distinct $-amounts is the "real fee table" signal that
    # justifies short-circuiting fee.extract.
    txt = "Per trimester $5,000. Full course $24,000."
    out = _pick_amounts_from_pdf_text(txt)
    assert out["international_fee"] == 24000
