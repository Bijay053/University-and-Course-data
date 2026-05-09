"""Tests for bond_static_extract.py and Bond-specific scraper behaviour.

Covers:
  1. is_bond_program_url() — host + path detection
  2. apply_bond_extraction() — pre-seed output for /program/ pages
  3. discovery.py — Bond post-filter keeps only /program/ URLs
  4. sibling_cache.py — min_quorum prevents single-source backfill
"""
from __future__ import annotations

import pytest

from app.services.scraper.bond_static_extract import (
    apply_bond_extraction,
    is_bond_program_url,
)


# ─────────────────────────────────────────────────────────────────────────────
# 1. is_bond_program_url
# ─────────────────────────────────────────────────────────────────────────────

class TestIsBondProgramUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://bond.edu.au/program/bachelor-of-laws",
            "https://www.bond.edu.au/program/master-of-business-administration",
            "https://bond.edu.au/program/master-of-finance-and-banking",
            "http://bond.edu.au/program/bachelor-of-commerce",
        ],
    )
    def test_true_for_program_paths(self, url: str) -> None:
        assert is_bond_program_url(url) is True

    @pytest.mark.parametrize(
        "url",
        [
            # Non-program Bond URLs
            "https://bond.edu.au/study/our-study-areas/business",
            "https://bond.edu.au/study/experience-bond-for-yourself/chat-rajan",
            "https://bond.edu.au/sport/swimming",
            "https://bond.edu.au/important-information",
            "https://bond.edu.au/study/program-finder",
            # Different host
            "https://www.acu.edu.au/program/master-of-business",
            "https://www.csu.edu.au/program/bachelor",
        ],
    )
    def test_false_for_non_program_or_other_hosts(self, url: str) -> None:
        assert is_bond_program_url(url) is False


# ─────────────────────────────────────────────────────────────────────────────
# 2. apply_bond_extraction — required always-present keys
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyBondExtractionAlwaysPresent:
    """Keys that must always be in the pre-seed regardless of page content."""

    def _run(self, html: str = "") -> dict:
        return apply_bond_extraction(
            "https://bond.edu.au/program/master-of-business-administration",
            html,
        )

    def test_has_central_fee_page_always_true(self) -> None:
        assert self._run()["has_central_fee_page"] is True

    def test_course_location_is_gold_coast(self) -> None:
        result = self._run()
        assert result["course_location"] == "Gold Coast, Queensland"

    def test_study_mode_not_set_when_no_delivery_keywords(self) -> None:
        """When page has no delivery keywords, study_mode must NOT be set.

        Defaulting to "On Campus" without evidence produces misleading data.
        The standard extractor chain should determine mode instead.
        """
        result = self._run()
        assert "study_mode" not in result, (
            "Bond pre-seed must not set study_mode='On Campus' as a default "
            "— mode should only be set when delivery keywords appear on the page."
        )

    def test_intake_months_not_set_when_not_on_page(self) -> None:
        """When no intake context found, intake_months must NOT be set.

        The old tri-semester fallback (Jan/May/Sep) produced inaccurate data
        for courses that don't run in all three semesters.
        """
        result = self._run()
        assert "intake_months" not in result, (
            "Bond pre-seed must not set intake_months with a hard-coded fallback "
            "— only set it when real intake months are found on the page."
        )

    def test_scrape_warning_added_when_no_fee_in_html(self) -> None:
        result = self._run()
        assert "bond_fee_js_rendered" in (result.get("scrape_warnings") or [])


# ─────────────────────────────────────────────────────────────────────────────
# 3. apply_bond_extraction — study mode detection
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyBondExtractionStudyMode:
    _URL = "https://bond.edu.au/program/master-of-business-administration"

    def test_no_mode_set_when_no_delivery_keywords(self) -> None:
        """No delivery keyword on page → study_mode must be absent from result.

        The standard extractor chain handles this; Bond must not fabricate a value.
        """
        html = "<h1>MBA</h1><p>Study at Gold Coast campus.</p>"
        result = apply_bond_extraction(self._URL, html)
        # "campus" is a location keyword, not a delivery-mode keyword.
        # Bond's _derive_study_mode only sets mode when ONLINE keywords are found.
        assert "study_mode" not in result

    def test_blended_when_online_and_campus_both_mentioned(self) -> None:
        html = (
            "<h1>MBA</h1>"
            "<p>Available via online delivery or on campus at Gold Coast.</p>"
        )
        result = apply_bond_extraction(self._URL, html)
        assert result.get("study_mode") == "Blended"

    def test_online_when_only_online_keyword_and_no_campus(self) -> None:
        html = "<h1>MBA Online</h1><p>Fully online delivery.</p>"
        result = apply_bond_extraction(self._URL, html)
        assert result.get("study_mode") == "Online"

    def test_study_online_keyword_triggers_online_detection(self) -> None:
        html = "<p>Study online from anywhere in Australia.</p>"
        result = apply_bond_extraction(self._URL, html)
        assert result.get("study_mode") in ("Online", "Blended")


# ─────────────────────────────────────────────────────────────────────────────
# 4. apply_bond_extraction — fee extraction from static HTML
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyBondExtractionFee:
    _URL = "https://bond.edu.au/program/master-of-business-administration"

    def test_extracts_international_fee_from_html(self) -> None:
        html = (
            "<div>International students: A$28,320 per year</div>"
        )
        result = apply_bond_extraction(self._URL, html)
        assert result.get("international_fee") == pytest.approx(28320.0)

    def test_extracts_annual_tuition_fee(self) -> None:
        html = "<p>Annual tuition fee: $32,600 AUD</p>"
        result = apply_bond_extraction(self._URL, html)
        assert result.get("international_fee") == pytest.approx(32600.0)

    def test_fee_term_is_year_when_fee_extracted(self) -> None:
        html = "<p>International students: A$28,320 per year</p>"
        result = apply_bond_extraction(self._URL, html)
        assert result.get("fee_term") == "year"

    def test_no_fee_warning_when_fee_extracted(self) -> None:
        html = "<p>International students: A$28,320 per year</p>"
        result = apply_bond_extraction(self._URL, html)
        assert "bond_fee_js_rendered" not in (result.get("scrape_warnings") or [])

    def test_ignores_implausible_fee_values(self) -> None:
        """Values outside 1,000–200,000 AUD should not be extracted."""
        html = "<p>International students: A$500</p>"  # too low
        result = apply_bond_extraction(self._URL, html)
        assert result.get("international_fee") is None

    def test_fallback_warning_when_no_fee_in_html(self) -> None:
        html = "<h1>MBA</h1><p>No fee information on this page.</p>"
        result = apply_bond_extraction(self._URL, html)
        assert result.get("international_fee") is None
        assert "bond_fee_js_rendered" in (result.get("scrape_warnings") or [])


# ─────────────────────────────────────────────────────────────────────────────
# 5. apply_bond_extraction — intake month extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyBondExtractionIntake:
    _URL = "https://bond.edu.au/program/master-of-business-administration"

    def test_extracts_months_from_intake_context(self) -> None:
        html = "<p>Intakes: January, May and September each year</p>"
        result = apply_bond_extraction(self._URL, html)
        months = result.get("intake_months", [])
        assert "January" in months
        assert "May" in months
        assert "September" in months

    def test_intake_months_absent_when_no_intake_text(self) -> None:
        """Empty page → intake_months must not be set (no hard-coded fallback)."""
        result = apply_bond_extraction(self._URL, "")
        assert "intake_months" not in result, (
            "Bond must not set intake_months with a hard-coded fallback. "
            "Leave the field absent when no intake months appear on the page."
        )

    def test_deduplicates_months(self) -> None:
        html = "<p>Semester start: January, January, May</p>"
        result = apply_bond_extraction(self._URL, html)
        months = result.get("intake_months", [])
        assert months.count("January") == 1


# ─────────────────────────────────────────────────────────────────────────────
# 6. discovery.py — Bond post-filter (structural check)
# ─────────────────────────────────────────────────────────────────────────────

class TestBondDiscoveryNonCoursePatterns:
    """Verify that Bond-specific non-course URL patterns are registered."""

    def test_experience_bond_pattern_registered(self) -> None:
        from app.services.scraper.discovery import _NON_COURSE_URL_PATTERNS
        assert any("/experience-bond-for-yourself/" in p for p in _NON_COURSE_URL_PATTERNS)

    def test_sport_pattern_registered(self) -> None:
        from app.services.scraper.discovery import _NON_COURSE_URL_PATTERNS
        assert any("/sport/" in p for p in _NON_COURSE_URL_PATTERNS)

    def test_important_information_pattern_registered(self) -> None:
        from app.services.scraper.discovery import _NON_COURSE_URL_PATTERNS
        assert any("/important-information/" in p for p in _NON_COURSE_URL_PATTERNS)

    def test_program_finder_in_junk_seg(self) -> None:
        from app.services.scraper.discovery import _JUNK_LAST_SEG_RE
        assert _JUNK_LAST_SEG_RE.match("program-finder")

    def test_our_study_areas_in_junk_seg(self) -> None:
        from app.services.scraper.discovery import _JUNK_LAST_SEG_RE
        assert _JUNK_LAST_SEG_RE.match("our-study-areas")


# ─────────────────────────────────────────────────────────────────────────────
# 7. sibling_cache.py — min_quorum prevents single-source backfill
# ─────────────────────────────────────────────────────────────────────────────

class TestSiblingCacheMinQuorum:
    """Verifies that min_quorum=2 suppresses backfill from a single source."""

    def _make_result(self, course_name: str, ielts: float | None) -> dict:
        payload: dict = {"course_name": course_name, "degree_level": "Bachelor's"}
        evidence: list = []
        if ielts is not None:
            payload["ielts_overall"] = ielts
            # Week 1 Prompt 4 — only "regex" / "css_selector" /
            # "gemini_primary" methods at conf >= 0.7 may seed the cache.
            evidence.append({
                "field_key": "ielts_overall",
                "value": ielts,
                "method": "regex",
                "confidence": 0.9,
                "source_url": "https://bond.edu.au/program/bachelor-of-laws",
            })
        return {"url": "https://bond.edu.au/program/test", "payload": payload, "evidence": evidence}

    def test_single_source_blocked_by_quorum_2(self) -> None:
        """Only one course has IELTS — quorum=2 should not backfill the others."""
        from app.services.scraper.sibling_cache import _build_bucket_cache
        results = [
            self._make_result("Bachelor of Laws", 6.5),
            self._make_result("Bachelor of Commerce", None),
            self._make_result("Bachelor of Business", None),
        ]
        cache, _origins, _prov = _build_bucket_cache(results, min_quorum=2)
        # Undergraduate bucket should be empty — only 1 source for IELTS 6.5
        ug_cache = cache.get("undergraduate", {})
        assert "ielts_overall" not in ug_cache

    def test_two_sources_meet_quorum_2(self) -> None:
        """Two courses agree on IELTS 6.5 — quorum=2 allows backfill."""
        from app.services.scraper.sibling_cache import _build_bucket_cache
        results = [
            self._make_result("Bachelor of Laws", 6.5),
            self._make_result("Bachelor of Commerce", 6.5),
            self._make_result("Bachelor of Business", None),
        ]
        cache, _origins, _prov = _build_bucket_cache(results, min_quorum=2)
        ug_cache = cache.get("undergraduate", {})
        assert ug_cache.get("ielts_overall") == 6.5

    def test_default_quorum_is_two_after_prompt_6(self) -> None:
        """Week 1 Prompt 6 raised the default ``min_quorum`` from 1 → 2.
        A single source no longer seeds the cache by default — the
        caller must explicitly pass ``min_quorum=1`` to opt back into
        the legacy behaviour."""
        from app.services.scraper.sibling_cache import _build_bucket_cache
        results = [
            self._make_result("Bachelor of Laws", 6.5),
            self._make_result("Bachelor of Commerce", None),
        ]
        # Default = 2 → single source is suppressed.
        cache_default, _o, _p = _build_bucket_cache(results)
        assert cache_default.get("undergraduate", {}).get("ielts_overall") is None
        # Explicit opt-in to legacy quorum=1 still works.
        cache_legacy, _o, _p = _build_bucket_cache(results, min_quorum=1)
        assert cache_legacy.get("undergraduate", {}).get("ielts_overall") == 6.5
