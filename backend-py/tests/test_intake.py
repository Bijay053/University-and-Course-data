"""DOM-aware label-detection regression tests for the intake extractor.

These tests lock in the structural pre-pass added in task #24 — the
generalisation of the `<strong>Delivery</strong>` boundary-collision
fix from study_mode.py to the other course fields. See
`backend-py/tests/test_study_mode.py` for the matching set of tests on
study_mode.

The bug class: tag-stripping flattens `<div><strong>Location</strong>
</div><div>Sydney, March</div><div><strong>Intake</strong></div><div>
February, July</div>` into a token run where the previous field's
value (March) sits next to the next field's label (Intake). The
keyword window then picks up months from the wrong cell. The
structural pre-pass reads each `<dd>` / `<td>` / next-sibling-text
directly out of the DOM so the boundary collision can't mislead it.
"""
from __future__ import annotations

import asyncio

from app.services.scraper.extractors import intake


def _run(coro):
    return asyncio.run(coro)


def test_strong_intake_sibling_div_classifies_via_structural_pass():
    """ASA-style adjacent-div idiom: `<div><strong>Intake</strong></div>
    <div>February, July</div>`. Pre-fix, the keyword window walked the
    flattened text and could pick up months from the previous field's
    value cell (e.g. a Location row that mentions "October" as part of
    a campus name). The structural pre-pass reads the value out of the
    next-sibling div directly."""
    html = (
        '<div class="course-header"><strong>Location</strong></div>'
        '<div class="course-header">Sydney, October Campus</div>'
        '<div class="course-header"><strong>Intake</strong></div>'
        '<div class="course-header">February, July</div>'
    )
    out = _run(intake.extract(html, "https://e/x"))
    assert out, "structural pre-pass should fire on <strong>Intake</strong>"
    n = out[0].normalized
    assert n["intake_months"] == ["February", "July"], (
        f"Expected only Feb/Jul from the Intake cell, got {n['intake_months']!r}. "
        f"The structural pre-pass must not bleed in 'October' from the "
        f"adjacent Location value."
    )
    assert out[0].method == "intake.structural"


def test_dt_dd_intake_classifies_via_structural_pass():
    """`<dt>Intake</dt><dd>February, July</dd>` — definition-list shape.
    Trailing marketing copy that mentions other months must not bleed
    into the captured value."""
    html = (
        "<dl><dt>Intake</dt><dd>February, July</dd></dl>"
        "<p>Apply by October to secure your seat in the next "
        "September application window.</p>"
    )
    out = _run(intake.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert n["intake_months"] == ["February", "July"], (
        f"<dt>/<dd> structural pre-pass must read only the dd value. "
        f"Got {n['intake_months']!r}."
    )
    assert out[0].method == "intake.structural"


def test_th_td_intake_classifies_via_structural_pass():
    """`<th>Intake</th><td>March, August</td>` — table key/value rows.
    A neighbouring row that contains month-shaped text in another
    field's value must not pollute the intake capture."""
    html = (
        "<table>"
        "<tr><th>Intake</th><td>March, August</td></tr>"
        "<tr><th>Location</th><td>Sydney, October Open Day campus tour</td></tr>"
        "</table>"
    )
    out = _run(intake.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert "March" in n["intake_months"] and "August" in n["intake_months"]
    assert "October" not in n["intake_months"], (
        f"<th>/<td> structural pre-pass must read only the matching td. "
        f"Got {n['intake_months']!r}."
    )
    assert out[0].method == "intake.structural"


def test_dt_dd_intake_with_full_dates_captures_day():
    """The pre-pass reuses the same two-pass parser as the keyword
    fallback: full `day Month` dates first, bare month names as a
    backup. Day-of-month should round-trip into intake_days."""
    html = "<dl><dt>Start dates</dt><dd>24 February 2026</dd></dl>"
    out = _run(intake.extract(html, "https://e/x"))
    assert out and out[0].normalized["intake_months"] == ["February"]
    assert out[0].normalized["intake_days"] == 24


def test_intake_label_does_not_misfire_on_unrelated_strong_tags():
    """`<strong>Apply Now</strong>` is not an intake label; the
    structural pre-pass must not consume any text after it."""
    html = (
        '<p>This is a great course.</p>'
        '<a><strong>Apply Now</strong></a>'
        '<div><strong>Contact</strong></div><div>info@uni.edu</div>'
        '<p>Available intakes: February, July.</p>'
    )
    out = _run(intake.extract(html, "https://e/x"))
    # Structural pre-pass shouldn't fire (no recognised intake label),
    # but the keyword fallback should still pick up the prose months.
    assert out
    months = out[0].normalized["intake_months"]
    assert "February" in months and "July" in months
    # Method must be the keyword fallback, NOT structural.
    assert out[0].method != "intake.structural"
