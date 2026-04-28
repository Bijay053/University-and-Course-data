"""DOM-aware label-detection regression tests for the location extractor.

The location cascade already handled `<dt>/<dd>` and `<th>/<td>` via
`_from_dl` / `_from_tables`. Task #24 added the structural
`<strong>Label</strong>` parent-sibling case (the ASA-style adjacent
divs idiom that `_from_headings` misses) — these tests lock the
expanded coverage in.
"""
from __future__ import annotations

import asyncio

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
    assert out[0].value == "Sydney"
    assert out[0].method == "location.strong"


def test_dt_dd_location_classifies_via_existing_dl_path():
    """Definition-list shape — already covered by `_from_dl`, locked
    in here so a future refactor of the cascade can't regress it."""
    html = "<dl><dt>Location</dt><dd>Melbourne, Brisbane</dd></dl>"
    out = _run(location.extract(html, "https://e/x"))
    assert out and out[0].value == "Melbourne, Brisbane"
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
    assert out and out[0].value == "Adelaide, Perth"
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
    assert out and out[0].value == "Sydney"
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
    assert out and out[0].value == "Brisbane"
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
        assert out[0].value == "Sydney, Melbourne, Brisbane", (
            f"Campus codes must be expanded to city names; got {out[0].value!r}"
        )
