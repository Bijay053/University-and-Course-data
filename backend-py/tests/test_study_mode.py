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


# --- Bug G regression tests --------------------------------------------------


def test_onshore_recognised_as_on_campus():
    # AU CRICOS PDFs say "Onshore - International students must be in
    # Australia" — Node maps this to On Campus and so must we.
    assert _classify("Onshore — International students attend on campus") == "On Campus"
    assert _classify("Required to attend on campus") == "On Campus"


def test_percent_online_with_on_campus_is_blended():
    # "33% online" alone is not enough — but combined with an on-campus
    # signal the course is officially blended.
    txt = "Onshore — required to attend on campus. Up to 33% online study permitted."
    assert _classify(txt) == "Blended"


def test_percent_online_alone_stays_online():
    # No on-campus signal → not Blended; the bare 'online' word triggers
    # the Online fallback.
    assert _classify("Up to 33% online delivery") == "Online"


# --- ASA prod-bug regression -------------------------------------------------


def test_label_priority_beats_marketing_copy():
    """Bug: 7 of 9 ASA prod rows staged as Online because the bare-keyword
    fallback fired on marketing copy ("explore our online courses") before
    checking the actual `Mode of study: On Campus` cell. The label-first
    detector must take precedence."""
    asa_like = (
        '<nav><a href="/online-courses">Explore our online courses</a></nav>'
        "<h1>Bachelor of Business</h1>"
        "<dl><dt>Mode of study:</dt><dd>On Campus</dd></dl>"
        "<p>Study online and on-campus is also available for select intakes.</p>"
    )
    assert _classify(asa_like) == "On Campus"


def test_label_value_token_capture_stops_at_unrelated_words():
    """The label-value capture must stop at the first non-mode token so a
    flattened HTML run like `On Campus Study online and on campus is also
    available...` doesn't accidentally classify as Blended via the
    `online and on campus` substring buried in unrelated copy."""
    flattened = (
        "Mode of study: On Campus Study online and on campus is also available."
    )
    assert _classify(flattened) == "On Campus"


def test_explicit_blended_label_value():
    """A label whose value names both modes ("On Campus and Online") is the
    canonical Blended signal."""
    assert _classify("<dl><dt>Study mode:</dt><dd>On Campus and Online</dd></dl>") == "Blended"


def test_online_label_beats_campus_mention_elsewhere():
    """Inverse of the ASA bug: a page that says `Mode of study: Online` but
    mentions a campus visit elsewhere must remain Online."""
    page = (
        "<dl><dt>Mode of study:</dt><dd>Online</dd></dl>"
        "<p>Visit our campus on open day to learn more.</p>"
    )
    assert _classify(page) == "Online"


def test_alternate_label_synonyms():
    """Operator-friendly label synonyms must all work — Node accepted these."""
    for label in ("Mode of study", "Study mode", "Delivery mode", "Mode of attendance"):
        assert _classify(f"<dl><dt>{label}:</dt><dd>Online</dd></dl>") == "Online"


def test_label_requires_delimiter_to_avoid_prose_false_positive():
    """Code-review regression: without a required colon/dash delimiter,
    `_LABEL_RE` matched prose like `learn about mode of study online`
    and treated it as authoritative — exactly the kind of footer copy
    that triggered the original ASA bug. Make sure the label-first path
    no longer over-fires on bare prose."""
    prose_with_phrase = "Learn about mode of study online and apply today."
    # Should NOT classify as Online via the label path; the bare-keyword
    # fallback may still fire on `online` and that's fine — the point is
    # that the label-first path doesn't claim authority over noisy prose.
    # We assert at least that the result isn't the *wrong* On Campus and
    # ideally Online (via fallback) — i.e. the label-first short-circuit
    # isn't triggered with a phantom value.
    from app.services.scraper.extractors.study_mode import _LABEL_RE
    assert _LABEL_RE.search(prose_with_phrase) is None
