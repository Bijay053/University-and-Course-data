"""Tests for Manchester scrape speed optimisations (Task #251).

Covers:
  1. max_admission_linked_pages is 2, not 3
  2. intake.default_confidence is 0.75 (gate-eligible)
  3. YAML IntakeConfig.default_confidence field exists and is YAML-settable
  4. single_course.py uses default_confidence (not hardcoded 0.4) in evidence
  5. Gemini gate fires classification_only when intake default is 0.75 + 4 other fields filled
  6. Gemini gate still fires full_extraction when intake default stays 0.4
"""
from __future__ import annotations

import sys
import os
import types
import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so we can import config modules without heavy dependencies
# ---------------------------------------------------------------------------
def _make_stub(name: str) -> types.ModuleType:
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m

for _mod in [
    "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
    "sqlalchemy.orm", "sqlalchemy.dialects", "sqlalchemy.dialects.postgresql",
    "celery", "celery.utils", "celery.utils.log",
    "redis", "redis.asyncio",
]:
    if _mod not in sys.modules:
        _make_stub(_mod)

# ---------------------------------------------------------------------------
# Test 1 & 2: YAML config loads correct values for Manchester
# ---------------------------------------------------------------------------

def test_manchester_yaml_max_admission_linked_pages():
    """max_admission_linked_pages must be 2 (reduced from 3 for speed)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="manchester",
        name="The University of Manchester",
        scrape_url="https://www.manchester.ac.uk/study/undergraduate/courses/list/",
    )
    assert cfg.extraction.max_admission_linked_pages == 2, (
        f"Expected 2, got {cfg.extraction.max_admission_linked_pages}. "
        "Reducing from 3 saves ~934 Scrape.do calls per run."
    )


def test_manchester_yaml_intake_default_confidence():
    """intake.default_confidence must be 0.75 (≥ Gemini gate threshold 0.70)."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="manchester",
        name="The University of Manchester",
        scrape_url="https://www.manchester.ac.uk/study/undergraduate/courses/list/",
    )
    conf = cfg.extraction.intake.default_confidence
    assert conf == 0.75, (
        f"Expected 0.75, got {conf}. "
        "0.75 > 0.70 (CONFIDENCE_THRESHOLD) so intake defaults count toward the gate."
    )


def test_manchester_yaml_other_speed_flags():
    """All existing speed flags still present and correct."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from app.services.scraper.config.loader import load_uni_config

    cfg = load_uni_config(
        slug="manchester",
        name="The University of Manchester",
        scrape_url="https://www.manchester.ac.uk/study/undergraduate/courses/list/",
    )
    assert cfg.extraction.max_parallel_fetch == 16
    assert cfg.extraction.follow_admission_links is True
    assert cfg.extraction.skip_browser_rescue is True
    assert cfg.extraction.skip_per_course_browser is True
    assert cfg.extraction.english.trust_vision_ocr is False


# ---------------------------------------------------------------------------
# Test 3: IntakeConfig.default_confidence schema field exists
# ---------------------------------------------------------------------------

def test_intake_config_default_confidence_schema_field():
    """IntakeConfig must have a default_confidence field defaulting to 0.4."""
    from app.services.scraper.config.schema import IntakeConfig

    cfg = IntakeConfig()
    assert hasattr(cfg, "default_confidence"), (
        "IntakeConfig is missing default_confidence field"
    )
    assert cfg.default_confidence == 0.4, (
        f"Default should be 0.4 (conservative), got {cfg.default_confidence}"
    )


def test_intake_config_default_confidence_can_be_set():
    """IntakeConfig.default_confidence can be set to any float (e.g. 0.75)."""
    from app.services.scraper.config.schema import IntakeConfig

    cfg = IntakeConfig(default_confidence=0.75)
    assert cfg.default_confidence == 0.75


# ---------------------------------------------------------------------------
# Test 4: single_course.py uses configurable confidence (not hardcoded 0.4)
# ---------------------------------------------------------------------------

def test_yaml_default_intake_uses_configurable_confidence():
    """single_course._apply_intake_default produces evidence with the YAML confidence."""
    import importlib
    import ast

    sc_path = os.path.join(
        os.path.dirname(__file__),
        "..", "app", "services", "scraper", "pipelines", "single_course.py",
    )
    with open(sc_path, encoding="utf-8") as fh:
        src = fh.read()

    # The evidence row for yaml_default_intake must NOT use a bare literal 0.4
    # — it must read from a variable (default_confidence from config).
    assert '"confidence": 0.4,' not in src or "_intk_conf" in src, (
        'single_course.py still has hardcoded "confidence": 0.4 '
        "in the yaml_default_intake evidence row. Use _intk_conf instead."
    )
    assert "_intk_conf" in src, (
        "single_course.py does not contain _intk_conf variable — "
        "default_confidence from IntakeConfig is not being used."
    )
    assert 'getattr(_intk_cfg, "default_confidence"' in src, (
        "single_course.py does not read default_confidence from _intk_cfg"
    )


# ---------------------------------------------------------------------------
# Test 5 & 6: Gemini gate logic with configurable intake confidence
# ---------------------------------------------------------------------------

def test_gemini_gate_fires_classification_only_when_intake_at_075():
    """Gate fires classification_only when intake confidence is 0.75 (≥ 0.70 threshold)."""
    from app.services.scraper.gemini_gate import should_skip_gemini_primary

    payload = {
        "course_name": "BSc Computer Science",
        "international_fee": 29000,
        "ielts_overall": 6.5,
        "duration": "3 years",
        "intake_months": ["September"],
        "study_mode": "On Campus",
        "category": None,          # still missing → classification_only, not skip
        "sub_category": None,
    }
    evidence = [
        {"field_key": "course_name",       "confidence": 0.90},
        {"field_key": "international_fee", "confidence": 0.85},
        {"field_key": "ielts_overall",     "confidence": 0.85},
        {"field_key": "duration",          "confidence": 0.80},
        {"field_key": "intake_months",     "confidence": 0.75},  # above threshold
        {"field_key": "study_mode",        "confidence": 0.80},
    ]
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert not skip, "Should not skip entirely (category missing)"
    assert reason == "classification_only", (
        f"Expected 'classification_only', got {reason!r}. "
        "All 6 non-class fields at ≥0.70 confidence → cheap prompt."
    )


def test_gemini_gate_fires_full_extraction_when_intake_at_040():
    """Gate fires full_extraction when intake confidence is 0.40 (< 0.70 threshold)."""
    from app.services.scraper.gemini_gate import should_skip_gemini_primary

    payload = {
        "course_name": "BSc Computer Science",
        "international_fee": 29000,
        "ielts_overall": 6.5,
        "duration": "3 years",
        "intake_months": ["September"],
        "study_mode": "On Campus",
        "category": None,
        "sub_category": None,
    }
    evidence = [
        {"field_key": "course_name",       "confidence": 0.90},
        {"field_key": "international_fee", "confidence": 0.85},
        {"field_key": "ielts_overall",     "confidence": 0.85},
        {"field_key": "duration",          "confidence": 0.80},
        {"field_key": "intake_months",     "confidence": 0.40},  # below threshold → not counted
        {"field_key": "study_mode",        "confidence": 0.80},
    ]
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert not skip
    assert reason == "full_extraction_needed", (
        f"Expected 'full_extraction_needed', got {reason!r}. "
        "intake_months at 0.40 < 0.70 → only 5/6 fields counted → 83% < 90% floor."
    )


def test_gemini_gate_skip_all_when_intake_at_075_with_category():
    """Gate fires skip entirely when intake at 0.75 AND category is populated."""
    from app.services.scraper.gemini_gate import should_skip_gemini_primary

    payload = {
        "course_name": "BSc Computer Science",
        "international_fee": 29000,
        "ielts_overall": 6.5,
        "duration": "3 years",
        "intake_months": ["September"],
        "study_mode": "On Campus",
        "category": "IT & Computer Science",
        "sub_category": "Computer Science",
    }
    evidence = [
        {"field_key": "course_name",       "confidence": 0.90},
        {"field_key": "international_fee", "confidence": 0.85},
        {"field_key": "ielts_overall",     "confidence": 0.85},
        {"field_key": "duration",          "confidence": 0.80},
        {"field_key": "intake_months",     "confidence": 0.75},
        {"field_key": "study_mode",        "confidence": 0.80},
    ]
    skip, reason = should_skip_gemini_primary(payload, evidence)
    assert skip is True, f"Expected skip=True, got skip={skip}, reason={reason!r}"
    assert reason == "all_high_value_fields_populated"
