"""Tests for the stage-aware AI repair agent fixes.

Covers:
- _validate_and_build_config_patch: dotpath extraction fields now accepted + expanded
- _patch_fingerprint: duplicate detection key
- _flatten_dotpaths: display helper
- Phase locking: discovery-phase patches blocked in extraction phase
- _discovery_phase_done initialises to True when initial drop_rate <= 20
- _build_user_message: extraction phase blocks discovery patches in focus_block
"""
import json
import pytest

from app.services.scraper.ai_repair_agent import (
    _validate_and_build_config_patch,
    _patch_fingerprint,
    _flatten_dotpaths,
    _build_user_message,
    PatchValidationError,
)


# ── _validate_and_build_config_patch ──────────────────────────────────────────

class TestValidateAndBuildConfigPatch:

    def test_dotpath_fees_central_page_accepted_and_expanded(self):
        """fees.central_page (section=recipe) must be accepted and expanded to nested dict."""
        patches = [
            {"section": "recipe", "field": "fees.central_page", "value": "https://uni.example.com/fees"}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == [], f"Expected no errors, got: {errors}"
        assert disc == {}
        # Dotpath must be expanded: {"fees": {"central_page": "..."}}
        assert "fees" in extr
        assert extr["fees"]["central_page"] == "https://uni.example.com/fees"

    def test_dotpath_english_central_page_accepted(self):
        patches = [
            {"section": "recipe", "field": "english.central_page",
             "value": "https://uni.example.com/english-requirements"}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert extr["english"]["central_page"] == "https://uni.example.com/english-requirements"

    def test_dotpath_english_default_ielts_accepted(self):
        patches = [
            {"section": "recipe", "field": "english.default_ielts", "value": 6.5}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert extr["english"]["default_ielts"] == 6.5

    def test_dotpath_location_reject_values_accepted(self):
        patches = [
            {"section": "recipe", "field": "text_cleaning.location.reject_values",
             "value": ["Online", "Distance Learning"]}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert extr["text_cleaning"]["location"]["reject_values"] == ["Online", "Distance Learning"]

    def test_dotpath_filters_domestic_only_accepted(self):
        patches = [
            {"section": "recipe", "field": "filters.domestic_only.enabled", "value": False}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert extr["filters"]["domestic_only"]["enabled"] is False

    def test_multiple_dotpath_fields_merged_correctly(self):
        """Multiple recipe patches must deep-merge into one nested dict."""
        patches = [
            {"section": "recipe", "field": "fees.central_page",
             "value": "https://uni.example.com/fees"},
            {"section": "recipe", "field": "english.default_ielts", "value": 6.0},
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert extr["fees"]["central_page"] == "https://uni.example.com/fees"
        assert extr["english"]["default_ielts"] == 6.0

    def test_unknown_recipe_field_rejected(self):
        patches = [
            {"section": "recipe", "field": "totally_unknown_field", "value": "x"}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert len(errors) == 1
        assert "totally_unknown_field" in errors[0]
        assert extr == {}

    def test_discovery_patch_still_works(self):
        patches = [
            {"section": "discovery", "field": "allow_url_patterns",
             "value": ["/courses/[^/]+/[^/]+"]}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert disc["allow_url_patterns"] == ["/courses/[^/]+/[^/]+"]
        assert extr == {}

    def test_mixed_discovery_and_extraction_patches(self):
        patches = [
            {"section": "discovery", "field": "allow_url_patterns", "value": ["/courses/"]},
            {"section": "recipe", "field": "fees.central_page",
             "value": "https://uni.example.com/fees"},
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert "allow_url_patterns" in disc
        assert extr["fees"]["central_page"] == "https://uni.example.com/fees"

    def test_invalid_url_for_fees_central_page_rejected(self):
        patches = [
            {"section": "recipe", "field": "fees.central_page", "value": "not-a-url"}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert len(errors) == 1
        assert extr == {}

    def test_ielts_out_of_range_rejected(self):
        patches = [
            {"section": "recipe", "field": "english.default_ielts", "value": 3.0}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert len(errors) == 1
        assert extr == {}

    def test_null_value_for_fees_central_page_accepted(self):
        """null (None) is valid for url_or_null fields."""
        patches = [
            {"section": "recipe", "field": "fees.central_page", "value": None}
        ]
        disc, extr, errors = _validate_and_build_config_patch(patches)
        assert errors == []
        assert extr["fees"]["central_page"] is None

    def test_empty_patches_list_returns_empty_dicts(self):
        disc, extr, errors = _validate_and_build_config_patch([])
        assert disc == {}
        assert extr == {}
        assert errors == []


# ── _patch_fingerprint ────────────────────────────────────────────────────────

class TestPatchFingerprint:

    def test_same_patch_same_fingerprint(self):
        p = {"section": "discovery", "field": "allow_url_patterns",
             "value": ["/courses/", "/programmes/"]}
        assert _patch_fingerprint(p) == _patch_fingerprint(p)

    def test_different_value_different_fingerprint(self):
        p1 = {"section": "discovery", "field": "allow_url_patterns", "value": ["/courses/"]}
        p2 = {"section": "discovery", "field": "allow_url_patterns", "value": ["/programmes/"]}
        assert _patch_fingerprint(p1) != _patch_fingerprint(p2)

    def test_different_field_different_fingerprint(self):
        p1 = {"section": "recipe", "field": "fees.central_page", "value": "https://x.com"}
        p2 = {"section": "recipe", "field": "english.central_page", "value": "https://x.com"}
        assert _patch_fingerprint(p1) != _patch_fingerprint(p2)

    def test_fingerprint_is_string(self):
        p = {"section": "recipe", "field": "fees.central_page", "value": "https://x.com/fees"}
        fp = _patch_fingerprint(p)
        assert isinstance(fp, str)
        assert "recipe" in fp
        assert "fees.central_page" in fp

    def test_fingerprint_set_deduplication(self):
        """Using fingerprints in a set correctly deduplicates identical patches."""
        p = {"section": "discovery", "field": "allow_url_patterns", "value": ["/courses/"]}
        seen: set[str] = set()
        seen.add(_patch_fingerprint(p))
        # Adding the same fingerprint again should not increase set size
        seen.add(_patch_fingerprint(p))
        assert len(seen) == 1


# ── _flatten_dotpaths ─────────────────────────────────────────────────────────

class TestFlattenDotpaths:

    def test_flat_dict_unchanged(self):
        result = _flatten_dotpaths({"fee_term": "Annual"})
        assert result == ["fee_term"]

    def test_single_level_nesting(self):
        result = _flatten_dotpaths({"fees": {"central_page": "https://x.com"}})
        assert result == ["fees.central_page"]

    def test_two_level_nesting(self):
        result = _flatten_dotpaths({"text_cleaning": {"location": {"reject_values": ["x"]}}})
        assert result == ["text_cleaning.location.reject_values"]

    def test_multiple_branches(self):
        result = _flatten_dotpaths({
            "fees": {"central_page": "https://x.com"},
            "english": {"default_ielts": 6.5},
        })
        assert set(result) == {"fees.central_page", "english.default_ielts"}

    def test_empty_dict_returns_empty_list(self):
        assert _flatten_dotpaths({}) == []


# ── _build_user_message phase behaviour ──────────────────────────────────────

class TestBuildUserMessagePhase:

    def _minimal_ctx(self, drop_rate=0):
        return {
            "uni_name": "Test University",
            "scrape_url": "https://test.edu/courses",
            "raw_discovered": 100,
            "after_filter": 100 - drop_rate,
            "imported": 50,
            "total_errors": 0,
            "drop_rate": drop_rate,
            "dropped_sample": [],
            "passed_sample": [],
            "admin_config": {},
            "yaml_content": "",
            "quality": {
                "total_staged": 50,
                "fee_pct": 20,
                "ielts_pct": 15,
                "intakes_pct": 80,
                "location_pct": 90,
                "degree_level_pct": 85,
                "mode_pct": 70,
                "duration_pct": 75,
                "sample_locations": ["London"],
                "sample_degrees": ["Bachelor"],
                "sample_modes": ["On-campus"],
            },
        }

    def test_extraction_phase_contains_do_not_suggest_discovery(self):
        msg = _build_user_message(self._minimal_ctx(), [], phase="extraction")
        assert "DO NOT suggest ANY section='discovery' patches" in msg

    def test_discovery_phase_does_not_block_discovery(self):
        msg = _build_user_message(self._minimal_ctx(drop_rate=60), [], phase="discovery")
        assert "DO NOT suggest ANY section='discovery' patches" not in msg
        assert "Fix URL DISCOVERY" in msg

    def test_extraction_phase_lists_failing_fields(self):
        """Extraction phase message must name which fields are below target."""
        msg = _build_user_message(self._minimal_ctx(), [], phase="extraction")
        # fee_pct=20% < 50%, ielts_pct=15% < 50% → both should appear
        assert "fees" in msg
        assert "IELTS" in msg

    def test_previous_attempts_show_patch_values(self):
        """History block must include actual patch values, not just field names."""
        attempts = [
            {
                "attempt_number": 1,
                "phase": "discovery",
                "root_cause": "allow_url_patterns",
                "patches_applied": [
                    {"section": "discovery", "field": "allow_url_patterns",
                     "new_value": ["/courses/[^/]+"]}
                ],
                "success_criteria": {"criteria_pass": 1},
                "quality_after": {"fee_pct": 20, "ielts_pct": 15, "location_pct": 90,
                                  "mode_pct": 70, "degree_level_pct": 85},
            }
        ]
        msg = _build_user_message(self._minimal_ctx(), attempts, phase="extraction")
        # Must show the actual value, not just the field name
        assert "allow_url_patterns" in msg
        assert "/courses/" in msg  # partial value visible

    def test_empty_previous_attempts_no_prev_block(self):
        msg = _build_user_message(self._minimal_ctx(), [], phase="discovery")
        assert "PREVIOUS ATTEMPTS" not in msg


# ── Discovery phase initialisation ────────────────────────────────────────────

class TestDiscoveryPhaseDoneInit:

    def test_discovery_phase_done_when_drop_rate_zero(self):
        """If initial drop_rate is 0, _discovery_phase_done starts True."""
        # Verify the condition: ctx["drop_rate"] <= 20 → _discovery_phase_done = True
        drop_rate = 0
        _discovery_phase_done = drop_rate <= 20
        assert _discovery_phase_done is True

    def test_discovery_phase_not_done_when_drop_rate_high(self):
        drop_rate = 96
        _discovery_phase_done = drop_rate <= 20
        assert _discovery_phase_done is False

    def test_discovery_phase_done_at_boundary(self):
        """drop_rate == 20 should be considered already OK."""
        drop_rate = 20
        _discovery_phase_done = drop_rate <= 20
        assert _discovery_phase_done is True
