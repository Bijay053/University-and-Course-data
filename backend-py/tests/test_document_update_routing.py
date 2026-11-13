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
    "https://www.aston.ac.uk/international/english-language-requirements\n"
    "ielts score are on this link update on all courses",
    "Use https://university.example/english to update IELTS overall and bands for all courses",
    "Check English requirements at https://university.example/entry; do not apply changes",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [
    "Read only these official course pages. Do not run discovery, add courses, change data or start any operation.",
    "Check the course award, duration, fees and requirements. Do not change data.",
    "Investigate this official page. Do not update any rows.",
    "Only explain the academic requirements; never change data.",
])
async def test_negated_edits_do_not_hijack_research(prompt, monkeypatch):
    from app.services import assistant_eligibility
    row = {"id": 1, "name": "MSc Engineering", "url": "https://university.example/engineering"}
    monkeypatch.setattr(assistant_eligibility, "scope", lambda *args: [row])
    service = SimpleNamespace(save_research_reply=AsyncMock())
    assert await handle_field_edit(service, "thread", "turn", "run", {"samples": [row]}, prompt) is False
    service.save_research_reply.assert_not_awaited()


@pytest.mark.parametrize("prompt", [
    "https://university.example/ielts/update/all-courses",
    "Do not update IELTS from https://university.example/english",
    "Read https://university.example/fees and update tuition",
])
def test_html_guidance_requires_an_actual_english_instruction(prompt):
    from app.services.assistant_field_edits import html_guidance_research_requested
    assert not html_guidance_research_requested(prompt)
