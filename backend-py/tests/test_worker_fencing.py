"""Real PostgreSQL transaction races, in an isolated disposable local cluster.

Never uses shared databases/brokers, application data, AI, or external services.
"""
import asyncio
import importlib.util
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.services import worker_fencing as fences


@pytest.fixture(scope="module")
def local_postgres(tmp_path_factory):
    if not shutil.which("initdb"):
        pytest.skip("PostgreSQL binaries required for real transaction race tests")
    root = tmp_path_factory.mktemp("fencing_postgres")
    data = root / "data"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    subprocess.run(["initdb", "-D", str(data), "-A", "trust", "-U", "fencing_test"],
                   check=True, capture_output=True)
    subprocess.run(["pg_ctl", "-D", str(data), "-l", str(root / "postgres.log"),
                    "-o", f"-h 127.0.0.1 -p {port} -k {root}", "-w", "start"],
                   check=True, capture_output=True)
    try:
        yield f"postgresql+asyncpg://fencing_test@127.0.0.1:{port}/postgres"
    finally:
        subprocess.run(["pg_ctl", "-D", str(data), "-m", "immediate", "-w", "stop"],
                       check=True, capture_output=True)


@pytest.fixture
async def db_factory(local_postgres):
    engine = create_async_engine(local_postgres, poolclass=NullPool)
    path = Path(__file__).parents[1] / "alembic/versions/382_autonomous_worker_claims.py"
    spec = importlib.util.spec_from_file_location("fence_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    async with engine.begin() as connection:
        def migrate(sync_connection):
            migration.op = SimpleNamespace(execute=lambda sql: sync_connection.execute(text(sql)))
            migration.upgrade()
            migration.upgrade()  # additive migration tolerates existing schema
            lineage_path = path.with_name("383_worker_claim_lock_lineage.py")
            lineage_spec = importlib.util.spec_from_file_location("lineage_migration", lineage_path)
            lineage = importlib.util.module_from_spec(lineage_spec)
            lineage_spec.loader.exec_module(lineage)
            lineage.op = migration.op
            lineage.upgrade()
            lineage.upgrade()
        await connection.run_sync(migrate)
        await connection.execute(text("TRUNCATE autonomous_worker_claims CASCADE"))
        await connection.execute(text("CREATE TABLE IF NOT EXISTS fence_writes (value text)"))
        await connection.execute(text("TRUNCATE fence_writes"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def acquire(factory, key="verification:deterministic-job", task_id="delivery"):
    async with factory() as db:
        owner = await fences.claim(db, key, task_id)
        await db.commit()
        return owner


@pytest.mark.asyncio
async def test_concurrent_claim_and_broker_redelivery(db_factory):
    owners = await asyncio.gather(*(acquire(db_factory) for _ in range(8)))
    assert sum(owner is not None for owner in owners) == 1
    assert await acquire(db_factory) is None


@pytest.mark.asyncio
async def test_revocation_waits_for_independent_transaction_then_rejects_late_write(db_factory):
    owner = await acquire(db_factory)
    wrote = asyncio.Event()
    release = asyncio.Event()

    async def independent_staging():
        with fences.ownership_scope(owner):
            async with db_factory() as db:
                await db.execute(text("INSERT INTO fence_writes VALUES ('before')"))
                wrote.set()
                await release.wait()
                await db.commit()

    async def stopped():
        async with db_factory() as db:
            await fences.acknowledge_stop(db, owner)

    writer = asyncio.create_task(independent_staging())
    await wrote.wait()
    stop = asyncio.create_task(stopped())
    await asyncio.sleep(0.08)
    assert not stop.done()  # conflicting row lock, not check-then-write
    release.set()
    await writer
    await stop
    with fences.ownership_scope(owner):
        async with db_factory() as late:
            with pytest.raises(fences.OwnershipLost):
                await late.execute(text("INSERT INTO fence_writes VALUES ('late')"))
            await late.rollback()
    async with db_factory() as db:
        assert (await db.execute(text("SELECT value FROM fence_writes"))).scalars().all() == ["before"]


@pytest.mark.asyncio
async def test_process_death_proof_is_bound_to_accepted_process(db_factory):
    old = await acquire(db_factory)
    async with db_factory() as db:
        await fences.record_process_death(db, "delivery", "different-process", "worker_lost")
        assert not await fences.revoke_stopped(db, old.key, old.generation)
        await db.commit()
        assert await acquire(db_factory) is None
        await fences.record_process_death(db, "delivery", fences.process_identity(), "worker_lost")
        assert await fences.revoke_stopped(db, old.key, old.generation)
        await db.commit()
    new = await acquire(db_factory)
    assert new.key == old.key and new.generation != old.generation
    with fences.ownership_scope(old):
        async with db_factory() as db:
            with pytest.raises(fences.OwnershipLost):
                await db.execute(text("INSERT INTO fence_writes VALUES ('old audit')"))
    with fences.ownership_scope(new):
        async with db_factory() as db:
            await db.execute(text("INSERT INTO fence_writes VALUES ('new audit')"))
            await db.commit()


@pytest.mark.asyncio
async def test_preexisting_transaction_is_checked_before_commit(db_factory):
    owner = await acquire(db_factory)
    async with db_factory() as db:
        await fences.acknowledge_stop(db, owner)
    async with db_factory() as late:
        await late.execute(text("INSERT INTO fence_writes VALUES ('uncommitted')"))
        with fences.ownership_scope(owner):
            with pytest.raises(fences.OwnershipLost):
                await late.commit()
        await late.rollback()
    async with db_factory() as db:
        assert (await db.execute(text("SELECT count(*) FROM fence_writes"))).scalar() == 0


@pytest.mark.asyncio
async def test_no_age_or_arbitrary_failure_can_revoke(db_factory):
    owner = await acquire(db_factory)
    async with db_factory() as db:
        await db.execute(text("UPDATE autonomous_worker_claims SET claimed_at = now() - interval '30 days'"))
        await db.commit()
        with pytest.raises(ValueError):
            await fences.record_process_death(db, "delivery", fences.process_identity(), "heartbeat_expired")
        assert not await fences.revoke_stopped(db, owner.key)
        await db.commit()
    assert await acquire(db_factory) is None


@pytest.mark.asyncio
async def test_actual_process_death_rolls_back_then_recovers_same_job(local_postgres, db_factory):
    script = """
import asyncio, sys
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from app.services.worker_fencing import claim, ownership_scope
async def main():
    engine = create_async_engine(sys.argv[1])
    factory = async_sessionmaker(engine)
    async with factory() as db:
        owner = await claim(db, 'verification:deterministic-job', 'dead-delivery')
        await db.commit()
        with ownership_scope(owner):
            await db.execute(text("INSERT INTO fence_writes VALUES ('must rollback')"))
            print(owner.generation, flush=True)
            await asyncio.Event().wait()
asyncio.run(main())
"""
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, local_postgres,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        generation = (await asyncio.wait_for(child.stdout.readline(), 10)).decode().strip()
        assert generation
        identity = fences.process_identity(child.pid)
        child.kill()
        await asyncio.wait_for(child.wait(), 10)
        assert child.returncode < 0
        async with db_factory() as db:
            await fences.record_process_death(db, "dead-delivery", identity, "worker_lost")
            assert await fences.revoke_stopped(db, "verification:deterministic-job", generation)
            await db.commit()
            assert (await db.execute(text("SELECT count(*) FROM fence_writes"))).scalar() == 0
        replacement = await acquire(db_factory)
        assert replacement.generation != generation
        assert await acquire(db_factory) is None
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()


@pytest.mark.asyncio
async def test_live_budget_survives_generation_replacement(db_factory, monkeypatch):
    from app import database
    monkeypatch.setattr(database, "AsyncSessionLocal", db_factory)
    old = await acquire(db_factory, "repair:budget")
    with fences.ownership_scope(old):
        assert await fences.reserve_live_fetch(2, 20, 15) == 15
    async with db_factory() as db:
        await fences.acknowledge_stop(db, old)
        assert await fences.revoke_stopped(db, old.key, old.generation)
        await db.commit()
    new = await acquire(db_factory, "repair:budget")
    with fences.ownership_scope(new):
        assert await fences.reserve_live_fetch(2, 20, 15) == 5
        assert await fences.reserve_live_fetch(2, 20, 15) == 0
    with fences.ownership_scope(old):
        with pytest.raises(fences.OwnershipLost):
            await fences.refund_live_fetch(15, 0)


@pytest.mark.asyncio
async def test_startup_reset_preserves_claimed_verification(db_factory):
    from app.tasks.celery_app import _RESET_SQL
    async with db_factory() as db:
        await db.execute(text(
            "CREATE TEMP TABLE scrape_runtime_jobs "
            "(status text, completed_at timestamptz, error_message text, job_type text, request_payload jsonb)"
        ))
        await db.execute(text("""
            INSERT INTO scrape_runtime_jobs (status, job_type, request_payload) VALUES
            ('running', 'scrape', '{"autonomousVerification": {}}'),
            ('running', 'scrape', '{"aiRepairWorkflow": {}}'),
            ('running', 'scrape', '{}')
        """))
        result = await db.execute(text(_RESET_SQL))
        assert result.rowcount == 1
        assert (await db.execute(text(
            "SELECT count(*) FROM scrape_runtime_jobs WHERE status = 'running'"
        ))).scalar() == 2


def test_startup_reset_excludes_autonomous_rows():
    from app.tasks.celery_app import _RESET_SQL
    assert "? 'autonomousVerification'" in _RESET_SQL
    assert "? 'aiRepairWorkflow'" in _RESET_SQL


def test_parent_request_records_only_actual_death(monkeypatch):
    from billiard.exceptions import WorkerLostError, Terminated
    from celery.worker.request import Request
    from app.tasks import fenced_request
    monkeypatch.setattr(Request, "on_failure", Mock())
    observe = Mock()
    monkeypatch.setattr(fenced_request.FencedRequest, "_observe_exit", observe)
    # Invoke through a real subclass instance without Celery app/network setup.
    request = object.__new__(fenced_request.FencedRequest)
    request.id = "delivery"
    request._accepted_process_identity = "accepted-process"
    request.on_failure(SimpleNamespace(exception=RuntimeError("ordinary failure")))
    observe.assert_not_called()
    # Celery's optimized on_success sends task-returned exception values here.
    for exception in (WorkerLostError("task raised this"), Terminated("task raised this")):
        request.on_failure(SimpleNamespace(exception=exception), return_ok=True)
    observe.assert_not_called()
    request.on_failure(SimpleNamespace(exception=WorkerLostError("pool confirms death")))
    request.on_failure(SimpleNamespace(exception=Terminated("pool confirms termination")))
    assert [call.args for call in observe.call_args_list] == [
        ("worker_lost",), ("terminated_acknowledgement",),
    ]


def test_exit_observer_does_not_accept_live_process(monkeypatch):
    import threading
    import time
    from app.tasks import fenced_request
    calls = []

    async def persist(*args):
        calls.append(args)

    monkeypatch.setattr(fenced_request, "_persist_death", persist)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        identity = fences.process_identity(child.pid)
        observer = threading.Thread(target=fenced_request._wait_for_exit, args=(
            "hard-timeout", identity, os.pidfd_open(child.pid), "worker_lost",
        ))
        observer.start()
        time.sleep(0.1)
        assert calls == []
        child.kill()
        child.wait(timeout=5)
        observer.join(timeout=5)
        assert not observer.is_alive()
        assert calls == [("hard-timeout", identity, "worker_lost")]
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)


def test_only_hard_timeout_schedules_exit_observation(monkeypatch):
    from celery.worker.request import Request
    from app.tasks.fenced_request import FencedRequest
    monkeypatch.setattr(Request, "on_timeout", Mock())
    observer = Mock()
    monkeypatch.setattr(FencedRequest, "_observe_exit", observer)
    request = object.__new__(FencedRequest)
    request.on_timeout(True, 1)
    observer.assert_not_called()
    request.on_timeout(False, 2)
    observer.assert_called_once_with("worker_lost")


@pytest.fixture
async def disposable_celery(local_postgres, db_factory, tmp_path):
    """A real private Redis + prefork worker, with synthetic tasks only."""
    import redis
    from celery import Celery
    if not shutil.which("redis-server"):
        pytest.skip("Redis binary required for isolated prefork lifecycle tests")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"redis://127.0.0.1:{port}/0"
    redis_process = subprocess.Popen([
        "redis-server", "--bind", "127.0.0.1", "--port", str(port),
        "--save", "", "--appendonly", "no", "--dir", str(tmp_path),
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    worker = None
    client = redis.Redis.from_url(url)
    app = Celery("fence-test-client", broker=url, backend=url)
    try:
        for _ in range(100):
            try:
                if client.ping():
                    break
            except redis.ConnectionError:
                pass
            await asyncio.sleep(.05)
        else:
            pytest.fail("Private Redis did not start")
        env = {
            **os.environ, "FENCE_TEST_REDIS": url, "DATABASE_URL": local_postgres,
            "DATABASE_REQUIRE_TLS": "false",
            "PYTHONPATH": str(Path(__file__).parents[1]),
        }
        worker = subprocess.Popen([
            sys.executable, "-m", "celery", "-A", "tests.fenced_celery_probe:app",
            "worker", "--pool=prefork", "--concurrency=1", "--loglevel=ERROR",
            "--without-gossip", "--without-mingle", "--without-heartbeat",
            "-Q", "fence-test", "-n", "fence-test@%h",
        ], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        yield app
    finally:
        if worker:
            worker.terminate()
            try:
                await asyncio.to_thread(worker.wait, timeout=10)
            except subprocess.TimeoutExpired:
                worker.kill()
                await asyncio.to_thread(worker.wait, timeout=5)
        app.close()
        client.close()
        redis_process.terminate()
        await asyncio.to_thread(redis_process.wait, timeout=5)


async def wait_for_claim(factory, key, expected):
    for _ in range(240):
        async with factory() as db:
            row = (await db.execute(text(
                "SELECT generation, state, process_identity FROM autonomous_worker_claims "
                "WHERE claim_key = :key"
            ), {"key": key})).first()
        if row and row[1] == expected:
            return row
        await asyncio.sleep(.1)
    pytest.fail(f"Private worker did not reach {expected} for {key}")


@pytest.mark.asyncio
async def test_real_prefork_hard_timeout_and_duplicate_redelivery(disposable_celery, db_factory):
    key = "verification:prefork-hard-timeout"
    app = disposable_celery
    delivery = "hard-timeout-delivery"
    app.send_task("fence_test.execute", args=[key, "hang"], task_id=delivery,
                  queue="fence-test", time_limit=2)
    old = await wait_for_claim(db_factory, key, "stopped")
    async with db_factory() as db:
        assert await fences.revoke_stopped(db, key, old[0])
        await db.commit()
    # Broker delivers the SAME logical task repeatedly after the dead generation.
    for _ in range(4):
        app.send_task("fence_test.execute", args=[key, "write"], task_id=delivery,
                      queue="fence-test", time_limit=20)
    new = await wait_for_claim(db_factory, key, "stopped")
    assert new[0] != old[0]
    assert new[2] != old[2]  # the timed-out prefork process really was replaced
    await asyncio.sleep(1)
    async with db_factory() as db:
        assert (await db.execute(text(
            "SELECT count(*) FROM fence_writes WHERE value = :key"
        ), {"key": key})).scalar() == 1


@pytest.mark.asyncio
async def test_real_prefork_replacement_reclaims_its_actual_locks_and_stages_remaining(
    disposable_celery, db_factory,
):
    import redis.asyncio as redis
    key = "verification:prefork-locked-child"
    job_id = "prefork-locked-child"
    app = disposable_celery
    client = redis.from_url(app.conf.broker_url, decode_responses=True)
    try:
        app.send_task(
            "fence_test.execute", args=[key, "lock_hang"], task_id="locked-delivery",
            queue="fence-test", time_limit=3,
        )
        old = await wait_for_claim(db_factory, key, "stopped")
        assert await client.get("scrape:uni_lock:42") == job_id
        assert await client.get("scrape:uni_lock:42:generation") == old[0]
        assert await client.ttl("scrape:uni_lock:42") > 14000
        assert await client.zscore("scrape:active_runs", job_id) is not None
        async with db_factory() as db:
            assert (await db.execute(text(
                "SELECT value FROM fence_writes"
            ))).scalars().all() == [f"{key}:first"]
            assert await fences.revoke_stopped(db, key, old[0])
            await db.commit()
        for _ in range(4):
            app.send_task(
                "fence_test.execute", args=[key, "lock_write"], task_id="locked-delivery",
                queue="fence-test", time_limit=20,
            )
        new = await wait_for_claim(db_factory, key, "stopped")
        assert new[0] != old[0] and new[2] != old[2]
        await asyncio.sleep(1)
        async with db_factory() as db:
            assert sorted((await db.execute(text(
                "SELECT value FROM fence_writes"
            ))).scalars().all()) == [f"{key}:first", f"{key}:remaining"]
        assert await client.get("scrape:uni_lock:42") is None
        assert await client.get("scrape:uni_lock:42:generation") is None
        assert await client.zscore("scrape:active_runs", job_id) is None
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_redis_lock_lineage_other_jobs_and_late_release(disposable_celery, db_factory):
    import redis.asyncio as redis
    from app.services.scraper import fenced_redis_locks as locks
    client = redis.from_url(disposable_celery.conf.broker_url, decode_responses=True)
    key, job, slot = "scrape:uni_lock:99", "own-job", "scrape:active_runs"
    owner = await acquire(db_factory, f"verification:{job}")
    try:
        async with db_factory() as db:
            with fences.ownership_scope(owner):
                # A bare same-job value is NOT sufficient without a revoked
                # predecessor. Neither are another job or an unknown token.
                for holder, generation in [(job, None), ("other-job", None), (job, "unproven")]:
                    await client.set(key, holder)
                    await client.delete(key + ":generation")
                    if generation:
                        await client.set(key + ":generation", generation)
                    assert not await locks.acquire_university_lock(db, client, key, job)
                    assert await client.get(key) == holder
                await client.delete(key, key + ":generation")
                assert await locks.acquire_university_lock(db, client, key, job)
                assert await locks.acquire_global_slot(db, client, slot, job, now=1, limit=1)
                await db.commit()

        # B dies before touching Redis, so C must recognize A's still-present
        # token from durable revoked lineage, not only its immediate predecessor.
        previous = owner
        for _ in range(2):
            async with db_factory() as db:
                await fences.acknowledge_stop(db, previous)
                assert await fences.revoke_stopped(db, previous.key, previous.generation)
                await db.commit()
            previous = await acquire(db_factory, owner.key)
        replacement = previous
        async with db_factory() as db:
            with fences.ownership_scope(replacement):
                assert await locks.acquire_university_lock(db, client, key, job)
                assert await locks.acquire_global_slot(db, client, slot, job, now=2, limit=1)
                await db.commit()
        assert not await locks.release_university_lock(client, key, job, owner.generation)
        assert not await locks.release_global_slot(client, slot, job, owner.generation)
        assert not await locks.release_legacy_university_lock(client, key, job)
        assert not await locks.replace_legacy_university_lock(client, key, job, "other-job")
        assert await client.get(key + ":generation") == replacement.generation
        assert await client.zscore(slot, job) == 2
        with fences.ownership_scope(owner):
            async with db_factory() as db:
                with pytest.raises(fences.OwnershipLost):
                    await locks.acquire_university_lock(db, client, key, job)
        other = await acquire(db_factory, "verification:other-job")
        with fences.ownership_scope(other):
            async with db_factory() as db:
                assert not await locks.acquire_university_lock(db, client, key, "other-job")
                await db.rollback()
        # Legacy job-ID-only locks can be refreshed ONLY with durable lineage.
        await client.delete(key + ":generation")
        with fences.ownership_scope(replacement):
            async with db_factory() as db:
                assert await locks.acquire_university_lock(db, client, key, job)
                await db.commit()
        assert await locks.release_university_lock(client, key, job, replacement.generation)
        assert await locks.release_global_slot(client, slot, job, replacement.generation)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_normal_global_slot_sweep_and_release_preserve_fenced_owners(
    disposable_celery, db_factory,
):
    import redis.asyncio as redis
    from app.services.scraper import fenced_redis_locks as locks
    client = redis.from_url(disposable_celery.conf.broker_url, decode_responses=True)
    slot, job = "scrape:active_runs", "fenced-mixed-job"
    owner = await acquire(db_factory, f"verification:{job}")
    try:
        with fences.ownership_scope(owner):
            async with db_factory() as db:
                assert await locks.acquire_global_slot(db, client, slot, job, now=1, limit=3)
                await db.commit()
        await client.zadd(slot, {"stale-ordinary": 1, "live-ordinary": 100})
        assert await locks.acquire_legacy_global_slot(
            client, slot, "new-ordinary", now=200, stale_before=50, limit=3,
        ) == -1
        assert await client.zscore(slot, "stale-ordinary") is None
        assert await client.zscore(slot, "live-ordinary") == 100
        assert await client.zscore(slot, job) == 1
        assert not await locks.release_legacy_global_slot(client, slot, job)
        # Even the SAME job ID cannot overwrite the score of a fenced member.
        assert await locks.acquire_legacy_global_slot(
            client, slot, job, now=300, stale_before=50, limit=10,
        ) >= 0
        assert await client.zscore(slot, job) == 1
        assert await client.hget(slot + ":generations", job) == owner.generation
        assert await locks.release_legacy_global_slot(client, slot, "live-ordinary")
        assert await locks.acquire_legacy_global_slot(
            client, slot, "blocked", now=10000, stale_before=9999, limit=1,
        ) == 1
        assert await client.zrange(slot, 0, -1) == [job]
        assert await locks.release_global_slot(client, slot, job, owner.generation)
        assert await locks.acquire_legacy_global_slot(
            client, slot, "ordinary-after-release", now=10001, stale_before=9999, limit=1,
        ) == -1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_actual_startup_cleanup_interleavings_preserve_new_generation(
    disposable_celery, db_factory, monkeypatch,
):
    """Run the real startup cleanup with a private Redis and stubbed DB sweep.

    The two races occur after keys enumeration/GET and during the DB status
    check. New locks are acquired through the real fenced production helper.
    No application workflow or shared broker/database is invoked.
    """
    import redis
    import redis.asyncio as async_redis
    from unittest.mock import AsyncMock
    from app.tasks import celery_app as startup
    from app.tasks import scrape_tasks
    from app.services.scraper import fenced_redis_locks as locks
    sync_client = redis.Redis.from_url(disposable_celery.conf.broker_url, decode_responses=True)
    async_client = async_redis.from_url(disposable_celery.conf.broker_url, decode_responses=True)
    changed_key = "scrape:uni_lock:77"
    absent_key = "scrape:uni_lock:78"
    stale_key = "scrape:uni_lock:79"
    sidecar_only = "scrape:uni_lock:80:generation"
    job = "new-startup-owner"
    owner = await acquire(db_factory, f"verification:{job}")

    async def acquire_new(key):
        # This coroutine also runs from the worker_ready test thread; NullPool
        # deliberately avoids sharing event-loop-bound test DB connections.
        import redis.asyncio as thread_redis
        client = thread_redis.from_url(disposable_celery.conf.broker_url, decode_responses=True)
        try:
            with fences.ownership_scope(owner):
                async with db_factory() as db:
                    assert await locks.acquire_university_lock(db, client, key, job)
                    await db.commit()
        finally:
            await client.aclose()

    class InterleavedRedis:
        def keys(self, pattern):
            # Absent key existed when enumeration occurred, but expires before
            # GET. Its replacement then appears before startup can delete.
            return sync_client.keys(pattern) + [absent_key]

        def get(self, key):
            observed = sync_client.get(key)
            if key == absent_key and observed is None:
                asyncio.run(acquire_new(key))
            return observed

        def eval(self, *args):
            return sync_client.eval(*args)

        def delete(self, *args):
            pytest.fail("Startup cleanup must never perform unconditional DELETE")

    async def status(holder):
        if holder == "old-terminal-job":
            sync_client.delete(changed_key)  # previous owner finished/expired
            await acquire_new(changed_key)  # replacement races the DB lookup
        return "completed"

    try:
        sync_client.set(changed_key, "old-terminal-job")
        sync_client.set(stale_key, "stationary-terminal-job")
        sync_client.set(sidecar_only, "must-not-delete")
        monkeypatch.setattr(startup, "_reset_ghost_running_jobs", AsyncMock(return_value=0))
        monkeypatch.setattr(startup, "_check_job_status_single", status)
        monkeypatch.setattr(scrape_tasks, "_immediate_requeue_hook", Mock())
        monkeypatch.setattr(redis, "from_url", lambda *a, **kw: InterleavedRedis())
        await asyncio.to_thread(startup.on_worker_ready)
        for key in (changed_key, absent_key):
            assert await async_client.get(key) == job
            assert await async_client.get(key + ":generation") == owner.generation
        assert await async_client.get(stale_key) is None
        assert await async_client.get(sidecar_only) == "must-not-delete"
        # Same-holder refresh is also protected by the atomic sidecar check.
        assert not locks.cleanup_legacy_university_lock(sync_client, changed_key, job)
        assert not locks.cleanup_legacy_university_lock(sync_client, absent_key, None)
    finally:
        sync_client.close()
        await async_client.aclose()


def test_shared_lock_callers_do_not_bypass_atomic_helpers():
    import inspect
    from app.services.scraper import repair, orchestrator
    from app.tasks import celery_app
    repair_source = inspect.getsource(repair.run_repair)
    assert "replace_legacy_university_lock(" in repair_source
    assert "release_legacy_university_lock(" in repair_source
    assert "_uni_lock_redis.delete(" not in repair_source
    orchestrator_source = inspect.getsource(orchestrator._run_claimed_scrape)
    assert "acquire_legacy_global_slot(" in orchestrator_source
    assert "release_legacy_global_slot(" in orchestrator_source
    assert "ZREMRANGEBYSCORE" not in orchestrator_source
    assert "_global_slot_redis.zrem(" not in orchestrator_source
    assert "_r.delete(" not in inspect.getsource(celery_app.on_worker_ready)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["task_worker_lost", "task_terminated"])
async def test_real_prefork_task_raised_death_exception_is_not_proof(
    disposable_celery, db_factory, mode,
):
    key = f"repair:{mode}"
    disposable_celery.send_task("fence_test.execute", args=[key, mode],
                               queue="fence-test", time_limit=20)
    row = await wait_for_claim(db_factory, key, "active")
    await asyncio.sleep(2)
    pid = int(row[2].split(":")[-2])
    assert fences.process_identity(pid) == row[2]  # process is still alive
    async with db_factory() as db:
        assert not await fences.revoke_stopped(db, key, row[0])
        await db.commit()
    assert await acquire(db_factory, key) is None