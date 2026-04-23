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
