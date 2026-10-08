import re
from types import SimpleNamespace

from app.services.scraper.verified_url_patterns import proposed_allow_patterns
from app.services.scraper.ai_repair_agent import _simulate_filter, _validated_ai_patches
from app.services.ai_repair_workflow import compare_quality


URLS = [
    "https://www.brighton.ac.uk/courses/study/advanced-manufacturing-msc.aspx",
    "https://www.brighton.ac.uk/courses/study/aerospace-engineering-beng-hons.aspx",
]


def test_brighton_total_loss_recovers_sibling_course_without_opening_other_hosts():
    patterns = proposed_allow_patterns(URLS, [r"/obsolete-courses/"])
    assert patterns
    sibling = "https://www.brighton.ac.uk/courses/study/aerospace-engineering-meng.aspx"
    assert _simulate_filter(URLS + [sibling], patterns, [])["after"] == 3
    for url in [
        "https://www.brighton.edu.au/courses/study/aerospace-engineering-meng.aspx",
        "https://www.brighton.ac.uk.evil.test/courses/study/engineering.aspx",
        "https://www.brighton.ac.uk/courses/study/",
        "https://www.brighton.ac.uk/news/aerospace-engineering-meng.aspx",
        "https://www.brighton.ac.uk/courses/study/engineering.aspx/related",
    ]:
        assert not any(re.search(pattern, url) for pattern in patterns)
    # Mandatory block and final detail gates still win.
    assert _simulate_filter(URLS, patterns, [r"aerospace"])["after"] == 1
    assert _simulate_filter(URLS, patterns, [], [], [r"/unrelated/"])["after"] == 0
    assert proposed_allow_patterns(URLS, patterns) is None


def test_single_course_queries_and_shallow_paths_cannot_generalise():
    assert proposed_allow_patterns(URLS[:1], []) is None
    assert proposed_allow_patterns([URLS[0], URLS[0] + "?year=2027"], []) is None
    assert proposed_allow_patterns(["https://uni.test/study/a", "https://uni.test/study/b"], []) is None
    assert proposed_allow_patterns(["https://uni.test/a/b/c.pdf", "https://uni.test/a/b/d.pdf"], []) is None


def test_existing_working_rules_are_preserved_and_schema_accepts_proposal():
    patterns = proposed_allow_patterns(URLS, ["/postgraduate/"])
    assert patterns[0] == "/postgraduate/"
    validated = _validated_ai_patches({"patches": [{
        "section": "discovery", "field": "allow_url_patterns",
        "action": "replace", "value": patterns,
    }]})
    assert validated


def test_partial_progress_is_measured_without_certifying_missing_fields():
    child = SimpleNamespace(
        status="completed", imported=50, current=50, total_found=50,
        skipped=0, errors=0, cost_ceiling_hit=False,
        discovered_config={}, gate_skip_counts={},
    )
    after = dict.fromkeys([
        "fee_pct", "ielts_pct", "location_pct", "duration_pct",
        "course_name_pct", "intakes_pct", "mode_pct", "degree_level_pct",
    ], 100)
    after.update(ielts_pct=98, mode_pct=80, total_staged=50)
    result = compare_quality({"fee_pct": 0, "ielts_pct": 70}, after, child)
    assert result["improved_fields"] == ["fee_pct", "ielts_pct"]
    assert result["unresolved_fields"] == ["ielts_pct", "mode_pct"]
    assert result["sample_verified"] is False
    assert result["full_catalogue_verified"] is False
    assert compare_quality({}, after, child)["improved_fields"] == []
