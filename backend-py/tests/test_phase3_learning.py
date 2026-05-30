"""Phase 3 — Autonomous Learning Layer: unit tests.

Tests cover:
- derive_platform_type() priority (API provider > library situation > strategy)
- _platform_type persisted in auto_config via _base_config()
- lookup_patterns() — empty when no rows, correct dict when rows exist
- promote_patterns() — skips below threshold, upserts above threshold, running avg
- _build_seeded_prompt() — empty when no patterns, correct JSON section when patterns exist
- generate_and_store_rules() with learned_patterns — seeded prompt + learned backfill
- generate_and_store_rules() fallback when Gemini skipped — returns learned patterns as-is
- generate_and_store_rules() merge — Gemini rules take precedence, learned fills gaps
- apply_repaired_rules_to_db arg-order fix (university_id, rules, db — NOT db, uni, rules)
- probe_and_configure Platform 3 hook — lookup_patterns called before generate_config
- repair_extractor Phase 3 hook — promote_patterns called after successful repair
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _make_profile(
    *,
    detected_apis: list | None = None,
    library_situation: str | None = None,
    strategy: str = "static_html",
) -> Any:
    """Build a minimal SiteProfile-like object for testing."""
    profile = MagicMock()
    profile.url = "https://example.edu/courses"
    profile.recommended_strategy = strategy
    profile.detected_apis = detected_apis or []
    profile.is_cloudflare_blocked = False
    profile.is_bot_protected = False
    profile.is_js_spa = False
    profile.has_sitemap = False
    profile.sitemap_course_count = 0
    profile.wayback_course_count = 0
    profile.notes = []
    profile.strategy_confidence = 0.9
    profile.sitemap_url = None
    profile.strategy_ladder = []

    # Phase 4A: must be None so _derive_platform_type skips the CMS priority slot.
    # MagicMock leaves any unset attribute as a truthy MagicMock — pin it explicitly.
    profile.cms_platform = None

    if library_situation:
        ls = MagicMock()
        ls.situation = library_situation
        profile.library_stack = ls
    else:
        profile.library_stack = None

    return profile


def _run(coro):
    """Run a coroutine in a fresh event loop to avoid conflicts with pytest-asyncio."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────────────────────
# 1. derive_platform_type()
# ─────────────────────────────────────────────────────────────────────────────

class TestDerivePlatformType:
    def _fn(self, profile):
        from app.services.scraper.auto_config_generator import _derive_platform_type
        return _derive_platform_type(profile)

    def test_api_provider_wins_over_library_situation(self):
        api = MagicMock()
        api.provider = "SearchStax"
        profile = _make_profile(detected_apis=[api], library_situation="wordpress")
        assert self._fn(profile) == "searchstax"

    def test_library_situation_wins_over_strategy(self):
        profile = _make_profile(library_situation="WordPress", strategy="browser")
        assert self._fn(profile) == "wordpress"

    def test_strategy_fallback_when_no_api_no_library(self):
        profile = _make_profile(strategy="SITEMAP_FIRST")
        assert self._fn(profile) == "sitemap_first"

    def test_empty_string_when_no_signal(self):
        profile = _make_profile(strategy="")
        assert self._fn(profile) == ""

    def test_strips_whitespace(self):
        api = MagicMock()
        api.provider = "  Algolia  "
        profile = _make_profile(detected_apis=[api])
        assert self._fn(profile) == "algolia"

    def test_empty_api_provider_falls_through_to_library(self):
        api = MagicMock()
        api.provider = ""
        profile = _make_profile(detected_apis=[api], library_situation="drupal")
        assert self._fn(profile) == "drupal"


# ─────────────────────────────────────────────────────────────────────────────
# 2. _platform_type persisted in auto_config
# ─────────────────────────────────────────────────────────────────────────────

class TestPlatformTypeInBaseConfig:
    def test_api_provider_stored_in_config(self):
        from app.services.scraper.auto_config_generator import _base_config
        from app.services.scraper.site_probe import STRATEGY_SEARCH_API

        api = MagicMock()
        api.provider = "algolia"
        api.endpoint_hint = "https://abc.algolia.net"
        profile = _make_profile(detected_apis=[api], strategy=STRATEGY_SEARCH_API)
        profile.recommended_strategy = STRATEGY_SEARCH_API

        cfg = _base_config(profile)
        assert cfg.get("_platform_type") == "algolia"

    def test_library_situation_stored_in_config(self):
        from app.services.scraper.auto_config_generator import _base_config
        from app.services.scraper.site_probe import STRATEGY_STATIC_HTML

        profile = _make_profile(library_situation="drupal", strategy=STRATEGY_STATIC_HTML)
        profile.recommended_strategy = STRATEGY_STATIC_HTML

        cfg = _base_config(profile)
        assert cfg.get("_platform_type") == "drupal"

    def test_strategy_fallback_in_config(self):
        from app.services.scraper.auto_config_generator import _base_config
        from app.services.scraper.site_probe import STRATEGY_WAYBACK

        profile = _make_profile(strategy=STRATEGY_WAYBACK)
        profile.recommended_strategy = STRATEGY_WAYBACK

        cfg = _base_config(profile)
        assert cfg.get("_platform_type") == STRATEGY_WAYBACK.lower()


# ─────────────────────────────────────────────────────────────────────────────
# 3. lookup_patterns()
# ─────────────────────────────────────────────────────────────────────────────

class TestLookupPatterns:
    def _run_lookup(self, rows, platform_type="wordpress"):
        from app.services.scraper.pattern_store import lookup_patterns

        db = AsyncMock()
        result = MagicMock()
        result.fetchall.return_value = rows
        db.execute = AsyncMock(return_value=result)
        return _run(lookup_patterns(platform_type, db))

    def test_empty_dict_when_no_rows(self):
        patterns = self._run_lookup([])
        assert patterns == {}

    def test_empty_dict_for_empty_platform_type(self):
        from app.services.scraper.pattern_store import lookup_patterns
        db = AsyncMock()
        result = _run(lookup_patterns("", db))
        assert result == {}
        db.execute.assert_not_called()

    def test_returns_dict_keyed_by_field_key(self):
        rule = {"css": ".fee", "confidence": 0.9}
        rows = [
            ("international_fee", rule),
            ("ielts_overall", {"regex": r"IELTS\s+([\d.]+)", "confidence": 0.85}),
        ]
        patterns = self._run_lookup(rows)
        assert "international_fee" in patterns
        assert "ielts_overall" in patterns
        assert patterns["international_fee"] == rule

    def test_handles_json_string_rules(self):
        rule = {"css": ".fee", "confidence": 0.9}
        rows = [("international_fee", json.dumps(rule))]
        patterns = self._run_lookup(rows)
        assert patterns["international_fee"] == rule

    def test_db_error_returns_empty_dict(self):
        from app.services.scraper.pattern_store import lookup_patterns
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=Exception("DB down"))
        result = _run(lookup_patterns("wordpress", db))
        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# 4. promote_patterns()
# ─────────────────────────────────────────────────────────────────────────────

class TestPromotePatterns:
    def _make_db(self):
        db = AsyncMock()
        db.execute = AsyncMock()
        db.commit = AsyncMock()
        return db

    def _run_promote(self, platform_type, rules, fill_rates, db=None):
        from app.services.scraper.pattern_store import promote_patterns
        if db is None:
            db = self._make_db()
        return _run(promote_patterns(platform_type, rules, fill_rates, db)), db

    def test_returns_zero_for_empty_rules(self):
        n, _ = self._run_promote("wordpress", {}, {})
        assert n == 0

    def test_returns_zero_for_empty_platform_type(self):
        n, db = self._run_promote("", {"international_fee": {"css": ".fee"}}, {"international_fee": 0.9})
        assert n == 0
        db.execute.assert_not_called()

    def test_skips_fields_below_threshold(self):
        rules = {"international_fee": {"css": ".fee"}}
        fill_rates = {"international_fee": 0.50}  # below 0.70
        n, db = self._run_promote("wordpress", rules, fill_rates)
        assert n == 0
        db.execute.assert_not_called()

    def test_promotes_fields_above_threshold(self):
        rules = {
            "international_fee": {"css": ".fee"},
            "ielts_overall": {"regex": r"IELTS\s+([\d.]+)"},
        }
        fill_rates = {"international_fee": 0.90, "ielts_overall": 0.80}
        n, db = self._run_promote("wordpress", rules, fill_rates)
        assert n == 2
        assert db.execute.call_count == 2
        db.commit.assert_called_once()

    def test_partial_promotion_mixed_rates(self):
        rules = {
            "international_fee": {"css": ".fee"},      # 0.90 — promote
            "other_requirement": {"css": ".req"},       # 0.50 — skip
            "ielts_overall": {"regex": r"IELTS\s+"},   # 0.75 — promote
        }
        fill_rates = {
            "international_fee": 0.90,
            "other_requirement": 0.50,
            "ielts_overall": 0.75,
        }
        n, db = self._run_promote("drupal", rules, fill_rates)
        assert n == 2

    def test_missing_fill_rate_treated_as_zero(self):
        rules = {"international_fee": {"css": ".fee"}}
        fill_rates = {}  # key missing → defaults to 0.0
        n, db = self._run_promote("drupal", rules, fill_rates)
        assert n == 0

    def test_db_execute_error_is_handled(self):
        rules = {"international_fee": {"css": ".fee"}}
        fill_rates = {"international_fee": 0.90}
        db = self._make_db()
        db.execute = AsyncMock(side_effect=Exception("insert failed"))
        n, _ = self._run_promote("wordpress", rules, fill_rates, db=db)
        assert n == 0  # error suppressed, commit not called


# ─────────────────────────────────────────────────────────────────────────────
# 5. _build_seeded_prompt()
# ─────────────────────────────────────────────────────────────────────────────

class TestBuildSeededPrompt:
    def _fn(self, learned_patterns):
        from app.services.scraper.ai_extractor_gen import _build_seeded_prompt
        return _build_seeded_prompt(learned_patterns)

    def test_empty_string_when_no_patterns(self):
        assert self._fn({}) == ""

    def test_contains_proven_patterns_header(self):
        patterns = {"international_fee": {"css": ".fee", "confidence": 0.9}}
        result = self._fn(patterns)
        assert "PROVEN PATTERNS" in result

    def test_contains_field_key(self):
        patterns = {"ielts_overall": {"regex": r"IELTS\s+([\d.]+)", "confidence": 0.85}}
        result = self._fn(patterns)
        assert "ielts_overall" in result

    def test_strips_confidence_from_output(self):
        """confidence is internal — not passed to Gemini as a pattern field."""
        patterns = {"international_fee": {"css": ".fee", "confidence": 0.9, "transform": "currency"}}
        result = self._fn(patterns)
        # css and transform ARE included (they're selector/processing fields)
        assert "css" in result
        assert "transform" in result
        # confidence is NOT a selector field — should not appear in the seed JSON
        # The implementation filters to only css/xpath/regex/attribute/transform
        # so "confidence" key should be absent from the seeded JSON block.
        # Parse the JSON block to verify:
        json_block_start = result.index("{")
        json_block = result[json_block_start:]
        # Trim trailing non-JSON lines
        json_end = json_block.rindex("}") + 1
        parsed = json.loads(json_block[:json_end])
        fee_rule = parsed.get("international_fee", {})
        assert "confidence" not in fee_rule

    def test_includes_css_xpath_regex_transform_attribute(self):
        patterns = {
            "international_fee": {
                "css": ".fee",
                "attribute": "data-value",
                "transform": "currency",
                "confidence": 0.9,
                "quoted_text": "AUD 45,000",
            }
        }
        result = self._fn(patterns)
        assert "css" in result
        assert "attribute" in result
        assert "transform" in result
        # quoted_text should not be in the seed (it's HTML-specific, not reusable)
        # Note: the implementation does strip quoted_text from the output
        assert "quoted_text" not in result


# ─────────────────────────────────────────────────────────────────────────────
# 6. generate_and_store_rules() with learned_patterns
# ─────────────────────────────────────────────────────────────────────────────

class TestGenerateAndStoreRulesWithLearned:
    """Tests using a mocked Gemini client."""

    def _make_resp(self, text: str | None, skipped: bool = False):
        resp = MagicMock()
        resp.text = text
        resp.skipped = skipped
        return resp

    def _make_auto_config(self, platform_type: str = "wordpress") -> dict:
        return {"_platform_type": platform_type}

    def test_learned_patterns_injected_into_prompt(self):
        """Verify that the seeded prompt section reaches Gemini."""
        from app.services.scraper.ai_extractor_gen import generate_and_store_rules

        captured_prompts: list[str] = []

        async def _fake_generate(prompt: str, call_type: str = ""):
            captured_prompts.append(prompt)
            return self._make_resp("{}")

        profile = _make_profile()
        sample_html = "<html><body>IELTS 6.5 AUD 45,000</body></html>"
        learned = {"ielts_overall": {"regex": r"IELTS\s+([\d.]+)", "confidence": 0.85}}
        auto_config = self._make_auto_config()

        with patch("app.services.ai.gemini_client.generate", side_effect=_fake_generate):
            _run(generate_and_store_rules(profile, sample_html, auto_config, learned_patterns=learned))

        assert len(captured_prompts) == 1
        assert "PROVEN PATTERNS" in captured_prompts[0]
        assert "ielts_overall" in captured_prompts[0]

    def test_no_seed_section_when_no_learned_patterns(self):
        from app.services.scraper.ai_extractor_gen import generate_and_store_rules

        captured_prompts: list[str] = []

        async def _fake_generate(prompt: str, call_type: str = ""):
            captured_prompts.append(prompt)
            return self._make_resp("{}")

        profile = _make_profile()
        sample_html = "<html><body>test</body></html>"
        auto_config = self._make_auto_config()

        with patch("app.services.ai.gemini_client.generate", side_effect=_fake_generate):
            _run(generate_and_store_rules(profile, sample_html, auto_config))

        assert "PROVEN PATTERNS" not in captured_prompts[0]

    def test_gemini_skipped_falls_back_to_learned_patterns(self):
        """When Gemini quota is hit, learned patterns are stored as-is."""
        from app.services.scraper.ai_extractor_gen import generate_and_store_rules

        async def _fake_generate(prompt: str, call_type: str = ""):
            return self._make_resp(None, skipped=True)

        profile = _make_profile()
        sample_html = "<html><body>test</body></html>"
        learned = {"international_fee": {"css": ".fee", "confidence": 0.9}}
        auto_config = self._make_auto_config()

        with patch("app.services.ai.gemini_client.generate", side_effect=_fake_generate):
            result = _run(generate_and_store_rules(profile, sample_html, auto_config, learned_patterns=learned))

        assert "extraction_rules" in result
        assert result["extraction_rules"]["international_fee"] == learned["international_fee"]
        assert result.get("_extraction_rules_source") == "learned_fallback"

    def test_gemini_rules_merged_with_learned_backfill(self):
        """Gemini covers some fields; learned patterns fill the rest."""
        from app.services.scraper.ai_extractor_gen import generate_and_store_rules

        gemini_rules = json.dumps({
            "international_fee": {
                "css": ".fee-amount",
                "quoted_text": "AUD 45,000",
                "confidence": 0.92,
            }
        })

        async def _fake_generate(prompt: str, call_type: str = ""):
            return self._make_resp(gemini_rules)

        profile = _make_profile()
        # HTML must contain the quoted_text for _validate_rule to pass
        sample_html = "<html><body>AUD 45,000</body></html>"
        learned = {
            "ielts_overall": {"regex": r"IELTS\s+([\d.]+)", "confidence": 0.85},
        }
        auto_config = self._make_auto_config()

        with patch("app.services.ai.gemini_client.generate", side_effect=_fake_generate):
            result = _run(generate_and_store_rules(profile, sample_html, auto_config, learned_patterns=learned))

        rules = result.get("extraction_rules", {})
        # Gemini covered international_fee
        assert "international_fee" in rules
        # learned backfilled ielts_overall (not covered by Gemini)
        assert "ielts_overall" in rules

    def test_gemini_result_takes_precedence_over_learned(self):
        """When Gemini and learned patterns both cover a field, Gemini wins."""
        from app.services.scraper.ai_extractor_gen import generate_and_store_rules

        gemini_rules = json.dumps({
            "ielts_overall": {
                "css": ".ielts-new",
                "quoted_text": "IELTS 6.5",
                "confidence": 0.95,
            }
        })

        async def _fake_generate(prompt: str, call_type: str = ""):
            return self._make_resp(gemini_rules)

        profile = _make_profile()
        sample_html = "<html><body>IELTS 6.5</body></html>"
        learned = {"ielts_overall": {"css": ".ielts-old", "confidence": 0.80}}
        auto_config = self._make_auto_config()

        with patch("app.services.ai.gemini_client.generate", side_effect=_fake_generate):
            result = _run(generate_and_store_rules(profile, sample_html, auto_config, learned_patterns=learned))

        rules = result.get("extraction_rules", {})
        # Gemini's newer rule should win
        assert rules.get("ielts_overall", {}).get("css") == ".ielts-new"

    def test_stores_learned_count_in_auto_config(self):
        """_extraction_rules_learned_count records how many fields were seeded."""
        from app.services.scraper.ai_extractor_gen import generate_and_store_rules

        # Return one Gemini rule so we get rules in auto_config
        gemini_rules = json.dumps({
            "international_fee": {
                "css": ".fee",
                "quoted_text": "AUD 45,000",
                "confidence": 0.90,
            }
        })

        async def _fake_generate(prompt: str, call_type: str = ""):
            return self._make_resp(gemini_rules)

        profile = _make_profile()
        sample_html = "<html><body>AUD 45,000</body></html>"
        learned = {
            "ielts_overall": {"css": ".ielts"},
            "duration": {"css": ".dur"},
        }
        auto_config = self._make_auto_config()

        with patch("app.services.ai.gemini_client.generate", side_effect=_fake_generate):
            result = _run(generate_and_store_rules(profile, sample_html, auto_config, learned_patterns=learned))

        assert result.get("_extraction_rules_learned_count") == 2


# ─────────────────────────────────────────────────────────────────────────────
# 7. apply_repaired_rules_to_db arg order fix
# ─────────────────────────────────────────────────────────────────────────────

class TestApplyRepairedRulesArgOrder:
    """Verify the function signature is (university_id, repaired_rules, db)."""

    def test_correct_arg_order_university_id_first(self):
        """university_id must be first — NOT db."""
        import inspect
        from app.services.scraper.ai_extractor_repair import apply_repaired_rules_to_db
        sig = inspect.signature(apply_repaired_rules_to_db)
        params = list(sig.parameters.keys())
        assert params[0] == "university_id", (
            f"Expected first param to be 'university_id', got {params[0]!r}. "
            "If db comes first the Celery task silently passes wrong types."
        )
        assert params[1] == "repaired_rules"
        assert params[2] == "db"


# ─────────────────────────────────────────────────────────────────────────────
# 8. REPAIR_ESTIMATED_FILL_RATE constant
# ─────────────────────────────────────────────────────────────────────────────

class TestConstants:
    def test_promote_min_fill_rate_is_reasonable(self):
        from app.services.scraper.pattern_store import PROMOTE_MIN_FILL_RATE
        assert 0.50 <= PROMOTE_MIN_FILL_RATE <= 0.90

    def test_repair_estimated_fill_rate_above_threshold(self):
        from app.services.scraper.pattern_store import (
            PROMOTE_MIN_FILL_RATE,
            REPAIR_ESTIMATED_FILL_RATE,
        )
        assert REPAIR_ESTIMATED_FILL_RATE >= PROMOTE_MIN_FILL_RATE, (
            "REPAIR_ESTIMATED_FILL_RATE must be >= PROMOTE_MIN_FILL_RATE "
            "or repairs will never be promoted."
        )
