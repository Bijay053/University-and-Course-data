"""Smoke tests for the ported scraper extractors. Each test feeds a small,
realistic HTML/text snippet through the extractor and asserts the
expected values come back. These tests run offline (no network)."""
from __future__ import annotations

import asyncio

from app.services.scraper.extractors import duration, english_test, fee, intake


def _run(coro):
    return asyncio.run(coro)


# --- Fee ---------------------------------------------------------------------
def test_fee_extracts_international_aud_per_year():
    html = """
    <html><body>
      <h2>International tuition fees</h2>
      <p>The international tuition fee for this program is A$42,000 per year (2026).</p>
      <p>Graduate salary outcomes: $85,000.</p>
    </body></html>
    """
    out = _run(fee.extract(html, "https://x", country="Australia"))
    assert len(out) == 1
    n = out[0].normalized
    assert n["international_fee"] == 42000
    assert n["currency"] == "AUD"
    assert n["fee_term"] == "Annual"
    assert n["fee_year"] == 2026


def test_fee_ignores_salary_only_pages():
    html = "<p>Average graduate salary: $95,000 per year.</p>"
    out = _run(fee.extract(html, "https://x"))
    assert out == []


def test_fee_no_emit_without_tuition_or_intl_context():
    # A page mentioning a $25,000 figure with no tuition/international cue
    # (e.g. a scholarship value, a deposit, a building cost) must NOT be
    # labelled as the international tuition fee.
    html = "<p>Annual scholarship value: $25,000 awarded to top students.</p>"
    out = _run(fee.extract(html, "https://x"))
    assert out == []


def test_fee_picks_intl_over_domestic_when_both_present():
    html = """
    <table>
      <tr><td>Domestic tuition</td><td>$8,500</td></tr>
      <tr><td>International tuition (per year)</td><td>$45,000</td></tr>
    </table>
    """
    out = _run(fee.extract(html, "https://x"))
    assert out and out[0].normalized["international_fee"] == 45000


# --- IELTS / PTE / TOEFL / Cambridge / Duolingo -----------------------------
def test_english_ielts_overall_with_no_band_below():
    html = "<p>IELTS Academic overall 6.5 with no individual band below 6.0.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.5 and n["ielts_listening"] == 6.0


def test_english_pte_score():
    html = "<p>PTE Academic 64 overall.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["pte_overall"].normalized["pte_overall"] == 64.0


def test_english_toefl_score():
    html = "<p>TOEFL iBT: 90 with no section below 20.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["toefl_overall"].normalized["toefl_overall"] == 90.0


def test_english_duolingo_and_cambridge():
    html = "<p>Cambridge C1 Advanced: 185. Duolingo English Test: 110.</p>"
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://x"))}
    assert out["cambridge_overall"].value == 185.0
    assert out["duolingo_overall"].value == 110.0


# --- VIT regression: PR-1.5 hot-fix #2 ---------------------------------------
# Real prose copied from https://vit.edu.au/mba/mba-project-management. Before
# the fix, all 5 IELTS patterns (and their PTE/TOEFL twins) blocked on the word
# "score" sitting between "Overall" and the digit, so 100% of VIT staged rows
# landed with IELTS=— even though the page plainly stated 6.5.
def test_english_ielts_overall_score_x_with_no_band_below_y():
    html = (
        "<p>English test results IELTS Academic: Overall score 6.5, "
        "with no band below 6.0, or Equivalent results in another approved test.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert "ielts_overall" in out
    n = out["ielts_overall"].normalized
    assert n["ielts_overall"] == 6.5 and n["ielts_listening"] == 6.0


def test_english_pte_overall_score_x_with_no_skill_below_y():
    html = (
        "<p>PTE Academic: Overall score 58, with no communicative skill below 50.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert out["pte_overall"].normalized["pte_overall"] == 58.0
    assert out["pte_overall"].normalized["pte_listening"] == 50.0


def test_english_toefl_overall_score_x_with_no_section_below_y():
    html = (
        "<p>TOEFL iBT: Overall score 87, with no section below 17.</p>"
    )
    out = {r.field_key: r for r in _run(english_test.extract(html, "https://vit.edu.au"))}
    assert out["toefl_overall"].normalized["toefl_overall"] == 87.0
    assert out["toefl_overall"].normalized["toefl_listening"] == 17.0


# --- Intake ------------------------------------------------------------------
def test_intake_parses_keyword_window():
    html = "<p>Available intakes: February, July and September.</p>"
    out = _run(intake.extract(html, "https://x"))
    months = out[0].normalized["intake_months"]
    assert "February" in months and "July" in months and "September" in months


def test_intake_parses_full_dates():
    html = "<p>Course start dates: 24 February 2026 and 15 July 2026.</p>"
    out = _run(intake.extract(html, "https://x"))
    n = out[0].normalized
    assert "February" in n["intake_months"] and "July" in n["intake_months"]
    assert n["intake_days"] in {15, 24}


# --- Duration ----------------------------------------------------------------
def test_duration_picks_standard_over_accelerated():
    html = """
    <p>Course duration: 3 years full-time.</p>
    <p>Accelerated stream: 1 year intensive study available.</p>
    """
    out = _run(duration.extract(html, "https://x"))
    n = out[0].normalized
    assert n["duration"] == 3.0 and n["duration_term"] == "Year"


def test_duration_handles_months():
    html = "<p>Program length: 18 months full-time.</p>"
    out = _run(duration.extract(html, "https://x"))
    n = out[0].normalized
    assert n["duration"] == 18.0 and n["duration_term"] == "Month"


# PR-1.5 prod regression: VIT MBA staged duration=10 Year because the loose
# `<num> <unit>` fallback (pattern 3) matched marketing copy like
# "over 10 years of industry partnerships". Tests below lock the contract:
# pattern 3 only fires when a duration-context word is in the same sentence
# AND no anti-context (experience/established/celebrating/...) is present.
def test_duration_rejects_years_experience_marketing_copy():
    """`10 years experience` is staff tenure, not program length."""
    html = "<p>Our staff have over 10 years experience in industry.</p>"
    out = _run(duration.extract(html, "https://x"))
    assert out == [], f"PR-1.5 regression: should not match staff tenure, got {out!r}"


def test_duration_rejects_anniversary_marketing_copy():
    html = "<p>Celebrating 10 years of academic excellence.</p>"
    out = _run(duration.extract(html, "https://x"))
    assert out == [], f"PR-1.5 regression: anniversary copy should not match, got {out!r}"


def test_duration_rejects_established_year_marketing_copy():
    html = "<p>Established in 2014, with 10 years of industry partnerships.</p>"
    out = _run(duration.extract(html, "https://x"))
    assert out == [], f"PR-1.5 regression: institutional history should not match, got {out!r}"


def test_duration_loose_fallback_still_matches_when_context_is_present():
    """Pattern-3 fallback still wins when duration context IS in the
    sentence — full-time without an explicit 'Course duration:' label."""
    html = "<p>Full-time study takes 4 years to complete.</p>"
    out = _run(duration.extract(html, "https://x"))
    n = out[0].normalized
    assert n["duration"] == 4.0 and n["duration_term"] == "Year"


def test_duration_real_signal_wins_over_marketing_noise():
    """Multi-sentence: the legitimate duration sentence must beat the
    rejected marketing-copy sentence — proves the filter rejects the
    bad signal entirely, not just demotes it."""
    html = """
    <p>Established 10 years ago by a team with 20 years experience.</p>
    <p>Course duration is 2 years full-time.</p>
    """
    out = _run(duration.extract(html, "https://x"))
    assert len(out) >= 1, "real duration signal should still extract"
    n = out[0].normalized
    assert n["duration"] == 2.0 and n["duration_term"] == "Year", (
        f"real 2-year duration should win, got {n!r}"
    )
