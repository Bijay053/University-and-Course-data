"""Tests for bond_static_extract.py and Bond-specific scraper behaviour.

Covers:
  1. is_bond_program_url() — host + path detection
  2. apply_bond_extraction() — pre-seed output for /program/ pages
  3. discovery.py — Bond post-filter keeps only /program/ URLs
  4. sibling_cache.py — min_quorum prevents single-source backfill
"""
from __future__ import annotations

import pytest

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.bond_static_extract import (
    _enrich_from_central_ielts,
    _enrich_from_details_api,
    _enrich_from_fees_api,
    _extract_program_ids,
    apply_bond_extraction,
    is_bond_program_url,
    suppress_authoritative_fee_omission,
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
            "https://bond.edu.au/microcredential/mastering-negotiations-behavioural-science",
            # Different host
            "https://www.acu.edu.au/program/master-of-business",
            "https://www.csu.edu.au/program/bachelor",
        ],
    )
    def test_false_for_non_program_or_other_hosts(self, url: str) -> None:
        assert is_bond_program_url(url) is False


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("2 years (6 semesters)", (2.0, "Year")),
        ("1 year 4 months (4 semesters)", (16.0, "Month")),
        ("16 months", (16.0, "Month")),
        ("5 semesters", (5.0, "Semester")),
    ],
)
def test_details_api_preserves_duration_value_and_unit(
    monkeypatch, source: str, expected: tuple[float, str]
) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_json",
        lambda _url: {"programs": [{"duration": source}]},
    )

    result = _enrich_from_details_api("123")

    assert (result["duration"], result["duration_term"]) == expected


def test_details_api_exposes_valid_program_code_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_json",
        lambda _url: {"programs": [{"id": "CC-60005", "duration": "2 years"}]},
    )
    assert _enrich_from_details_api("5622")["_program_code"] == "CC-60005"


def test_program_ids_accept_spacing_absolute_urls_and_lowercase_codes() -> None:
    html = (
        '<div class="program-detail" '
        'data-program-detail-url = "https://www.bond.edu.au/api/program-details/5622" '
        'data-program = "cc-60005"></div>'
    )
    assert _extract_program_ids(html) == ("5622", "cc-60005")


def test_program_ids_choose_paired_main_program_over_related_card() -> None:
    html = (
        '<article data-program-detail-url="/api/program-details/999" '
        'data-program="WRONG-999"></article>'
        '<div class="program-detail notranslate" '
        'data-program-detail-url="/api/program-details/351" '
        'data-program="HS-20021"></div>'
    )
    assert _extract_program_ids(html) == ("351", "HS-20021")


def test_program_ids_reject_multiple_unscoped_components() -> None:
    html = (
        '<article data-program-detail-url="/api/program-details/1" '
        'data-program="AA-100"></article>'
        '<article data-program-detail-url="/api/program-details/2" '
        'data-program="BB-200"></article>'
    )
    assert _extract_program_ids(html) == (None, None)


@pytest.mark.parametrize(
    ("international", "expected"),
    [
        ({"annual": 43700, "total": 174800}, (43700.0, "Annual")),
        ({"semester": 25040, "total": 150240}, (75120.0, "Annual")),
        ({"total": 21200}, (21200.0, "Full Course")),
    ],
)
def test_fee_api_preserves_authoritative_period(
    monkeypatch, international: dict, expected: tuple[float, str]
) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_json",
        lambda _url: {
            "fees": [
                {"year": "2025", "international": {"total": 9999}},
                {"year": "2027", "international": international},
            ]
        },
    )
    result = _enrich_from_fees_api("123", "AA-100")
    assert (result["international_fee"], result["fee_term"]) == expected
    assert result["fee_year"] == 2027
    assert result["currency"] == "AUD"


def test_empty_fee_api_is_an_authoritative_omission(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_json",
        lambda _url: {"fees": []},
    )
    assert _enrich_from_fees_api("123", "AA-100") == {
        "_authoritative_fee_omission": True
    }


def test_central_ielts_is_limited_to_exact_authoritative_groups(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_html",
        lambda _url: (
            "<table><tr><td>Master of Occupational Therapy</td>"
            "<td>Overall 7.0 with no sub-score less than 6.5</td></tr></table>"
            "<table><tr><td>All higher degree by research (HDR) programs</td>"
            "<td>Overall 7.0 with no sub-score less than 6.5</td></tr></table>"
        ),
    )
    expected = {
        "ielts_overall": 7.0,
        "ielts_writing": 6.5,
        "ielts_reading": 6.5,
        "ielts_listening": 6.5,
        "ielts_speaking": 6.5,
    }
    assert _enrich_from_central_ielts(
        "https://bond.edu.au/program/master-of-philosophy"
    ) == expected
    assert _enrich_from_central_ielts(
        "https://bond.edu.au/program/bachelor-of-health-sciences-master-of-occupational-therapy"
    ) == expected
    assert _enrich_from_central_ielts(
        "https://bond.edu.au/program/master-of-accounting"
    ) == {}
    assert _enrich_from_central_ielts(
        "https://bond.edu.au/program/unrelated-research"
    ) == {}


def test_central_ielts_does_not_borrow_neighboring_row_score(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_html",
        lambda _url: (
            "<table>"
            "<tr><td>Master of Occupational Therapy</td><td>No numeric score published</td></tr>"
            "<tr><td>Unrelated program</td><td>Overall 6.0 with no sub-score less than 5.5</td></tr>"
            "</table>"
        ),
    )
    assert _enrich_from_central_ielts(
        "https://bond.edu.au/program/master-of-occupational-therapy"
    ) == {}


def test_central_ielts_honors_rowspan_group(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._get_html",
        lambda _url: (
            "<table>"
            "<tr><td>Doctor of Physiotherapy</td>"
            "<td rowspan='2'>Overall 7.0 with no sub-score less than 6.5</td></tr>"
            "<tr><td>Master of Occupational Therapy</td></tr>"
            "</table>"
        ),
    )
    assert _enrich_from_central_ielts(
        "https://bond.edu.au/program/master-of-occupational-therapy"
    )["ielts_overall"] == 7.0


def test_course_english_wins_and_central_only_fills_missing_slots(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._extract_program_ids",
        lambda _html: (None, None),
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_entry_requirements",
        lambda _url: {"ielts_overall": 7.5},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_central_ielts",
        lambda _url: {
            "ielts_overall": 7.0,
            "ielts_writing": 6.5,
            "ielts_reading": 6.5,
        },
    )
    result = apply_bond_extraction(
        "https://bond.edu.au/program/master-of-philosophy", ""
    )
    assert result["ielts_overall"] == 7.5
    assert result["ielts_writing"] == 6.5
    assert result["_source_urls"]["ielts_overall"].endswith("/entry_requirements")
    assert result["_source_urls"]["ielts_writing"].endswith(
        "/english-language-requirements/ielts"
    )


def test_disagreeing_html_and_details_codes_skip_fee_lookup(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_details_api",
        lambda _id: {"_program_code": "FALLBACK-999"},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_fees_api",
        lambda _id, code: called.append(code) or {},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_entry_requirements",
        lambda _url: {},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_central_ielts",
        lambda _url: {},
    )
    apply_bond_extraction(
        "https://bond.edu.au/program/example",
        '<div class="program-detail" '
        'data-program-detail-url="/api/program-details/123" '
        'data-program="EXPLICIT-123"></div>',
    )
    assert called == []


def test_matching_html_and_details_codes_use_paired_fee_lookup(monkeypatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_details_api",
        lambda _id: {"_program_code": "EXPLICIT-123"},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_fees_api",
        lambda _id, code: called.append(code) or {},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_entry_requirements",
        lambda _url: {},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_central_ielts",
        lambda _url: {},
    )
    apply_bond_extraction(
        "https://bond.edu.au/program/example",
        '<div class="program-detail" '
        'data-program-detail-url="/api/program-details/123" '
        'data-program="EXPLICIT-123"></div>',
    )
    assert called == ["EXPLICIT-123"]


def test_authoritative_empty_fee_blocks_static_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_details_api",
        lambda _id: {"_program_code": "AA-100"},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_fees_api",
        lambda _id, _code: {"_authoritative_fee_omission": True},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_entry_requirements",
        lambda _url: {},
    )
    monkeypatch.setattr(
        "app.services.scraper.bond_static_extract._enrich_from_central_ielts",
        lambda _url: {},
    )
    result = apply_bond_extraction(
        "https://bond.edu.au/program/example",
        '<div class="program-detail" '
        'data-program-detail-url="/api/program-details/123"></div>'
        "<p>International students: A$99,999</p>",
    )
    assert "international_fee" not in result
    assert result["_authoritative_fee_omission"] is True
    assert "bond_fee_source_empty" in result["scrape_warnings"]


def test_authoritative_empty_fee_removes_all_downstream_guesses() -> None:
    payload = {
        "international_fee": 99999,
        "currency": "AUD",
        "fee_term": "Annual",
        "fee_year": 2026,
        "course_name": "Keep me",
    }
    evidence = [
        {"field_key": "international_fee", "method": "ai_fallback"},
        {"field_key": "fee_term", "method": "page_regex"},
        {"field_key": "course_name", "method": "page_regex"},
    ]
    suppress_authoritative_fee_omission(payload, evidence)
    assert all(
        payload[key] is None
        for key in ("international_fee", "domestic_fee", "currency", "fee_term", "fee_year")
    )
    assert evidence == [{"field_key": "course_name", "method": "page_regex"}]
    assert payload["course_name"] == "Keep me"


def test_enrichment_script_never_writes_internal_metadata() -> None:
    from scripts.bond_enrich import _build_update

    sql, params = _build_update(
        {
            "id": 123,
            "_authoritative_fee_omission": True,
            "_source_urls": {"international_fee": "https://example.test"},
            "scrape_warnings": ["bond_fee_source_empty"],
            "course_location": "Gold Coast, Queensland",
        }
    )
    assert "_authoritative_fee_omission" not in sql
    assert "_source_urls" not in sql
    assert "scrape_warnings" not in sql
    assert params["course_location"] == "Gold Coast, Queensland"


def test_enrichment_script_clears_existing_fee_after_authoritative_empty(
    monkeypatch,
) -> None:
    from scripts import bond_enrich

    monkeypatch.setattr(bond_enrich, "_get", lambda _url: "<html>Bond program</html>")
    monkeypatch.setattr(
        bond_enrich,
        "apply_bond_extraction",
        lambda _url, _html: {
            "_authoritative_fee_omission": True,
            "_source_urls": {
                "international_fee": "https://bond.edu.au/api/program-fees/1/ABC"
            },
            "scrape_warnings": ["bond_fee_source_empty"],
        },
    )

    fields = bond_enrich.enrich_one(
        {
            "id": 123,
            "source_url": "https://bond.edu.au/program/example",
            # Represents stale values already present in the database.  The
            # generated UPDATE must explicitly clear every associated slot.
            "international_fee": 99999,
            "domestic_fee": 88888,
            "currency": "AUD",
            "fee_term": "Annual",
            "fee_year": 2025,
        }
    )
    sql, params = bond_enrich._build_update(fields)

    for key in (
        "international_fee",
        "domestic_fee",
        "currency",
        "fee_term",
        "fee_year",
    ):
        assert f"{key} = :{key}" in sql
        assert key in params
        assert params[key] is None
    assert "_authoritative_fee_omission" not in sql
    assert "scrape_warnings" not in sql


def test_bond_config_excludes_microcredentials_from_degree_discovery() -> None:
    config = load_uni_config(
        slug="bond",
        name="Bond University",
        scrape_url="https://bond.edu.au",
        university_id=29,
        db_scrape_config=None,
    )
    api = config.discovery.generic_search_api

    assert api is not None
    assert config.extraction.fees.annual_fee_warning_max_aud == 90_000
    assert all("microcredential" not in pattern for pattern in api.allow_url_patterns)
    assert any(
        "microcredential" in pattern
        for pattern in config.discovery.block_url_patterns
    )
    assert config.extraction.english.course_english_priority is True
    assert config.extraction.english.central_page is None


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

    def test_course_location_is_not_fabricated(self) -> None:
        result = self._run()
        assert "course_location" not in result

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
        assert "study_mode" not in result

    def test_page_wide_delivery_keywords_do_not_preseed_mode(self) -> None:
        html = (
            "<h1>MBA</h1>"
            "<p>Available via online delivery or on campus at Gold Coast.</p>"
        )
        result = apply_bond_extraction(self._URL, html)
        assert "study_mode" not in result

    def test_online_keyword_does_not_override_structured_extractor(self) -> None:
        html = "<h1>MBA Online</h1><p>Fully online delivery.</p>"
        result = apply_bond_extraction(self._URL, html)
        assert "study_mode" not in result

    def test_footer_style_study_online_copy_does_not_set_mode(self) -> None:
        html = "<p>Study online from anywhere in Australia.</p>"
        result = apply_bond_extraction(self._URL, html)
        assert "study_mode" not in result


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
    """Verifies the sibling-cache backfill behaviour.

    History:
    - Week 1 Prompt 6: min_quorum raised from 1 → 2 (quorum gate).
    - 2026-05-15: _SIBLING_BACKFILL_SLOTS emptied globally — IELTS (and all
      English-test scores) are no longer propagated between sibling courses.

    Concrete failure that prompted the global disable: Flinders' Master of
    Science (Biology) and Master of Science (Environmental Science) staged with
    ielts_overall=6.0 inherited from an 8-course postgrad consensus even though
    those course pages publish no IELTS at all.  The user's stance:
    "if there is no IELTS, leave blank — do not add from a sibling."

    The min_quorum gate remains in place so it can be re-engaged immediately
    when any slot is re-added to _SIBLING_BACKFILL_SLOTS in the future.
    """

    def _make_result(self, course_name: str, ielts: float | None) -> dict:
        payload: dict = {"course_name": course_name, "degree_level": "Bachelor's"}
        evidence: list = []
        if ielts is not None:
            payload["ielts_overall"] = ielts
            evidence.append({
                "field_key": "ielts_overall",
                "value": ielts,
                "method": "regex",
                "confidence": 0.9,
                "source_url": "https://bond.edu.au/program/bachelor-of-laws",
            })
        return {"url": "https://bond.edu.au/program/test", "payload": payload, "evidence": evidence}

    def test_single_source_no_backfill(self) -> None:
        """Only one course has IELTS — no backfill (slots globally disabled)."""
        from app.services.scraper.sibling_cache import _build_bucket_cache
        results = [
            self._make_result("Bachelor of Laws", 6.5),
            self._make_result("Bachelor of Commerce", None),
            self._make_result("Bachelor of Business", None),
        ]
        cache, _origins, _prov = _build_bucket_cache(results, min_quorum=2)
        ug_cache = cache.get("undergraduate", {})
        assert "ielts_overall" not in ug_cache

    def test_two_sources_no_backfill_slots_globally_disabled(self) -> None:
        """Even when quorum=2 is met, no IELTS backfill occurs.

        _SIBLING_BACKFILL_SLOTS is globally empty since 2026-05-15 — the
        min_quorum check is never reached because no slots are accumulated
        into the counter in the first place.  The cache is always empty for
        English-test fields regardless of how many courses agree.
        """
        from app.services.scraper.sibling_cache import _build_bucket_cache
        results = [
            self._make_result("Bachelor of Laws", 6.5),
            self._make_result("Bachelor of Commerce", 6.5),
            self._make_result("Bachelor of Business", None),
        ]
        cache, _origins, _prov = _build_bucket_cache(results, min_quorum=2)
        ug_cache = cache.get("undergraduate", {})
        # ielts_overall is NOT backfilled — _SIBLING_BACKFILL_SLOTS = ()
        assert ug_cache.get("ielts_overall") is None

    def test_cache_always_empty_regardless_of_quorum(self) -> None:
        """With _SIBLING_BACKFILL_SLOTS empty, quorum setting has no effect.

        Both min_quorum=2 (default) and min_quorum=1 produce an empty cache
        because no slots are eligible for accumulation.
        """
        from app.services.scraper.sibling_cache import _build_bucket_cache
        results = [
            self._make_result("Bachelor of Laws", 6.5),
            self._make_result("Bachelor of Commerce", None),
        ]
        cache_default, _o, _p = _build_bucket_cache(results)
        assert cache_default.get("undergraduate", {}).get("ielts_overall") is None
        # Even with quorum lowered to 1, slots are still globally disabled.
        cache_q1, _o, _p = _build_bucket_cache(results, min_quorum=1)
        assert cache_q1.get("undergraduate", {}).get("ielts_overall") is None
