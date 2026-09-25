from datetime import datetime, timezone
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.dialects import sqlite

from app.models.course import Course
from app.models.course_id_alias import CourseIdAlias
from app.routers import courses
from app.schemas.course import CourseUpdate
from app.services.course_id_aliases import (
    COURSE_ALIAS_EXCLUSION_SQL,
    CourseAliasChainError,
    exclude_aliased_courses,
    resolve_course_id,
    validate_alias_target,
)


def test_alias_graph_rejects_self_and_chains():
    aliases = {101: 201}
    with pytest.raises(CourseAliasChainError, match="itself"):
        validate_alias_target(101, 101, aliases)
    with pytest.raises(CourseAliasChainError, match="already an alias"):
        validate_alias_target(101, 301, aliases)
    with pytest.raises(CourseAliasChainError, match="directly"):
        validate_alias_target(301, 101, aliases)
    with pytest.raises(CourseAliasChainError, match="cannot become an alias"):
        validate_alias_target(201, 301, aliases)


def test_alias_ids_are_excluded_from_list_and_search_rows():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE courses (id INTEGER PRIMARY KEY);
        CREATE TABLE course_id_aliases (
            alias_course_id INTEGER PRIMARY KEY,
            canonical_course_id INTEGER NOT NULL
        );
        INSERT INTO courses (id) VALUES (101), (201);
        INSERT INTO course_id_aliases (alias_course_id, canonical_course_id)
        VALUES (101, 201);
        """
    )

    list_query = exclude_aliased_courses(select(Course.id))
    list_sql = str(
        list_query.compile(
            dialect=sqlite.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert connection.execute(list_sql).fetchall() == [(201,)]

    search_sql = (
        "SELECT c.id FROM courses c WHERE " + COURSE_ALIAS_EXCLUSION_SQL
    )
    assert connection.execute(search_sql).fetchall() == [(201,)]
    connection.close()


class AliasLookupDB:
    def __init__(self, canonical_course):
        self.course = canonical_course
        self.calls = []

    async def get(self, model, course_id):
        self.calls.append((model, course_id))
        if model is CourseIdAlias and course_id == 101:
            return SimpleNamespace(alias_course_id=101, canonical_course_id=201)
        if model is Course and course_id == 201:
            return self.course
        return None


@pytest.mark.asyncio
async def test_get_legacy_id_returns_canonical_id_and_requested_course_id(monkeypatch):
    now = datetime.now(timezone.utc)
    canonical = SimpleNamespace(
        id=201,
        name="Bachelor of Arts",
        university_id=7,
        status="active",
        eligibility_status="unknown",
        approval_status="approved",
        created_at=now,
        updated_at=now,
    )
    db = AliasLookupDB(canonical)

    async def no_offerings(_db, ids):
        assert ids == [201]
        return {201: []}

    monkeypatch.setattr(courses, "read_offerings", no_offerings)
    result = await courses.get_course(101, db)

    assert result.id == 201
    assert result.requestedCourseId == 101
    assert (Course, 101) not in db.calls
    assert (Course, 201) in db.calls


@pytest.mark.asyncio
async def test_patch_and_delete_legacy_id_reject_without_mutating_course():
    canonical = SimpleNamespace(id=201)
    db = AliasLookupDB(canonical)

    with pytest.raises(HTTPException) as patch_error:
        await courses.update_course(101, CourseUpdate(description="changed"), db, {})
    assert patch_error.value.status_code == 409

    with pytest.raises(HTTPException) as delete_error:
        await courses.delete_course(101, db, {})
    assert delete_error.value.status_code == 409
    assert all(model is CourseIdAlias for model, _ in db.calls)
    assert not hasattr(canonical, "description")


def test_alias_foreign_keys_restrict_course_deletion():
    foreign_keys = {
        column.name: next(iter(column.foreign_keys))
        for column in CourseIdAlias.__table__.columns
        if column.foreign_keys
    }
    assert set(foreign_keys) == {"alias_course_id", "canonical_course_id"}
    assert all(key.ondelete == "RESTRICT" for key in foreign_keys.values())