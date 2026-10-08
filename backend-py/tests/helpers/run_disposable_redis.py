"""Run pytest with a private broker, redirecting only child-process connections.

Examples (from backend-py):
  python tests/helpers/run_disposable_redis.py --full-regression
  python tests/helpers/run_disposable_redis.py tests/test_stale_requeue.py -q

Nothing listens on the shared broker port. The worker's Redis configuration is
never changed. Separate invocations own separate brokers, so one suite cannot
shut down another's dependency.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time


def redirect_child_connections(port: int) -> None:
    import redis.connection
    import redis.asyncio.connection

    def redirect(connection):
        # Dedicated Redis servers created by other tests retain their address.
        # Never let a real connection reach the shared broker or a remote host.
        if connection.host not in {"127.0.0.1", "localhost", "::1"}:
            raise RuntimeError("disposable validation forbids remote Redis connections")
        if int(connection.port) == 6379:
            connection.host = "127.0.0.1"
            connection.port = port
            connection.username = None
            connection.password = None

    original_sync = redis.connection.Connection._connect
    original_async = redis.asyncio.connection.Connection._connect

    def sync_connect(self):
        redirect(self)
        return original_sync(self)

    async def async_connect(self):
        redirect(self)
        return await original_async(self)

    redis.connection.Connection._connect = sync_connect
    redis.asyncio.connection.Connection._connect = async_connect


def child_main(port: int, args: list[str]) -> int:
    # pytest is imported after redirects are installed, before app/test imports.
    sys.path.insert(0, str(Path.cwd()))
    redirect_child_connections(port)
    if args == ["--smoke"]:
        import redis
        import redis.asyncio as async_redis

        client = redis.from_url("redis://127.0.0.1:6379/4")
        try:
            client.set("private-smoke", "sync")
            assert client.get("private-smoke") == b"sync"
            connection = client.connection_pool.get_connection()
            try:
                assert connection.port == port
            finally:
                client.connection_pool.release(connection)
        finally:
            client.close()

        async def check_async():
            client = async_redis.from_url("redis://localhost:6379/4")
            try:
                assert await client.get("private-smoke") == b"sync"
                assert await client.delete("private-smoke") == 1
            finally:
                await client.aclose()

        asyncio.run(check_async())
        print("Private synchronous and asynchronous Redis connections passed.", flush=True)
        return 0
    import pytest
    return int(pytest.main(args))


@contextmanager
def private_broker():
    import redis

    if not shutil.which("redis-server"):
        raise RuntimeError("redis-server is required for disposable validation")
    with tempfile.TemporaryDirectory(prefix="validation-redis-") as directory:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        root = Path(directory)
        with (root / "redis.log").open("wb") as log:
            process = subprocess.Popen([
                "redis-server", "--bind", "127.0.0.1", "--port", str(port),
                "--save", "", "--appendonly", "no", "--dir", directory,
                "--loglevel", "warning",
            ], stdout=log, stderr=subprocess.STDOUT)
            client = redis.Redis(host="127.0.0.1", port=port, socket_timeout=0.2)
            try:
                deadline = time.monotonic() + 10
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("private Redis exited before becoming ready")
                    try:
                        if client.ping():
                            break
                    except redis.RedisError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("private Redis did not become ready")
                        time.sleep(0.05)
                print(f"Disposable Redis ready on private port {port}.", flush=True)
                yield port
            finally:
                client.close()
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=10)
                assert process.poll() is not None
        print(json.dumps({"disposable_redis_stopped": True}), flush=True)


def parent_main(args: list[str]) -> int:
    full = bool(args and args[0] == "--full-regression")
    batches = [["-q", *args[1:]]] if full else [args]
    if full:
        batches.append([
            "-q", "tests/test_winchester_catalogue_review.py",
            "-o", "asyncio_default_test_loop_scope=function",
        ])
    child = None

    def interrupted(signum, frame):
        if child is not None and child.poll() is None:
            child.send_signal(signum)
        raise SystemExit(128 + signum)

    signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    old_handlers = {sig: signal.signal(sig, interrupted) for sig in signals}
    try:
        with private_broker() as port:
            for batch in batches:
                child = subprocess.Popen([
                    sys.executable, str(Path(__file__).resolve()), "--child",
                    str(port), *batch,
                ])
                try:
                    code = child.wait()
                finally:
                    if child.poll() is None:
                        child.terminate()
                        try:
                            child.wait(timeout=10)
                        except subprocess.TimeoutExpired:
                            child.kill()
                            child.wait(timeout=10)
                if code:
                    return code
            return 0
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        raise SystemExit(child_main(int(sys.argv[2]), sys.argv[3:]))
    raise SystemExit(parent_main(sys.argv[1:]))
