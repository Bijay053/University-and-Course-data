"""Tests for the fetch-layer brief (A3, B, C1, C4).

A3 — http_fetcher final-failure registry (+ discovery error-line wiring)
B  — account-wide Scrape.do Redis semaphore (scrape_do_semaphore.py)
C1 — 7-day discovery URL cache (model, schema plumbing, orchestrator gates)
C4 — per-phase DONE-line timing

Run standalone (avoids the session-scoped event-loop conflict and the heavy
conftest import chain under Celery load):

    cd backend-py && PYTHONPATH=. python -m pytest tests/test_fetch_layer_brief.py \
        -q -c /dev/null -p no:cacheprovider
"""
from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest
import pytest_asyncio

_SCRAPER_DIR = Path(__file__).resolve().parent.parent / "app" / "services" / "scraper"
_ORCH_SRC = (_SCRAPER_DIR / "orchestrator.py").read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# A3 — fetch-error registry
# ═══════════════════════════════════════════════════════════════════════════

class TestFetchErrorRegistry:
    def setup_method(self):
        from app.services.scraper import http_fetcher as hf
        hf._last_fetch_errors.clear()

    def test_record_and_get_roundtrip(self):
        from app.services.scraper import http_fetcher as hf
        hf._record_fetch_error(
            "https://x.edu/c1", status=429, tier="scrape_do", detail="rate limited"
        )
        err = hf.get_last_fetch_error("https://x.edu/c1")
        assert err is not None
        assert err["status"] == 429
        assert err["tier"] == "scrape_do"
        assert err["detail"] == "rate limited"
        assert err["ts"] <= time.time()

    def test_bare_url_fallback_when_query_stripped(self):
        from app.services.scraper import http_fetcher as hf
        hf._record_fetch_error("https://x.edu/c2", status=403, tier="cffi")
        # Discovery retries may re-query with params appended.
        err = hf.get_last_fetch_error("https://x.edu/c2?international=true")
        assert err is not None and err["status"] == 403

    def test_unknown_url_returns_none_and_empty_format(self):
        from app.services.scraper import http_fetcher as hf
        assert hf.get_last_fetch_error("https://never-seen.edu/") is None
        assert hf.format_fetch_error("https://never-seen.edu/") == ""

    def test_format_fetch_error_one_liner(self):
        from app.services.scraper import http_fetcher as hf
        hf._record_fetch_error(
            "https://x.edu/c3", status=503, tier="wayback", detail="cf challenge"
        )
        line = hf.format_fetch_error("https://x.edu/c3")
        assert "HTTP 503" in line
        assert "tier=wayback" in line
        assert "cf challenge" in line

    def test_registry_bounded_eviction(self):
        from app.services.scraper import http_fetcher as hf
        for i in range(hf._MAX_FETCH_ERRORS + 10):
            hf._record_fetch_error(f"https://x.edu/{i}", status=500)
        assert len(hf._last_fetch_errors) <= hf._MAX_FETCH_ERRORS
        # Oldest evicted, newest kept.
        assert hf.get_last_fetch_error("https://x.edu/0") is None
        assert hf.get_last_fetch_error(
            f"https://x.edu/{hf._MAX_FETCH_ERRORS + 9}"
        ) is not None

    def test_detail_truncated_to_200(self):
        from app.services.scraper import http_fetcher as hf
        hf._record_fetch_error("https://x.edu/c4", detail="A" * 1000)
        err = hf.get_last_fetch_error("https://x.edu/c4")
        assert len(err["detail"]) == 200


# ═══════════════════════════════════════════════════════════════════════════
# B — account-wide Scrape.do semaphore
# ═══════════════════════════════════════════════════════════════════════════

@pytest_asyncio.fixture(loop_scope="session")
async def _sem_env(monkeypatch):
    """Enable the semaphore (cap=2) against local dev Redis; clean key."""
    from app.config import settings
    from app.services.scraper import scrape_do_semaphore as sem
    monkeypatch.setattr(settings, "scrape_do_account_concurrency", 2)
    monkeypatch.setattr(sem, "_MAX_WAIT_S", 1.0)

    async def _clean():
        import redis.asyncio as aioredis
        c = aioredis.from_url(settings.redis_url, decode_responses=True)
        try:
            await c.delete(sem._KEY)
        finally:
            await c.aclose()

    await _clean()
    yield sem
    await _clean()


class TestScrapeDoSemaphore:
    async def test_disabled_returns_none_without_redis(self, monkeypatch):
        from app.config import settings
        from app.services.scraper import scrape_do_semaphore as sem
        monkeypatch.setattr(settings, "scrape_do_account_concurrency", 0)
        assert await sem.acquire_slot() is None

    async def test_acquire_release_cycle(self, _sem_env):
        sem = _sem_env

        async def flow():
            t1 = await sem.acquire_slot()
            t2 = await sem.acquire_slot()
            assert t1 and t2 and t1 != t2
            # Saturated: third acquire exhausts the (patched 1s) wait budget
            # and fails open with None.
            t3 = await sem.acquire_slot()
            assert t3 is None
            # Releasing one slot frees capacity again.
            await sem.release_slot(t1)
            t4 = await sem.acquire_slot()
            assert t4 is not None
            await sem.release_slot(t2)
            await sem.release_slot(t4)

        await flow()

    async def test_stale_holder_reaped(self, _sem_env):
        sem = _sem_env
        from app.config import settings

        async def flow():
            import redis.asyncio as aioredis
            c = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                # Two dead holders acquired > TTL ago.
                stale = time.time() - sem._HOLD_TTL_S - 10
                await c.zadd(sem._KEY, {"dead1": stale, "dead2": stale})
            finally:
                await c.aclose()
            # Reap happens inside the acquire Lua — both stale slots cleared.
            t = await sem.acquire_slot()
            assert t is not None
            await sem.release_slot(t)

        await flow()

    async def test_fail_open_on_unreachable_redis(self, monkeypatch):
        from app.config import settings
        from app.services.scraper import scrape_do_semaphore as sem
        monkeypatch.setattr(settings, "scrape_do_account_concurrency", 2)
        monkeypatch.setattr(settings, "redis_url", "redis://127.0.0.1:1/0")
        loop = asyncio.get_running_loop()
        previous = sem._clients.pop(loop, None)
        if previous is not None:
            await previous.aclose()
        try:
            assert await sem.acquire_slot() is None  # no exception — fail open
        finally:
            unreachable = sem._clients.pop(loop, None)
            if unreachable is not None:
                await unreachable.aclose()

    async def test_account_slot_context_manager_releases(self, _sem_env):
        sem = _sem_env
        from app.config import settings

        async def flow():
            async with sem.account_slot():
                pass
            import redis.asyncio as aioredis
            c = aioredis.from_url(settings.redis_url, decode_responses=True)
            try:
                assert await c.zcard(sem._KEY) == 0
            finally:
                await c.aclose()

        await flow()

    async def test_release_none_token_is_noop(self):
        from app.services.scraper import scrape_do_semaphore as sem
        await sem.release_slot(None)  # must not raise

    def test_http_fetcher_nests_account_slot_inside_local_sem(self):
        # Local per-process semaphore FIRST, fleet-wide Redis slot second —
        # a coroutine must never hold a scarce account slot while merely
        # queueing behind its own process's semaphore.
        src = (_SCRAPER_DIR / "http_fetcher.py").read_text(encoding="utf-8")
        i = src.index("async with _get_scrape_do_sem(")
        assert "async with account_slot():" in src[i : i + 120], (
            "account_slot must be nested INSIDE the local Scrape.do semaphore"
        )

    def test_local_semaphores_are_per_event_loop(self):
        # Regression for the JCU whole-job discovery failure (2026-07-09):
        # module-level asyncio.Semaphore binds to the first loop that awaits
        # it, so the SECOND scrape job in the same prefork worker process
        # failed every Scrape.do call with "bound to a different event loop".
        import importlib

        hf = importlib.import_module("app.services.scraper.http_fetcher")

        async def grab():
            return hf._get_sem(), hf._get_scrape_do_sem()

        loop1 = asyncio.new_event_loop()
        try:
            s1a, s1b = loop1.run_until_complete(grab())
            s1a2, s1b2 = loop1.run_until_complete(grab())
        finally:
            loop1.close()
        loop2 = asyncio.new_event_loop()
        try:
            s2a, s2b = loop2.run_until_complete(grab())
        finally:
            loop2.close()
        assert s1a is s1a2 and s1b is s1b2, "same loop must reuse its semaphores"
        assert s2a is not s1a and s2b is not s1b, (
            "a new event loop must get FRESH semaphores"
        )
        # No module-level bound primitives may remain in either module.
        for mod in ("http_fetcher.py", "stealth_browser.py"):
            src2 = (_SCRAPER_DIR / mod).read_text(encoding="utf-8")
            import re
            bad = re.findall(
                r"^_\w+(?::[^=\n]+)?\s*=\s*asyncio\.(?:Semaphore|Lock|Event|Condition)\(",
                src2,
                flags=re.M,
            )
            assert not bad, f"{mod} still has module-level asyncio primitives: {bad}"

    async def test_semaphore_reuses_per_loop_client(self):
        from app.services.scraper import scrape_do_semaphore as sem

        async def flow():
            c1 = sem._get_client()
            c2 = sem._get_client()
            assert c1 is c2, "same loop must reuse one Redis client"

        await flow()

    def test_config_alias_choices(self):
        from app.config import Settings
        field = Settings.model_fields["scrape_do_account_concurrency"]
        aliases = getattr(field.validation_alias, "choices", [])
        assert "SCRAPEDO_MAX_CONCURRENCY" in aliases


# ═══════════════════════════════════════════════════════════════════════════
# C1 — 7-day discovery URL cache
# ═══════════════════════════════════════════════════════════════════════════

class TestDiscoveryUrlCacheC1:
    def test_start_scrape_body_parses_force_discovery_alias(self):
        from app.schemas.scrape import StartScrapeBody
        assert StartScrapeBody(universityId=1, forceDiscovery=True).force_discovery
        assert StartScrapeBody(university_id=1, force_discovery=True).force_discovery
        assert StartScrapeBody(universityId=1).force_discovery is False

    def test_router_stores_force_discovery_in_request_payload(self):
        src = Path("app/routers/scrape.py").read_text(encoding="utf-8")
        assert '"forceDiscovery": bool(body.force_discovery)' in src

    async def test_model_roundtrip_dev_db(self):
        from datetime import datetime, timezone
        from sqlalchemy import delete
        from app.database import AsyncSessionLocal
        from app.models import DiscoveryUrlCache

        async def flow():
            links = [{"name": f"C{i}", "url": f"https://x.edu/c{i}"} for i in range(6)]
            async with AsyncSessionLocal() as db:
                await db.execute(
                    delete(DiscoveryUrlCache).where(
                        DiscoveryUrlCache.university_id == 999_999
                    )
                )
                db.add(
                    DiscoveryUrlCache(
                        university_id=999_999,
                        links=links,
                        link_count=6,
                        discovered_at=datetime.now(timezone.utc),
                    )
                )
                await db.commit()
            async with AsyncSessionLocal() as db:
                row = await db.get(DiscoveryUrlCache, 999_999)
                assert row is not None
                assert row.link_count == 6
                assert row.links[0]["url"] == "https://x.edu/c0"
                assert row.discovered_at.tzinfo is not None
                await db.delete(row)
                await db.commit()

        await flow()

    # Orchestrator gate wiring (source-level, mirrors the parity-test pattern —
    # importing orchestrator pulls the full genai chain, which stalls under
    # worker load).
    def test_orchestrator_cache_read_gates(self):
        assert '_c1_rp.get("forceDiscovery")' in _ORCH_SRC
        assert "_c1_has_api_provider = (" in _ORCH_SRC
        assert "not _c1_has_api_provider" in _ORCH_SRC
        assert "_c1_scope_matches" in _ORCH_SRC
        assert "_c1_coverage_ok" in _ORCH_SRC
        assert "_c1_age_d < 7.0" in _ORCH_SRC
        # Cache hit must disable browser discovery.
        i = _ORCH_SRC.index("_disc_cache_hit = True")
        assert "_always_browser = False" in _ORCH_SRC[i : i + 120]

    def test_orchestrator_wayback_gate_respects_cache_hit(self):
        i = _ORCH_SRC.index("_use_wayback =")
        assert "and not _disc_cache_hit" in _ORCH_SRC[i : i + 800]

    def test_orchestrator_capture_skips_provider_payloads(self):
        assert (
            '_c1_payload_keys = ("searchstax_result", "swiftype_result", "payload")'
            in _ORCH_SRC
        )
        i = _ORCH_SRC.index("_c1_links_for_cache: list[dict] = []")
        block = _ORCH_SRC[i : i + 700]
        assert "not _targeted_retry and not _disc_cache_hit and links" in block

    def test_orchestrator_write_through_health_gates(self):
        # Gate counts COURSE links only (fee-page entries excluded).
        assert "_c1_fail_rate < 0.30" in _ORCH_SRC
        assert "_c1_cache_coverage_ok" in _ORCH_SRC
        assert "on_conflict_do_update" in _ORCH_SRC

    def test_orchestrator_persists_and_restores_blocked_fee_urls(self):
        # Capture side: BFS-blocked fee URLs stored with fee_page=True marker.
        assert '{"url": _u, "fee_page": True}' in _ORCH_SRC
        # Read side: fee entries split back into _discover_blocked_fee_urls
        # and metadata is excluded from the course-link freshness count.
        assert 'and not _lk.get("fee_page")' in _ORCH_SRC
        assert 'and not _lk.get("cache_meta")' in _ORCH_SRC
        i = _ORCH_SRC.index("_disc_cache_hit = True")
        pre = _ORCH_SRC[max(0, i - 800) : i]
        assert "_discover_blocked_fee_urls.extend(" in pre

    def test_orchestrator_requires_matching_cache_scope(self):
        assert "discovery_cache_scope_key as _discovery_cache_scope_key" in _ORCH_SRC
        assert '_c1_meta.get("scope_key") == _c1_scope_key' in _ORCH_SRC
        assert "legacy/unscoped" in _ORCH_SRC

    def test_migration_script_creates_table(self):
        src = Path("scripts/apply_migration_046.py").read_text(encoding="utf-8")
        assert "CREATE TABLE IF NOT EXISTS discovery_url_cache" in src
        assert "university_id INTEGER PRIMARY KEY" in src


# ═══════════════════════════════════════════════════════════════════════════
# C4 — per-phase DONE-line timing
# ═══════════════════════════════════════════════════════════════════════════

class TestPhaseTimingC4:
    def test_phase_marks_exist(self):
        for mark in ('_ph_marks["disc_end"]', '_ph_marks["sweep_start"]',
                     '_ph_marks["sweep_end"]'):
            assert mark in _ORCH_SRC, f"missing phase mark {mark}"

    def test_done_line_includes_phase_breakdown(self):
        for label in ("Discovery:", "Extraction:", "Sweep:", "Staging:"):
            assert label in _ORCH_SRC, f"DONE line missing {label} timing"

    def test_negative_durations_guarded(self):
        assert "max(0" in _ORCH_SRC.split("Discovery:")[0][-2500:] or (
            "max(0" in _ORCH_SRC
        )

    def test_done_emit_carries_phase_timings_kwarg(self):
        assert "phase_timings=" in _ORCH_SRC
