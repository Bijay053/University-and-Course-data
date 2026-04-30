"""DOM-aware label-detection regression tests for the location extractor.

The location cascade already handled `<dt>/<dd>` and `<th>/<td>` via
`_from_dl` / `_from_tables`. Task #24 added the structural
`<strong>Label</strong>` parent-sibling case (the ASA-style adjacent
divs idiom that `_from_headings` misses) — these tests lock the
expanded coverage in.

Option C (ACAP location strip_patterns) tests are at the bottom of
this file: they verify that per-uni strip_patterns from the contextvar
are applied before sanitisation so ACAP's footnote cruft is cleaned.
"""
from __future__ import annotations

import asyncio

from app.services.scraper.config import set_uni_config
from app.services.scraper.config.context import get_uni_config
from app.services.scraper.config.schema import UniConfig
from app.services.scraper.extractors import location


def _run(coro):
    return asyncio.run(coro)


def test_strong_location_sibling_div_classifies_via_structural_pass():
    """Exact ASA layout: `<div><strong>Location</strong></div><div>
    Sydney</div>`. The strong tag's own `find_next_sibling()` is the
    `<br/>` and then nothing; only walking forward through document
    order (across the parent's next sibling) recovers the value."""
    html = (
        '<div class="course-header-text"><strong>Location</strong><br/></div>'
        '<div class="course-header-text">Sydney</div>'
    )
    out = _run(location.extract(html, "https://e/x"))
    assert out, (
        "Structural <strong>Location</strong> sibling-div pre-pass must "
        "fire; pre-fix this cascade left the value on the floor."
    )
    assert out[0].value == "Sydney, Australia"
    assert out[0].method == "location.strong"


def test_dt_dd_location_classifies_via_existing_dl_path():
    """Definition-list shape — already covered by `_from_dl`, locked
    in here so a future refactor of the cascade can't regress it."""
    html = "<dl><dt>Location</dt><dd>Melbourne, Brisbane</dd></dl>"
    out = _run(location.extract(html, "https://e/x"))
    assert out and out[0].value == "Melbourne, Brisbane, Australia"
    assert out[0].method == "location.dl"


def test_th_td_location_classifies_via_existing_table_path():
    """Table key/value shape — already covered by `_from_tables`,
    locked in here so a future refactor can't regress it."""
    html = (
        "<table>"
        "<tr><th>Campus</th><td>Adelaide, Perth</td></tr>"
        "<tr><th>Duration</th><td>3 years</td></tr>"
        "</table>"
    )
    out = _run(location.extract(html, "https://e/x"))
    assert out and out[0].value == "Adelaide, Perth, Australia"
    assert out[0].method == "location.table"


def test_strong_location_strips_online_virtual_from_value():
    """Same `_sanitise_for_display` rule as the dl/table paths: an
    `Online` token must be stripped so the staged value is the real
    physical campus only."""
    html = (
        '<div><strong>Location</strong></div>'
        '<div>Sydney, Online</div>'
    )
    out = _run(location.extract(html, "https://e/x"))
    assert out and out[0].value == "Sydney, Australia"
    assert out[0].method == "location.strong"


def test_strong_location_does_not_misfire_on_unrelated_strong_tags():
    """`<strong>Apply Now</strong>` is not a location label; the
    structural pre-pass must skip it. `<strong>Course Overview</strong>`
    likewise. Only the recognised label vocabulary triggers the walk."""
    html = (
        '<a><strong>Apply Now</strong></a>'
        '<div><strong>Course Overview</strong></div>'
        '<div>This program covers a wide range of topics.</div>'
        '<dl><dt>Location</dt><dd>Brisbane</dd></dl>'
    )
    out = _run(location.extract(html, "https://e/x"))
    assert out and out[0].value == "Brisbane, Australia"
    # Should fall through to the dl path (NOT the strong walker).
    assert out[0].method == "location.dl"


# ---------------------------------------------------------------------------
# Campus code expansion tests (APIC College fix)
# ---------------------------------------------------------------------------

class TestCampusCodeExpansion:
    """_expand_campus_codes must convert 3-letter airport-style campus
    codes to full city names and handle various separator styles."""

    def _expand(self, text: str) -> str:
        return location._expand_campus_codes(text)

    def test_pipe_separated_three_codes(self):
        assert self._expand("SYD | MEL | BNE") == "Sydney, Melbourne, Brisbane"

    def test_slash_separated_two_codes(self):
        assert self._expand("PER / ADL") == "Perth, Adelaide"

    def test_comma_separated_codes(self):
        assert self._expand("CBR, SYD") == "Canberra, Sydney"

    def test_gold_coast_ool(self):
        assert self._expand("OOL | SYD") == "Gold Coast, Sydney"

    def test_gold_coast_gc_code(self):
        assert self._expand("GC") == "Gold Coast"

    def test_single_known_code(self):
        assert self._expand("MEL") == "Melbourne"

    def test_already_city_names_unchanged(self):
        result = self._expand("Sydney, Melbourne")
        assert result == "Sydney, Melbourne"

    def test_mixed_codes_and_cities_all_known(self):
        result = self._expand("SYD | Melbourne")
        assert "Sydney" in result
        assert "Melbourne" in result

    def test_unknown_tokens_left_unchanged(self):
        result = self._expand("SYD | UNKNOWN_CAMPUS")
        assert result == "SYD | UNKNOWN_CAMPUS"

    def test_deduplication_same_code_twice(self):
        result = self._expand("SYD | SYD | MEL")
        assert result == "Sydney, Melbourne"

    def test_normalise_pipeline_expands_codes(self):
        """_normalise() must invoke _expand_campus_codes so that raw
        code strings like 'SYD | MEL | BNE' are stored as city names."""
        result = location._normalise("SYD | MEL | BNE")
        assert result == "Sydney, Melbourne, Brisbane", (
            f"_normalise must expand campus codes; got {result!r}"
        )

    def test_end_to_end_dl_with_codes(self):
        """Full extraction pipeline: location field containing codes
        must come out as full city names."""
        html = "<dl><dt>Campus Location</dt><dd>SYD | MEL | BNE</dd></dl>"
        out = _run(location.extract(html, "https://apicollege.edu.au/courses/test/"))
        assert out, "Location must be extracted from dl"
        assert out[0].value == "Sydney, Melbourne, Brisbane, Australia", (
            f"Campus codes must be expanded to city names (with country suffix); got {out[0].value!r}"
        )


# ── KBS slash-separated location format ──────────────────────────────────────


def test_slash_separated_cities_normalised_to_comma():
    """KBS pages publish location as 'Adelaide / Brisbane / Melbourne / Sydney /'
    (slash separators, trailing slash).  _normalise() must convert this to
    'Adelaide, Brisbane, Melbourne, Sydney' before the cascade returns it."""
    html = (
        "<dl><dt>Location</dt>"
        "<dd>Adelaide / Brisbane / Gold Coast / Melbourne / Perth / Sydney /</dd></dl>"
    )
    out = _run(location.extract(html, "https://www.kbs.edu.au/courses/test/"))
    assert out, "Location must be extracted from slash-separated value"
    assert out[0].value == "Adelaide, Brisbane, Gold Coast, Melbourne, Perth, Sydney, Australia", (
        f"Slash-separated cities must be comma-normalised; got {out[0].value!r}"
    )


def test_slash_without_spaces_left_unchanged():
    """A bare 'City1/City2' (no spaces around slash) is NOT the KBS pattern
    and must not be mangled by the normaliser — the guard checks for ' / '."""
    html = "<dl><dt>Location</dt><dd>Sydney/Melbourne</dd></dl>"
    out = _run(location.extract(html, "https://e/x"))
    assert out, "Location must be extracted"
    assert "Sydney" in out[0].value, (
        f"Slash without surrounding spaces must not be converted; got {out[0].value!r}"
    )


# ── Option C: per-uni strip_patterns (ACAP footnote cruft) ───────────────────

_BARE_CFG = UniConfig.model_validate({
    "slug": "_test",
    "name": "_test uni",
    "base_url": "https://test.edu.au",
    "scrape_url": "https://test.edu.au/courses/",
})

_ACAP_CFG = UniConfig.model_validate({
    "slug": "acap",
    "name": "ACAP",
    "base_url": "https://www.acap.edu.au",
    "scrape_url": "https://www.acap.edu.au/courses/",
    "extraction": {
        "text_cleaning": {
            "location": {"strip_patterns": [r'\^\s*\^.*$']}
        }
    },
})


def test_strip_patterns_remove_acap_footnote_suffix():
    """ACAP footnote pattern: 'Perth^ ^Available in Perth from Trimester 3, 2026'.

    The '^' is a superscript footnote marker that the HTML extractor reads as
    literal text.  The strip_pattern r'\\^\\s*\\^.*$' must remove everything
    from the first '^ ^' sequence to end-of-string, leaving only 'Perth'.

    This covers courses with multiple campuses like
    'Adelaide, Melbourne, Sydney, Perth^ ^Available in Perth from Trimester 3, 2026'.
    """
    set_uni_config(_ACAP_CFG)
    try:
        html = (
            "<dl><dt>Location</dt>"
            "<dd>Adelaide, Melbourne, Sydney, Perth^ ^Available in Perth from Trimester 3, 2026</dd></dl>"
        )
        out = _run(location.extract(html, "https://www.acap.edu.au/test/"))
        assert out, "Location must be extracted"
        assert out[0].value == "Adelaide, Melbourne, Sydney, Perth, Australia", (
            f"strip_patterns must remove ACAP footnote suffix; got {out[0].value!r}"
        )
    finally:
        set_uni_config(_BARE_CFG)


def test_strip_patterns_not_applied_without_config():
    """When strip_patterns is empty (bare defaults), location values pass
    through unchanged — verifying that no other uni is affected."""
    set_uni_config(_BARE_CFG)  # bare defaults — strip_patterns = []
    try:
        html = (
            "<dl><dt>Location</dt>"
            "<dd>Sydney, Melbourne</dd></dl>"
        )
        out = _run(location.extract(html, "https://www.someuni.edu.au/test/"))
        assert out, "Location must still be extracted with bare config"
        assert out[0].value == "Sydney, Melbourne, Australia"
    finally:
        set_uni_config(_BARE_CFG)
