"""DOM-aware label-detection regression tests for the duration extractor.

Mirrors `test_study_mode.py`'s structural-label coverage. The bug
class: the loose `<num> <unit>` fallback in duration.py can match the
wrong number when tag-stripping flattens a label/value layout where
the previous field's value happens to contain a number adjacent to a
duration unit (e.g. `<div>Sydney, 3 days a week</div><div>
<strong>Duration</strong></div><div>2 years</div>` → flattened to
"...3 days a week Duration 2 years..."; the fallback gates on a
duration-context word in the same sentence but the boundary collision
can still mislead it on minimalist templates).

The structural pre-pass reads the value cell directly out of the DOM
so the boundary collision can't pollute the captured number.
"""
from __future__ import annotations

import asyncio

from app.services.scraper.extractors import duration


def _run(coro):
    return asyncio.run(coro)


def test_strong_duration_sibling_div_classifies_via_structural_pass():
    """ASA-style adjacent-div idiom: `<div><strong>Duration</strong>
    </div><div>3 years</div>`. Pre-fix the keyword fallback could
    grab a `<num> <unit>` from an adjacent value cell when the page
    minimised duration-context words on the page."""
    html = (
        '<div class="course-header"><strong>Location</strong></div>'
        '<div class="course-header">Sydney - 5 days a week on campus</div>'
        '<div class="course-header"><strong>Duration</strong></div>'
        '<div class="course-header">3 years</div>'
    )
    out = _run(duration.extract(html, "https://e/x"))
    assert out, "structural pre-pass should fire on <strong>Duration</strong>"
    n = out[0].normalized
    assert n["duration"] == 3.0 and n["duration_term"] == "Year", (
        f"Expected 3 years from the Duration cell, got {n!r}. "
        f"The structural pre-pass must not bleed in '5 days a week' "
        f"from the adjacent Location value."
    )
    assert out[0].method == "duration.structural"


def test_dt_dd_duration_classifies_via_structural_pass():
    """`<dt>Course duration</dt><dd>2 years</dd>` — definition list,
    with optional trailing marketing copy that mentions an unrelated
    duration figure that must NOT be picked up."""
    html = (
        "<dl><dt>Course duration</dt><dd>2 years</dd></dl>"
        "<p>Our institution has 50 years of teaching experience.</p>"
    )
    out = _run(duration.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert n["duration"] == 2.0 and n["duration_term"] == "Year"
    assert out[0].method == "duration.structural"


def test_th_td_duration_classifies_via_structural_pass():
    """`<th>Duration</th><td>18 months</td>` — table key/value rows.
    The structural pre-pass must read only the matching td."""
    html = (
        "<table>"
        "<tr><th>Duration</th><td>18 months</td></tr>"
        "<tr><th>Location</th><td>Sydney - 24 weeks of orientation included</td></tr>"
        "</table>"
    )
    out = _run(duration.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert n["duration"] == 18.0 and n["duration_term"] == "Month", (
        f"<th>/<td> structural pre-pass must read only the matching td. "
        f"Got {n!r}."
    )
    assert out[0].method == "duration.structural"


def test_duration_label_does_not_misfire_on_unrelated_strong_tags():
    """Random `<strong>` tags whose text isn't a duration label must
    not trigger the structural pre-pass — the keyword fallback should
    still handle the actual duration sentence in the prose."""
    html = (
        '<a><strong>Apply Now</strong></a>'
        '<div><strong>Contact</strong></div><div>info@uni.edu</div>'
        '<p>Course duration: 4 years full-time.</p>'
    )
    out = _run(duration.extract(html, "https://e/x"))
    assert out
    n = out[0].normalized
    assert n["duration"] == 4.0 and n["duration_term"] == "Year"
    # Method should be the keyword fallback, not structural.
    assert out[0].method != "duration.structural"


def test_dt_dd_duration_rejects_accelerated_in_value_cell():
    """If the dd cell describes an accelerated/fast-track variant,
    the structural pre-pass must NOT short-circuit with that value —
    same `_ACCELERATED` rule as the keyword fallback. Verifies the
    structural path declines so a downstream extractor / keyword
    fallback gets a chance to surface the real duration."""
    html = "<dl><dt>Duration</dt><dd>1 year (accelerated stream)</dd></dl>"
    out = _run(duration.extract(html, "https://e/x"))
    # The accelerated dd is rejected by both the structural pre-pass
    # AND the keyword fallback (which skips accelerated sentences too).
    # Either way, the structural pre-pass must NOT have emitted a
    # 1-year accelerated value as authoritative.
    structural = [r for r in out if r.method == "duration.structural"]
    assert not structural, (
        f"Structural pre-pass must reject accelerated value cells, "
        f"got {structural!r}."
    )
