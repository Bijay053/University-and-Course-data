"""Bug C: study_mode extractor.

Without this extractor the Review table's Mode column was always "--" and
the operator couldn't tell On Campus from Online courses at a glance.
"""
from __future__ import annotations

import asyncio

from app.services.scraper.extractors import study_mode


def _classify(text: str) -> str | None:
    out, _ = study_mode.classify_study_mode(text)
    return out


def test_blended_wins_over_on_campus():
    # When a course is offered both ways the Node taxonomy collapses to
    # "Blended" — verify our precedence matches.
    assert _classify("This course is delivered on-campus and online.") == "Blended"
    assert _classify("Mixed mode delivery") == "Blended"
    assert _classify("hybrid program") == "Blended"


def test_online_recognised():
    assert _classify("Fully online course") == "Online"
    assert _classify("100% online delivery") == "Online"
    assert _classify("Distance learning available") == "Online"


def test_on_campus_recognised():
    assert _classify("On-campus, full-time study") == "On Campus"
    assert _classify("Face-to-face delivery in Sydney") == "On Campus"


def test_returns_none_when_no_signal():
    assert _classify("Apply now for the next intake") is None


def test_extract_returns_extraction_result():
    out = asyncio.run(study_mode.extract("<p>On-campus delivery</p>", "https://e/x"))
    assert len(out) == 1
    assert out[0].field_key == "study_mode"
    assert out[0].value == "On Campus"
    assert out[0].normalized == {"study_mode": "On Campus"}
