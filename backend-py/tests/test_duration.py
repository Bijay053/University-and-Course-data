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
    assert out[0].method != "duration.structural"


def test_dt_dd_duration_rejects_accelerated_in_value_cell():
    """If the dd cell describes an accelerated/fast-track variant,
    the structural pre-pass must NOT short-circuit with that value —
    same `_ACCELERATED` rule as the keyword fallback. Verifies the
    structural path declines so a downstream extractor / keyword
    fallback gets a chance to surface the real duration."""
    html = "<dl><dt>Duration</dt><dd>1 year (accelerated stream)</dd></dl>"
    out = _run(duration.extract(html, "https://e/x"))
    structural = [r for r in out if r.method == "duration.structural"]
    assert not structural, (
        f"Structural pre-pass must reject accelerated value cells, "
        f"got {structural!r}."
    )


# ── Admission-boilerplate anti-context (Fix 1) ────────────────────────────────

def test_years_of_schooling_boilerplate_is_not_duration():
    """'completed 12 years of schooling' must not extract as a program duration.

    'completed' is in _DURATION_CONTEXT so Pattern-2 would fire on '12 years'
    without the admission-boilerplate guard in _DURATION_ANTI_CONTEXT.
    This is the exact Torrens bug: '12 years of schooling' admission requirement
    on the same page as '1 year, 8 months' program duration → 12 Year wins.
    """
    html = "<p>Applicants must have completed 12 years of schooling.</p>"
    out = _run(duration.extract(html, "https://www.torrens.edu.au/test/"))
    assert not out, (
        f"'completed 12 years of schooling' must not produce a duration; "
        f"got {out!r}. Check _DURATION_ANTI_CONTEXT schooling guard."
    )


def test_year_12_boilerplate_is_not_duration():
    """'Year 12 or equivalent' must not extract as a program duration."""
    html = "<p>Applicants must have completed Year 12 or equivalent.</p>"
    out = _run(duration.extract(html, "https://www.torrens.edu.au/test/"))
    assert not out, (
        f"'Year 12 or equivalent' must not produce a duration; got {out!r}."
    )


def test_compound_duration_beats_schooling_boilerplate():
    """Real Torrens page pattern: compound duration in the body text alongside
    '12 years of schooling' in the admission requirements section.

    Without the schooling guard: 'completed' fires _DURATION_CONTEXT, Pattern-2
    picks up '12 years' (weight 62 400) and beats the compound '1 year 8 months'
    result (weight 12 003) → stored as 12 Year.

    With the guard: '12 years of schooling' is blocked by _DURATION_ANTI_CONTEXT,
    the compound result (20 Month) is the only candidate → correct.
    """
    html = (
        "<p>Duration: 1 year, 8 months / 12 subjects / 5 trimesters</p>"
        "<p>Applicants must have completed 12 years of schooling or equivalent.</p>"
    )
    out = _run(duration.extract(html, "https://www.torrens.edu.au/test/"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 20.0 and n["duration_term"] == "Month", (
        f"Expected 20 Month (= 1 year 8 months); got {n!r}. "
        f"'12 years of schooling' must not beat the compound duration."
    )


def test_slash_delimited_duration_structural_pass():
    """KBS / Torrens pattern: '8 months / 4 subjects / 2 trimesters' in a
    dt/dd pair.  The structural pre-pass reads the value cell directly so
    _classify_duration_value picks up '8 months' as the first digit match.
    """
    html = (
        "<dl>"
        "<dt>Duration</dt>"
        "<dd>8 months / 4 subjects / 2 trimesters</dd>"
        "</dl>"
        "<p>Applicants must have completed Year 12 or equivalent.</p>"
    )
    out = _run(duration.extract(html, "https://www.kbs.edu.au/test/"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"Expected 8 Month from dt/dd cell; got {n!r}."
    )
    assert out[0].method == "duration.structural"


# ── Same-N cross-check ────────────────────────────────────────────────────────

def test_same_n_cross_check_prefers_month_when_year_escapes_anti_context():
    """Same-N cross-check fix (KBS / Torrens grad cert bug).

    Candidature-deadline phrasings vary widely — not all are caught by the
    anti-context guard.  When the tournament contains both (N, Year) and
    (N, Month) for the same integer N >= 5, Month wins because no accredited
    Australian graduate certificate runs for 5+ years.

    This test uses 'expected course duration is up to 8 years for part-time
    students' — duration_context fires (course/duration/part-time), it is NOT
    a cap sentence, and it does NOT match the completion-deadline anti-context
    guard, so (8, Year) enters the tournament.  Without the cross-check it
    would win; with it, Month is preferred.
    """
    html = (
        "<p>8 months of full-time study required.</p>"
        "<p>The expected course duration is up to 8 years for part-time students.</p>"
    )
    out = _run(duration.extract(html, "https://www.kbs.edu.au/courses/test/"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"Same-N cross-check must prefer Month=8 over Year=8 when both are "
        f"in the tournament and N >= 5; got {n!r}."
    )


def test_cross_check_does_not_fire_for_different_n():
    """Cross-check only fires when Year and Month have the SAME numeric value N.

    If N differs (e.g., labeled 'Duration: 2 years' while the page also
    mentions '8 months of work placement'), the cross-check must NOT flip
    the winner — Pattern-0 (labeled) wins at x100 priority regardless.
    """
    html = (
        "<div><strong>Duration</strong></div>"
        "<div>2 years full-time.</div>"
        "<p>The program includes 8 months of industry placement.</p>"
    )
    out = _run(duration.extract(html, "https://e/x"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 2.0 and n["duration_term"] == "Year", (
        f"Cross-check must NOT fire: Year N=2 differs from Month N=8. "
        f"Labeled 'Duration: 2 years' must win unchanged; got {n!r}."
    )


# ── Bug A: KBS slash-structured program cells ─────────────────────────────


def test_bug_a_slash_structure_months_wins_over_candidature_deadline():
    """Bug A (KBS grad certs): 'N months / M subjects / K trimesters' format.

    The first slash-token is the real program duration.  The sentence contains
    no duration-context word so Pattern-2 was blocked; a candidature-deadline
    sentence ('complete within 8 years') then won the weight tournament and was
    nullified by the grad-cert sanity cap (4 yr max), dropping the course.

    Fix: _SLASH_PROGRAM_STRUCTURE_RE fires at Pattern-0 priority (×100) so
    (8, Month) always beats any fallback year match.
    """
    html = (
        "<p>8 months / 4 subjects / 2 trimesters</p>"
        "<p>Students must complete their qualification within 8 years of "
        "commencement of studies.</p>"
    )
    out = _run(duration.extract(html, "https://www.kbs.edu.au/courses/grad-cert-accounting/"))
    assert out, "extractor must find a duration for the KBS grad cert"
    n = out[0].normalized
    assert n["duration"] == 8.0 and n["duration_term"] == "Month", (
        f"Slash-structure pre-check must yield (8, Month); got {n!r}. "
        f"Before Bug A fix the candidature deadline '8 years' won the "
        f"tournament and was nullified by the grad-cert sanity cap."
    )


def test_bug_a_slash_structure_trimesters_variant():
    """Slash-structure where the first token is trimesters, not months."""
    html = "<p>2 trimesters / 8 subjects / 1 year</p>"
    out = _run(duration.extract(html, "https://e/x"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 2.0 and n["duration_term"] == "Trimester", (
        f"First slash-token (2 trimesters) must win; got {n!r}."
    )


def test_bug_a_slash_structure_does_not_fire_mid_sentence():
    """Slash-structure regex only fires when the number+unit begins the sentence.

    A slash embedded mid-sentence ('Fee: $12,000 / year') must NOT trigger it.
    The labeled 'Duration: 3 years' should still win via the structural pre-pass.
    """
    html = (
        "<div><strong>Duration</strong></div>"
        "<div>3 years full-time</div>"
        "<p>International fee is $12,000 / year or $6,000 / semester.</p>"
    )
    out = _run(duration.extract(html, "https://e/x"))
    assert out, "extractor must find a duration"
    n = out[0].normalized
    assert n["duration"] == 3.0 and n["duration_term"] == "Year", (
        f"Mid-sentence slash must not trigger slash-structure logic; got {n!r}."
    )


def test_bug_a_slash_structure_multiple_kbs_variants():
    """All three KBS grad-cert durations that were previously dropped.

    KBS pages expose duration in a labeled <dt>/<dd> cell.  The structural
    pre-pass reads the value cell directly and calls _classify_duration_value
    which picks the first digit+unit match from finditer — always "8 months"
    — regardless of trailing slash tokens.  Previously these courses were
    dropped because the sentence-level path (which fires when no structural
    label exists) produced NULL duration after the sanity-cap nullification.
    This test confirms the structural pre-pass path works correctly for the
    canonical KBS HTML format.
    """
    for months, course in [
        (8, "Graduate Certificate in Accounting"),
        (8, "Graduate Certificate in Business Administration"),
        (8, "Graduate Certificate in Business Analytics"),
    ]:
        html = (
            f"<h1>{course}</h1>"
            f"<dl><dt>Duration</dt><dd>{months} months / 4 subjects / 2 trimesters</dd></dl>"
            "<p>Candidates must complete their studies within 8 years of enrolment.</p>"
        )
        out = _run(duration.extract(html, f"https://www.kbs.edu.au/courses/{course.lower().replace(' ', '-')}/"))
        assert out, f"extractor must find a duration for {course}"
        n = out[0].normalized
        assert n["duration"] == float(months) and n["duration_term"] == "Month", (
            f"{course}: expected ({months}, Month); got {n!r}"
        )
        assert out[0].method == "duration.structural", (
            f"{course}: structural pre-pass must fire on <dt>Duration</dt>; "
            f"got method={out[0].method!r}"
        )


def test_bug_a2_slash_sentence_no_label_returns_regex_method():
    """Bug A.2 regression: when no DOM Duration label exists, the slash
    sentence-level regex fires and returns method='regex'.

    'regex' must be in _STRUCTURAL_COURSE_PAGE_EXACT in single_course.py so
    the atomic duration-term guard (Bug A.2 fix) protects duration_term from
    being overwritten by Gemini primary when it runs afterwards.

    This test verifies the extractor correctly extracts 8 Month via the
    tournament path and that it returns method='regex', which is what
    single_course.py uses to decide whether to lock the duration_term.
    """
    # compact() collapses <p> newlines to spaces, so sentence-boundary
    # detection needs a period to split the slash sentence from the rest.
    # Real pages have the slash text in a cell that starts a period-terminated
    # "sentence" after compaction (e.g. a table cell whose value ends with ".").
    # We simulate that here: the slash sentence MUST end with "." so the
    # sentence splitter (r"(?<=[.!?])\s+|\n") isolates it as its own segment
    # that starts with "8 months" — the position _SLASH_PROGRAM_STRUCTURE_RE
    # requires (re.match anchors at the start of the string).
    html = (
        # No <dt>Duration</dt> label — structural pre-pass will NOT fire.
        "<p>8 months / 4 subjects / 2 trimesters. "
        "Candidates must complete their studies within 8 years of enrolment.</p>"
    )
    out = _run(duration.extract(html, "https://www.kbs.edu.au/courses/grad-cert-accounting/"))
    assert out, "extractor must find a duration from the slash sentence"
    n = out[0].normalized
    assert n["duration"] == 8.0, (
        f"Expected duration=8.0 from '8 months / 4 subjects', got {n!r}"
    )
    assert n["duration_term"] == "Month", (
        f"Expected duration_term=Month, got {n!r}. "
        f"The slash regex must lock BOTH value AND unit so the downstream "
        f"atomic guard in single_course.py can protect duration_term from "
        f"Gemini overwrite."
    )
    assert out[0].method == "regex", (
        f"Expected method='regex' for tournament path; got {out[0].method!r}. "
        f"'regex' must be in _STRUCTURAL_COURSE_PAGE_EXACT so the Bug A.2 "
        f"atomic guard fires in single_course.py."
    )
