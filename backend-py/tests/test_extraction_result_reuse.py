from copy import deepcopy
from unittest.mock import AsyncMock
from contextlib import asynccontextmanager
import httpx
import pytest
import pytest_asyncio
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


@pytest.mark.parametrize("method,accepted", [
    ("course_name.title", True), ("course_name.h1", True),
    ("course_name.title_openai", False), ("course_name.guessed", False),
])
def test_real_title_methods_still_require_selected_literal_source_proof(method, accepted):
    value = result()
    value["evidence"][0]["extraction_method"] = method
    assert cache.authoritative(value, URL, HTML) is accepted
    value["evidence"][0]["raw_value"] = "A title not on the source"
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


@pytest.mark.asyncio
async def test_stale_source_config_audience_year_cannot_relabel_cached_authority(store):
    """A prior audience/year receipt cannot become authority for a new retry."""
    async with store() as db:
        await cache.ensure_schema(db)
        await db.commit()
    prior = result()
    extract = AsyncMock(return_value=prior)
    await cache.reuse(URL, HTML, SCOPE, extract, session_factory=store)
    original_key = cache.cache_key(URL, HTML, SCOPE)
    current_html = HTML.replace("2027 GBP 20000", "2028 GBP 21000")
    current_scope = {**SCOPE, "config": "config-v2", "audience": "international-new-intake", "year": 2028}
    fresh = result()
    fresh["payload"].update(international_fee=21000, fee_year=2028)
    fresh["evidence"][1].update(normalized_value=21000,
        raw_value="Overseas tuition 2028 GBP 21000 per year")
    refresh = AsyncMock(return_value=fresh)
    recovered = await cache.reuse(URL, current_html, current_scope, refresh, session_factory=store)
    assert refresh.await_count == 1 and not recovered.get("_result_cache_hit")
    assert recovered["payload"]["international_fee"] == 21000
    assert recovered["evidence"] == fresh["evidence"]
    # Persisted older authority remains intact, under its original exact key.
    async with store() as db:
        saved = await db.scalar(text(
            "SELECT payload FROM scrape_extraction_result_cache WHERE cache_key=:key"),
            {"key": original_key})
    assert saved["scope"] == SCOPE and saved["result"] == prior
    hit = await cache.reuse(URL, current_html, current_scope, refresh, session_factory=store)
    assert hit["_result_cache_hit"] and refresh.await_count == 1


@pytest_asyncio.fixture
async def ordinary_transport(monkeypatch, store):
    """Keep fetch_html and extract_course real; replace only the HTTP socket."""
    from app.services.scraper import http_fetcher, orchestrator
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.schema import UniConfig
    async with store() as db:
        await cache.ensure_schema(db)
        await db.commit()
    config = UniConfig(slug="example", name="Example University",
                       base_url="https://www.example.edu", scrape_url=URL)
    config.extraction.skip_remote_ai_enrichment = True
    config.extraction.skip_per_course_browser = True
    config.extraction.skip_browser_rescue = True
    token = current_uni_config.set(config)
    state = {"html": HTML, "status": 200, "urls": []}

    def respond(request):
        state["urls"].append(str(request.url))
        return httpx.Response(state["status"], text=state["html"],
                              headers={"Content-Type": "text/html"}, request=request)

    @asynccontextmanager
    async def client():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as transport:
            yield transport

    monkeypatch.setattr(http_fetcher, "_client", client)
    monkeypatch.setattr(cache, "current_scope", lambda **kwargs: SCOPE)
    monkeypatch.setattr(orchestrator, "schedule_snapshot", lambda coro: coro.close())
    async def forbidden(*args, **kwargs):
        raise AssertionError("No secondary download or AI allowed")
    for fallback in ("fetch_html_scrape_do", "fetch_html_cffi", "fetch_html_wayback"):
        monkeypatch.setattr(http_fetcher, fallback, forbidden)
    monkeypatch.setattr("app.services.scraper.extractors.gemini_primary.extract_primary", forbidden)
    try:
        yield state
    finally:
        current_uni_config.reset(token)


@pytest.mark.asyncio
async def test_ordinary_url_retry_fetches_current_bytes_once_and_reuses_proof(
    store, monkeypatch, ordinary_transport,
):
    from app.services.scraper import orchestrator
    await cache.remember(URL, HTML, SCOPE, result(), session_factory=store)
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    emit = AsyncMock()
    link = {"url": URL, "name": "BSc Accounting"}
    for _ in range(2):
        hit = await orchestrator._extract_only(link, "UK", emit=emit)
        assert hit["_result_cache_hit"]
        assert hit["payload"] == result()["payload"]
        assert hit["evidence"] == result()["evidence"]
    assert ordinary_transport["urls"] == [URL, URL]
    assert "_surrey_html" not in link and "_uel_html" not in link
    assert sum(c.kwargs.get("kind") == "selective_retry_cache_hit"
               for c in emit.await_args_list) == 2
    async with store() as db:
        pending = (await db.execute(text(
            "SELECT course_name, international_fee FROM scraped_courses WHERE id=1"
        ))).one()
    assert pending.course_name == "BSc Music Technology" and pending.international_fee == 9790


@pytest.mark.asyncio
async def test_ordinary_miss_continues_on_same_bytes_and_persists_only_proven_results(
    store, monkeypatch, ordinary_transport,
):
    from app.services.scraper import orchestrator
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    # The real downstream extractors still run on a miss. Observe their input
    # rather than replacing extract_course or inserting provider HTML.
    from app.services.scraper.extractors import course_name
    original = course_name.extract
    seen = []
    async def title(html, url, **kwargs):
        seen.append(html)
        return await original(html, url, **kwargs)
    monkeypatch.setattr(course_name, "extract", title)
    fresh = await orchestrator._extract_only({"url": URL, "name": "BSc Accounting"}, "UK")
    assert not fresh.get("error"), fresh
    assert not fresh.get("_result_cache_hit")
    assert seen and "BSc Accounting" in seen[0]
    assert ordinary_transport["urls"] == [URL]
    async with store() as db:
        saved = await db.scalar(text("SELECT count(*) FROM scrape_extraction_result_cache"))
    assert saved == int(cache.authoritative(fresh, URL, HTML))


@pytest.mark.asyncio
async def test_changed_ordinary_source_cannot_reuse_old_fee(store, monkeypatch, ordinary_transport):
    from app.services.scraper import orchestrator
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    await cache.remember(URL, HTML, SCOPE, result(), session_factory=store)
    ordinary_transport["html"] = (
        "<h1>BSc Accounting</h1><p>Overseas tuition GBP 21000 per year (2027)</p>"
    )
    fresh = await orchestrator._extract_only({"url": URL, "name": "BSc Accounting"}, "UK")
    assert not fresh.get("_result_cache_hit") and not fresh.get("error"), fresh
    assert fresh["payload"]["international_fee"] == 21000
    assert ordinary_transport["urls"] == [URL]


@pytest.mark.asyncio
async def test_failed_ordinary_transport_cannot_use_warm_result(store, monkeypatch, ordinary_transport):
    from app.services.scraper import orchestrator
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    await cache.remember(URL, HTML, SCOPE, result(), session_factory=store)
    ordinary_transport["status"] = 403
    ordinary_transport["html"] = "Forbidden"
    failed = await orchestrator._extract_only({"url": URL}, "UK")
    assert failed.get("error") and not failed.get("_result_cache_hit")
    assert not failed.get("payload")


@pytest.mark.asyncio
async def test_ordinary_rewrite_uses_effective_audience_url_for_source_identity(
    store, monkeypatch, ordinary_transport,
):
    from app.services.scraper import orchestrator
    from app.services.scraper.config.context import current_uni_config
    from app.services.scraper.config.schema import UrlRewrite
    current_uni_config.get().extraction.url_rewrites = [
        UrlRewrite(host="www.example.edu", append_query="audience=international"),
    ]
    target = URL + "?audience=international"
    proof = result()
    proof["url"] = target
    for evidence in proof["evidence"]:
        evidence["source_url"] = target
    await cache.remember(target, HTML, SCOPE, proof, session_factory=store)
    monkeypatch.setattr("app.database.AsyncSessionLocal", store)
    hit = await orchestrator._extract_only({"url": URL, "name": "BSc Accounting"}, "UK")
    assert hit["_result_cache_hit"] and hit["url"] == target
    assert ordinary_transport["urls"] == [target]
    assert all(e["source_url"] == target for e in hit["evidence"])


@pytest.mark.parametrize("method", ["wayback", "uel_variant_source", "unknown"])
def test_noncurrent_or_unproven_transport_is_not_reuse_authority(method):
    from app.services.scraper.snapshot_context import (
        current_source_scope, is_current_source, stage_snapshot,
    )
    with current_source_scope():
        stage_snapshot(URL, HTML, method)
        assert not is_current_source(URL, HTML)
        stage_snapshot(URL, HTML, "httpx")
        assert is_current_source(URL, HTML)
        assert not is_current_source(URL, HTML + " changed")
        assert not is_current_source(URL + "/other", HTML)
    with current_source_scope():
        assert not is_current_source(URL, HTML)


def test_replay_and_nested_capture_cannot_leak_current_source_proof():
    from app.services.scraper.snapshot_context import (
        current_source_scope, is_current_source, replay_mode_scope, stage_snapshot,
    )
    with current_source_scope():
        stage_snapshot(URL, HTML, "httpx")
        with current_source_scope():
            assert not is_current_source(URL, HTML)
        assert is_current_source(URL, HTML)
        with replay_mode_scope():
            stage_snapshot(URL, HTML, "httpx")
            assert not is_current_source(URL, HTML)
