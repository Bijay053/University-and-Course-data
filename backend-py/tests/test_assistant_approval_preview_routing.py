"""Preview requests must reach evidence research without triggering writes."""
from types import SimpleNamespace

import pytest

from app.services.assistant_field_edits import handle_field_edit
from app.services.assistant_repair_proposals import research_only
from app.services.scrape_assistant_autorun import (
    choose_action, execution_requested, preview_requested,
)

PROMPTS = [
    "Prepare the MBA tuition update for approval using the official source you just verified. "
    "Show current and proposed values. Leave all records unchanged.",
    "Please prepare the Master of Business Administration fee update for approval.",
    "Can you show current and proposed tuition values?",
    "I need an approval preview for the existing MBA tuition.",
    "Do not change records; prepare the fee update for approval.",
    "Preview the fee correction first.",
    "Show a proposal for updating IELTS.",
]


@pytest.mark.parametrize("prompt",PROMPTS)
@pytest.mark.asyncio
async def test_preview_bypasses_literal_edit_form_and_cannot_autorun(prompt):
    assert preview_requested(prompt)
    assert not execution_requested(prompt)
    assert research_only(prompt)
    # Returning False delegates to the ordinary evidence research path. The
    # legacy field editor must neither demand the edit form nor touch storage.
    assert not await handle_field_edit(SimpleNamespace(),"thread","turn","run",{},prompt)
    assert choose_action([{"id":"plan","kind":"fix_fields","fields":["international_fee"],
        "courseCount":1,"state":"proposed","automaticPending":True,
        "requiresConfirmation":research_only(prompt)}]) is None


@pytest.mark.parametrize("prompt",[
    "Fix the MBA fee.",
    "Update the tuition using official evidence; do not ask for approval.",
    "Set the MBA fee to 25000, staff-provided.",
    "Do not prepare a preview; fix the fee.",
])
def test_ordinary_authorized_edits_do_not_become_preview_only(prompt):
    assert not preview_requested(prompt)
    assert execution_requested(prompt)


def test_course_abbreviation_is_provider_inventory_scoped_and_collisions_are_not_guessed():
    from app.services.assistant_course_names import matches, result
    mba={"title":"Master of Business Administration",
         "url":"https://www.tru.ca/programs/catalogue/master-of-business-administration.html"}
    unrelated={"title":"Graduate Diploma in Business Administration",
               "url":"https://www.tru.ca/programs/catalogue/gdba.html"}
    assert matches(PROMPTS[0],[mba,unrelated])==[mba]
    assert matches("Prepare the mba tuition update for approval.",[mba,unrelated])==[mba]
    other={"title":"Master of Business Administration","url":"https://www.tru.ca/distance/mba.html"}
    ambiguous=matches(PROMPTS[0],[mba,other,unrelated])
    assert len(ambiguous)==2 and result(ambiguous)["state"]=="choice"
    assert matches("Check fees and requirements.",[mba,unrelated])==[]
    assert matches("Check CAD tuition.",[{"title":"Certificate in Art and Design",
                                        "url":"https://www.tru.ca/cad.html"}])==[]


@pytest.mark.asyncio
async def test_existing_mba_preview_needs_no_row_number_url_or_catalogue_discovery():
    from contextlib import asynccontextmanager
    from app.services.assistant_course_names import resolve
    class DB:
        def __init__(self):self.calls=0
        async def execute(self,*args):
            self.calls+=1
            rows=[] if self.calls==1 else [("Master of Business Administration",
                "https://www.tru.ca/programs/catalogue/master-of-business-administration.html")]
            return SimpleNamespace(all=lambda:rows)
    db=DB()
    @asynccontextmanager
    async def factory():yield db
    async def forbidden(*args):raise AssertionError("Never discover for an existing named course")
    result=await resolve(PROMPTS[0],[{"role":"user","content":PROMPTS[0]}],
        {"home":"https://www.tru.ca","university_id":119},factory,forbidden)
    assert result["state"]=="resolved"
    assert [c["title"] for c in result["choices"]]==["Master of Business Administration"]
