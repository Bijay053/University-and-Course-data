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


def test_completion_deadline_does_not_displace_monthly_duration():
    """ACAP / KBS / ACU regression: Graduate Certificate pages that publish a
    completion deadline *before* the real program duration caused the sentence
    tournament to score the deadline year-value higher than the real months
    value.

    Example page layout:
        <p>Students must complete the qualification within 8 years
           of commencement of studies.</p>
        <div>8 months full-time</div>

    Tournament scoring (before fix):
      "…within 8 years of commencement…"  → 8 Year  weight=41,604  (Pattern-2)
      "8 months full-time"                → 8 Month weight= 3,202  (Pattern-2)
      → 8 Year incorrectly wins.

    Root cause: _DURATION_ANTI_CONTEXT did not cover the
    "complete/completing within N years" or "within N years of
    commencement/enrolment" completion-deadline pattern class.

    After the fix, the deadline sentence is excluded from Pattern-2 and the
    real duration sentence wins the tournament.
    """
    html = (
        "<p>Students must complete the qualification within 8 years "
        "of commencement of studies.</p>"
        "<div>8 months full-time</div>"
    )
    out = _run(duration.extract(html, "https://www.acap.edu.au/test/"))
    assert out, "extractor must find the 8-month duration"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"Completion-deadline sentence ('within 8 years of commencement') "
        f"must not beat the real program duration (8 months). Got {n!r}."
    )


def test_within_n_years_of_enrolment_is_anti_context():
    """'within 8 years of enrolment' is always a completion deadline —
    the semester/trimester variant of the same bug class as the commencement
    pattern. Must not win over a real '8 months full-time' duration."""
    html = (
        "<p>Candidates must complete within 8 years of enrolment.</p>"
        "<p>8 months full-time.</p>"
    )
    out = _run(duration.extract(html, "https://www.acap.edu.au/test/"))
    assert out, "extractor must find the 8-month duration"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"'within 8 years of enrolment' deadline must be excluded from "
        f"Pattern-2; got {n!r}."
    )


def test_labeled_duration_unaffected_by_completion_deadline_fix():
    """Pattern-0 (explicit duration label) must still fire on sentences that
    contain 'complete within N years' — the anti-context gate only applies to
    Pattern-2 (the loose fallback). A labeled sentence is always preferred."""
    html = "<p>Course duration: 2 years full-time. Must complete within 4 years.</p>"
    out = _run(duration.extract(html, "https://e/x"))
    assert out, "extractor must fire on the labeled duration sentence"
    n = out[0].normalized
    assert n["duration"] == 2.0 and n["duration_term"] == "Year", (
        f"Pattern-0 (duration label) must fire on 'Course duration: 2 years' "
        f"regardless of the completion-deadline clause. Got {n!r}."
    )


def test_complete_intervening_words_within_years_is_anti_context():
    """KBS grad cert bug: 'complete their qualification within 8 years' has
    1-4 words between 'complete' and 'within' — the old pattern required
    zero intervening words so it silently failed to block the deadline
    sentence, letting 8 Year beat the real '8 months' duration.

    The fix extends the pattern to (?:\\s+\\w+){0,4} before 'within'."""
    html = (
        "<p>Typical Duration / Standard Study Option 8 months / 4 subjects / 2 trimesters.</p>"
        "<p>Students must complete their qualification within 8 years of commencement.</p>"
    )
    out = _run(duration.extract(html, "https://www.kbs.edu.au/courses/grad-cert/"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"'complete their qualification within 8 years' must be blocked by "
        f"anti-context; expected 8 Month but got {n!r}."
    )


def test_complete_all_subjects_within_years_is_anti_context():
    """Variant: 'complete all subjects within 8 years' — two intervening words."""
    html = (
        "<p>8 months full-time study.</p>"
        "<p>You must complete all subjects within 8 years.</p>"
    )
    out = _run(duration.extract(html, "https://www.kbs.edu.au/courses/test/"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"'complete all subjects within 8 years' must be blocked; got {n!r}."
    )
