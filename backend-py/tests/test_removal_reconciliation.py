from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.scraper.removal_reconciliation import (
    decide_course_removal,
    get_removal_reconciliation,
)


def _result(*, scalars=None):
    result = MagicMock()
    result.scalars.return_value.all.return_value = scalars or []
    return result


@pytest.mark.asyncio
async def test_reconciliation_waits_until_all_staged_rows_are_reviewed():
    job = SimpleNamespace(
        university_id=4, status="completed", errors=0, runtime_job_id="job-1",
        request_payload={}, job_type="single",
    )
    staged = [SimpleNamespace(status="pending", course_id=None)]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result()])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["ready"] is False
    assert result["courses"] == []
    assert "still need review" in result["blockedReason"]


@pytest.mark.asyncio
async def test_only_active_courses_without_an_approved_link_are_candidates():
    job = SimpleNamespace(
        university_id=4, status="completed", errors=0,
        request_payload={}, job_type="single",
    )
    staged = [
        SimpleNamespace(status="approved", course_id=11),
        SimpleNamespace(status="approved", course_id=11),
        SimpleNamespace(status="rejected", course_id=12),
        SimpleNamespace(status="approved", course_id=None),
    ]
    live = [
        SimpleNamespace(id=11, name="Present", course_website="/present", status="active"),
        SimpleNamespace(id=12, name="Rejected staged", course_website="/old", status="active"),
        SimpleNamespace(id=13, name="Absent", course_website="/absent", status="active"),
        SimpleNamespace(id=14, name="Already inactive", course_website="/inactive", status="inactive"),
    ]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result(scalars=live)])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["ready"] is True
    assert [row["courseId"] for row in result["courses"]] == [12, 13]
    assert result["duplicateLinkedCourseIds"] == [11]
    assert result["rejectedOrUnlinkedCount"] == 2


@pytest.mark.asyncio
async def test_confirm_removal_uses_shared_lock_and_writes_audit(monkeypatch):
    job = SimpleNamespace(university_id=4, request_payload={}, job_type="single")
    course = SimpleNamespace(
        id=13, university_id=4, status="active", last_edited_at=None, last_edited_by=None
    )
    db = MagicMock()
    db.get = AsyncMock(side_effect=[job, course])
    db.execute = AsyncMock()
    db.add = MagicMock()
    db.commit = AsyncMock()

    async def ready(*_args, **_kwargs):
        return {
            "ready": True,
            "courses": [{"courseId": 13, "decision": None}],
            "blockedReason": None,
        }

    monkeypatch.setattr(
        "app.services.scraper.removal_reconciliation.get_removal_reconciliation",
        ready,
    )
    result = await decide_course_removal(
        db, "job-1", 13, remove=True, actor="reviewer@example.test"
    )

    assert result["status"] == "inactive"
    assert course.status == "inactive"
    assert "pg_advisory_xact_lock" in str(db.execute.await_args_list[0].args[0])
    audit = db.add.call_args.args[0]
    assert audit.action == "confirmed_removed"
    assert audit.actor == "reviewer@example.test"
    assert audit.reason == "scrape_reconciliation:job-1"


@pytest.mark.asyncio
async def test_every_non_terminal_review_status_blocks_reconciliation():
    job = SimpleNamespace(
        university_id=4, status="completed", errors=0,
        request_payload={}, job_type="single",
    )
    staged = [SimpleNamespace(status="ready", course_id=11)]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result()])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["ready"] is False
    assert result["courses"] == []


@pytest.mark.asyncio
async def test_continuation_parent_approvals_count_as_present():
    child = SimpleNamespace(
        university_id=4,
        status="completed",
        errors=0,
        request_payload={"retrySourceJobId": "parent"},
        job_type="single",
    )
    parent = SimpleNamespace(
        university_id=4,
        status="completed",
        errors=0,
        request_payload={},
        job_type="single",
    )
    staged = [
        SimpleNamespace(status="approved", course_id=11),
        SimpleNamespace(status="approved", course_id=12),
    ]
    live = [
        SimpleNamespace(id=11, name="Parent course", course_website="/parent", status="active"),
        SimpleNamespace(id=12, name="Child course", course_website="/child", status="active"),
        SimpleNamespace(id=13, name="Absent", course_website="/absent", status="active"),
    ]
    db = MagicMock()
    db.get = AsyncMock(side_effect=[child, child, parent])
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result(scalars=live)])

    result = await get_removal_reconciliation(db, "child")

    assert result["comparisonJobIds"] == ["child", "parent"]
    assert [row["courseId"] for row in result["courses"]] == [13]


@pytest.mark.asyncio
async def test_resume_course_approval_still_blocks_whole_catalogue_reconciliation():
    job = SimpleNamespace(
        university_id=4,
        status="completed",
        errors=0,
        request_payload={"resumeCourseIds": [302]},
        job_type="single",
    )
    staged = [
        SimpleNamespace(id=301, status="approved", course_id=11),
        SimpleNamespace(id=302, status="approved", course_id=12),
    ]
    live = [
        SimpleNamespace(id=11, name="Current", course_website="/current", status="active"),
        SimpleNamespace(id=12, name="Resumed", course_website="/resumed", status="active"),
        SimpleNamespace(id=13, name="Absent", course_website="/absent", status="active"),
    ]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result(scalars=live)])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["resumeCourseIds"] == [302]
    assert result["ready"] is False
    assert result["courses"] == []


@pytest.mark.asyncio
async def test_unreviewed_resume_course_blocks_reconciliation():
    job = SimpleNamespace(
        university_id=4,
        status="completed",
        errors=0,
        request_payload={"resumeCourseIds": [302]},
        job_type="single",
    )
    staged = [
        SimpleNamespace(id=301, status="approved", course_id=11),
        SimpleNamespace(id=302, status="pending", course_id=12),
    ]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result()])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["ready"] is False
    assert result["courses"] == []


@pytest.mark.asyncio
async def test_completed_with_warnings_is_ready_with_caution():
    job = SimpleNamespace(
        university_id=4,
        status="completed_with_warnings",
        errors=0,
        request_payload={},
        job_type="single",
    )
    staged = [SimpleNamespace(id=301, status="approved", course_id=11)]
    live = [
        SimpleNamespace(id=11, name="Present", course_website="/present", status="active"),
    ]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result(scalars=live)])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["ready"] is True
    assert result["warning"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("job_type", "payload"),
    [
        ("targeted", {"courseUrls": ["https://example.edu/course/a"]}),
        ("repair", {"repair_targets": [{"course_id": 11}]}),
        ("single", {"resumeCourseIds": [301]}),
    ],
)
async def test_partial_job_scopes_never_offer_catalogue_removals(job_type, payload):
    job = SimpleNamespace(
        university_id=4,
        status="completed",
        errors=0,
        request_payload=payload,
        job_type=job_type,
    )
    staged = [SimpleNamespace(id=301, status="approved", course_id=11)]
    live = [
        SimpleNamespace(id=11, name="Target", course_website="/target", status="active"),
        SimpleNamespace(id=12, name="Unrelated", course_website="/unrelated", status="active"),
    ]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.execute = AsyncMock(side_effect=[_result(scalars=staged), _result(), _result(scalars=live)])

    result = await get_removal_reconciliation(db, "job-1")

    assert result["ready"] is False
    assert result["fullCatalogueScope"] is False
    assert result["courses"] == []
    assert "full catalogue" in result["blockedReason"]


@pytest.mark.asyncio
async def test_targeted_job_cannot_apply_a_removal_decision():
    job = SimpleNamespace(
        university_id=4,
        status="completed",
        errors=0,
        request_payload={"courseUrls": ["https://example.edu/course/a"]},
        job_type="targeted",
    )
    staged = [SimpleNamespace(id=301, status="approved", course_id=11)]
    live = [
        SimpleNamespace(id=12, name="Unrelated", course_website="/unrelated", status="active"),
    ]
    db = MagicMock()
    db.get = AsyncMock(return_value=job)
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(),
        _result(scalars=staged),
        _result(),
        _result(scalars=live),
    ])

    with pytest.raises(ValueError, match="full catalogue"):
        await decide_course_removal(
            db, "job-1", 12, remove=True, actor="reviewer@example.test"
        )

    db.add.assert_not_called()
    db.commit.assert_not_awaited()