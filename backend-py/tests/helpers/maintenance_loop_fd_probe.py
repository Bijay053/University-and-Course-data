"""Fresh-process acceptance: real read-only DB, HTTP and isolated Redis sockets.

Business operations are replaced, not executed: no repair claims, snapshot
writes, task dispatch, live broker access, or startup ghost-job resets occur.
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
import sqlalchemy.ext.asyncio as sa_async

from app import database, schema_readiness
from app.config import settings
from app.services.scraper import ai_repair_agent, http_fetcher, scrape_do_semaphore
from app.tasks import auto_repair_task as repair, health_snapshot, snapshot_tasks, fenced_request
from importlib import import_module

boot = import_module("app.tasks.celery_app")
application_name = f"maintenance-loop-probe-{os.getpid()}"
engines, loops = [], []
real_create_engine = create_async_engine
fail = False


def new_engine(*_, **__):
    args = database.postgres_tls_connect_args()
    args["server_settings"] = {"application_name": application_name, "default_transaction_read_only": "on"}
    engine = real_create_engine(settings.database_url, pool_size=1, max_overflow=0, connect_args=args)
    if fail:
        # Dedicated engines fail after acquiring a real connection. This tests
        # boot/death/schema exception cleanup without executing business SQL.
        @event.listens_for(engine.sync_engine, "before_cursor_execute", retval=True)
        def fail_query(conn, cursor, statement, parameters, context, executemany):
            return "SELECT 1/0", ()
    engines.append(engine)
    return engine


engine = new_engine()
database.engine = engine
database.AsyncSessionLocal = async_sessionmaker(engine)
health_snapshot.engine = snapshot_tasks.engine = engine
# Dedicated startup/death engines are owned by the tested functions, not the
# shared pool. Intercept construction solely to tag/read-only their sessions.
sa_async.create_async_engine = new_engine
observer = real_create_engine(settings.database_url, pool_size=1, max_overflow=0,
                              connect_args=database.postgres_tls_connect_args())
observer_loop = asyncio.new_event_loop()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
temp = tempfile.TemporaryDirectory(prefix="maintenance-loop-probe-")
redis_socket = os.path.join(temp.name, "redis.sock")
redis_process = subprocess.Popen(
    ["redis-server", "--port", "0", "--unixsocket", redis_socket, "--save", "", "--appendonly", "no"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not os.path.exists(redis_socket) and time.monotonic() < deadline:
    assert redis_process.poll() is None
    time.sleep(0.01)
assert os.path.exists(redis_socket)
scrape_do_semaphore.settings = SimpleNamespace(redis_url=f"unix://{redis_socket}")
gc.disable()  # Retain loops/engines too: finalizers must not hide leaked sockets.


async def read(db):
    loops.append(asyncio.get_running_loop())
    assert (await db.execute(text("SELECT 1"))).scalar_one() == 1


async def work(*_, fetch=False):
    async with database.AsyncSessionLocal() as db:
        await read(db)
        if fetch:
            client = http_fetcher._get_shared_client()
            assert (await client.get(f"http://127.0.0.1:{server.server_port}/")).text == "ok"
            await scrape_do_semaphore._get_client().ping()  # No broker keys.
        if fail:
            raise ValueError("isolated failure")
    return {"ok": True}


async def fetch_work(*args):
    return await work(*args, fetch=True)


async def autonomous(*args):
    await fetch_work(*args)
    return None  # Exercise the second fresh loop in a legacy delivery.


async def persist(*args):
    # Failure audit remains a separate DB-only loop.
    async with database.AsyncSessionLocal() as db:
        await read(db)
    return {}


async def death(db, *_):
    await read(db)
    if fail:
        raise ValueError("isolated failure")


async def schema(connection):
    await read(connection)
    if fail:
        raise ValueError("isolated failure")


repair._run = fetch_work
repair._run_autonomous_repair = autonomous
repair._run_ai_repair = fetch_work
repair._persist_ai_repair_failure = persist
repair._reconcile_autonomous_repair = work
repair._recover_ai_repair_workflows = work
health_snapshot._run = work
snapshot_tasks._async_run_snapshot = work
ai_repair_agent.claim_repair_session = lambda *_: True
ai_repair_agent.release_repair_lease = MagicMock()
ai_repair_agent._write_session = MagicMock()
fenced_request.record_process_death = death
schema_readiness.require_qualification_guidance_schema = schema
boot._RESET_SQL = "SELECT 1"  # Absolutely no ghost-job UPDATE.
for task in (repair.generate_repair_suggestion, repair.monitor_ai_scrape_repair,
             repair.recover_ai_repair_workflows):
    task.retry = MagicMock(side_effect=ValueError("isolated retry"))


async def active_sessions():
    async with observer.connect() as connection:
        return (await connection.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name"
        ), {"name": application_name})).scalar_one()


def fd_count():
    return len(os.listdir("/proc/self/fd"))


def tcp_sockets():
    # Count this process's TCP FDs only, not the active user's processes.
    inodes = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        if not os.path.exists(path):  # IPv6 can be disabled in the container.
            continue
        with open(path) as table:
            inodes.update(line.split()[9] for line in list(table)[1:])
    sockets = set()
    for fd in os.listdir("/proc/self/fd"):
        try:
            target = os.readlink(f"/proc/self/fd/{fd}")
        except FileNotFoundError:
            continue
        if target.startswith("socket:[") and target[8:-1] in inodes:
            sockets.add(target)
    return sockets


# Warm the independent observer connection before measuring the baseline.
assert observer_loop.run_until_complete(active_sessions()) == 0
baseline, baseline_tcp = fd_count(), tcp_sockets()
samples = []
remaining_sessions = remaining_tcp = -1
try:
    for index in range(12):
        fail = index % 2 == 1
        operations = [
            lambda: repair.generate_repair_suggestion.run(7),
            lambda: repair.monitor_ai_scrape_repair.run("isolated", "session"),
            lambda: repair.recover_ai_repair_workflows.run(),
            lambda: repair.run_ai_scrape_repair.run("isolated", 7, "session"),
            lambda: repair.run_ai_scrape_repair.run("isolated"),
            lambda: health_snapshot.snapshot_health_daily.run(),
            lambda: snapshot_tasks._run_snapshot("isolated"),
            lambda: asyncio.run(boot._reset_via_asyncpg(settings.database_url)),
            lambda: asyncio.run(boot._check_job_status_single("isolated-no-job")),
            lambda: asyncio.run(fenced_request._persist_death("isolated", {}, "worker_lost")),
            lambda: asyncio.run(schema_readiness.check_worker_schema()),
        ]
        for operation in operations:
            try:
                operation()
            except (ValueError, SQLAlchemyError):
                assert fail
            assert all(e.pool.checkedout() == 0 and e.pool.checkedin() == 0 for e in engines)
            assert all(loop.is_closed() for loop in loops)
            assert not any(loop in http_fetcher._LOOP_CLIENTS for loop in loops)
            assert not any(loop in scrape_do_semaphore._clients for loop in loops)
            # Observe peer EOF and PostgreSQL backend exit, not just pool size.
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                remaining_sessions = observer_loop.run_until_complete(active_sessions())
                remaining_tcp = len(tcp_sockets() - baseline_tcp)
                if remaining_sessions == remaining_tcp == 0 and fd_count() <= baseline:
                    break
                time.sleep(0.01)
            assert remaining_sessions == remaining_tcp == 0
            samples.append(fd_count())
            assert samples[-1] <= baseline, (baseline, samples)
finally:
    observer_loop.run_until_complete(observer.dispose())
    observer_loop.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    redis_process.terminate()
    redis_process.wait(timeout=5)
    temp.cleanup()
    gc.enable()

print(json.dumps({"loops": len(loops), "baseline_fds": baseline, "max_fds": max(samples),
                  "remaining_db_sessions": remaining_sessions,
                  "remaining_tcp_sockets": remaining_tcp, "samples": samples}))