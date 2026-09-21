"""Transaction-level ownership for autonomous workers.

Every SQLAlchemy Session opened in an execution context (including independent
staging/snapshot sessions and inherited asyncio tasks) locks the fence FOR SHARE
before doing any work. Revocation takes the conflicting row lock. Consequently
a committed write is ordered before revocation, or rejected, never after it.
This is deliberately not a lease: timestamps cannot revoke ownership.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import os
from pathlib import Path
import socket
from functools import wraps
from uuid import uuid4

from sqlalchemy import event, text
from sqlalchemy.orm import Session


class OwnershipLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Ownership:
    key: str
    generation: str


current_owner: ContextVar[Ownership | None] = ContextVar("worker_owner", default=None)


def fenced_external_write(function):
    """Keep the DB revocation lock across non-DB side effects (e.g. S3 PUT)."""
    @wraps(function)
    async def guarded(*args, **kwargs):
        if current_owner.get() is None:
            return await function(*args, **kwargs)
        from app.database import AsyncSessionLocal
        async with AsyncSessionLocal() as db:
            await db.connection()  # after_begin obtains and verifies the lock
            return await function(*args, **kwargs)
    return guarded


def process_identity(pid: int | None = None) -> str:
    """Linux boot + PID + process start ticks; unlike PID alone, cannot be reused."""
    pid = pid or os.getpid()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    # comm can contain spaces and parentheses; fields after the final ')' start
    # at field 3, and starttime is field 22.
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return f"{socket.gethostname()}:{boot}:{pid}:{fields[19]}"


@contextmanager
def ownership_scope(owner: Ownership):
    token = current_owner.set(owner)
    try:
        yield
    finally:
        current_owner.reset(token)


def _check(connection, owner: Ownership) -> None:
    row = connection.execute(text(
        "SELECT generation, state FROM autonomous_worker_claims "
        "WHERE claim_key = :key FOR SHARE"
    ), {"key": owner.key}).first()
    if not row or row[0] != owner.generation or row[1] != "active":
        raise OwnershipLost(f"Autonomous worker ownership revoked: {owner.key}")


@event.listens_for(Session, "after_begin")
def _transaction_started(session, transaction, connection):
    owner = current_owner.get()
    if owner:
        _check(connection, owner)
        session.info["autonomous_checked_transaction"] = (transaction, owner)


@event.listens_for(Session, "before_commit")
@event.listens_for(Session, "before_flush")
def _before_write(session, *args):
    owner = current_owner.get()
    if owner:
        transaction = session.get_transaction()
        if session.info.get("autonomous_checked_transaction") != (transaction, owner):
            connection = session.connection()
            _check(connection, owner)
            session.info["autonomous_checked_transaction"] = (session.get_transaction(), owner)


async def guard_transaction(db):
    """Guard synchronous filesystem mutations using this transaction's lock."""
    if current_owner.get() is not None:
        await db.run_sync(_before_write)


async def claim(db, key: str, task_id: str | None = None) -> Ownership | None:
    """Caller commits this alongside its lifecycle claim, never separately."""
    if task_id is None:
        from celery import current_task
        task_id = str(getattr(getattr(current_task, "request", None), "id", "") or "")
    generation = uuid4().hex
    row = (await db.execute(text(
        "INSERT INTO autonomous_worker_claims "
        "(claim_key, generation, state, task_id, process_identity) "
        "VALUES (:key, :generation, 'active', :task_id, :process) "
        "ON CONFLICT (claim_key) DO UPDATE SET "
        "generation = EXCLUDED.generation, state = 'active', "
        "task_id = EXCLUDED.task_id, process_identity = EXCLUDED.process_identity, "
        "revoked_generations = autonomous_worker_claims.revoked_generations || "
        "jsonb_build_array(autonomous_worker_claims.generation), "
        "proof = NULL, claimed_at = now() "
        "WHERE autonomous_worker_claims.state = 'revoked' "
        "AND autonomous_worker_claims.proof IN "
        "('worker_lost', 'terminated_acknowledgement', 'completed_stop_acknowledgement') "
        "RETURNING generation"
    ), {"key": key, "generation": generation, "task_id": task_id,
        "process": process_identity()})).first()
    return Ownership(key, generation) if row else None


async def acknowledge_stop(db, owner: Ownership) -> None:
    """Call only after the owning execution has unwound, outside its scope."""
    await db.rollback()
    await db.execute(text(
        "UPDATE autonomous_worker_claims SET state = 'stopped', "
        "proof = 'completed_stop_acknowledgement' "
        "WHERE claim_key = :key AND generation = :generation AND state = 'active'"
    ), {"key": owner.key, "generation": owner.generation})
    await db.commit()


async def record_process_death(db, task_id: str, identity: str, proof: str) -> None:
    if proof not in {"worker_lost", "terminated_acknowledgement"}:
        raise ValueError("Not authoritative process-death evidence")
    await db.execute(text(
        "UPDATE autonomous_worker_claims SET state = 'stopped', proof = :proof "
        "WHERE task_id = :task_id AND process_identity = :identity AND state = 'active'"
    ), {"task_id": task_id, "identity": identity, "proof": proof})
    await db.commit()


async def revoke_stopped(db, key: str, generation: str | None = None) -> bool:
    """No heartbeat/inspect fallback. Legacy claims without proof stay fenced."""
    row = (await db.execute(text(
        "UPDATE autonomous_worker_claims SET state = 'revoked' "
        "WHERE claim_key = :key AND state = 'stopped' "
        "AND (CAST(:generation AS TEXT) IS NULL OR generation = :generation) "
        "AND proof IN ('worker_lost', 'terminated_acknowledgement', "
        "'completed_stop_acknowledgement') RETURNING generation"
    ), {"key": key, "generation": generation})).first()
    return row is not None


async def reserve_live_fetch(max_pages: int, max_seconds: float, timeout: float) -> float:
    """Reserve before network I/O; a process dying mid-fetch consumes its reservation."""
    owner = current_owner.get()
    if owner is None:
        return timeout
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(text(
            "INSERT INTO autonomous_worker_budgets (claim_key) VALUES (:key) "
            "ON CONFLICT DO NOTHING"
        ), {"key": owner.key})
        row = (await db.execute(text(
            "SELECT live_pages, live_seconds FROM autonomous_worker_budgets "
            "WHERE claim_key = :key FOR UPDATE"
        ), {"key": owner.key})).one()
        allocation = min(timeout, max_seconds - row[1]) if row[0] < max_pages else 0
        if allocation > 0:
            await db.execute(text(
                "UPDATE autonomous_worker_budgets SET live_pages = live_pages + 1, "
                "live_seconds = live_seconds + :seconds WHERE claim_key = :key"
            ), {"key": owner.key, "seconds": allocation})
        await db.commit()
        return max(0, allocation)


async def refund_live_fetch(reserved: float, elapsed: float) -> None:
    owner = current_owner.get()
    if owner is None or elapsed >= reserved:
        return
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        await db.execute(text(
            "UPDATE autonomous_worker_budgets SET live_seconds = "
            "GREATEST(0, live_seconds - :refund) WHERE claim_key = :key"
        ), {"key": owner.key, "refund": reserved - max(0, elapsed)})
        await db.commit()