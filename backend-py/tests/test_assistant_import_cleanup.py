from copy import deepcopy
from datetime import datetime, timezone, timedelta
from types import SimpleNamespace

import pytest

from app.models import ScrapedCourse
from app.services.assistant_import_cleanup import pairs, pristine, requested
from app.services.assistant_pending_automation import supported
from app.services.scraper.pending_imports import ATTEMPT


NOW = datetime.now(timezone.utc)


def row(id, job, **updates):
    result = ScrapedCourse(**{
        "id": id, "university_id": 114, "scrape_job_id": job,
        "course_name": "PGCE Secondary Mathematics",
        "course_website": "https://example.edu/courses/pgce",
        "degree_level": "Postgraduate", "study_load": "Full Time",
        "course_location": "Cyncoed", "study_mode": "On Campus",
        "fee_year": 2027, "fee_term": "Full Course", "currency": "GBP",
        "status": "pending", "created_at": NOW + timedelta(minutes=1)
        if job == "new" else NOW - timedelta(days=1), **updates,
    })
    for column in ScrapedCourse.__table__.columns:
        if getattr(result, column.key) is None and column.default is not None and column.default.is_scalar:
            setattr(result, column.key, column.default.arg)
    return result


def snapshot(r):
    return SimpleNamespace(scrape_job_id=r.scrape_job_id, snapshot_type=ATTEMPT,
                           original_extraction={
        "schema": 1, "offeringIdentities": [{"courseId": r.id, "identity": "source"}],
        "incoming": [{
            **{c.key: deepcopy(getattr(r, c.key)) for c in ScrapedCourse.__table__.columns},
            "_snapshot_schema": "staged_row_v1",
        }], "reused": [],
    })


def job(**updates):
    return SimpleNamespace(**{
        "runtime_job_id": "new", "status": "completed", "started_at": NOW,
        "request_payload": {"fullCatalogueReviewOnly": True}, **updates,
    })


def test_only_untouched_new_copy_is_selected_and_original_remains_unchanged():
    original, copy = row(1, "old"), row(2, "new", study_load=None)
    saved = snapshot(copy)
    before = {c.key: deepcopy(getattr(original, c.key)) for c in ScrapedCourse.__table__.columns}
    assert pairs([original, copy], [saved], job()) == [(copy, original)]
    assert {c.key: getattr(original, c.key) for c in ScrapedCourse.__table__.columns} == before


@pytest.mark.parametrize("update", [
    {"notes": "Staff reviewed"}, {"fee_year": 2028},
    {"reviewed_at": NOW}, {"status": "approved"},
])
def test_changed_or_reviewed_copies_are_not_archived(update):
    original, copy = row(1, "old"), row(2, "new", study_load=None)
    saved = snapshot(copy)
    for key, value in update.items():
        setattr(copy, key, value)
    assert pairs([original, copy], [saved], job()) == []


def test_snapshot_must_belong_to_this_exact_row_and_import():
    copy = row(2, "new", study_load=None)
    saved = snapshot(copy)
    saved.original_extraction["offeringIdentities"][0]["courseId"] = 99
    assert not pristine(copy, [saved])
    saved = snapshot(copy)
    saved.scrape_job_id = "other"
    assert not pristine(copy, [saved])


def test_known_variants_and_ambiguous_originals_are_not_combined():
    original = row(1, "old")
    copy = row(2, "new", study_load=None, fee_year=2028)
    assert pairs([original, copy], [snapshot(copy)], job()) == []
    copy.fee_year = None
    another = row(3, "old", fee_year=2028)
    assert pairs([original, another, copy], [snapshot(copy)], job()) == []


def test_a_copy_with_any_subsequent_audit_is_not_archived():
    original, copy = row(1, "old"), row(2, "new", study_load=None)
    assert pairs([original, copy], [snapshot(copy)], job(), {copy.id}) == []


def test_two_new_variants_cannot_claim_the_same_incomplete_original():
    original = row(1, "old", fee_year=None)
    a, b = row(2, "new"), row(3, "new", fee_year=2028)
    assert pairs([original, a, b], [snapshot(a), snapshot(b)], job()) == []


@pytest.mark.parametrize("changes", [
    {"status": "running"}, {"request_payload": {}},
])
def test_active_or_non_review_import_is_not_a_cleanup_target(changes):
    with pytest.raises(ValueError, match="finished review-only"):
        pairs([], [], job(**changes))


def test_only_explicit_cleanup_intent_routes_here():
    assert requested("Archive duplicate copies from the latest full scan, retaining originals.")
    assert requested("Archive duplicate copies from the latest full scan. Do not publish anything.")
    assert not requested("Why are there duplicate rows from the last scan?")
    assert not requested("Do not remove duplicate records from the last scan.")
    assert not requested("Remove all online courses")


def test_automation_requires_fenced_unique_new_and_kept_rows():
    action = {
        "kind": "cleanup_import_duplicates", "state": "proposed", "courseCount": 1,
        "_sourceJobId": "new", "_universityId": 114,
        "_targets": [{"id": 2, "keepId": 1, "version": "copy", "keepVersion": "original"}],
    }
    assert supported(action)
    assert not supported({**action, "_targets": [{**action["_targets"][0], "keepId": 2}]})
    assert not supported({**action, "_sourceJobId": None})
