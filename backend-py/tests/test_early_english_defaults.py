"""Regression coverage for opt-in English defaults before remote enrichment."""
from types import SimpleNamespace

import pytest

from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.pipelines.single_course import (
    _apply_english_defaults_before_remote_enrichment,
    _resolve_configured_english_defaults,
)


def _config(**overrides):
    values = {
        "default_ielts": 6.0,
        "default_pte": 60,
        "default_toefl": 80,
        "degree_level_defaults": {},
        "apply_defaults_before_remote_enrichment": True,
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


def test_research_master_uses_research_defaults_not_coursework_defaults():
    research = SimpleNamespace(ielts=6.5, pte=64, toefl=91)
    postgraduate = SimpleNamespace(ielts=6.0, pte=57, toefl=79)
    config = _config(
        degree_level_defaults={
            "postgraduate": postgraduate,
            "research": research,
        }
    )
    assert _resolve_configured_english_defaults(
        {
            "course_name": "Master of Education (Research)",
            "degree_level": "Master",
        },
        config,
    ) == {
        "ielts_overall": 6.5,
        "pte_overall": 64,
        "toefl_overall": 91,
    }


def _wlv_config(university_id: int):
    return load_uni_config(
        slug="wlv",
        name="University of Wolverhampton",
        scrape_url="https://www.wlv.ac.uk",
        university_id=university_id,
        create_missing_stub=False,
    )


@pytest.mark.parametrize("university_id", [74, 1761])
def test_both_wlv_recipes_opt_in_to_early_english_defaults(university_id):
    english = _wlv_config(university_id).extraction.english
    assert english.apply_defaults_before_remote_enrichment is True


@pytest.mark.parametrize(
    ("degree_level", "expected_ielts"),
    [
        ("Undergraduate", 6.0),
        ("Postgraduate", 6.5),
        ("Doctorate", 7.0),
    ],
)
def test_wlv_non_pathway_values_are_ready_before_remote_enrichment(
    degree_level,
    expected_ielts,
):
    payload = {
        "course_name": f"Example {degree_level} Course",
        "degree_level": degree_level,
    }
    evidence = []

    filled = _apply_english_defaults_before_remote_enrichment(
        payload,
        evidence,
        url="https://www.wlv.ac.uk/courses/example",
        english_config=_wlv_config(1761).extraction.english,
    )

    assert filled == ["ielts_overall"]
    assert payload["ielts_overall"] == expected_ielts
    assert evidence[0]["method"] == "uni_config:english_default"
    assert "before remote enrichment" in evidence[0]["snippet"]


def test_early_defaults_preserve_existing_course_ielts():
    payload = {
        "course_name": "Bachelor of Nursing",
        "degree_level": "Bachelor",
        "ielts_overall": 7.0,
    }
    evidence = []

    filled = _apply_english_defaults_before_remote_enrichment(
        payload,
        evidence,
        url="https://www.wlv.ac.uk/courses/nursing",
        english_config=_wlv_config(1761).extraction.english,
    )

    assert filled == []
    assert payload["ielts_overall"] == 7.0
    assert evidence == []


def test_foundation_pathway_guard_remains_unchanged():
    payload = {
        "course_name": "International Foundation Programme",
        "degree_level": "Foundation",
    }
    evidence = []

    filled = _apply_english_defaults_before_remote_enrichment(
        payload,
        evidence,
        url="https://www.wlv.ac.uk/courses/international-foundation-programme",
        english_config=_wlv_config(1761).extraction.english,
    )

    assert filled == []
    assert payload["is_pathway"] is True
    assert "ielts_overall" not in payload
    assert evidence == []
