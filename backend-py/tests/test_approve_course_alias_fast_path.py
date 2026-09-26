from types import SimpleNamespace

import pytest

from app.models.course_id_alias import CourseIdAlias
from app.services.scraper.approve_course import (
    ApprovalValidationError,
    approve_scraped_course,
)


class _DB:
    def __init__(self, alias_ids):
        self.alias_ids = set(alias_ids)

    async def execute(self, *_args, **_kwargs):
        return None

    async def get(self, model, course_id):
        if model is CourseIdAlias and course_id in self.alias_ids:
            return SimpleNamespace(alias_course_id=course_id, canonical_course_id=999)
        return None


@pytest.mark.asyncio
async def test_already_approved_staged_alias_is_rejected_before_idempotent_return(monkeypatch):
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.unresolved_fee_selection",
        lambda _row: False,
    )
    staged = SimpleNamespace(
        id=123, university_id=8, status="approved", course_id=321,
        course_name="Legacy Award",
    )
    with pytest.raises(ApprovalValidationError, match="is an alias"):
        await approve_scraped_course(_DB({321}), staged)


@pytest.mark.asyncio
async def test_already_approved_canonical_idempotent_path_remains_unchanged(monkeypatch):
    monkeypatch.setattr(
        "app.services.scraper.fee_selection.unresolved_fee_selection",
        lambda _row: False,
    )
    staged = SimpleNamespace(
        id=123, university_id=8, status="approved", course_id=999,
        course_name="Canonical Award",
    )
    result = await approve_scraped_course(_DB(set()), staged)
    assert result == {
        "ok": True, "course_id": 999, "scraped_course_id": 123,
        "auto_publish": False, "reason": "Already approved",
    }