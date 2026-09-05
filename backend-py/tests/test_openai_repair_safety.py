"""Safety regressions for the operator-facing OpenAI scrape repair agent."""

from __future__ import annotations

import pytest

from app.services.scraper.ai_repair_agent import (
    PatchValidationError,
    _apply_to_yaml,
    _restore_yaml,
    _simulate_filter,
    _validate_and_build_config_patch,
    _validated_ai_patches,
    validate_url_repair_target,
)
from app.services.scraper.config.loader import get_config_for_host


def test_openai_can_clear_each_url_filter_gate() -> None:
    discovery, extraction, errors = _validate_and_build_config_patch(
        [
            {
                "section": "discovery",
                "field": field,
                "action": "replace",
                "value": [],
            }
            for field in (
                "allow_url_patterns",
                "block_url_patterns",
                "must_contain",
                "course_detail_url_patterns",
            )
        ]
    )

    assert extraction == {}
    assert errors == []
    assert discovery == {
        "allow_url_patterns": [],
        "block_url_patterns": [],
        "must_contain": [],
        "course_detail_url_patterns": [],
    }


def test_full_filter_simulation_includes_must_contain_and_detail_gate() -> None:
    urls = [
        "https://www.unisc.edu.au/study/courses-and-programs/postgraduate-degrees/master-of-business",
        "https://www.unisc.edu.au/study/courses-and-programs/bachelor-degrees-undergraduate-programs/bachelor-of-arts",
        "https://www.unisc.edu.au/study/courses-and-programs/courses/search-for-unisc-courses",
    ]

    blocked = _simulate_filter(
        urls,
        allow_pats=[r"/study/"],
        block_pats=[r"/search-for-"],
        must_contain=["/courses-and-programs/"],
        course_detail_pats=[r"/programmes/[^/]+$"],
    )
    repaired = _simulate_filter(
        urls,
        allow_pats=[r"/study/"],
        block_pats=[r"/search-for-"],
        must_contain=["/courses-and-programs/"],
        course_detail_pats=[
            r"/study/courses-and-programs/[a-z][a-z-]+/[a-z][a-z-]+/?$"
        ],
    )

    assert blocked["after"] == 0
    assert repaired["after"] == 2
    assert repaired["total"] == 3


def test_invalid_openai_envelope_is_rejected_before_patch_processing() -> None:
    with pytest.raises(PatchValidationError, match="patches list"):
        _validated_ai_patches({"confidence": 90})
    with pytest.raises(PatchValidationError, match="more than 3"):
        _validated_ai_patches(
            {"confidence": 90, "patches": [{}, {}, {}, {}]}
        )
    with pytest.raises(PatchValidationError, match="integer from 0 to 100"):
        _validated_ai_patches({"confidence": 101, "patches": []})


def test_yaml_apply_preserves_explicit_empty_list_and_can_rollback(tmp_path) -> None:
    yaml_file = tmp_path / "portable_1.yaml"
    original = (
        "discovery:\n"
        "  allow_url_patterns:\n"
        "    - /broken/\n"
        "  must_contain:\n"
        "    - /obsolete/\n"
    )
    yaml_file.write_text(original, encoding="utf-8")

    path, previous = _apply_to_yaml(
        yaml_file,
        tmp_path,
        1,
        "https://portable.edu",
        {
            "discovery": {
                "allow_url_patterns": [],
                "must_contain": [],
            }
        },
    )

    updated = path.read_text(encoding="utf-8")
    assert "allow_url_patterns: []" in updated
    assert "must_contain: []" in updated

    _restore_yaml(path, previous)
    assert path.read_text(encoding="utf-8") == original


def test_repair_target_requires_terminal_job_with_filter_failure_evidence() -> None:
    evidence = {
        "pipeline_stats": {
            "raw_discovered": 142,
            "after_filter": 0,
            "dropped_sample": [
                "https://www.unisc.edu.au/study/courses-and-programs/"
                "postgraduate-degrees/master-of-business"
            ],
        }
    }

    assert validate_url_repair_target("running", evidence)[0] is False
    assert validate_url_repair_target("completed", evidence)[0] is True
    assert validate_url_repair_target(
        "completed",
        {
            "pipeline_stats": {
                "raw_discovered": 142,
                "after_filter": 140,
                "dropped_sample": evidence["pipeline_stats"]["dropped_sample"],
            }
        },
    )[0] is False
    assert validate_url_repair_target(
        "completed",
        {"pipeline_stats": {"raw_discovered": 142, "after_filter": 0}},
    )[0] is False


def test_unisc_verified_recipe_wins_for_recreated_database_id() -> None:
    config = get_config_for_host(
        hostname="www.unisc.edu.au",
        name="University of the Sunshine Coast",
        scrape_url="https://www.unisc.edu.au",
        university_id=20,
        db_scrape_config={
            "admin_config": {
                "discovery": {
                    "allow_url_patterns": [r"^/study/.+"],
                    "block_url_patterns": [r"/courses-and-programs/"],
                    "seed_urls": ["https://www.unisc.edu.au/wrong-listing"],
                    "sitemap_url": "https://www.unisc.edu.au/wrong.xml",
                }
            }
        },
    )

    assert config.discovery.allow_url_patterns == [
        r"/study/courses-and-programs/[a-z][a-z-]+/[a-z][a-z-]+/?$"
    ]
    assert "/courses-and-programs/" not in config.discovery.block_url_patterns
    assert config.discovery.sitemap_url == "https://www.unisc.edu.au/XMLsitemap"
    assert config.discovery.seed_urls[0].endswith("/study/courses-and-programs")