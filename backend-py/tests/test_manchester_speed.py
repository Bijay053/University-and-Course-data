"""Tests for Manchester scrape speed optimisations (Task #251).

Covers:
  1. max_admission_linked_pages is 2, not 3
  2. intake.default_confidence is 0.75 (gate-eligible)
  3. All other speed flags still correct
  4. IntakeConfig.default_confidence schema field exists and defaults to 0.4
  5. default_confidence can be set to non-default values via YAML/init
  6. single_course.py uses configurable confidence (not hardcoded 0.4)
  7. Gemini gate fires classification_only when intake default is 0.75 + 5 fields filled
  8. Gemini gate fires full_extraction when intake default stays 0.40
  9. Gemini gate skips entirely when intake at 0.75 + category also populated
"""
from __future__ import annotations

import os

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.config.schema import IntakeConfig
from app.services.scraper.gemini_gate import should_skip_gemini_primary


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _manchester_cfg():
    return load_uni_config(
        slug="manchester",
        name="The University of Manchester",
        scrape_url="https://www.manchester.ac.uk/study/undergraduate/courses/list/",
    )


def _evidence_at(intake_conf: float) -> list[dict]:
    return [
        {"field_key": "course_name",       "confidence": 0.90},
        {"field_key": "international_fee", "confidence": 0.85},
        {"field_key": "ielts_overall",     "confidence": 0.85},
        {"field_key": "duration",          "confidence": 0.80},
        {"field_key": "intake_months",     "confidence": intake_conf},
        {"field_key": "study_mode",        "confidence": 0.80},
    ]


def _base_payload() -> dict:
    return {
        "course_name": "BSc Computer Science",
        "international_fee": 29000,
        "ielts_overall": 6.5,
        "duration": "3 years",
        "intake_months": ["September"],
        "study_mode": "On Campus",
        "category": None,
        "sub_category": None,
    }


# ---------------------------------------------------------------------------
# 1–3: Manchester YAML values
# ---------------------------------------------------------------------------

def test_manchester_yaml_max_admission_linked_pages():
    """max_admission_linked_pages must be 2 (reduced from 3 for speed).

    Speed maths at 16 parallel slots / ~7 s per Scrape.do call:
      3 linked pages → 934 × 4 / 16 × 7 ≈ 1634 s ≈ 27 min
      2 linked pages → 934 × 3 / 16 × 7 ≈ 1225 s ≈ 20 min
    """
    cfg = _manchester_cfg()
    assert cfg.extraction.max_admission_linked_pages == 2, (
        f"Expected 2, got {cfg.extraction.max_admission_linked_pages}"
    )


def test_manchester_yaml_intake_default_confidence():
    """intake.default_confidence must be 0.75 (≥ Gemini gate threshold 0.70).

    The Gemini gate (gemini_gate.py) counts intake_months as 'populated'
    only when evidence confidence ≥ 0.70.  With the old default of 0.4,
    intake defaults never contributed — every Manchester course fell through
    to full_extraction_needed.  Setting 0.75 enables classification_only for
    courses where fee + IELTS + duration + name + mode are already found.
    """
    cfg = _manchester_cfg()
    assert cfg.extraction.intake.default_confidence == 0.75, (
        f"Expected 0.75, got {cfg.extraction.intake.default_confidence}"
    )


def test_manchester_yaml_other_speed_flags():
    """All existing speed flags still present and correct."""
    cfg = _manchester_cfg()
    assert cfg.extraction.max_parallel_fetch == 16
    assert cfg.extraction.follow_admission_links is True
    assert cfg.extraction.skip_browser_rescue is True
    assert cfg.extraction.skip_per_course_browser is True
    assert cfg.extraction.english.trust_vision_ocr is False


# ---------------------------------------------------------------------------
# 4–5: IntakeConfig schema field
# ---------------------------------------------------------------------------

def test_intake_config_default_confidence_schema_field():
    """IntakeConfig must have a default_confidence field defaulting to 0.4."""
    cfg = IntakeConfig()
    assert hasattr(cfg, "default_confidence"), (
        "IntakeConfig is missing default_confidence field"
    )
    assert cfg.default_confidence == 0.4, (
        f"Default should be 0.4 (conservative), got {cfg.default_confidence}"
    )


def test_intake_config_default_confidence_can_be_set():
    """IntakeConfig.default_confidence can be overridden (e.g. to 0.75)."""
    cfg = IntakeConfig(default_confidence=0.75)
    assert cfg.default_confidence == 0.75


# ---------------------------------------------------------------------------
# 6: single_course.py uses configurable confidence (source-text check)
# ---------------------------------------------------------------------------

def test_yaml_default_intake_uses_configurable_confidence():
    """single_course.py must read default_confidence from config, not hardcode 0.4."""
    sc_path = os.path.join(
        os.path.dirname(__file__),
        "..", "app", "services", "scraper", "pipelines", "single_course.py",
    )
    with open(sc_path, encoding="utf-8") as fh:
        src = fh.read()

    assert "_intk_conf" in src, (
        "single_course.py does not contain _intk_conf — "
        "default_confidence from IntakeConfig is not being used."
    )
    assert 'getattr(_intk_cfg, "default_confidence"' in src, (
        "single_course.py does not call getattr(_intk_cfg, 'default_confidence')"
    )
    assert '"confidence": _intk_conf,' in src, (
        'single_course.py evidence row does not use _intk_conf variable'
    )


# ---------------------------------------------------------------------------
# 7–9: Gemini gate behaviour at different intake confidence levels
# ---------------------------------------------------------------------------

def test_gemini_gate_fires_classification_only_when_intake_at_075():
    """Gate fires classification_only when intake confidence is 0.75 (≥ 0.70 threshold).

    With category=None the gate cannot fully skip, but all 6 non-class fields
    (course_name, international_fee, ielts_overall, duration, intake_months,
    study_mode) are at ≥ 0.70 → triggers cheap 80-token classification-only prompt.
    """
    skip, reason = should_skip_gemini_primary(_base_payload(), _evidence_at(0.75))
    assert not skip, "Should not skip entirely (category missing)"
    assert reason == "classification_only", (
        f"Expected 'classification_only', got {reason!r}"
    )


def test_gemini_gate_fires_full_extraction_when_intake_at_040():
    """Gate fires full_extraction when intake confidence is 0.40 (< 0.70 threshold).

    intake_months at 0.40 is below CONFIDENCE_THRESHOLD → not counted →
    5/6 non-class fields = 83% < 90% COVERAGE_FLOOR → full pass required.
    """
    skip, reason = should_skip_gemini_primary(_base_payload(), _evidence_at(0.40))
    assert not skip
    assert reason == "full_extraction_needed", (
        f"Expected 'full_extraction_needed', got {reason!r}"
    )


def test_gemini_gate_skip_all_when_intake_at_075_with_category():
    """Gate fires skip entirely when intake at 0.75 AND category is populated."""
    payload = {
        **_base_payload(),
        "category": "IT & Computer Science",
        "sub_category": "Computer Science",
    }
    skip, reason = should_skip_gemini_primary(payload, _evidence_at(0.75))
    assert skip is True, f"Expected skip=True, got skip={skip}, reason={reason!r}"
    assert reason == "all_high_value_fields_populated"
