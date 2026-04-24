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


def test_select_dropdown_noise_does_not_poison_keyword_fallback():
    """B20 root cause: VIT pages embed an enquiry-form `<select>` whose
    options literally read 'Online Studies / On Campus / Blended'. After
    naive tag stripping the word 'Blended' shows up in the page text,
    and the keyword fallback (which scans for the literal word) claims
    the course as Blended even when the actual course is on-campus.
    With noise-block stripping the dropdown text is removed before
    classification, so a page whose only mode signal is 'On Campus'
    must classify as On Campus."""
    html = (
        "<h1>Bachelor of Business</h1>"
        "<p>Delivered fully on campus at our Melbourne CBD site.</p>"
        # The dropdown — pure form noise, not a course attribute.
        '<form><select name="study_mode">'
        "<option>Online Studies</option>"
        "<option>On Campus</option>"
        "<option>Blended</option>"
        "</select></form>"
    )
    assert _classify(html) == "On Campus"


def test_nav_and_footer_noise_stripped_before_classification():
    """Site-wide nav and footer often list every delivery option as
    menu items (`Online courses` / `On-campus courses` / `Blended
    learning`). Those must not poison the per-page mode classifier."""
    html = (
        "<nav><a>Online Courses</a><a>Blended Learning</a></nav>"
        "<main><p>This Master of Public Health is delivered on campus.</p></main>"
        "<footer><a>Online study options</a></footer>"
    )
    assert _classify(html) == "On Campus"


def test_learning_mode_label_recognised():
    """B20: VIT and similar pages label the field 'Learning Mode' /
    'Learning Method' / 'Delivery Method' rather than the more common
    'Mode of Study'. Without these synonyms the label-first path falls
    through to the bare-keyword fallback, which is fragile against
    enquiry-form noise (the literal string 'Blended' on the dropdown).
    Make sure all three new label synonyms are first-class."""
    for label in ("Learning Mode", "Learning Method", "Mode of Learning", "Delivery Method"):
        assert _classify(f"<dl><dt>{label}:</dt><dd>On Campus</dd></dl>") == "On Campus"


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


# ──────────────────────────────────────────────────────────────────
# PR-1.5 prod-regression coverage: bare blended/hybrid keyword
# Job_01cec454ebd2 (VIT) staged 24 courses — 100% defaulted to
# "Blended" because the keyword pattern matched marketing copy
# ("blended learning environment", "blended teaching approach")
# anywhere on the page. The patterns now require an explicit
# delivery noun (delivery|mode|format|program(me)?) right after
# the keyword. These tests lock the contract in.
# ──────────────────────────────────────────────────────────────────


def test_bare_blended_marketing_copy_does_not_default_to_blended():
    from app.services.scraper.extractors.study_mode import classify_study_mode
    # Sentences that mention 'blended'/'hybrid'/'mixed' without a
    # delivery-method noun must NOT classify as Blended — they're
    # marketing fluff, not delivery descriptions.
    for txt in (
        "We provide a blended learning environment for all students.",
        "Our blended teaching approach combines theory and practice.",
        "A blended team of academics and industry experts.",
        "Our hybrid teaching style supports diverse learners.",
        "Mixed cohort sizes keep classes engaging.",
        "VIT offers a blended community of local and international students.",
    ):
        out, _ = classify_study_mode(txt)
        assert out is None, (
            f"PR-1.5 regression: marketing copy should NOT classify as Blended.\n"
            f"  text: {txt!r}\n  got:  {out!r}"
        )


def test_blended_with_delivery_noun_still_classifies_as_blended():
    """Sanity check that the tightened pattern still catches real
    blended-delivery signals — only the bare keyword case is rejected."""
    from app.services.scraper.extractors.study_mode import classify_study_mode
    for txt in (
        "Mixed mode delivery is available.",
        "hybrid program with online and on-campus components",
        "Blended delivery allows flexible study.",
        "Hybrid mode supports both in-person and remote study.",
        "Blended format suits working professionals.",
    ):
        out, _ = classify_study_mode(txt)
        assert out == "Blended", (
            f"tightened pattern broke a real blended signal.\n"
            f"  text: {txt!r}\n  got:  {out!r}"
        )
