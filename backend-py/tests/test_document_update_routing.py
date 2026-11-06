"""Source-based bulk edits must reach research without a manual-form detour."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_field_edits import (
    document_research_requested, handle_field_edit,
)


@pytest.mark.parametrize("prompt", [
    "https://www.lsbu.ac.uk/_media_old/pdf/guidelines/Eng-lang-requirements.pdf\n"
    "update each band ielts for all courses",
    "Read `https://uni.example/English.pdf.pdf` and update IELTS overall and bands",
    "Use the uploaded PDF to update fees for all applicable courses",
    "Extract intakes from this PDF",
    "Check scholarships using https://uni.example/fees.pdf?year=2027",
])
@pytest.mark.asyncio
async def test_document_update_reaches_research_without_actions(prompt):
    service = SimpleNamespace(save_research_reply=AsyncMock())
    assert document_research_requested(prompt)
    assert await handle_field_edit(service, "thread", "turn", "run", {}, prompt) is False
    service.save_research_reply.assert_not_awaited()


@pytest.mark.parametrize("prompt", [
    "Set IELTS overall to 6.5 for row 123",
    "Add Sydney as the location",
    "Master of Counselling add intake February and July",
    "Add this course",
    "Update fee to GBP 17400 annual",
])
def test_literal_staff_edits_remain_on_existing_route(prompt):
    assert not document_research_requested(prompt)
