from app.services.scraper.repair_quality_cohort import matched_comparison, course_presence


def test_different_cohort_percentages_are_not_improvements_or_regressions():
    baseline = {"total_staged": 88, "fee_pct": 2, "ielts_pct": 97,
                "course_presence": {"course-a": [True] * 8, "course-b": [False] * 8}}
    verified = {"total_staged": 1, "fee_pct": 100, "ielts_pct": 100,
                "course_presence": {"course-a": [True] * 8}}
    result = matched_comparison(baseline, verified)
    assert result["regressions"] == result["improved_fields"] == []
    assert result["comparison_verified"]


def test_real_loss_is_not_hidden_by_another_courses_gain():
    before = {"course_presence": {"a": [True] * 8, "b": [False] * 8}}
    after = {"total_staged": 2, "course_presence": {"a": [False] * 8, "b": [True] * 8}}
    result = matched_comparison(before, after)
    assert "fee_pct" in result["regressions"]
    assert result["improved_fields"] == []
    assert result["recovered_field_counts"]["fee_pct"] == 1
    assert result["lost_field_counts"]["fee_pct"] == 1


def test_legacy_or_unmatched_baseline_is_unverified_not_a_false_improvement():
    result = matched_comparison({"fee_pct": 2}, {"total_staged": 31, "fee_pct": 6})
    assert not result["comparison_verified"]
    assert result["regressions"] == result["improved_fields"] == []


def test_duplicate_identity_cannot_silently_overwrite_evidence():
    row = {"url": "https://example.ac.uk/course/a/", "name": "Degree",
           "location": "City", "fields": [True] * 8}
    assert course_presence([row, {**row, "fields": [False] * 8}]) == {}
