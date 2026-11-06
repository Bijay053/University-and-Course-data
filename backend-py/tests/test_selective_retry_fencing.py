"""Real generation checks for selective children; never shared application data."""
from uuid import uuid4
from unittest.mock import AsyncMock
import pytest
from sqlalchemy import text
from app.database import Base
from app.models import ScrapeRuntimeJob
from app.services import worker_fencing as fences
from app.services.scraper import orchestrator
from tests.test_worker_fencing import db_factory, local_postgres


async def child(factory):
    engine = factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE TABLE IF NOT EXISTS retry_test_effects (value TEXT)"))
    jid = "retry-" + uuid4().hex
    async with factory() as db:
        db.add(ScrapeRuntimeJob(runtime_job_id=jid, job_type="scrape", status="queued",
                               request_payload={"selectiveRetry": {"test": True}}))
        await db.commit()
    return jid


@pytest.mark.asyncio
async def test_duplicate_delivery_cannot_enter_selected_worker_twice(db_factory, monkeypatch):
    jid = await child(db_factory)
    calls = []
    async def entry(db, job_id):
        assert fences.current_owner.get().key == "selective-retry:" + jid
        calls.append(job_id)
        job = await db.get(ScrapeRuntimeJob, job_id)
        job.status = "completed"
        await db.commit()
        return {"ok": True}
    monkeypatch.setattr(orchestrator, "_run_scrape_entry", entry)
    async with db_factory() as db:
        assert (await orchestrator.run_scrape(db, jid))["ok"]
    async with db_factory() as db:
        assert (await orchestrator.run_scrape(db, jid))["reason"] == "already_claimed"
    assert calls == [jid]


@pytest.mark.asyncio
async def test_generation_revocation_rejects_independent_late_writes(db_factory, monkeypatch):
    jid = await child(db_factory)
    async def entry(db, job_id):
        await db.commit()  # lifecycle/lease transaction finishes before external work
        async with db_factory.kw["bind"].begin() as control:
            await control.execute(text(
                "UPDATE autonomous_worker_claims SET state='revoked',proof='worker_lost' WHERE claim_key=:key"
            ), {"key": "selective-retry:" + jid})
        async with db_factory() as late:
            await late.execute(text("INSERT INTO retry_test_effects(value) VALUES (:value)"), {"value": jid})
            await late.commit()
    monkeypatch.setattr(orchestrator, "_run_scrape_entry", entry)
    async with db_factory() as db:
        result = await orchestrator.run_scrape(db, jid)
    assert result["reason"] == "ownership_revoked"
    async with db_factory() as db:
        assert not (await db.execute(text(
            "SELECT value FROM retry_test_effects WHERE value=:value"), {"value": jid})).first()


@pytest.mark.asyncio
async def test_lost_ownership_never_falls_back_to_uncached_network(db_factory):
    from app.services.scraper.result_reuse import reuse
    async with db_factory() as db:
        owner = await fences.claim(db, "retry-cache-" + uuid4().hex)
        await db.commit()
    async with db_factory.kw["bind"].begin() as control:
        await control.execute(text("UPDATE autonomous_worker_claims SET state='revoked' WHERE claim_key=:key"), {"key": owner.key})
    fresh = AsyncMock()
    with fences.ownership_scope(owner), pytest.raises(fences.OwnershipLost):
        await reuse("https://example.edu/course", "<h1>Course</h1>",
                    {"config": "cfg", "parser": "parser"}, fresh, session_factory=db_factory)
    fresh.assert_not_called()


@pytest.mark.asyncio
async def test_selected_worker_uses_real_generation_aware_broker_locks(db_factory):
    """Selected retries must reach transport, without weakening Redis fencing."""
    from redis.asyncio import Redis
    from tests.helpers.run_disposable_redis import private_broker
    from app.services.scraper import fenced_redis_locks as locks

    jid = await child(db_factory)
    async with db_factory() as db:
        owner = await fences.claim(db, "selective-retry:" + jid)
        await db.commit()
    with private_broker() as port:
        async with Redis.from_url(f"redis://127.0.0.1:{port}/0", decode_responses=True) as redis:
            with fences.ownership_scope(owner):
                async with db_factory() as db:
                    assert await locks.acquire_university_lock(db, redis, "uni:canary", jid)
                    assert await locks.acquire_global_slot(
                        db, redis, "slots:canary", jid, now=1, limit=1)
                    await db.commit()
                async with db_factory() as db:
                    with pytest.raises(fences.OwnershipLost):
                        await locks.acquire_university_lock(db, redis, "uni:other", "unrelated")
            # A wrong generation cannot release the selected worker's slot.
            assert not await locks.release_university_lock(redis, "uni:canary", jid, "wrong")
            assert not await locks.release_global_slot(redis, "slots:canary", jid, "wrong")
            assert await redis.get("uni:canary") == jid
            assert await redis.zcard("slots:canary") == 1
            async with db_factory.kw["bind"].begin() as control:
                await control.execute(text(
                    "UPDATE autonomous_worker_claims SET state='revoked',proof='worker_lost' WHERE claim_key=:key"
                ), {"key": owner.key})
            with fences.ownership_scope(owner):
                async with db_factory() as db:
                    with pytest.raises(fences.OwnershipLost):
                        await locks.acquire_university_lock(db, redis, "uni:canary", jid)
            assert await locks.release_university_lock(redis, "uni:canary", jid, owner.generation)
            assert await locks.release_global_slot(redis, "slots:canary", jid, owner.generation)
