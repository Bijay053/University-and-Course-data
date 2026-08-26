from unittest.mock import AsyncMock

import pytest

from scripts.backfill_subcategories import (
    CandidateRow,
    EVIDENCE_BATCH_SIZE,
    PlannedChange,
    _apply_grouped_updates,
    build_plan,
    plan_change,
)


def test_evidence_batch_stays_below_postgresql_bind_parameter_limit():
    # _apply_grouped_updates currently writes 13 bound values per evidence row.
    assert EVIDENCE_BATCH_SIZE * 13 < 65_535


def test_apply_serializes_script_instances_without_named_db_constraint():
    import inspect
    from scripts import backfill_subcategories

    source = inspect.getsource(backfill_subcategories.run)
    assert "pg_advisory_xact_lock" in source


def test_backfill_plans_pictured_course_for_staged_and_live_rows():
    rows = [
        CandidateRow(
            "scraped_courses",
            1,
            10,
            "Bachelor of Media and Communication",
            "Media & Communications",
            None,
        ),
        CandidateRow(
            "courses",
            2,
            10,
            "Bachelor of Media and Communication",
            "Media & Communications",
            "",
        ),
    ]
    changes, unresolved = build_plan(rows)
    assert unresolved == []
    assert [(c.scope, c.new_sub_category) for c in changes] == [
        ("scraped_courses", "Communications"),
        ("courses", "Communications"),
    ]


def test_backfill_never_overwrites_existing_subcategory():
    row = CandidateRow(
        "courses",
        3,
        11,
        "Bachelor of Media and Communication",
        "Media & Communications",
        "Manual Discipline",
    )
    assert plan_change(row) is None


def test_backfill_can_fill_parent_before_subcategory():
    row = CandidateRow(
        "scraped_courses",
        4,
        12,
        "Bachelor of Computer Science",
        None,
        None,
    )
    change = plan_change(row)
    assert change is not None
    assert change.new_category == "Computer Science & IT"
    assert change.new_sub_category == "Computer Science"


def test_backfill_leaves_genuinely_unclassifiable_title_unchanged():
    row = CandidateRow(
        "scraped_courses",
        5,
        12,
        "Foundation Pathway Program",
        None,
        None,
    )
    assert plan_change(row) is None


def test_backfill_does_not_create_subcategory_under_legacy_parent_alias():
    row = CandidateRow(
        "courses",
        6,
        13,
        "Bachelor of Mechanical Engineering",
        "Engineering",
        None,
    )
    assert plan_change(row) is None


@pytest.mark.asyncio
async def test_apply_uses_original_parent_as_compare_and_set_precondition():
    class _Result:
        def scalars(self):
            return []

    db = AsyncMock()
    db.execute.return_value = _Result()
    change = PlannedChange(
        scope="courses",
        row_id=99,
        university_id=1,
        course_name="Bachelor of Media and Communication",
        old_category="Media & Communications",
        old_sub_category=None,
        new_category="Media & Communications",
        new_sub_category="Communications",
    )

    updated = await _apply_grouped_updates(db, [change])
    assert updated == set()
    statement = db.execute.await_args.args[0]
    compiled = statement.compile()
    assert "courses.category =" in str(compiled)
    assert "Media & Communications" in compiled.params.values()