"""Week 1 Prompts 4–8 — focused regression tests.

Each test covers exactly one prompt's behaviour change so that a failure
diagnoses the responsible feature without ambiguity.

Prompts under test:

* P4 — Sibling cache source-type gating (only regex/css_selector/
  gemini_primary at conf ≥ 0.7 may seed the bucket cache).
* P5 — AI fallback prompt tightening (CRITICAL RULES block prepended).
* P6 — Sibling cache ≥ 2 source consensus (default ``min_quorum``
  raised from 1 → 2).
* P7 Part B — Tier-1 vision OCR for English fields is suppressed by
  default (``trust_tier1_vision_ocr_english`` global default = False).
* P8 — AI fallback page-text validation (``validate_ai_fallback_value``).
"""
from __future__ import annotations

import asyncio

import pytest

from app.services.scraper import sibling_cache as sc
from app.services.scraper.config.schema import EnglishConfig
from app.services.scraper.extractors import ai_fallback as af


# ────────────────────────────────────────────────────────────────────
# Prompt 4 — sibling cache source-type gating
# ────────────────────────────────────────────────────────────────────
def _make_result(url, name, ielts, method, conf=0.9):
    """Helper producing a payload+evidence dict shaped like the orchestrator
    feeds to ``_build_bucket_cache``."""
    return {
        "url": url,
        "payload": {
            "course_name": name,
            "degree_level": "Bachelor",
            "ielts_overall": ielts,
        },
        "evidence": [
            {
                "field_key": "ielts_overall",
                "value": ielts,
                "method": method,
                "confidence": conf,
            }
        ],
    }


def test_p4_only_high_precision_methods_seed_cache():
    """vision_ocr / ai_fallback values must NOT vote in the bucket cache;
    only regex / css_selector / gemini_primary at conf ≥ 0.7 may."""
    results = [
        _make_result("https://u.edu/c1", "Bachelor of A", 6.5, "regex", 0.95),
        _make_result("https://u.edu/c2", "Bachelor of B", 6.5, "regex", 0.95),
        # Disallowed methods — must be filtered out:
        _make_result("https://u.edu/c3", "Bachelor of C", 8.0, "vision_ocr", 0.99),
        _make_result("https://u.edu/c4", "Bachelor of D", 8.0, "ai_fallback", 0.99),
        # Allowed method but below confidence threshold:
        _make_result("https://u.edu/c5", "Bachelor of E", 8.0, "regex", 0.5),
    ]
    cache, _origins, prov = sc._build_bucket_cache(results, min_quorum=2)
    assert cache.get("undergraduate", {}).get("ielts_overall") == 6.5, (
        f"vision_ocr / ai_fallback / low-conf values should not have "
        f"polluted the bucket — got {cache!r}"
    )
    # Provenance must point back to a high-precision source.
    assert prov["undergraduate"]["ielts_overall"]["source_method"] == "regex"
    assert prov["undergraduate"]["ielts_overall"]["consensus_count"] == 2


def test_p4_can_seed_cache_helper():
    assert sc._can_seed_cache("regex", 0.95) is True
    assert sc._can_seed_cache("css_selector", 0.7) is True
    assert sc._can_seed_cache("gemini_primary", 0.8) is True
    assert sc._can_seed_cache("vision_ocr", 1.0) is False
    assert sc._can_seed_cache("ai_fallback", 1.0) is False
    assert sc._can_seed_cache("regex", 0.69) is False


# ────────────────────────────────────────────────────────────────────
# Prompt 5 — AI fallback prompt CRITICAL RULES block
# ────────────────────────────────────────────────────────────────────
def test_p5_critical_rules_block_present_in_prompt_template():
    tmpl = af._PROMPT_TEMPLATE
    assert "CRITICAL RULES:" in tmpl
    assert "PRIORITY ORDER for missing fields" in tmpl
    assert "NEVER OVERRIDE existing extraction" in tmpl
    assert "NEVER GUESS" in tmpl
    assert "SECTION HEADERS are NOT values" in tmpl
    assert "non-negotiable" in tmpl


def test_p5_critical_rules_appears_before_field_block_placeholder():
    tmpl = af._PROMPT_TEMPLATE
    assert tmpl.index("CRITICAL RULES:") < tmpl.index("{fields_block}"), (
        "CRITICAL RULES must precede the field schema so the model reads "
        "the rules before the per-field hints."
    )


# ────────────────────────────────────────────────────────────────────
# Prompt 6 — default ``min_quorum`` is now 2
# ────────────────────────────────────────────────────────────────────
def test_p6_default_min_quorum_is_two_in_build_bucket_cache():
    import inspect
    sig = inspect.signature(sc._build_bucket_cache)
    assert sig.parameters["min_quorum"].default == 2


def test_p6_default_min_quorum_is_two_in_backfill():
    import inspect
    sig = inspect.signature(sc.backfill_english_from_siblings)
    assert sig.parameters["min_quorum"].default == 2


def test_p6_single_source_does_not_propagate():
    """A single course extracting an IELTS value must NOT seed the cache
    when the global default of min_quorum=2 is in effect."""
    results = [
        _make_result("https://u.edu/c1", "Bachelor of A", 6.5, "regex", 0.95),
        # Sibling that did NOT extract — should remain null after backfill.
        {
            "url": "https://u.edu/c2",
            "payload": {
                "course_name": "Bachelor of B",
                "degree_level": "Bachelor",
            },
            "evidence": [],
        },
    ]
    fills = asyncio.run(sc.backfill_english_from_siblings(results))
    assert fills == 0, "Single-source bucket must not backfill under quorum=2"
    assert results[1]["payload"].get("ielts_overall") in (None, ""), (
        f"Sibling should remain unfilled — got {results[1]['payload']!r}"
    )


def test_p6_two_source_consensus_does_propagate():
    """Two siblings agreeing on an IELTS value SHOULD seed and backfill."""
    results = [
        _make_result("https://u.edu/c1", "Bachelor of A", 6.5, "regex", 0.95),
        _make_result("https://u.edu/c2", "Bachelor of B", 6.5, "regex", 0.95),
        {
            "url": "https://u.edu/c3",
            "payload": {
                "course_name": "Bachelor of C",
                "degree_level": "Bachelor",
            },
            "evidence": [],
        },
    ]
    fills = asyncio.run(sc.backfill_english_from_siblings(results))
    assert fills == 1
    assert results[2]["payload"]["ielts_overall"] == 6.5


# ────────────────────────────────────────────────────────────────────
# Prompt 7 Part B — tier-1 vision OCR English suppressed globally
# ────────────────────────────────────────────────────────────────────
def test_p7b_trust_tier1_vision_ocr_english_default_is_false():
    """Default schema value flips from True → False so tier-1 vision
    OCR for English fields is suppressed unless a per-uni YAML opts in."""
    cfg = EnglishConfig()
    assert cfg.trust_tier1_vision_ocr_english is False, (
        "Week 1 Prompt 7 Part B requires the global default to be False."
    )


# ────────────────────────────────────────────────────────────────────
# Prompt 8 — AI fallback page-text validation
# ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "field,value,page_text,expected",
    [
        # Fees: digits or comma-grouped form must appear.
        ("international_fee", 38950, "Annual fee: $38,950 AUD", True),
        ("international_fee", 38950, "Annual fee: $38950 AUD", True),
        ("international_fee", 38950, "Fees vary by major", False),
        ("domestic_fee", 12000, "csp $12,000", True),
        # IELTS: integer scores must accept "6" and "6.0" variants.
        ("ielts_overall", 6.5, "IELTS overall 6.5 with no band below 6.0", True),
        ("ielts_overall", 6, "IELTS overall 6.0 with all sub-bands 5.5", True),
        ("ielts_overall", 7.5, "Minimum IELTS 6.5", False),
        # Categorical: every token must appear.
        ("course_location", "Sydney, Melbourne", "Campuses: Sydney and Melbourne", True),
        ("course_location", "Sydney, Brisbane", "Campuses: Sydney and Melbourne", False),
        # intake_months: every month name must appear.
        ("intake_months", ["March", "July"], "Intakes: March and July", True),
        ("intake_months", ["March", "November"], "Intakes: March and July", False),
        # Pass-through cases.
        ("international_fee", None, "anything", True),
        ("course_location", "", "anything", True),
        ("unknown_field", "anything", "anything", True),
    ],
)
def test_p8_validate_ai_fallback_value(field, value, page_text, expected):
    assert af.validate_ai_fallback_value(field, value, page_text) is expected, (
        f"validate_ai_fallback_value({field!r}, {value!r}) returned "
        f"{not expected} for page text {page_text!r}; expected {expected}"
    )


def test_p8_empty_page_text_does_not_reject():
    """When page text is empty (extractor produced no text), the
    validator must not blanket-reject — it should pass-through so a
    legitimate AI value is not lost."""
    assert af.validate_ai_fallback_value("international_fee", 38950, "") is True
