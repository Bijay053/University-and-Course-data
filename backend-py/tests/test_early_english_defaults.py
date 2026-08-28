"""Regression coverage for opt-in English defaults before remote enrichment."""
from types import SimpleNamespace

from app.services.scraper.pipelines.single_course import (
    _resolve_configured_english_defaults,
)


def _config(**overrides):
    values = {
        "default_ielts": 6.0,
        "default_pte": 60,
        "default_toefl": 80,
        "degree_level_defaults": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_flat_institutional_defaults_resolve_for_law_course():
    payload = {"degree_level": "Master"}
    assert _resolve_configured_english_defaults(payload, _config()) == {
        "ielts_overall": 6.0,
        "pte_overall": 60,
        "toefl_overall": 80,
    }


def test_degree_level_defaults_take_precedence_over_flat_values():
    postgraduate = SimpleNamespace(ielts=6.5, pte=58, toefl=90)
    config = _config(degree_level_defaults={"postgraduate": postgraduate})
    assert _resolve_configured_english_defaults(
        {"degree_level": "Master"},
        config,
    ) == {
        "ielts_overall": 6.5,
        "pte_overall": 58,
        "toefl_overall": 90,
    }