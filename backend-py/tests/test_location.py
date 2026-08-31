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


def test_table_location_header_does_not_become_a_campus():
    """Column labels are metadata, not the location value."""
    html = """
    <table>
      <thead>
        <tr><th>Location</th><th>Domestic</th><th>International</th></tr>
      </thead>
      <tbody>
        <tr><td>Online</td><td>Term 1</td><td>Term 1</td></tr>
      </tbody>
    </table>
    """
    out = _run(location.extract(html, "https://e/x"))
    assert out == []


def test_teaching_period_header_is_not_a_course_location():
    html = """
    <table>
      <thead><tr><th>Location</th><th>Teaching period</th></tr></thead>
      <tbody><tr><td>Online</td><td>Term 1</td></tr></tbody>
    </table>
    """
    out = _run(location.extract(html, "https://e/x"))
    assert out == []
    assert location._classify_location_value("Teaching period") is None


def test_teaching_period_pivot_preserves_a_physical_campus():
    """The header fix must not erase real campus rows in availability tables."""
    html = """
    <table>
      <thead><tr><th>Location</th><th>Teaching period</th></tr></thead>
      <tbody>
        <tr><td>Gold Coast</td><td>Term 1</td></tr>
        <tr><td>Online</td><td>Term 1</td></tr>
      </tbody>
    </table>
    """
    out = _run(location.extract(html, "https://e/x"))
    assert out
    assert out[0].value == "Gold Coast"
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
    assert out[0].value == "Adelaide, Brisbane, Gold Coast, Melbourne, Perth, Sydney", (
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
        assert out[0].value == "Adelaide, Melbourne, Sydney, Perth", (
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
        assert out[0].value == "Sydney, Melbourne"
    finally:
        set_uni_config(_BARE_CFG)


# ── LocationCleaningConfig.reject_values + allowed_values (UWL / law.ac.uk) ──
#
# These tests cover the new YAML-configurable location filtering fields:
#   extraction.text_cleaning.location.reject_values   → recipe location_reject_values
#   extraction.text_cleaning.location.allowed_values  → recipe location_allowed_values
#
# The filtering itself lives in recipe_rules._apply_location_rules; the YAML
# bridge in the orchestrator wires the YAML values into the recipe dict before
# apply_recipe_rules() is called.  These tests verify schema, recipe-rules
# behaviour, and round-trip YAML loading.


def test_location_cleaning_config_reject_values_field_exists():
    """LocationCleaningConfig must accept reject_values after schema extension."""
    from app.services.scraper.config.schema import LocationCleaningConfig
    cfg = LocationCleaningConfig(
        reject_values=["Fees", "Apply"],
        allowed_values=["London", "Online"],
    )
    assert cfg.reject_values == ["Fees", "Apply"]
    assert cfg.allowed_values == ["London", "Online"]


def test_recipe_rules_location_reject_values_clears_fees_exact():
    """'Fees' in location_reject_values must clear course_location='Fees' (UWL bug)."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "Fees", "course_name": "MSc Data Science and AI"}
    recipe = {"location_reject_values": ["Fees", "Course fees", "Funding"]}
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] is None, (
        "location_reject_values must clear 'Fees' location; got %r" % out["course_location"]
    )


def test_recipe_rules_location_reject_values_case_insensitive():
    """reject_values match is case-insensitive — 'fees' rejects 'FEES AND FUNDING'."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "FEES AND FUNDING"}
    recipe = {"location_reject_values": ["fees"]}
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] is None, (
        "Case-insensitive reject must clear 'FEES AND FUNDING'; got %r" % out["course_location"]
    )


def test_recipe_rules_location_reject_values_preserves_valid_campus():
    """reject_values must NOT clear a valid campus like 'Newcastle'."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "Newcastle"}
    recipe = {
        "location_reject_values": ["fees", "apply", "entry requirements"],
        "location_allowed_values": ["Newcastle", "Birmingham", "London Bloomsbury"],
    }
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] == "Newcastle", (
        "Valid campus 'Newcastle' must survive reject+allowlist; got %r" % out["course_location"]
    )


def test_recipe_rules_location_allowed_values_clears_section_heading():
    """allowed_values clears 'Study Mode' — a section heading, not a campus."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "Study Mode"}
    recipe = {
        "location_reject_values": ["fees", "apply"],
        "location_allowed_values": ["Birmingham", "Leeds", "Online"],
    }
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] is None, (
        "'Study Mode' not in campus allowlist; must be cleared. Got %r" % out["course_location"]
    )


def test_recipe_rules_location_allowed_values_normalises_compound_location():
    """allowed_values filter preserves both campus tokens from a compound location."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "London Bloomsbury, London Moorgate"}
    recipe = {
        "location_allowed_values": [
            "London Bloomsbury", "London Moorgate", "Birmingham",
        ],
    }
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] is not None
    assert "London Bloomsbury" in out["course_location"]
    assert "London Moorgate" in out["course_location"]


def test_recipe_rules_location_reject_takes_priority_over_allowed():
    """reject_values fires before allowed_values — reject wins if both would match."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "Fees"}
    recipe = {
        "location_reject_values": ["fees"],
        "location_allowed_values": ["Fees"],
    }
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] is None, (
        "reject_values must fire before allowed_values; got %r" % out["course_location"]
    )


def test_law_yaml_search_api_config_round_trips():
    """The suffixed University of Law config loads through the normal loader."""
    import re
    from app.services.scraper.config.loader import load_uni_config
    cfg = load_uni_config(
        slug="law",
        name="University of Law",
        scrape_url="https://www.law.ac.uk/study/",
        university_id=1902,
    )
    api = cfg.discovery.generic_search_api
    assert api is not None
    assert api.root_path == "Courses"
    assert "Url" in api.url_fields
    assert api.page_size == 20
    assert api.max_pages == 10
    assert api.page_number_param == "page"
    patterns = cfg.discovery.allow_url_patterns
    campus_url = (
        "https://www.law.ac.uk/study/postgraduate/business/"
        "msc-project-management/"
    )
    online_url = campus_url + "online/"
    assert any(re.search(pattern, campus_url) for pattern in patterns)
    assert not any(re.search(pattern, online_url) for pattern in patterns)


def test_law_yaml_overrides_stale_online_only_auto_config():
    """A probe learned from an online page must not hide AU18 campus courses."""
    import re
    from app.services.scraper.config.loader import load_uni_config
    cfg = load_uni_config(
        slug="law",
        name="University of Law",
        scrape_url="https://www.law.ac.uk/study/",
        university_id=1902,
        db_scrape_config={
            "auto_config": {
                "discovery": {
                    "allow_url_patterns": [
                        "/study/postgraduate/.*/.*/online/"
                    ]
                }
            }
        },
    )
    patterns = cfg.discovery.allow_url_patterns
    campus_url = (
        "https://www.law.ac.uk/study/postgraduate/business/"
        "msc-project-management/"
    )
    assert any(re.search(pattern, campus_url) for pattern in patterns)
    assert not any(re.search(pattern, campus_url + "online/") for pattern in patterns)
    assert cfg.extraction.filters.online_only.enabled is True


def test_law_yaml_english_defaults_configured():
    """University of Law uses explicit score defaults in its active config."""
    from app.services.scraper.config.loader import load_uni_config
    cfg = load_uni_config(
        slug="law",
        name="University of Law",
        scrape_url="https://www.law.ac.uk/study/",
        university_id=1902,
    )
    english = cfg.extraction.english
    assert english.default_ielts == 6.0
    assert english.default_pte == 60
    assert english.default_toefl == 80
    assert english.apply_defaults_before_remote_enrichment is True
    assert english.skip_vision_when_core_found is True
    assert cfg.extraction.skip_browser_rescue is True
    assert cfg.extraction.skip_per_course_browser is True
    assert cfg.extraction.recovery_sweep_max_items == 0
    assert cfg.extraction.recovery_sweep_time_budget_seconds == 0
    assert cfg.discovery.always_sitemap_supplement is False


def test_leeds_yaml_uses_static_residential_proxy():
    """Leeds' SSR catalogue must bypass the direct/browser 403 paths."""
    from app.services.scraper.config.loader import load_uni_config
    cfg = load_uni_config(
        slug="leeds",
        name="University of Leeds",
        scrape_url="https://www.leeds.ac.uk/",
        university_id=2172,
    )
    assert cfg.extraction.scrape_do_static is True
    assert cfg.extraction.scrape_do_render is False
    assert cfg.extraction.skip_browser_rescue is True
    assert cfg.extraction.skip_per_course_browser is True


def test_swinburne_yaml_disables_dead_browser_vision_and_sweep_paths():
    """Swinburne's fully SSR pages must not spend 90s on remote rescue work."""
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="swinburne",
        name="Swinburne University of Technology",
        scrape_url="https://www.swinburne.edu.au/",
        university_id=1747,
    )

    assert cfg.extraction.skip_browser_rescue is True
    assert cfg.extraction.skip_per_course_browser is True
    assert cfg.extraction.skip_remote_ai_enrichment is True
    assert cfg.extraction.english.trust_vision_ocr is False
    assert cfg.extraction.english.skip_vision_when_core_found is True
    assert cfg.extraction.recovery_sweep_max_items == 0
    assert cfg.extraction.recovery_sweep_time_budget_seconds == 0


def test_empty_reject_and_allowed_values_are_no_ops():
    """Empty reject_values and allowed_values in recipe must leave location unchanged."""
    from app.services.scraper.recipe_rules import apply_recipe_rules
    payload = {"course_location": "Melbourne"}
    recipe = {"location_reject_values": [], "location_allowed_values": []}
    out = apply_recipe_rules(payload, recipe)
    assert out["course_location"] == "Melbourne", (
        "Empty lists must not alter location; got %r" % out["course_location"]
    )
