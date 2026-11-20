"""A bounded add-courses instruction must not become an unsupported field edit."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.services.assistant_field_edits import (
    explicit_course_import, handle_field_edit,
)

NAME = "Cardiff Metropolitan University"
PREFIX = (
    "Add these missing Cardiff Metropolitan courses to pending review from their "
    "official pages. Keep existing records and evidence. Do not approve or publish.\n"
)
PROMPT = PREFIX + "\n".join([
    "https://www.cardiffmet.ac.uk/courses/undergraduate/foundation-leading-to-sport/",
    "https://www.cardiffmet.ac.uk/courses/postgraduate/mdes-global-design/",
    "https://www.cardiffmet.ac.uk/courses/postgraduate/msc-psychology-in-education-online/",
])


@pytest.mark.parametrize("prompt", [
    PROMPT,
    "Add these missing Cardiff Metropolitan University courses.",
    "Add the following three courses to pending review.",
    "Fetch these courses.",
    "Import the requested programmes.",
    "Recover missing courses.",
    "Add this course.",
])
def test_explicit_bounded_course_imports_are_recognised(prompt):
    assert explicit_course_import(prompt, NAME)


@pytest.mark.parametrize("prompt", [
    "Add IELTS to all courses.",
    "Add accreditation to these courses.",
    "Add the location to the existing course.",
    "Update the course name.",
    "Fix all fees in this review.",
    "Compare the course https://example.edu/add-these-courses/",
])
def test_field_edits_and_url_text_are_not_import_instructions(prompt):
    assert not explicit_course_import(prompt, NAME)


@pytest.mark.asyncio
async def test_existing_url_does_not_turn_plural_import_into_unsupported_edit():
    # A supplied URL can already match a pending row. The normal import path
    # must reconcile/reuse it, not demand an edit form or create a duplicate.
    ctx = {
        "name": NAME, "home": "https://www.cardiffmet.ac.uk", "university_id": 114,
        "samples": [{
            "id": 1, "name": "Psychology in Education - MSc",
            "url": "https://www.cardiffmet.ac.uk/courses/postgraduate/msc-psychology-in-education-online/",
        }],
    }
    assert not await handle_field_edit(
        SimpleNamespace(), "thread", "turn", "run", ctx, PROMPT,
    )


@pytest.mark.asyncio
async def test_unsupported_literal_field_edit_still_cannot_start_discovery():
    replies = []

    async def save(*args, **kwargs):
        replies.append(args[3])

    ctx = {
        "name": NAME, "home": "https://www.cardiffmet.ac.uk", "university_id": 114,
        "samples": [{
            "id": 1, "name": "Global Design",
            "url": "https://www.cardiffmet.ac.uk/courses/postgraduate/mdes-global-design/",
        }],
    }
    prompt = ctx["samples"][0]["url"] + "\nAdd accreditation for this course."
    assert await handle_field_edit(
        SimpleNamespace(save_research_reply=save, now=lambda: datetime.now(timezone.utc)),
        "thread", "turn", "run", ctx, prompt,
    )
    assert not replies[0]["actions"]
    assert "edit form" in replies[0]["content"]
