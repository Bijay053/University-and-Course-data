"""Provider-access failures use real official-source replay, never auth bypass."""
import json
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.services.scraper import ai_repair_agent as agent, ai_repair_live as live
from app.services.scraper.config.schema import DiscoveryConfig
from app.services.scraper.config.loader import _deep_merge, get_config_for_host
from app.services.scraper.discovery_cache_scope import discovery_cache_scope_key
from app.services.scraper.provider_failure import access_denied, recognize_failure, ProviderAccessDenied
from app.services.ai_repair_workflow import accepted_live_probe, merge_audit, verification_payload
from tests.test_ai_repair_live import context, config, course, mock_loop, FakeDb, SEED, ONE, TWO
from tests.test_searchstax_hud import _FlakyAsyncClient, _cfg, _page
from app.services.scraper import searchstax_hud


def repair_context(**kwargs):
    cfg = config()
    cfg.discovery = DiscoveryConfig()
    return context(effective_config=cfg, effective_discovery={},
                   provider_failure=access_denied(), **kwargs)


def sitemap(urls):
    return '<?xml version="1.0"?><urlset>' + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"


def official_fetch(monkeypatch, *, bad=False, source_changes=False):
    calls = []
    async def fetch(url, *_args):
        calls.append(url)
        if url.endswith("sitemap.xml"):
            return sitemap([ONE, TWO] if not source_changes or calls.count(url) == 1 else [ONE]), "", ""
        return ("<main><h1>Our partners</h1></main>" if bad else course()), "", ""
    monkeypatch.setattr(live, "_fetch_official", fetch)
    return calls


def test_historical_detection_is_narrow_and_sanitized():
    error = "SearchStax provider returned 0 links — Provider error: none"
    message = "[SEARCHSTAX links_only] WARNING: page fetch failed at start=0 after 3 retries (Client error '401 Unauthorized' for url 'https://private-provider/?token=secret')"
    failure = recognize_failure({}, status="failed", error_message=error, logs=[message])
    assert failure == access_denied()
    assert "secret" not in json.dumps(failure) and "https:" not in json.dumps(failure)
    for status, count in [("completed", 0), ("failed", 1), ("running", 0)]:
        assert recognize_failure({}, status=status, total_found=count, error_message=error, logs=[message]) is None
    assert recognize_failure({}, status="failed", error_message=error, logs=["https://university.example/401"]) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("partial", [False, True])
async def test_auth_failure_never_returns_partial_catalogue(monkeypatch, partial):
    request = httpx.Request("GET", "https://provider.example/?token=secret")
    error = httpx.HTTPStatusError("secret", request=request, response=httpx.Response(401, request=request))
    responses = ({0: [_page([{"url_t": "/course/a", "title_t": "Course A"},
                             {"url_t": "/course/b", "title_t": "Course B"}], num_found=4)], 2: [error]}
                 if partial else {0: [error]})
    client = _FlakyAsyncClient(responses)
    monkeypatch.setattr(searchstax_hud.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(ProviderAccessDenied) as caught:
        await searchstax_hud._fetch_links_only(_cfg())
    assert caught.value.provider_failure == access_denied()
    assert "secret" not in str(caught.value)
    assert client.calls == ([0, 2] if partial else [0])


def test_override_disables_provider_through_merges_and_invalidates_cache():
    base = {"searchstax": {"endpoint": "https://provider.example/search", "links_only": True}}
    healthy = DiscoveryConfig(**base)
    merged = _deep_merge(base, {"official_catalogue_fallback": True, "sitemap_url": SEED + "/sitemap.xml"})
    merged = _deep_merge(merged, base)
    fallback = DiscoveryConfig(**merged)
    assert fallback.searchstax is None
    assert healthy.searchstax is not None
    assert discovery_cache_scope_key(scrape_url=SEED, discovery_config=healthy) != discovery_cache_scope_key(scrape_url=SEED, discovery_config=fallback)


@pytest.mark.asyncio
async def test_official_source_replay_and_fresh_validation(monkeypatch):
    calls = official_fetch(monkeypatch)
    evidence = live.LiveRepairEvidence(repair_context())
    await evidence.probe()
    patch = {"official_catalogue_fallback": True, "sitemap_url": evidence.fallback["source"]}
    report = await evidence.validate({}, patch, {})
    assert report["accepted"]
    assert calls.count(ONE) == calls.count(TWO) == 2
    assert calls.count(evidence.fallback["source"]) == 2
    assert evidence.pages_checked <= 12
    assert not evidence.discovery_validation({}, {**patch, "sitemap_url": "https://evil.example/map.xml"})["accepted"]


@pytest.mark.asyncio
@pytest.mark.parametrize("bad,changed", [(True, False), (False, True)])
async def test_no_write_for_unverified_or_changed_sources(monkeypatch, tmp_path, bad, changed):
    ctx = repair_context(yaml_file=tmp_path / "university.yaml", unis_dir=tmp_path)
    chat = mock_loop(monkeypatch, ctx, {})
    official_fetch(monkeypatch, bad=bad, source_changes=changed)
    db = FakeDb()
    result = await agent.run_ai_repair_loop("job-live", db)
    assert result["status"] == "failed"
    assert not db.writes and not (tmp_path / "university.yaml").exists()
    assert not accepted_live_probe(result)
    chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_uses_existing_cas_apply_and_verification_contract(monkeypatch, tmp_path):
    ctx = repair_context(yaml_file=tmp_path / "university.yaml", unis_dir=tmp_path)
    chat = mock_loop(monkeypatch, ctx, {})
    official_fetch(monkeypatch)
    monkeypatch.setattr(agent, "_assert_effective_discovery_patch", AsyncMock())
    db = FakeDb()
    result = await agent.run_ai_repair_loop("job-live", db)
    assert result["status"] == "completed", result.get("error")
    assert accepted_live_probe(result), result["attempts"]
    saved = json.loads(db.writes[0]["cfg"])
    assert saved["admin_config"]["discovery"]["official_catalogue_fallback"] is True
    assert json.loads(db.writes[0]["expected"]) == {}
    chat.assert_not_awaited()
    parent = SimpleNamespace(url=SEED, university_id=1, university_name="University", runtime_job_id="job-live")
    child = verification_payload(result, parent)
    assert child["forceDiscovery"] and child["autonomousVerification"]["max_courses"] == 50
    assert "courseUrls" not in child  # rediscover actual catalogue, not just samples


def test_progress_reload_preserves_owned_evidence_not_stale_workers():
    backup = {"session_id": "s", "status": "running", "autonomous": {"enabled": True, "worker_claim": "owner", "worker_generation": 2}}
    audit = deepcopy(backup)
    audit["autonomous"]["discovery_repair"] = {"status": "validating", "candidate_count": 2}
    assert merge_audit(backup, audit)["autonomous"]["discovery_repair"]["candidate_count"] == 2
    audit["autonomous"]["worker_generation"] = 1
    assert "discovery_repair" not in merge_audit(backup, audit)["autonomous"]


def test_official_host_and_sitemap_entity_safety():
    for url in ["https://evil.example/courses/law", "https://university.example.evil/courses/law",
                "http://user:secret@university.example/courses/law", "https://127.0.0.1/courses/law"]:
        assert not live.official_url(url, SEED)
    assert live.inspect_page(SEED, '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x SYSTEM "file:///etc/passwd">]><urlset/>')["classification"] == "unconfirmed"


def test_merged_reload_preserves_existing_leeds_degree_exception():
    cfg = get_config_for_host(
        hostname="www.leedstrinity.ac.uk", name="Leeds Trinity University",
        scrape_url="https://www.leedstrinity.ac.uk/courses/", university_id=89,
        db_scrape_config={"admin_config": {"discovery": {"official_catalogue_fallback": True}}},
        create_missing_stub=False,
    )
    assert cfg.discovery.searchstax is None
    assert cfg.extraction.staging.skip_degree_qualifier_check is True


@pytest.mark.asyncio
async def test_cas_loss_preserves_operator_config_and_restores_yaml(monkeypatch, tmp_path):
    ctx = repair_context(yaml_file=tmp_path / "university.yaml", unis_dir=tmp_path)
    mock_loop(monkeypatch, ctx, {})
    official_fetch(monkeypatch)
    db = FakeDb(cfg={"admin_config": {"operator": "newer"}})
    result = await agent.run_ai_repair_loop("job-live", db)
    assert result["status"] == "failed"
    assert db.cfg["admin_config"]["operator"] == "newer"
    assert not db.writes
    assert not (tmp_path / "university.yaml").exists()
    assert not accepted_live_probe(result)


@pytest.mark.asyncio
async def test_official_host_resolving_private_is_never_fetched(monkeypatch):
    from app.services import scraper_config_ai
    monkeypatch.setattr(scraper_config_ai, "_is_safe_public_url", lambda url: (False, "private address"))
    client = AsyncMock()
    monkeypatch.setattr(live.httpx, "AsyncClient", client)
    record = await live.LiveRepairEvidence(repair_context()).fetch(ONE)
    assert record["classification"] == "unsafe_url"
    client.assert_not_called()


@pytest.mark.asyncio
async def test_unpaginated_official_catalogue_alternative_and_paginated_refusal(monkeypatch):
    from tests.test_ai_repair_live import LISTING
    async def fetch(url, *_args):
        return ("", "not_published", "HTTP 404") if url.endswith(".xml") else (LISTING, "", "")
    monkeypatch.setattr(live, "_fetch_official", fetch)
    from app.services.scraper.official_catalogue_repair import discover_official_catalogue
    result = await discover_official_catalogue(live.LiveRepairEvidence(repair_context()))
    assert result["source"] == SEED and len(result["candidates"]) == 2
    async def paginated(url, *_args):
        return ("", "not_published", "HTTP 404") if url.endswith(".xml") else (LISTING + '<a rel="next" href="?page=2">Next page</a>', "", "")
    monkeypatch.setattr(live, "_fetch_official", paginated)
    result = await discover_official_catalogue(live.LiveRepairEvidence(repair_context()))
    assert not result["candidates"]


@pytest.mark.asyncio
@pytest.mark.skipif(os.environ.get("RUN_PUBLIC_CATALOGUE_ACCEPTANCE") != "1",
                    reason="Opt-in bounded public website acceptance; no database or credentials")
async def test_real_leeds_official_source_acceptance():
    url = "https://www.leedstrinity.ac.uk/courses/"
    cfg = get_config_for_host(
        hostname="www.leedstrinity.ac.uk", name="Leeds Trinity University",
        scrape_url=url, university_id=89, db_scrape_config={}, create_missing_stub=False,
    )
    evidence = live.LiveRepairEvidence({
        "scrape_url": url, "effective_config": cfg,
        "provider_failure": access_denied(), "effective_discovery": cfg.discovery.model_dump(),
    })
    await evidence.probe()
    assert len(evidence.fallback["candidates"]) >= 2
    patch = {"official_catalogue_fallback": True, "sitemap_url": evidence.fallback["source"]}
    report = await evidence.validate(cfg.discovery.model_dump(), patch, {})
    assert report["accepted"], report
    assert len(report["courses"]) >= 2 and evidence.pages_checked <= 12
    assert cfg.discovery.searchstax is not None  # probe never mutates configuration