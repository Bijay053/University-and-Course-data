"""T001 — Verbose Gemini Vision phrasing unit tests.

ASA's MaSTER.png when OCR'd by Gemini returns text that looks like:

    IELTS Academic Overall Band Score: 6.5
    IELTS Academic Listening: 6
    IELTS Academic Reading: 6
    IELTS Academic Writing: 6
    IELTS Academic Speaking: 6
    PTE Academic Overall score: 58
    TOEFL iBT: 85

Previous regexes required the exact vision-prompt format ("IELTS overall: 6.5")
but Gemini sometimes adds "Band Score", "Academic", colon separators, etc.
These tests pin the patterns that handle that verbose output.
"""
from __future__ import annotations

import pytest
from app.services.scraper.extractors import english_test as et


# ── Verbose IELTS overall (Band Score phrasing) ──────────────────────────────

def test_ielts_overall_band_score_colon():
    """Gemini: 'IELTS Academic Overall Band Score: 6.5'."""
    out = et._ielts("IELTS Academic Overall Band Score: 6.5")
    assert out is not None
    assert out["overall"] == 6.5


def test_ielts_overall_band_score_with_sub_bands():
    """Combined: Overall Band Score + per-skill Listening/Reading/Writing/Speaking."""
    text = (
        "IELTS Academic Overall Band Score: 6.5\n"
        "IELTS Academic Listening: 6\n"
        "IELTS Academic Reading: 6\n"
        "IELTS Academic Writing: 6\n"
        "IELTS Academic Speaking: 6"
    )
    out = et._ielts(text)
    assert out is not None
    assert out["overall"] == 6.5
    assert out["listening"] == 6.0
    assert out["reading"] == 6.0
    assert out["writing"] == 6.0
    assert out["speaking"] == 6.0


def test_ielts_sub_bands_colon_separator():
    """Sub-band colon separator: 'IELTS Academic listening: 6'."""
    out = et._ielts(
        "IELTS Academic Overall Band Score: 6.0\n"
        "IELTS Academic listening: 6\n"
        "IELTS Academic reading: 6\n"
        "IELTS Academic writing: 6\n"
        "IELTS Academic speaking: 6"
    )
    assert out is not None
    assert out["overall"] == 6.0
    assert out["listening"] == 6.0
    assert out["reading"] == 6.0
    assert out["writing"] == 6.0
    assert out["speaking"] == 6.0


def test_ielts_overall_bare():
    """Bare vision-prompt format still works: 'IELTS overall: 6.5'."""
    out = et._ielts("IELTS overall: 6.5")
    assert out is not None
    assert out["overall"] == 6.5


# ── Verbose PTE overall ───────────────────────────────────────────────────────

def test_pte_overall_score_colon():
    """Gemini: 'PTE Academic Overall score: 58'."""
    out = et._pte("PTE Academic Overall score: 58")
    assert out is not None
    assert out["overall"] == 58.0


def test_pte_overall_bare():
    """Bare vision-prompt format: 'PTE overall: 58'."""
    out = et._pte("PTE overall: 58")
    assert out is not None
    assert out["overall"] == 58.0


# ── TOEFL parsing ─────────────────────────────────────────────────────────────

def test_toefl_ibt_colon():
    """'TOEFL iBT: 85' — straightforward colon form."""
    out = et._toefl("TOEFL iBT: 85")
    assert out is not None
    assert out["overall"] == 85.0


# ── Full MaSTER.png OCR simulation via extract() ─────────────────────────────

MASTER_PNG_OCR = (
    "IELTS Academic Overall Band Score: 6.5\n"
    "IELTS Academic Listening: 6\n"
    "IELTS Academic Reading: 6\n"
    "IELTS Academic Writing: 6\n"
    "IELTS Academic Speaking: 6\n"
    "PTE Academic Overall score: 58\n"
    "TOEFL iBT: 85"
)


@pytest.mark.asyncio
async def test_extract_full_master_png():
    """End-to-end: extract() on the MaSTER.png OCR text returns all 7 values."""
    html = "<pre>" + MASTER_PNG_OCR + "</pre>"
    results = await et.extract(html, "https://example.com")

    # Build a flat dict of all normalized values from all results.
    merged: dict[str, float] = {}
    for r in results:
        if r.normalized:
            for k, v in r.normalized.items():
                if v not in (None, "", 0):
                    merged.setdefault(k, v)

    assert merged.get("ielts_overall") == 6.5, merged
    assert merged.get("ielts_listening") == 6.0, merged
    assert merged.get("ielts_reading") == 6.0, merged
    assert merged.get("ielts_writing") == 6.0, merged
    assert merged.get("ielts_speaking") == 6.0, merged
    assert merged.get("pte_overall") == 58.0, merged
    assert merged.get("toefl_overall") == 85.0, merged


# ── Noisy-OCR guard: extra surrounding text must not corrupt values ────────────

def test_ielts_sub_bands_not_cross_contaminated_by_pte():
    """PTE minimum skill score (50) must not bleed into IELTS sub-bands."""
    text = (
        "IELTS Academic Overall Band Score: 6.5\n"
        "IELTS Academic Listening: 6\n"
        "IELTS Academic Reading: 6\n"
        "IELTS Academic Writing: 6\n"
        "IELTS Academic Speaking: 6\n"
        "PTE Academic Overall score: 58\n"
        "Minimum PTE skill score: 50\n"  # noisy extra line — must not contaminate
        "TOEFL iBT: 85\n"
        "Note: All scores must be achieved in a single sitting."
    )
    out = et._ielts(text)
    assert out is not None
    assert out["overall"] == 6.5
    # IELTS sub-bands must all be 6.0 — the PTE 50/58 must not leak into them
    for band in ("listening", "reading", "writing", "speaking"):
        assert out[band] == 6.0, f"Band {band} contaminated: {out}"


def test_pte_not_confused_by_ielts_numbers():
    """PTE extraction must pick up 58, not misfire on an adjacent IELTS 6.5."""
    text = (
        "IELTS Academic Overall Band Score: 6.5\n"
        "PTE Academic Overall score: 58\n"
        "No PTE skill below 50\n"
    )
    out = et._pte(text)
    assert out is not None
    assert out["overall"] == 58.0, out
