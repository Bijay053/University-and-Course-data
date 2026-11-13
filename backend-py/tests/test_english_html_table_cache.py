from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_source_cache as cache
from app.services import assistant_retained_source
from tests.test_assistant_access_failures import Store

URL = "https://uni.example/english"


@pytest.mark.asyncio
async def test_old_verified_html_is_refreshed_once_then_its_table_layout_is_reused(monkeypatch):
    store = Store()
    old = {"url": URL, "verified": True, "sourceType": "html", "text": "English guidance.",
           "_sourceScopeVersion": 2, "_sourceCheckedAt": 1000}
    store.thread.messages = [{"role": "user", "_sourceChecks": {URL: deepcopy(old)}}]
    monkeypatch.setattr(assistant_retained_source, "retained_course_source", AsyncMock(return_value=None))
    fresh = {**old, "_englishTablesSchema": 1, "_englishTables": [], "_ieltsFactsVersion": 2}
    reader = AsyncMock(return_value=fresh)
    service = SimpleNamespace(AsyncSessionLocal=store)
    first = await cache.cached_source(service, "thread", "turn", "owner", URL,
                                      "https://uni.example", reader, 1, html_table_schema=1)
    second = await cache.cached_source(service, "thread", "turn", "owner", URL,
                                       "https://uni.example", reader, 1, html_table_schema=1)
    assert first["_englishTablesSchema"] == second["_englishTablesSchema"] == 1
    assert second["_sourceCacheHit"]
    reader.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["pdf", "blocked"])
async def test_table_metadata_upgrade_never_refetches_cached_pdfs_or_recent_refusals(monkeypatch, kind):
    store = Store()
    source = {"url": URL, "verified": kind == "pdf", "sourceType": "pdf" if kind == "pdf" else "html",
              "text": "English guidance", "_sourceScopeVersion": 2, "_sourceCheckedAt": 1000}
    if kind == "blocked":
        source.update(accessIssue="robots_unavailable", httpStatus=403)
    store.thread.messages = [{"role": "user", "_sourceChecks": {URL: source}}]
    monkeypatch.setattr(cache, "wall_time", lambda: 1001)
    monkeypatch.setattr(assistant_retained_source, "retained_course_source", AsyncMock(return_value=None))
    reader = AsyncMock(side_effect=AssertionError("No repeat PDF or blocked-origin request"))
    result = await cache.cached_source(SimpleNamespace(AsyncSessionLocal=store),
        "thread", "turn", "owner", URL, "https://uni.example", reader, 1, html_table_schema=1)
    assert result["_sourceCacheHit"]
    reader.assert_not_awaited()
