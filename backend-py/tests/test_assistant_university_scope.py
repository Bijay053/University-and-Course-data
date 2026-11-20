"""University-qualified corrections preserve current provider and field scope."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.assistant_university_scope import existing_course_scope

ROWS = [
    {"id": 1, "name": "Computing MSc", "url": "https://www.cardiffmet.ac.uk/course/a"},
    {"id": 2, "name": "Finance MSc", "url": "https://www.cardiffmet.ac.uk/course/b"},
]


def resolve(prompt):
    return existing_course_scope(prompt, ROWS, university_name="Cardiff Metropolitan University",
                                 home="https://www.cardiffmet.ac.uk/")


@pytest.mark.parametrize("name", ["Cardiff Met", "Cardiff Metropolitan", "Cardiff Metropolitan University", "CARDIFFMET"])
def test_provider_name_or_official_domain_alias_resolves_existing_records(name):
    assert resolve(f"Check and fix IELTS scores for all pending {name} courses.") == ROWS
    assert resolve(f"Fix fees for all {name} courses. Keep Finance MSc unchanged.") == ROWS[:1]


@pytest.mark.parametrize("prompt", [
    "Fix IELTS for all pending Aston courses.",
    "Fix IELTS for all Cardiff Met engineering courses.",
    "Fix IELTS for all Cardiff Met courses with missing fees.",
    "Fix IELTS for all Cardiff Met courses in London.",
    "Do not update all Cardiff Met courses.",
])
def test_unknown_provider_subsets_and_negation_do_not_expand_scope(prompt):
    assert resolve(prompt) == []


@pytest.mark.asyncio
async def test_mixed_field_correction_keeps_all_five_ielts_values_in_refresh(monkeypatch):
    from app.services import scrape_assistant as service, assistant_source_cache
    from app.services.assistant_field_edits import handle_field_edit, existing_ielts_recheck, field_recheck_options
    prompt = (
        "Check and fix IELTS overall, Listening, Reading, Writing and Speaking, delivery mode "
        "and campus for all pending Cardiff Met courses using their official course requirements. "
        "Refresh verified source evidence for unchanged scores too. "
        "Keep unsupported exceptions unchanged and everything pending."
    )
    ctx = {"samples": ROWS, "name": "Cardiff Metropolitan University",
           "home": "https://www.cardiffmet.ac.uk/", "versions": {"1": "a", "2": "b"},
           "config_version": "config"}
    monkeypatch.setattr(assistant_source_cache, "cached_source", AsyncMock(return_value={"verified": True}))
    save = AsyncMock()
    fake = SimpleNamespace(action_proposal=service.action_proposal, now=service.now,
                           inspect_source=AsyncMock(), save_research_reply=save)
    assert await handle_field_edit(fake, "thread", "turn", "run", ctx, prompt)
    action = save.call_args.args[3]["actions"][0]
    assert action["_ids"] == [1, 2]
    assert set(action["fields"]) == {"ielts_overall", "study_mode", "course_location"}
    assert "_values" not in action and "_requestedUrls" not in action
    assert existing_ielts_recheck(action)
    assert field_recheck_options(action)["forceFields"] == ["course_location", "study_mode"]
    assert not existing_ielts_recheck({**action, "_evidenceUrls": []})
