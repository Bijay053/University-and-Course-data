"""Read-only socket acceptance in a disposable process, not a Celery worker."""
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
from unittest.mock import AsyncMock

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.database import postgres_tls_connect_args
from app.services.scraper import browser_pool, http_fetcher, job_claim, scrape_do_semaphore
from app.tasks import scrape_tasks as st


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
# This Redis instance has no TCP port and no access to the user's broker/keys.
temp = tempfile.TemporaryDirectory(prefix="loop-fd-probe-")
redis_socket = os.path.join(temp.name, "redis.sock")
redis_process = subprocess.Popen(
    ["redis-server", "--port", "0", "--unixsocket", redis_socket,
     "--save", "", "--appendonly", "no"],
    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)
deadline = time.monotonic() + 5
while not os.path.exists(redis_socket) and time.monotonic() < deadline:
    assert redis_process.poll() is None, "isolated Redis exited during startup"
    time.sleep(0.01)
assert os.path.exists(redis_socket), "isolated Redis socket did not appear"
scrape_do_semaphore.settings = SimpleNamespace(redis_url=f"unix://{redis_socket}")
# Separate engine, read-only queries, never the application pool or user rows.
engine = create_async_engine(settings.database_url, pool_size=2, max_overflow=0,
                             connect_args=postgres_tls_connect_args())
st.engine = engine
st.AsyncSessionLocal = async_sessionmaker(engine)
browser_pool.pool.close = AsyncMock()  # No browser launched in this acceptance.
loops = []  # Keep old loops alive so WeakKeyDictionary/GC cannot hide a leak.
gc.disable()


def fd_count():
    return len(os.listdir("/proc/self/fd"))


async def work(db, _job_id, *, fail=False):
    loops.append(asyncio.get_running_loop())
    await db.execute(text("SELECT 1"))
    client = http_fetcher._get_shared_client()
    assert (await client.get(f"http://127.0.0.1:{server.server_port}/")).text == "ok"
    await scrape_do_semaphore._get_client().ping()  # No distributed keys touched.
    if fail:
        raise ValueError("isolated failure")


async def db_work(fail):
    loops.append(asyncio.get_running_loop())
    async with st.AsyncSessionLocal() as db:
        await db.execute(text("SELECT 1"))
    if fail:
        raise ValueError("isolated failure")
    return 7


async def no_claim(db, job_id):
    await work(db, job_id)
    return False  # Never reads or modifies an actual job.


baseline = fd_count()
samples = []
try:
    for index in range(16):
        fail = index % 2 == 1
        try:
            result = st._run_db_coro(db_work(fail))
            assert result == 7 and not fail
        except ValueError:
            assert fail
        async def repair(db, job_id):
            await work(db, job_id, fail=fail)
        st.run_repair = repair
        st._sync_dispose()
        try:
            asyncio.run(st._async_repair("isolated-no-job"))
            assert not fail
        except ValueError:
            assert fail
        job_claim.claim_runtime_job = no_claim
        st._sync_dispose()
        asyncio.run(st._async_bulk_fix("isolated-no-job"))
        assert engine.pool.checkedout() == 0
        assert engine.pool.checkedin() == 0
        assert all(loop.is_closed() for loop in loops)
        assert not any(loop in http_fetcher._LOOP_CLIENTS for loop in loops)
        assert not any(loop in scrape_do_semaphore._clients for loop in loops)
        # Give the local HTTP server thread time to observe client socket EOF.
        deadline = time.monotonic() + 2
        while fd_count() > baseline and time.monotonic() < deadline:
            time.sleep(0.01)
        samples.append(fd_count())
    assert max(samples) <= baseline + 2, (baseline, samples)
finally:
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    redis_process.terminate()
    redis_process.wait(timeout=5)
    temp.cleanup()
    gc.enable()

print(json.dumps({"loops": len(loops), "baseline_fds": baseline,
                  "max_fds": max(samples), "final_fds": fd_count(), "samples": samples}))