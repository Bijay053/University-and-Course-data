"""Regression: 40 saved intake corrections were labelled 177 unchanged."""
from app.services.fix_field_changes import field_change_counts


def test_completed_aston_shape_preserves_issue_counters_but_reports_actual_writes():
    corrected = [{"ok": True, "attempted": True, "outcome": "no_progress",
                  "updated_fields": ["intake_months"], "target_fields": ["intake_months"],
                  "refreshed_evidence_fields": ["intake_months"]} for _ in range(40)]
    evidence = [{"ok": True, "attempted": True, "outcome": "no_progress",
                 "updated_fields": ["scrape_warnings"],
                 "refreshed_evidence_fields": ["intake_months"]} for _ in range(60)]
    unchanged = [{"ok": True, "outcome": "no_progress"} for _ in range(77)]
    failed = [{"ok": False, "outcome": "failed"} for _ in range(5)]
    assert field_change_counts(corrected + evidence + unchanged + failed, ["intake_months"]) == {
        "valueUpdated": 40, "valueUnchanged": 137, "evidenceRefreshedOnly": 60,
    }


def test_no_false_success_for_unrelated_fields_or_failed_verification():
    rows = [
        {"ok": True, "updated_fields": ["category", "scrape_warnings"]},
        {"ok": False, "updated_fields": ["intake_months"]},
        {"ok": True, "attempted": False, "updated_fields": ["intake_months"]},
        {"ok": True, "outcome": "already_resolved"},
        {"ok": True, "outcome": "skipped"},
    ]
    assert field_change_counts(rows, ["intake_months"]) == {
        "valueUpdated": 0, "valueUnchanged": 1, "evidenceRefreshedOnly": 0,
    }


def test_field_companions_and_english_bands_are_counted_once_per_course():
    assert field_change_counts([
        {"ok": True, "target_fields": ["ielts_overall"],
         "updated_fields": ["ielts_reading", "ielts_writing"]},
    ])["valueUpdated"] == 1
    assert field_change_counts([
        {"ok": True, "updated_fields": ["duration_term"]},
    ], ["duration"])["valueUpdated"] == 1


def test_pending_results_do_not_count_as_processed_and_empty_is_safe():
    assert field_change_counts([]) == {
        "valueUpdated": 0, "valueUnchanged": 0, "evidenceRefreshedOnly": 0,
    }


def test_durable_status_derives_historical_field_counts_without_changing_issue_counts():
    from types import SimpleNamespace
    from datetime import datetime, timezone
    from app.routers.scrape import _bulk_fix_job_dict

    job = SimpleNamespace(
        runtime_job_id="historical-fix", request_payload={"smart": True, "targetFields": ["intake_months"]},
        approval_summary={"counts": {"completed": 0, "failed": 0}, "results": [{
            "id": 1, "ok": True, "outcome": "no_progress", "attempted": True,
            "updated_fields": ["intake_months"],
        }]},
        status="completed", total_found=1, current=1, imported=0, skipped=0,
        errors=0, error_message=None, created_at=datetime.now(timezone.utc), completed_at=None,
    )
    result = _bulk_fix_job_dict(job)
    assert result["completed"] == 0
    assert result["noProgress"] == 1
    assert result["fieldChanges"]["valueUpdated"] == 1
