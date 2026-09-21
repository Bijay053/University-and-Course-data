"""Regression tests for repair candidates on small all-filtered jobs."""

from app.services.scraper.auto_repair_candidates import (
    AutoRepairEngine,
    filter_config_fingerprint,
    filter_config_drifted,
    filter_repair_safety_issue,
    strip_stale_filter_suggestions,
    is_intentionally_excluded_course_url,
)


def test_law_online_variants_are_not_missing_course_evidence():
    online = "https://www.law.ac.uk/study/postgraduate/law/llm/online/"
    campus = "https://www.law.ac.uk/study/postgraduate/law/llm/"
    assert is_intentionally_excluded_course_url(online)
    assert not is_intentionally_excluded_course_url(campus)
    assert not is_intentionally_excluded_course_url(online.replace("www.law.ac.uk", "example.edu"))
    engine = AutoRepairEngine(
        uni_id=1, uni_name="Law", scrape_url="https://www.law.ac.uk",
        current_allow_pats=[r"/study/postgraduate/[^/]+/[^/]+/$"],
        current_must_contain=[], current_block_pats=[r"/news/"],
        raw_discovered=180, after_filter=94, imported=94,
        historical_urls=[campus, online], pipeline_stats={}, dropped_sample=[online],
    )
    assert engine.historical_urls == [campus]
    assert engine.dropped_sample == []
    assert engine._url_filter_candidates() == []
    assert engine._smart_replace_allow_patterns() == []
    assert engine.block_pats == [r"/news/"]


def test_filter_snapshot_drift_is_detected_and_missing_snapshot_is_safe():
    run_config = {
        "allow_url_patterns": [r"/Programme/Course/[^/?#]+"],
        "must_contain": [],
        "block_url_patterns": [],
        "course_detail_url_patterns": [],
    }

    assert filter_config_drifted(run_config, run_config.copy()) is False
    assert filter_config_drifted(
        run_config,
        {
            **run_config,
            "allow_url_patterns": [],
        },
    ) is True
    assert filter_config_drifted(None, run_config) is False
    assert filter_config_drifted({}, {}) is False
    assert "legacy job" in (filter_repair_safety_issue({}, run_config) or "")
    assert filter_repair_safety_issue(
        {},
        {},
        snapshot_present=True,
    ) is None
    assert filter_repair_safety_issue(
        {},
        {"must_contain": ["/courses/"]},
        snapshot_present=True,
    ) is not None
    assert filter_config_fingerprint(run_config) == filter_config_fingerprint(
        {
            "course_detail_url_patterns": [],
            "block_url_patterns": [],
            "must_contain": [],
            "allow_url_patterns": [r"/Programme/Course/[^/?#]+"],
        }
    )


def test_stale_snapshot_blocks_clear_filter_recommendations():
    reason = filter_repair_safety_issue(
        {"allow_url_patterns": [r"/old-course/"]},
        {"allow_url_patterns": [r"/Programme/Course/[^/?#]+"], "must_contain": []},
    )

    assert reason is not None
    assert "No filter-clearing recommendation is safe" in reason

    sanitized = strip_stale_filter_suggestions(
        {
            "discovery": {
                "allow_url_patterns": [],
                "course_detail_url_patterns": [],
                "bfs_page_budget": 80,
            },
            "extraction": {"render": True},
        },
        reason,
    )
    assert sanitized == {
        "discovery": {"bfs_page_budget": 80},
        "extraction": {"render": True},
    }


def test_small_all_filtered_job_gets_url_filter_candidates():
    engine = AutoRepairEngine(
        uni_id=1,
        uni_name="Example University",
        scrape_url="https://example.edu/courses",
        current_allow_pats=[r"/wrong-path/"],
        current_must_contain=[],
        current_block_pats=[],
        raw_discovered=4,
        after_filter=0,
        imported=0,
        historical_urls=[],
        pipeline_stats={},
        dropped_sample=["https://example.edu/courses/arts"],
    )

    candidates = engine.generate_candidates()

    assert any(candidate.id == "clear_allow_patterns" for candidate in candidates)


def test_course_detail_gate_has_a_targeted_repair_candidate():
    engine = AutoRepairEngine(
        uni_id=1,
        uni_name="Example University",
        scrape_url="https://example.edu/courses",
        current_allow_pats=[],
        current_must_contain=[],
        current_block_pats=[],
        current_course_detail_pats=[r"/programmes/[^/]+$"],
        raw_discovered=2,
        after_filter=0,
        imported=0,
        historical_urls=[
            "https://example.edu/courses/arts",
            "https://example.edu/courses/law",
        ],
        pipeline_stats={},
        dropped_sample=["https://example.edu/courses/arts"],
    )

    candidates = engine.generate_candidates()
    detail_fix = next(
        candidate
        for candidate in candidates
        if candidate.id == "clear_course_detail_patterns"
    )

    assert detail_fix.recipe_patch == {
        "discovery": {"course_detail_url_patterns": []}
    }
    assert detail_fix.simulation.after_count == 2


def test_smart_allow_replacement_keeps_existing_must_block_detail_gates():
    dropped = [
        "https://example.edu/Programme/Course/arts",
        "https://example.edu/Programme/Course/law",
        "https://example.edu/Programme/Course/engineering",
    ]
    engine = AutoRepairEngine(
        uni_id=1,
        uni_name="Example University",
        scrape_url="https://example.edu",
        current_allow_pats=[r"/old-course/"],
        current_must_contain=["/never-matches/"],
        current_block_pats=[r"/blocked/"],
        current_course_detail_pats=[r"/detail/[^/]+$"],
        raw_discovered=3,
        after_filter=0,
        imported=0,
        historical_urls=[],
        pipeline_stats={},
        dropped_sample=dropped,
    )

    smart = next(
        candidate
        for candidate in engine.generate_candidates()
        if candidate.id == "smart_replace_patterns"
    )

    assert smart.simulation.after_count == 0
    assert smart.safety_gate_passed is False