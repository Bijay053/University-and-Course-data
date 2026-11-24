from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import scrape_assistant as service
from app.services.assistant_ielts_preview import collect, proposal, requested, run
from tests.test_ielts_approval_preview_fallback import PROMPT, data


@pytest.mark.parametrize("prompt,expected", [
    (PROMPT, True),
    ("Prepare an IELTS preview for every pending course", True),
    ("Prepare an IELTS preview for Nursing", False),
    ("Preview all five IELTS values for Nursing", False),
    ("Preview IELTS for all selected courses", False),
    ("Fix IELTS for all courses", False),
    ("Prepare all course fee previews", False),
    ("https://example.com/policy Prepare an IELTS preview for all courses", False),
])
def test_only_explicit_whole_review_ielts_preview_is_selected(prompt, expected):
    assert requested(prompt) is expected


@pytest.mark.asyncio
async def test_all_343_rows_are_checked_once_including_after_old_sample_boundary():
    rows = [{"id": i, "url": f"https://example.com/course/{i}"} for i in range(343)]
    read = AsyncMock(side_effect=lambda url: {"url": url, "verified": True})
    notify = AsyncMock()
    before = deepcopy(rows)
    sources = await collect(rows, read, notify)
    assert len(sources) == read.await_count == 343
    assert len({source["url"] for source in sources}) == 343
    assert sources[-1]["url"] == rows[-1]["url"]
    assert notify.await_count == 58
    assert rows == before


@pytest.mark.asyncio
async def test_failure_does_not_cancel_other_sources_or_retry_blocked_url():
    rows = [{"url": "https://example.com/denied"},
            {"url": "https://example.com/good"},
            {"url": "https://example.com/denied"}]
    read = AsyncMock(side_effect=lambda url: {
        "url": url, "verified": url.endswith("good"), "httpStatus": 403})
    sources = await collect(rows, read, AsyncMock())
    assert read.await_count == 2
    assert [source["verified"] for source in sources] == [False, True]


@pytest.mark.asyncio
async def test_budget_exhaustion_does_not_claim_remaining_rows_checked():
    ticks = iter([0, 0, *([0] * 6), 100])
    rows = [{"url": f"https://example.com/{i}"} for i in range(9)]
    read = AsyncMock(side_effect=lambda url: {"url": url, "verified": True})
    sources = await collect(rows, read, AsyncMock(), budget=10, clock=lambda: next(ticks))
    assert len(sources) == 6
    assert read.await_count == 6


def test_preview_never_offers_unverified_rows_as_executable_whole_review():
    ctx, sources = data()
    action, citations = proposal(service, PROMPT, ctx, sources)
    assert action["_ids"] == [1]
    assert action["courseCount"] == 1
    assert action["requiresConfirmation"] is True
    assert {change["field"] for change in action["repairPreview"][0]["changes"]} == {
        "ielts_overall", "ielts_listening", "ielts_reading", "ielts_writing", "ielts_speaking"}
    assert citations


@pytest.mark.asyncio
async def test_real_handler_saves_coverage_without_mutating_or_running_action(monkeypatch):
    from app.services import assistant_source_cache
    ctx, sources = data()
    ctx.update(home="https://example.com", config=None)
    reader = AsyncMock(side_effect=lambda *args, **kwargs: next(
        (deepcopy(source) for source in sources if source["url"] == args[4]),
        {"url": args[4], "verified": False, "httpStatus": 403}))
    monkeypatch.setattr(assistant_source_cache, "cached_source", reader)
    fake = SimpleNamespace(
        action_proposal=service.action_proposal, progress=AsyncMock(),
        save_research_reply=AsyncMock(), now=service.now,
    )
    before = deepcopy(ctx)
    await run(fake, "thread", "turn", "run", ctx, PROMPT)
    reply = fake.save_research_reply.await_args.args[3]
    assert reply["researchCoverage"]["checked"] == 1
    assert reply["researchCoverage"]["failed"] == 1
    assert reply["researchCoverage"]["unchecked"] == 0
    assert reply["actions"][0]["_ids"] == [1]
    assert "No course records or provider settings were changed" in reply["content"]
    assert ctx == before
