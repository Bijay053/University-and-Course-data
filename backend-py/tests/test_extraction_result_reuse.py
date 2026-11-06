from copy import deepcopy
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import text
from app.services.scraper import result_reuse as cache
from tests.test_scrape_prompt_assistant import store, assistant_postgres

URL = "https://www.example.edu/course"
HTML = "<h1>BSc Accounting</h1><p>Overseas tuition 2027 GBP 20000 per year</p>"
SCOPE = {"config": "config-v1", "parser": "release-v1", "audience": "international",
         "year": 2027, "identity": "BSc Accounting", "central": "source-v1"}


def result():
    return {"url": URL, "payload": {"course_name": "BSc Accounting",
        "international_fee": 20000, "currency": "GBP", "fee_term": "Annual", "fee_year": 2027},
        "evidence": [{"field_key": k, "normalized_value": v, "source_url": URL,
                      "raw_value": "BSc Accounting" if k == "course_name" else "Overseas tuition 2027 GBP 20000 per year",
                      "extraction_method": "regex", "decision_status": "selected"}
                     for k, v in (("course_name", "BSc Accounting"), ("international_fee", 20000))],
        "gemini_calls": 0}


@pytest.mark.asyncio
async def test_persisted_cache_reuses_exact_current_evidence_without_calling_extractor(store):
    async with store() as db:
        await cache.ensure_schema(db)
        await db.commit()
    extract = AsyncMock(return_value=result())
    first = await cache.reuse(URL, HTML, SCOPE, extract, session_factory=store)
    second = await cache.reuse(URL, HTML, SCOPE, extract, session_factory=store)
    assert first["payload"] == second["payload"]
    assert extract.await_count == 1 and second["_result_cache_hit"]
    # A caller cannot mutate the persisted authority.
    second["payload"]["international_fee"] = 1
    third = await cache.reuse(URL, HTML, SCOPE, extract, session_factory=store)
    assert third["payload"]["international_fee"] == 20000


@pytest.mark.asyncio
async def test_source_audience_year_identity_parser_and_config_invalidate_reuse(store):
    async with store() as db:
        await cache.ensure_schema(db)
        await db.commit()
    extract = AsyncMock(return_value=result())
    await cache.reuse(URL, HTML, SCOPE, extract, session_factory=store)
    changes = [{"config": "v2"}, {"parser": "v2"}, {"audience": "domestic"},
               {"year": 2028}, {"identity": "with placement"}, {"central": "new-source"}]
    for changed in changes:
        await cache.reuse(URL, HTML, {**SCOPE, **changed}, extract, session_factory=store)
    await cache.reuse(URL, HTML + " updated", SCOPE, extract, session_factory=store)
    assert extract.await_count == 8
    async with store() as db:
        await db.execute(text("UPDATE scrape_extraction_result_cache SET expires_at=now()-interval '1 second'"))
        await db.commit()
    await cache.reuse(URL, HTML, SCOPE, extract, session_factory=store)
    assert extract.await_count == 9


@pytest.mark.parametrize("change", ["ai", "central", "unselected", "unsupported", "contradictory", "error"])
def test_unproven_or_changed_evidence_is_never_reused(change):
    value = result()
    if change == "ai":
        value["evidence"][0]["extraction_method"] = "regex_openai"
    elif change == "central":
        value["evidence"][0]["source_url"] = "https://www.example.edu/central-fees"
    elif change == "unselected":
        value["evidence"][0]["decision_status"] = "rejected"
    elif change == "unsupported":
        value["payload"]["intake_months"] = ["February"]
    elif change == "contradictory":
        value["payload"]["international_fee"] = 1
    else:
        value["error"] = "per_course_timeout"
    assert not cache.authoritative(value, URL, HTML)


@pytest.mark.asyncio
async def test_unknown_source_or_version_performs_fresh_extraction():
    extract = AsyncMock(return_value=result())
    await cache.reuse(URL, None, SCOPE, extract)
    await cache.reuse(URL, HTML, {**SCOPE, "config": None}, extract)
    assert extract.await_count == 2


@pytest.mark.asyncio
async def test_real_extract_only_path_reuses_current_html_and_retains_evidence(store, monkeypatch):
    from app.services.scraper import orchestrator
    async with store() as db:
        await cache.ensure_schema(db)
        await db.commit()
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    monkeypatch.setattr(cache, "current_scope", lambda **kwargs: SCOPE)
    extract = AsyncMock(return_value=result())
    monkeypatch.setattr(orchestrator, "extract_course", extract)
    monkeypatch.setattr(orchestrator, "schedule_snapshot", lambda coro: coro.close())
    link = {"name": "BSc Accounting", "url": URL, "_surrey_html": HTML}
    first = await orchestrator._extract_only(link, "UK")
    second = await orchestrator._extract_only(link, "UK")
    assert extract.await_count == 1
    assert first["evidence"] == second["evidence"] and second["_result_cache_hit"]
