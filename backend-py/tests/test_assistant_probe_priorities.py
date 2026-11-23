"""A linked fee example must not be displaced by alphabetically earlier courses."""
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_source_gate import (
    prioritised_probe_urls, prompt_probe_preferences, require_reachable_sources,
)

HOME = "https://www.beds.ac.uk"
URLS = [HOME + "/courses/" + slug + "/" for slug in (
    "accounting", "accounting-and-business-finance", "accounting-and-finance",
    "business-management-with-finance", "chemistry",
)]
TARGET = URLS[3]
PROMPT = (
    "Business Management with Finance BSc (Hons) per year fee is £10,050 "
    f"mention website {TARGET}\nfix this bug\nalso same issue with other courses"
)


def test_preference_preserves_every_row_and_only_promotes_the_matching_course():
    assert prioritised_probe_urls(URLS + [URLS[0], None], [TARGET + "#fees"]) == [
        TARGET, *URLS[:3], URLS[4],
    ]


@pytest.mark.parametrize("unrelated", [
    "https://another-university.ac.uk/courses/business-management-with-finance/",
    TARGET + "?option=placement", "http://www.beds.ac.uk/courses/accounting/",
    HOME + "/news/tuition/", "https://user:password@www.beds.ac.uk/courses/accounting/",
])
def test_preference_cannot_expand_the_cohort_or_substitute_another_option(unrelated):
    assert prioritised_probe_urls(URLS, [unrelated]) == URLS


def test_extract_preferences_from_markdown_and_plain_text_without_inferring_urls():
    assert prompt_probe_preferences(f"Check [fees]({TARGET}#fees).") == [TARGET + "#fees"]
    assert prompt_probe_preferences(PROMPT) == [TARGET]
    assert prompt_probe_preferences("Business Management with Finance") == []


@pytest.mark.asyncio
async def test_linked_course_is_checked_first_within_the_existing_three_page_budget():
    checked = []
    async def inspect(url, home):
        checked.append(url)
        return {"verified": url == TARGET}
    results = await require_reachable_sources(
        URLS, HOME, inspect, preferred_urls=prompt_probe_preferences(PROMPT))
    assert checked == [TARGET, *URLS[:2]]
    assert results[0]["verified"]


@pytest.mark.asyncio
async def test_actual_target_refusal_is_reported_without_proxy_retry_or_queueing():
    checked = []
    async def denied(url, home):
        checked.append(url)
        return {"verified": False, "accessIssue": "source_access_denied",
                "error": "Official source returned a protection challenge."}
    with pytest.raises(ValueError, match="not queued") as error:
        await require_reachable_sources(URLS, HOME, denied, preferred_urls=[TARGET])
    assert checked == [TARGET, *URLS[:2]]
    assert TARGET in str(error.value)
    assert len(checked) == 3


@pytest.mark.asyncio
async def test_saved_checked_source_stays_first_when_confirmation_sorts_rows_by_id():
    from app.services.assistant_source_gate import repair_probe_urls
    rows = [SimpleNamespace(id=i, course_website=url) for i, url in enumerate(URLS)]
    action = {"_evidenceUrls": [TARGET]}
    inspect = AsyncMock(return_value={"verified": True})
    await require_reachable_sources(
        repair_probe_urls(rows, action), HOME, inspect,
        preferred_urls=action["_evidenceUrls"])
    assert inspect.await_args_list[0].args[0] == TARGET
    assert inspect.await_count == 3


@pytest.mark.asyncio
async def test_real_fee_instruction_checks_its_example_without_copying_the_uk_amount(monkeypatch):
    from app.services.assistant_field_edits import handle_field_edit
    from app.services import assistant_source_cache
    rows = [{"id": i, "name": "Business Management with Finance BSc (Hons)"
             if url == TARGET else f"Accounting option {i}", "url": url}
            for i, url in enumerate(URLS, 1)]
    checked = []
    async def cache(service, thread, turn, run, url, home, inspect, seconds):
        checked.append(url)
        return {"verified": url == TARGET, "url": url}
    monkeypatch.setattr(assistant_source_cache, "cached_source", cache)
    save = AsyncMock()
    service = SimpleNamespace(
        action_proposal=lambda plan, ids: {**plan, "_ids": ids},
        inspect_source=AsyncMock(), save_research_reply=save,
        now=lambda: datetime.now(timezone.utc),
    )
    ctx = {"samples": rows, "name": "University of Bedfordshire", "home": HOME,
           "university_id": 115, "versions": {str(r["id"]): "version" for r in rows},
           "config_version": "configuration"}
    assert await handle_field_edit(service, "thread", "turn", "run", ctx, PROMPT)
    assert checked == [TARGET, *URLS[:2]]
    reply = save.await_args.args[3]
    action = reply["actions"][0]
    assert action["_ids"] == [r["id"] for r in rows]
    assert action["fields"] == ["international_fee"]
    assert action["_evidenceUrls"] == [TARGET]
    assert "10050" not in str(action) and "10,050" not in str(action)
