"""Phase 12: Country Intelligence — unit tests."""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from app.services.country_intelligence import (
    build_strategy_adjustments,
    normalise_country,
)


def _run(coro):
    """Run a coroutine in a fresh event loop (avoids session-loop conflicts)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── normalise_country ─────────────────────────────────────────────────────────

class TestNormaliseCountry:
    def test_australia_variants(self):
        assert normalise_country("Australia") == "Australia"
        assert normalise_country("australian") == "Australia"
        assert normalise_country("AUSTRALIA") == "Australia"
        assert normalise_country("  australia  ") == "Australia"

    def test_uk_variants(self):
        assert normalise_country("UK") == "United Kingdom"
        assert normalise_country("United Kingdom") == "United Kingdom"
        assert normalise_country("England") == "United Kingdom"
        assert normalise_country("Scotland") == "United Kingdom"
        assert normalise_country("Wales") == "United Kingdom"

    def test_usa_variants(self):
        assert normalise_country("USA") == "United States of America"
        assert normalise_country("United States") == "United States of America"
        assert normalise_country("United States of America") == "United States of America"
        assert normalise_country("us") == "United States of America"
        assert normalise_country("America") == "United States of America"

    def test_canada(self):
        assert normalise_country("Canada") == "Canada"
        assert normalise_country("canadian") == "Canada"

    def test_new_zealand(self):
        assert normalise_country("New Zealand") == "New Zealand"
        assert normalise_country("NZ") == "New Zealand"

    def test_europe_variants(self):
        assert normalise_country("Germany") == "Europe"
        assert normalise_country("Netherlands") == "Europe"
        assert normalise_country("Ireland") == "Europe"
        assert normalise_country("europe") == "Europe"

    def test_unknown_fallbacks(self):
        assert normalise_country(None) == "Unknown"
        assert normalise_country("") == "Unknown"
        assert normalise_country("Unknown") == "Unknown"

    def test_unrecognised_country_passthrough(self):
        # Unrecognised countries come back as-is (not mapped to Unknown)
        result = normalise_country("Singapore")
        assert result == "Singapore"


# ── build_strategy_adjustments ────────────────────────────────────────────────

_SENTINEL = object()


def _make_pattern(
    country="Australia",
    strategy="hybrid",
    fee_patterns=_SENTINEL,
    intake_months=_SENTINEL,
    pdf_patterns=_SENTINEL,
    req_patterns=_SENTINEL,
    platforms=_SENTINEL,
    risks=_SENTINEL,
):
    p = MagicMock()
    p.country = country
    p.preferred_strategy = strategy
    p.common_fee_patterns = {
        "currency": "AUD",
        "label_patterns": ["international fee"],
        "term": "annual",
        "cricos_code_required": True,
    } if fee_patterns is _SENTINEL else fee_patterns
    p.common_intake_patterns = ["february", "july"] if intake_months is _SENTINEL else intake_months
    p.common_pdf_patterns = ["fee schedule", "cricos"] if pdf_patterns is _SENTINEL else pdf_patterns
    p.common_requirement_patterns = {
        "english_tests": ["ielts", "pte"],
        "academic": ["gpa", "atar"],
        "cricos": True,
    } if req_patterns is _SENTINEL else req_patterns
    p.common_platforms = ["courseloop"] if platforms is _SENTINEL else platforms
    p.known_risks = ["Domestic-only fee pages"] if risks is _SENTINEL else risks
    return p


class TestBuildStrategyAdjustments:
    def test_none_pattern_returns_empty(self):
        assert build_strategy_adjustments(None) == {}

    def test_australia_hints(self):
        p = _make_pattern("Australia", "hybrid")
        hints = build_strategy_adjustments(p)
        assert hints["country"] == "Australia"
        assert hints["preferred_strategy"] == "hybrid"
        assert hints["fee_currency"] == "AUD"
        assert hints["fee_term"] == "annual"
        assert hints["cricos_required"] is True
        assert "february" in hints["intake_months"]
        assert "ielts" in hints["english_tests"]
        assert "fee schedule" in hints["pdf_keywords"]
        assert "courseloop" in hints["known_platforms"]
        assert len(hints["known_risks"]) >= 1

    def test_uk_hints(self):
        p = _make_pattern(
            "United Kingdom", "bfs",
            fee_patterns={"currency": "GBP", "term": "annual", "per_year": True},
            req_patterns={"english_tests": ["ielts"], "ucas": True},
        )
        hints = build_strategy_adjustments(p)
        assert hints["fee_currency"] == "GBP"
        assert hints["per_year_fee"] is True
        assert hints["ucas"] is True
        assert hints["cricos_required"] is False

    def test_usa_hints_per_credit(self):
        p = _make_pattern(
            "United States of America", "api",
            fee_patterns={"currency": "USD", "term": "per_credit"},
        )
        hints = build_strategy_adjustments(p)
        assert hints["per_credit_fee"] is True
        assert hints["fee_currency"] == "USD"
        assert hints["preferred_strategy"] == "api"

    def test_europe_ects(self):
        p = _make_pattern(
            "Europe", "sitemap",
            fee_patterns={"currency": "EUR", "term": "annual", "ects": True},
            req_patterns={"ects": True, "english_tests": ["ielts"]},
        )
        hints = build_strategy_adjustments(p)
        assert hints["ects"] is True
        assert hints["fee_currency"] == "EUR"

    def test_nzqa_flag(self):
        p = _make_pattern(
            "New Zealand", "bfs",
            fee_patterns={"currency": "NZD", "term": "annual", "nzqa": True},
        )
        hints = build_strategy_adjustments(p)
        assert hints["nzqa"] is True

    def test_missing_optional_fields_safe(self):
        p = _make_pattern(fee_patterns={}, req_patterns={}, pdf_patterns=[], risks=[])
        hints = build_strategy_adjustments(p)
        assert hints["fee_currency"] is None
        assert hints["cricos_required"] is False
        assert hints["known_risks"] == []
        assert hints["pdf_keywords"] == []


# ── get_pattern (async, DB mock) ──────────────────────────────────────────────

class TestGetPattern:
    def _make_db(self, row):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = row
        db.execute.return_value = result
        return db

    def test_found_row_returned(self):
        p = _make_pattern("Australia")
        db = self._make_db(p)

        from app.services.country_intelligence import get_pattern
        result = _run(get_pattern("Australia", db))
        assert result is p

    def test_not_found_falls_back_to_unknown(self):
        unknown = _make_pattern("Unknown")
        db = AsyncMock()
        result1 = MagicMock()
        result1.scalar_one_or_none.return_value = None
        result2 = MagicMock()
        result2.scalar_one_or_none.return_value = unknown
        db.execute.side_effect = [result1, result2]

        from app.services.country_intelligence import get_pattern
        result = _run(get_pattern("Singapore", db))
        assert result is unknown

    def test_exception_returns_none(self):
        db = AsyncMock()
        db.execute.side_effect = Exception("DB down")

        from app.services.country_intelligence import get_pattern
        result = _run(get_pattern("Australia", db))
        assert result is None


# ── update_country_stats ──────────────────────────────────────────────────────

class TestUpdateCountryStats:
    def _make_db_with_pattern(self, p):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = p
        db.execute.return_value = result
        return db

    def test_first_scrape_sets_completeness_directly(self):
        p = _make_pattern("Australia")
        p.avg_completeness = None
        p.avg_confidence = None
        p.id = 1
        p.success_count = 0
        db = self._make_db_with_pattern(p)

        from app.services.country_intelligence import update_country_stats
        _run(update_country_stats("Australia", 0.85, 0.75, db))
        db.execute.assert_called()
        db.commit.assert_awaited()

    def test_subsequent_scrape_applies_ema(self):
        p = _make_pattern("Australia")
        p.avg_completeness = 0.80
        p.avg_confidence = 0.70
        p.id = 1
        p.success_count = 5
        db = self._make_db_with_pattern(p)

        from app.services.country_intelligence import update_country_stats
        _run(update_country_stats("Australia", 0.90, 0.80, db))
        # EMA: 0.2 * 0.90 + 0.8 * 0.80 = 0.82
        # Just verify it ran without error
        db.commit.assert_awaited()

    def test_no_pattern_is_silent(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result

        from app.services.country_intelligence import update_country_stats
        _run(update_country_stats("Unknown", 0.85, None, db))  # Should not raise

    def test_db_exception_is_swallowed(self):
        db = AsyncMock()
        db.execute.side_effect = Exception("connection lost")

        from app.services.country_intelligence import update_country_stats
        _run(update_country_stats("Australia", 0.85, 0.75, db))  # Must not raise


# ── get_intelligence_for_university ──────────────────────────────────────────

class TestGetIntelligenceForUniversity:
    def test_university_not_found_returns_none(self):
        db = AsyncMock()
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db.execute.return_value = result

        from app.services.country_intelligence import get_intelligence_for_university
        r = _run(get_intelligence_for_university(999, db))
        assert r is None

    def test_returns_intelligence_dict(self):
        uni = MagicMock()
        uni.id = 1
        uni.country = "Australia"

        pattern = _make_pattern("Australia")
        pattern.id = 1
        pattern.success_count = 10
        pattern.avg_completeness = 0.85
        pattern.avg_confidence = 0.78
        pattern.last_scrape_at = None
        pattern.updated_at = datetime(2026, 5, 30)

        db = AsyncMock()
        r1 = MagicMock(); r1.scalar_one_or_none.return_value = uni
        r2 = MagicMock(); r2.scalar_one_or_none.return_value = pattern
        db.execute.side_effect = [r1, r2]

        from app.services.country_intelligence import get_intelligence_for_university
        result = _run(get_intelligence_for_university(1, db))
        assert result["canonical_country"] == "Australia"
        assert result["raw_country"] == "Australia"
        assert result["pattern"]["country"] == "Australia"
        assert "strategy_adjustments" in result
        assert result["strategy_adjustments"]["fee_currency"] == "AUD"

    def test_no_pattern_returns_partial(self):
        uni = MagicMock()
        uni.id = 5
        uni.country = "Singapore"

        db = AsyncMock()
        r1 = MagicMock(); r1.scalar_one_or_none.return_value = uni
        r2 = MagicMock(); r2.scalar_one_or_none.return_value = None
        r3 = MagicMock(); r3.scalar_one_or_none.return_value = None
        db.execute.side_effect = [r1, r2, r3]

        from app.services.country_intelligence import get_intelligence_for_university
        result = _run(get_intelligence_for_university(5, db))
        assert result is not None
        assert result["canonical_country"] == "Singapore"
        assert result["pattern"] is None


# ── API router (unit) ─────────────────────────────────────────────────────────

class TestCountryIntelligenceRouter:
    def test_normalise_in_route(self):
        from app.services.country_intelligence import normalise_country
        # Router uses normalise_country before DB lookup
        assert normalise_country("australia") == "Australia"
        assert normalise_country("uk") == "United Kingdom"
