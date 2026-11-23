"""Exercise the actual nested source reader without database/model calls."""
import ast
import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
import uuid

import pytest

from app.services import scrape_assistant, assistant_source_cache as cache
from app.services import assistant_retained_source
from tests.test_assistant_access_failures import Store
from tests.test_aru_linked_english import URL, POLICY, page
from app.services.scraper.extractors import aru_linked_english as aru


@pytest.mark.asyncio
async def test_guidance_source_uses_its_own_bounded_window_after_course_budget_expires(monkeypatch):
    module = ast.parse(inspect.getsource(scrape_assistant))
    reader = next(node for node in ast.walk(module)
                  if isinstance(node, ast.AsyncFunctionDef) and node.name == "source_check")
    namespace = dict(
        asyncio=asyncio, time=SimpleNamespace(monotonic=lambda: 1000), uuid=uuid,
        source_slots=asyncio.Semaphore(6), source_deadline=999,
        thread_id="thread", turn_id="turn", run_id="run", ctx={"home": "https://www.aru.ac.uk"},
        prompt="verify IELTS", record_activity=AsyncMock(),
        event=lambda *args, **kwargs: kwargs, inspect_source=AsyncMock(),
        this_service=SimpleNamespace(), urlsplit=urlsplit, re=__import__("re"),
    )
    exec(compile(ast.Module(body=[reader], type_ignores=[]), "<real-source-reader>", "exec"), namespace)
    cached = AsyncMock(return_value={"url": aru.POLICY_URL, "verified": True, "sourceType": "html"})
    monkeypatch.setattr(cache, "cached_source", cached)
    expired = await namespace["source_check"](URL)
    assert not expired["verified"]
    cached.assert_not_awaited()
    result = await namespace["source_check"](aru.POLICY_URL, deadline=1015)
    assert result["verified"]
    assert cached.await_args.args[7] == 12
    assert cached.await_args.kwargs["html_english_scope_schema"] == aru.SCHEMA
    deferred_calls = [node for node in ast.walk(module) if isinstance(node, ast.Call)
                      and isinstance(node.func, ast.Name) and node.func.id == "source_check"
                      and any(k.arg == "deadline" for k in node.keywords)]
    assert len(deferred_calls) == 2
    assert all(any(k.arg == "deadline" and isinstance(k.value, ast.Name)
                   and k.value.id == "guidance_deadline" for k in node.keywords) for node in deferred_calls)


@pytest.mark.asyncio
async def test_aru_scope_upgrade_refreshes_once_but_never_retries_a_recent_403(monkeypatch):
    store = Store()
    store.thread.run_id = "run"
    old = {"url": URL, "verified": True, "sourceType": "html", "text": "Saved course",
           "_sourceScopeVersion": 2, "_sourceCheckedAt": 1000}
    store.thread.messages = [{"role": "user", "_sourceChecks": {URL: old}}]
    monkeypatch.setattr(assistant_retained_source, "retained_course_source", AsyncMock(return_value=None))
    fresh = {**old, "_aruEnglishScopeVersion": aru.SCHEMA,
             "_aruEnglishReferral": aru.referral(page(), URL)}
    read = AsyncMock(return_value=fresh)
    service = SimpleNamespace(AsyncSessionLocal=store)
    first = await cache.cached_source(service, "thread", "turn", "run", URL,
        "https://www.aru.ac.uk", read, 1, html_english_scope_schema=aru.SCHEMA)
    second = await cache.cached_source(service, "thread", "turn", "run", URL,
        "https://www.aru.ac.uk", read, 1, html_english_scope_schema=aru.SCHEMA)
    assert first["_aruEnglishReferral"] == second["_aruEnglishReferral"]
    read.assert_awaited_once()
    denied = {**old, "verified": False, "accessIssue": "robots_unavailable", "httpStatus": 403}
    store.thread.messages = [{"role": "user", "_sourceChecks": {URL: denied}}]
    monkeypatch.setattr(cache, "wall_time", lambda: 1001)
    read.reset_mock()
    result = await cache.cached_source(service, "thread", "turn", "run", URL,
        "https://www.aru.ac.uk", read, 1, html_english_scope_schema=aru.SCHEMA)
    assert not result["verified"] and result["httpStatus"] == 403
    read.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_derived_facts_cannot_combine_discontinuous_cached_excerpts():
    prefix = "IELTS (Academic) Overall 6.0"
    suffix = "with 5.5 in each of the four language skills (listening, speaking, reading and writing)"
    source = {"verified": True, "sourceType": "html", "text": prefix,
              "_fullText": prefix + " " + suffix,
              "excerpts": [{"text": suffix}], "facts": [], "_ieltsFactsVersion": 2}
    result = await cache.refresh_ielts_facts(source, aru.POLICY_URL)
    assert all(len(item["normalized"]) == 1 for item in result["facts"])
    assert result["_ieltsFactsVersion"] == cache.IELTS_FACTS_VERSION
