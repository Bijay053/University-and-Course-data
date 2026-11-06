"""Capacity accounting races using private, real PostgreSQL transactions."""
import asyncio
from copy import deepcopy
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.models import ScrapeRuntimeJob
from app.services import selective_retry as retry, worker_fencing as fences
from app.services.scraper import orchestrator
from tests.test_selective_retry import seed, URL
from tests.test_scrape_prompt_assistant import store, assistant_postgres


@pytest_asyncio.fixture
async def capacity_store(store, monkeypatch):
    await seed(store, monkeypatch)
    path = Path(__file__).parents[1] / "alembic/versions/382_autonomous_worker_claims.py"
    spec = importlib.util.spec_from_file_location("capacity_claim_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with store.kw["bind"].begin() as conn:
        def migrate(connection):
            migration.op = SimpleNamespace(execute=lambda sql: connection.execute(text(sql)))
            migration.upgrade()
            lineage_spec = importlib.util.spec_from_file_location(
                "capacity_lineage_migration", path.with_name("383_worker_claim_lock_lineage.py"))
            lineage = importlib.util.module_from_spec(lineage_spec)
            lineage_spec.loader.exec_module(lineage)
            lineage.op = migration.op
            lineage.upgrade()
        await conn.run_sync(migrate)
    return store


async def queued(factory, attempts=3):
    async with factory() as db:
        action = await retry.proposal(db, "source", [URL], attempts=attempts)
        job = await retry.queue(db, action, 77)
        return job.runtime_job_id


async def started(factory, jid):
    async with factory() as db:
        owner = await fences.claim(db, f"selective-retry:{jid}", f"delivery:{jid}")
        job = await db.get(ScrapeRuntimeJob, jid)
        payload = deepcopy(job.request_payload)
        payload["selectiveRetry"]["capacityGeneration"] = owner.generation
        job.request_payload = payload
        job.status = "running"
        await db.commit()
    return owner


async def settle(factory, jid):
    async with factory() as db:
        job = await db.get(ScrapeRuntimeJob, jid)
        job.status = "failed"
        from datetime import datetime, timezone
        job.completed_at = datetime.now(timezone.utc)
        await db.commit()


async def spend(factory):
    async with factory() as db:
        _, _, spent, _ = await retry.inventory(db, "source")
        await db.commit()
        return spent[URL]


@pytest.mark.asyncio
@pytest.mark.parametrize("proof", ["completed_stop_acknowledgement", "worker_lost", "terminated_acknowledgement"])
async def test_only_unstarted_capacity_released_with_original_receipts_intact(capacity_store, proof):
    factory = capacity_store
    jid = await queued(factory)
    owner = await started(factory, jid)
    with fences.ownership_scope(owner):
        first = await retry.reserve_attempt(jid, URL)
    await settle(factory, jid)
    # Terminal status and arbitrarily old claims are not stop proof.
    async with factory() as db:
        await db.execute(text("UPDATE autonomous_worker_claims SET claimed_at=now()-interval '60 days'"))
        await db.commit()
    assert await spend(factory) == 3
    async with factory() as db:
        before = deepcopy((await db.get(ScrapeRuntimeJob, jid)).request_payload)
        if proof == "completed_stop_acknowledgement":
            await fences.acknowledge_stop(db, owner)
        else:
            await fences.record_process_death(db, f"delivery:{jid}", fences.process_identity(), proof)
    assert await spend(factory) == 1
    assert await spend(factory) == 1  # idempotent receipt, not repeated subtraction
    async with factory() as db:
        job = await db.get(ScrapeRuntimeJob, jid)
        after = deepcopy(job.request_payload)
        receipt = after["selectiveRetry"].pop("capacityRelease")
        assert after == before
        assert receipt["generation"] == owner.generation
        assert receipt["started"] == {URL: 1} and receipt["unstarted"] == {URL: 2}
        assert receipt["proof"] == proof
        rows = (await db.execute(text("SELECT id,outcome FROM scrape_retry_attempts WHERE job_id=:job"),
                                 {"job": jid})).all()
        assert rows == [(first[0], "started")]  # uncertain attempt is still spent
        source = await db.get(ScrapeRuntimeJob, "source")
        assert (source.current, source.imported, source.status) == (17, 1, "completed")
        action = await retry.proposal(db, "source", [URL], attempts=3)
        assert action["_budgets"] == {URL: 2}
    replacement = await queued(factory)
    replacement_owner = await started(factory, replacement)
    with fences.ownership_scope(replacement_owner):
        assert (await retry.reserve_attempt(replacement, URL))[1] == 1
        assert (await retry.reserve_attempt(replacement, URL))[1] == 2
        assert await retry.reserve_attempt(replacement, URL) is None
    await settle(factory, replacement)
    async with factory() as db:
        await fences.acknowledge_stop(db, replacement_owner)
    assert await spend(factory) == 3
    async with factory() as db:
        with pytest.raises(ValueError, match="reserved or exhausted"):
            await retry.proposal(db, "source", [URL], attempts=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["legacy", "wrong_generation", "wrong_process", "untrusted_proof"])
async def test_uncertain_capacity_remains_fully_reserved(capacity_store, kind):
    factory = capacity_store
    jid = await queued(factory)
    owner = await started(factory, jid)
    await settle(factory, jid)
    async with factory() as db:
        if kind == "legacy":
            job = await db.get(ScrapeRuntimeJob, jid)
            payload = deepcopy(job.request_payload)
            payload["selectiveRetry"].pop("capacityAccounting")
            job.request_payload = payload
            await db.commit()
            await fences.acknowledge_stop(db, owner)
        elif kind == "wrong_generation":
            await fences.acknowledge_stop(db, fences.Ownership(owner.key, "other"))
        elif kind == "wrong_process":
            await fences.record_process_death(db, f"delivery:{jid}", "different-process", "worker_lost")
        else:
            await db.execute(text("UPDATE autonomous_worker_claims SET state='stopped',proof='heartbeat_expired'"))
            await db.commit()
    assert await spend(factory) == 3


@pytest.mark.asyncio
async def test_stop_ack_waits_for_late_reservation_before_freezing_count(capacity_store, monkeypatch):
    factory = capacity_store
    jid = await queued(factory)
    owner = await started(factory, jid)
    entered, release = asyncio.Event(), asyncio.Event()
    real_guard = retry.guard_source

    async def delayed_guard(db, job):
        await real_guard(db, job)
        entered.set()
        await release.wait()
    monkeypatch.setattr(retry, "guard_source", delayed_guard)

    async def reserve():
        with fences.ownership_scope(owner):
            return await retry.reserve_attempt(jid, URL)

    async def stop():
        async with factory() as db:
            await fences.acknowledge_stop(db, owner)

    reservation = asyncio.create_task(reserve())
    await asyncio.wait_for(entered.wait(), 5)
    acknowledgement = asyncio.create_task(stop())
    try:
        await asyncio.sleep(.08)
        assert not acknowledgement.done()
        release.set()
        assert await asyncio.wait_for(reservation, 5)
        await asyncio.wait_for(acknowledgement, 5)
        await settle(factory, jid)
        assert await spend(factory) == 1
        async with factory() as db:
            job = await db.get(ScrapeRuntimeJob, jid)
            job.status = "running"  # stale label cannot authorize an unscoped call
            await db.commit()
        with fences.ownership_scope(owner), pytest.raises(fences.OwnershipLost):
            await retry.reserve_attempt(jid, URL)
        # Even a caller outside the worker context cannot spend released slots.
        with pytest.raises(fences.OwnershipLost):
            await retry.reserve_attempt(jid, URL)
    finally:
        release.set()
        await asyncio.gather(reservation, acknowledgement, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoke_first", [False, True])
async def test_ack_revocation_release_ordering_and_concurrent_readers(capacity_store, revoke_first):
    factory = capacity_store
    jid = await queued(factory)
    owner = await started(factory, jid)
    with fences.ownership_scope(owner):
        assert await retry.reserve_attempt(jid, URL)
    await settle(factory, jid)
    async with factory() as stop:
        await fences.acknowledge_stop(stop, owner)
    async with factory() as blocker:
        if revoke_first:
            assert await fences.revoke_stopped(blocker, owner.key, owner.generation)
        else:
            job = await blocker.get(ScrapeRuntimeJob, jid)
            assert await retry.released_capacity(blocker, job) == {URL: 2}
        readers = [asyncio.create_task(spend(factory)) for _ in range(3)]
        try:
            await asyncio.sleep(.08)
            assert all(not reader.done() for reader in readers)
            await blocker.commit()
            assert await asyncio.wait_for(asyncio.gather(*readers), 5) == [1, 1, 1]
        finally:
            await blocker.rollback()
            await asyncio.gather(*readers, return_exceptions=True)
    async with factory() as db:
        assert not await fences.revoke_stopped(db, owner.key, "wrong")
        assert await fences.claim(db, owner.key) is not None
        await db.rollback()  # no replacement authorized by capacity accounting


@pytest.mark.asyncio
async def test_actual_worker_binding_and_redelivery_cannot_reuse_released_budget(capacity_store, monkeypatch):
    factory = capacity_store
    jid = await queued(factory)
    calls = []

    async def entry(db, job_id):
        calls.append(job_id)
        job = await db.get(ScrapeRuntimeJob, job_id)
        job.status = "running"
        await db.commit()
        assert await retry.reserve_attempt(job_id, URL)
        job.status = "failed"
        await db.commit()
        return {"ok": False}

    monkeypatch.setattr(orchestrator, "_run_scrape_entry", entry)
    async with factory() as db:
        await orchestrator.run_scrape(db, jid)
    assert await spend(factory) == 1
    # Stale queued labels, broker redelivery and concurrent deliveries must not
    # create another spending generation for this child.
    async with factory() as db:
        job = await db.get(ScrapeRuntimeJob, jid)
        job.status = "queued"
        await db.commit()

    async def redeliver():
        async with factory() as db:
            return await orchestrator.run_scrape(db, jid)
    assert all(result["reason"] == "capacity_released"
               for result in await asyncio.gather(*(redeliver() for _ in range(4))))
    assert calls == [jid]
    assert await spend(factory) == 1


@pytest.mark.asyncio
async def test_initial_delivery_read_before_release_cannot_replace_stopped_generation(capacity_store, monkeypatch):
    factory = capacity_store
    jid = await queued(factory)
    entered, resume = asyncio.Event(), asyncio.Event()
    original_claim = fences.claim
    calls = []

    async def delayed_claim(*args, **kwargs):
        if asyncio.current_task().get_name() == "delayed-initial-delivery":
            entered.set()
            await resume.wait()
        return await original_claim(*args, **kwargs)

    async def entry(db, job_id):
        calls.append(job_id)
        job = await db.get(ScrapeRuntimeJob, job_id)
        job.status = "running"
        await db.commit()
        assert await retry.reserve_attempt(job_id, URL)
        job.status = "failed"
        await db.commit()
        return {"ok": False}

    async def deliver():
        async with factory() as db:
            return await orchestrator.run_scrape(db, jid)

    monkeypatch.setattr(fences, "claim", delayed_claim)
    monkeypatch.setattr(orchestrator, "_run_scrape_entry", entry)
    delayed = asyncio.create_task(deliver(), name="delayed-initial-delivery")
    try:
        await asyncio.wait_for(entered.wait(), 5)
        await deliver()
        assert await spend(factory) == 1
        async with factory() as db:
            original_generation = await db.scalar(text(
                "SELECT generation FROM autonomous_worker_claims WHERE claim_key=:key"),
                {"key": f"selective-retry:{jid}"})
        resume.set()
        assert (await asyncio.wait_for(delayed, 5))["reason"] == "already_claimed"
        assert calls == [jid]
        async with factory() as db:
            claim = (await db.execute(text(
                "SELECT generation,state FROM autonomous_worker_claims WHERE claim_key=:key"),
                {"key": f"selective-retry:{jid}"})).one()
            assert claim == (original_generation, "revoked")
        assert await spend(factory) == 1
    finally:
        resume.set()
        await asyncio.gather(delayed, return_exceptions=True)


@pytest.mark.asyncio
async def test_concurrent_requests_cannot_both_spend_restored_capacity(capacity_store):
    factory = capacity_store
    jid = await queued(factory)
    owner = await started(factory, jid)
    with fences.ownership_scope(owner):
        assert await retry.reserve_attempt(jid, URL)
    await settle(factory, jid)
    async with factory() as db:
        await fences.acknowledge_stop(db, owner)
    assert await spend(factory) == 1
    async with factory() as db:
        actions = [await retry.proposal(db, "source", [URL], attempts=3) for _ in range(2)]

    async def dispatch(action):
        async with factory() as db:
            try:
                return (await retry.queue(db, action, 77)).runtime_job_id
            except ValueError:
                return None

    results = await asyncio.gather(*(dispatch(action) for action in actions))
    assert sum(result is not None for result in results) == 1
    assert await spend(factory) == 3


@pytest.mark.asyncio
async def test_new_unclaimed_child_cannot_reserve_attempts_outside_its_fence(capacity_store):
    factory = capacity_store
    jid = await queued(factory)
    async with factory() as db:
        job = await db.get(ScrapeRuntimeJob, jid)
        job.status = "running"
        await db.commit()
    with pytest.raises(fences.OwnershipLost):
        await retry.reserve_attempt(jid, URL)
    assert await spend(factory) == 3


@pytest.mark.asyncio
async def test_descendant_retries_keep_original_lineage_cap_and_trimmed_urls_spent(capacity_store):
    from app.models.scrape_runtime import ScrapeRuntimeLog
    factory = capacity_store
    jid = await queued(factory)
    owner = await started(factory, jid)
    with fences.ownership_scope(owner):
        assert await retry.reserve_attempt(jid, URL)
    await settle(factory, jid)
    async with factory() as db:
        await fences.acknowledge_stop(db, owner)
        db.add(ScrapeRuntimeLog(runtime_job_id=jid, sequence=1, event="status",
            payload={"kind": "extract_error", "url": URL, "retryable": True, "reason": "timeout"}))
        await db.commit()
        action = await retry.proposal(db, jid, [URL], attempts=3)
        assert action["_budgets"] == {URL: 2}
        descendant = await retry.queue(db, action, 77)
        assert descendant.request_payload["selectiveRetry"]["rootJobId"] == "source"
        # validate_before_run can trim work already resolved by another path.
        # That must never erase the durable reservation from lineage accounting.
        payload = deepcopy(descendant.request_payload)
        payload["courseUrls"] = payload["course_urls"] = []
        descendant.request_payload = payload
        await db.commit()
    assert await spend(factory) == 3
