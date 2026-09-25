"""Safe resolution and validation helpers for published course ID aliases."""

from collections.abc import Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.course import Course
from app.models.course_id_alias import CourseIdAlias


class CourseAliasChainError(ValueError):
    """Alias rows must point directly to a canonical course, never another alias."""


COURSE_ALIAS_EXCLUSION_SQL = """
NOT EXISTS (
    SELECT 1 FROM course_id_aliases alias
    WHERE alias.alias_course_id = c.id
)
"""


def exclude_aliased_courses(statement):
    """Exclude legacy duplicate rows from catalogue/list queries."""
    return statement.where(~Course.id.in_(select(CourseIdAlias.alias_course_id)))


def validate_alias_target(
    alias_course_id: int,
    canonical_course_id: int,
    aliases: Mapping[int, int],
) -> None:
    """Validate a prospective mapping against the existing one-hop alias graph."""
    if alias_course_id == canonical_course_id:
        raise CourseAliasChainError("A course cannot be an alias of itself")
    if alias_course_id in aliases:
        raise CourseAliasChainError("The legacy course ID is already an alias")
    if canonical_course_id in aliases:
        raise CourseAliasChainError("An alias must point directly to a canonical course")
    if alias_course_id in aliases.values():
        raise CourseAliasChainError("A canonical course with aliases cannot become an alias")


async def resolve_course_id(db: AsyncSession, requested_course_id: int) -> tuple[int, bool]:
    """Resolve exactly one alias hop; malformed chains fail closed."""
    alias = await db.get(CourseIdAlias, requested_course_id)
    if alias is None:
        return requested_course_id, False
    if alias.alias_course_id == alias.canonical_course_id:
        raise CourseAliasChainError("Invalid self-referential course alias")
    next_alias = await db.get(CourseIdAlias, alias.canonical_course_id)
    if next_alias is not None:
        raise CourseAliasChainError("Invalid chained course alias")
    return alias.canonical_course_id, True