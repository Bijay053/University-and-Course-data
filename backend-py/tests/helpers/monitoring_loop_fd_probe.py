"""Read-only watcher acceptance in a child process, never a live worker.

Real probe clients and task/session boundaries run repeatedly. Only the due
watcher result and scrape dispatch are replaced: no university rows are read
or written and no broker is contacted. GC is disabled and old loops retained
so finalizers cannot conceal a leak.
"""
from __future__ import annotations

import asyncio
import gc
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import postgres_tls_connect_args
from app.services import monitoring_engine as service
from app.tasks import monitoring_tasks as tasks


application_name = f"watcher-loop-probe-{os.getpid()}"
args = postgres_tls_connect_args()
args["server_settings"] = {
    "application_name": application_name,
    "default_transaction_read_only": "on",
}
engine = create_async_engine(settings.database_url, pool_size=1, max_overflow=0, connect_args=args)
sessions = async_sessionmaker(engine)
observer = create_async_engine(
    settings.database_url, pool_size=1, max_overflow=0,
    connect_args=postgres_tls_connect_args(),
)
observer_loop = asyncio.new_event_loop()
loops, clients, dispatches = [], [], []
mode = ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def reply(self, *, head=False):
        if self.path == "/fail":
            self.close_connection = True
            return
        self.send_response(200)
        self.send_header("ETag", '"current"')
        self.send_header("Content-Length", "2")
        self.end_headers()
        if not head:
            self.wfile.write(b"ok")

    def do_GET(self):
        self.reply()

    def do_HEAD(self):
        self.reply(head=True)

    def log_message(self, *_):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
thread = threading.Thread(target=server.serve_forever, daemon=True)
thread.start()
url = f"http://127.0.0.1:{server.server_port}"
real_client = service.httpx.AsyncClient


def tracked_client(*args, **kwargs):
    client = real_client(*args, **kwargs)
    clients.append(client)
    return client


service.httpx.AsyncClient = tracked_client


class ReadonlyCycleSession:
    """Run real SELECTs; supply only synthetic due watchers to the service."""

    async def __aenter__(self):
        loops.append(asyncio.get_running_loop())
        self.db = sessions()
        await self.db.__aenter__()
        return self

    async def __aexit__(self, *args):
        return await self.db.__aexit__(*args)

    async def execute(self, statement):
        # PostgreSQL enforces read-only even if the service changes accidentally.
        assert (await self.db.execute(text("SHOW transaction_read_only"))).scalar_one() == "on"
        await self.db.execute(text("SELECT 1/0" if mode == "database_error" else "SELECT 1"))
        watchers = [] if mode == "empty" else [SimpleNamespace(
            id=1, university_id=1, probe_url=url + ("/fail" if mode == "probe_error" else ""),
            monitoring_strategy="deep" if mode == "deep_changed" else mode,
            etag='"old"' if mode == "deep_changed" else '"current"',
            page_hash=service._sha256(b"ok"), sitemap_hash=None,
            total_checks=0, total_changes_detected=0, consecutive_unchanged=0,
            last_changed_at=None, change_frequency_days=None,
        )]
        self.watchers = watchers
        return SimpleNamespace(scalars=lambda: watchers)

    async def commit(self):
        raise AssertionError("acceptance must never commit business data")

    def add(self, _):
        raise AssertionError("acceptance must never add business rows")


async def dispatch(watcher, db):
    assert mode == "deep_changed"
    assert watcher.last_probe_result == "changed"
    assert watcher.total_changes_detected == 1
    dispatches.append(watcher.id)
    return "isolated-no-job"


tasks.engine = engine
tasks.AsyncSessionLocal = ReadonlyCycleSession
service.trigger_scrape = dispatch
# Defense in depth: task.run is direct, and dispatch is replaced above.
tasks.celery_app.send_task = MagicMock(side_effect=AssertionError("live broker forbidden"))


async def active_sessions():
    async with observer.connect() as connection:
        return (await connection.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE application_name = :name"
        ), {"name": application_name})).scalar_one()


def fd_count():
    return len(os.listdir("/proc/self/fd"))


def tcp_sockets():
    inodes = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        if os.path.exists(path):
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


assert observer_loop.run_until_complete(active_sessions()) == 0
baseline, baseline_tcp = fd_count(), tcp_sockets()
samples = []
remaining_sessions = remaining_tcp = -1
gc.disable()
try:
    for _ in range(6):
        for mode in ("empty", "passive", "active", "deep_changed", "probe_error", "database_error"):
            result = tasks.check_watchers.run()
            if mode == "database_error":
                assert result["error"]
                assert {key: result[key] for key in ("checked", "changed", "triggered")} == {
                    "checked": 0, "changed": 0, "triggered": 0,
                }
            else:
                assert result == {
                    "checked": int(mode != "empty"),
                    "changed": int(mode == "deep_changed"),
                    "triggered": int(mode == "deep_changed"),
                    "errors": int(mode == "probe_error"),
                }, (mode, result)
            assert engine.pool.checkedout() == engine.pool.checkedin() == 0
            assert all(loop.is_closed() for loop in loops)
            assert all(client.is_closed for client in clients)
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
    assert len(dispatches) == 6
    tasks.celery_app.send_task.assert_not_called()
finally:
    observer_loop.run_until_complete(observer.dispose())
    observer_loop.close()
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    gc.enable()

print(json.dumps({
    "loops": len(loops), "probe_clients": len(clients), "mock_dispatches": len(dispatches),
    "baseline_fds": baseline, "max_fds": max(samples),
    "remaining_db_sessions": remaining_sessions, "remaining_tcp_sockets": remaining_tcp,
    "samples": samples,
}))
