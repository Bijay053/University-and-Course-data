"""PR-5 Bug 1: postgrad uni-PDF english bump.

Without this, when the per-course extractor + AI fallback both fail to
fill the english slots, every course on the catalogue inherits the same
single english tier from the uni-level PDF (typically the bachelor
floor). Prod sweep job_8af4a... staged 9/9 ASA courses with IELTS=6.0
TOEFL=60 CAE=169 — masters and bachelors identical — because the PDF
publishes one tier and the loop in single_course.py applied it
uniformly.

Fix: bump IELTS by +0.5, TOEFL/PTE by +5 when the recipient course is
postgrad. Cambridge is left alone (most providers don't shift it
across degree levels).
"""
from __future__ import annotations

from app.services.scraper.pipelines.single_course import (
    _is_postgraduate,
    _postgrad_english_bump,
)


# ──── _is_postgraduate ────────────────────────────────────────────


def test_postgraduate_detected_from_degree_level():
    for lvl in (
        "Master's",
        "master",
        "Postgraduate",
        "Doctorate",
        "PhD",
        "Graduate Certificate",
        "Graduate Diploma",
    ):
        assert _is_postgraduate({"degree_level": lvl}), f"degree_level={lvl!r}"


def test_undergraduate_not_detected_from_degree_level():
    for lvl in ("Bachelor's", "bachelor", "Undergraduate", "Diploma", "Certificate"):
        assert not _is_postgraduate({"degree_level": lvl}), f"degree_level={lvl!r}"


def test_postgraduate_detected_from_course_name_when_degree_level_missing():
    # Defence in depth: when the degree_level extractor failed, fall
    # back to course-name pattern matching so we still bucket correctly.
    for name in (
        "Master of Project Management",
        "Master of Information Technology (Cyber Security)",
        "Doctor of Philosophy",
        "Graduate Certificate in Business",
        "Graduate Diploma of Education",
    ):
        assert _is_postgraduate({"course_name": name}), f"course_name={name!r}"


def test_undergraduate_course_names_not_postgrad():
    for name in (
        "Bachelor of Business",
        "Bachelor of Professional Accounting",
        "Diploma of Information Technology",
    ):
        assert not _is_postgraduate({"course_name": name}), f"course_name={name!r}"


def test_empty_payload_not_postgrad():
    assert not _is_postgraduate({})
    assert not _is_postgraduate({"degree_level": "", "course_name": ""})


# ──── _postgrad_english_bump ──────────────────────────────────────


def test_ielts_bumps_by_half_band():
    assert _postgrad_english_bump("ielts_overall", 6.0) == 6.5
    assert _postgrad_english_bump("ielts_overall", 6.5) == 7.0
    assert _postgrad_english_bump("ielts_overall", 5.5) == 6.0


def test_ielts_bump_handles_int_input():
    # Some PDFs surface IELTS as an int (6) rather than a float (6.0).
    # Result must still be a float so downstream rendering doesn't show
    # "7" instead of "7.0" / "6.5".
    out = _postgrad_english_bump("ielts_overall", 6)
    assert out == 6.5
    assert isinstance(out, float)


def test_toefl_bumps_by_five():
    assert _postgrad_english_bump("toefl_overall", 60) == 65
    assert _postgrad_english_bump("toefl_overall", 79) == 84


def test_pte_bumps_by_five():
    assert _postgrad_english_bump("pte_overall", 50) == 55
    assert _postgrad_english_bump("pte_overall", 58) == 63


def test_cambridge_is_not_bumped():
    # Cambridge Advanced English overall (B2 First / C1 Advanced score
    # 160-210) typically doesn't shift across degree levels — most
    # providers list 169 / 176 for both bachelor and master programs.
    assert _postgrad_english_bump("cambridge_overall", 169) == 169


def test_unknown_slot_returned_unchanged():
    # Defensive: if a future PDF loader surfaces a new slot key we
    # haven't taught the bump function about, leave it alone rather
    # than guess.
    assert _postgrad_english_bump("ielts_listening", 6.0) == 6.0
    assert _postgrad_english_bump("duet_overall", 110) == 110


def test_non_numeric_value_returned_unchanged():
    # Defensive: PDFs occasionally surface text like "6.0-6.5" that
    # didn't parse cleanly. Don't crash, don't guess — return as-is.
    assert _postgrad_english_bump("ielts_overall", "6.0-6.5") == "6.0-6.5"
    assert _postgrad_english_bump("ielts_overall", None) is None


def test_bool_value_returned_unchanged():
    # In Python, bool is-a int. Without the bool guard, True would
    # become 1.5 (or 6 → 11 for toefl). Make sure we don't bump bools.
    assert _postgrad_english_bump("ielts_overall", True) is True
    assert _postgrad_english_bump("toefl_overall", False) is False


# ──── End-to-end: bump fires for masters, not for bachelors ───────


def test_bump_fires_for_master_payload():
    """Sanity: when a payload looks like a masters course, the helpers
    combine to produce the bumped value."""
    payload = {"course_name": "Master of Project Management", "degree_level": "Master's"}
    assert _is_postgraduate(payload)
    assert _postgrad_english_bump("ielts_overall", 6.0) == 6.5
    assert _postgrad_english_bump("toefl_overall", 60) == 65


def test_bump_does_not_fire_for_bachelor_payload():
    """Sanity: bachelors keep the uni-PDF value as-is."""
    payload = {"course_name": "Bachelor of Business", "degree_level": "Bachelor's"}
    assert not _is_postgraduate(payload)
    # The single_course.py loop guards on _is_postgraduate(), so the
    # bump is never called for bachelors. But the helper itself is
    # pure — calling it on a bachelor would still bump. The contract
    # is "caller must check _is_postgraduate first", verified by the
    # _is_postgraduate test above.
