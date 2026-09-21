"""Synthetic tasks for private prefork lifecycle tests; never used by the app."""
import asyncio
import os
import time

from billiard.exceptions import Terminated, WorkerLostError
from celery import Celery
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.services.worker_fencing import claim, ownership_scope, acknowledge_stop
from app.services.scraper import fenced_redis_locks

app = Celery(
    "fence-lifecycle-probe",
    broker=os.environ["FENCE_TEST_REDIS"],
    backend=os.environ["FENCE_TEST_REDIS"],
    task_cls="app.tasks.fenced_request:FencedTask",
)
app.conf.update(
    task_acks_late=True, task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
)


async def locked_run(db, owner, key, mode):
    """Exercise the actual production Redis lock helpers, no duplicated Lua."""
    import redis.asyncio as redis
    client = redis.from_url(os.environ["FENCE_TEST_REDIS"], decode_responses=True)
    job_id = key.removeprefix("verification:")
    lock_key = "scrape:uni_lock:42"
    slot_key = "scrape:active_runs"
    try:
        with ownership_scope(owner):
            if not await fenced_redis_locks.acquire_university_lock(db, client, lock_key, job_id):
                raise RuntimeError("Replacement incorrectly blocked by its university lock")
            if not await fenced_redis_locks.acquire_global_slot(
                db, client, slot_key, job_id, now=time.time(), limit=1,
            ):
                raise RuntimeError("Replacement incorrectly blocked by its global slot")
            for suffix in ("first", "remaining"):
                value = f"{key}:{suffix}"
                exists = (await db.execute(text(
                    "SELECT count(*) FROM fence_writes WHERE value = :value"
                ), {"value": value})).scalar()
                if not exists:
                    await db.execute(text(
                        "INSERT INTO fence_writes VALUES (:value)"
                    ), {"value": value})
                await db.commit()
                if mode == "lock_hang":
                    # Database progress and BOTH actual Redis locks exist
                    # before the real prefork hard-time-limit kills this task.
                    time.sleep(60)
            await fenced_redis_locks.release_university_lock(
                client, lock_key, job_id, owner.generation,
            )
            await fenced_redis_locks.release_global_slot(
                client, slot_key, job_id, owner.generation,
            )
    finally:
        await client.aclose()


@app.task(name="fence_test.execute", bind=True)
def execute(self, key, mode):
    async def run():
        engine = create_async_engine(os.environ["DATABASE_URL"])
        try:
            async with async_sessionmaker(engine)() as db:
                owner = await claim(db, key, self.request.id)
                await db.commit()
                if owner is None:
                    return "duplicate"
                if mode.startswith("lock_"):
                    await locked_run(db, owner, key, mode)
                    await acknowledge_stop(db, owner)
                    return "staged"
                if mode == "hang":
                    time.sleep(60)  # intentionally killed by the real prefork pool
                if mode == "task_worker_lost":
                    raise WorkerLostError("raised by task code, not by the pool")
                if mode == "task_terminated":
                    raise Terminated("raised by task code, not by the pool")
                with ownership_scope(owner):
                    await db.execute(text("INSERT INTO fence_writes VALUES (:value)"), {"value": key})
                    await db.commit()
                await acknowledge_stop(db, owner)
                return "written"
        finally:
            await engine.dispose()
    return asyncio.run(run())