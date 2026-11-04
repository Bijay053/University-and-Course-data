"""Private broker/worker harness. Never imports the portal or its configuration.

Run from backend-py (no portal conftest, database or configured services needed):
  PYTHONPATH=. python -m pytest tests/test_partial_release_recovery.py \
      -q -c /dev/null --noconftest -p no:cacheprovider

Requires the existing Celery/Redis Python packages and redis-server executable.
Missing dependencies fail rather than silently skipping the transport coverage.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

from celery import Celery
from kombu import Queue
import redis


def application(url, prefix, client_name="fixture-controller"):
    app = Celery("recovery_transport_fixture", broker=url, set_as_current=False)
    app.conf.update(
        task_queues=(Queue("scrape"), Queue("fixture-control")),
        task_default_queue="fixture-control",
        broker_transport_options={
            "global_keyprefix": prefix, "polling_interval": 0.1,
            "client_name": client_name,
        },
        broker_connection_retry_on_startup=True,
        worker_prefetch_multiplier=1,
        task_ignore_result=True,
    )
    # Celery permits environment variables to override explicit configuration.
    # Refuse before opening any connection if the caller has such overrides.
    if any(value != url for value in (
        app.conf.broker_url, app.conf.broker_read_url, app.conf.broker_write_url,
    )) or app.conf.result_backend:
        app.close()
        raise RuntimeError("fixture broker must be private and have no result backend")

    @app.task(name="fixture.unrelated_scrape")
    def unrelated_scrape(ledger, repo, runtime):
        head = subprocess.check_output(
            ["git", "-C", repo, "rev-parse", "HEAD"], text=True,
        ).strip()
        with open(ledger, "a") as stream:
            stream.write(json.dumps({"head": head, "runtime": runtime}) + "\n")

    return app


def eventually(predicate, *, timeout=15, description="fixture readiness"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {description}")


class WorkerCluster:
    def __init__(self, root, repo, predecessor):
        self.root, self.repo, self.predecessor = root, repo, predecessor
        self.socket = root / "redis.sock"
        self.url = f"redis+socket://{self.socket}?virtual_host=0"
        self.prefix = f"recovery-{uuid4().hex}:"
        self.app = application(self.url, self.prefix)
        self.client = redis.Redis(
            unix_socket_path=str(self.socket), socket_timeout=1,
        )
        self.processes = {}
        self.logs = []
        self.ledger = root / "started.jsonl"

    def inspect(self):
        return self.app.control.inspect(timeout=0.5)

    def queues(self):
        return self.inspect().active_queues() or {}

    def start(self, name):
        assert name not in self.processes or self.processes[name].poll() is not None
        log = (self.root / f"{name}-{len(self.logs)}.log").open("wb")
        self.logs.append(log)
        # Deliberately do not inherit credentials, app settings, PYTHONPATH or
        # broker/database URLs from the parent Replit/production environment.
        env = {
            "PATH": os.environ["PATH"], "HOME": str(self.root),
            "LANG": "C.UTF-8", "PYTHONUNBUFFERED": "1",
        }
        process = subprocess.Popen([
            sys.executable, "-B", str(Path(__file__).resolve()),
            self.url, self.prefix, name,
        ], cwd=self.root, env=env, stdout=log, stderr=subprocess.STDOUT)
        self.processes[name] = process

        def ready():
            assert process.poll() is None, self.log_text()
            return name in self.queues()
        eventually(ready, description=f"private worker {name}")

    def log_text(self):
        return "\n".join(Path(log.name).read_text(errors="replace") for log in self.logs)

    def stop(self, name):
        process = self.processes[name]
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        assert process.poll() is not None

    def stop_all(self):
        for name in self.processes:
            self.stop(name)

    def enqueue(self):
        return self.app.send_task(
            "fixture.unrelated_scrape",
            args=[str(self.ledger), str(self.repo), self.predecessor],
            queue="scrape",
        )

    def pending(self):
        return self.client.llen(self.prefix + "scrape")

    def reconnect(self):
        """Sever the actual worker's blocking socket, not its control replies."""
        def blocking():
            return [c for c in self.client.client_list()
                    if c["cmd"] == "brpop" and c["name"] in self.processes]
        clients = eventually(blocking, description="worker blocking Redis socket")
        old_ids = {c["id"] for c in clients}
        for client in clients:
            assert self.client.client_kill_filter(_id=client["id"]) == 1
        eventually(
            lambda: any(c["id"] not in old_ids for c in blocking()),
            description="worker reconnecting to disposable Redis",
        )
        assert all(p.poll() is None for p in self.processes.values())

    def assert_blocked(self, *, stopped=False):
        """A real broker round trip, not merely a mocked enabled flag."""
        before = self.ledger.read_text() if self.ledger.exists() else ""
        assert all(row["head"] == row["runtime"]
                   for row in map(json.loads, before.splitlines()))
        if stopped:
            assert all(p.poll() is not None for p in self.processes.values())
        else:
            queues = self.queues()
            assert queues
            assert all(not any(q["name"] == "scrape" for q in qs)
                       for qs in queues.values())
        self.enqueue()
        # Let live workers cycle through BRPOP, then inspect them once more.
        time.sleep(0.3)
        if not stopped:
            assert self.inspect().ping()
        assert self.pending() == 1
        after = self.ledger.read_text() if self.ledger.exists() else ""
        assert after == before, after

    def close(self):
        try:
            self.stop_all()
        finally:
            self.app.close()
            self.client.close()
            for log in self.logs:
                log.close()


@contextmanager
def private_cluster(root, repo, predecessor):
    root.mkdir()
    cluster = WorkerCluster(root, repo, predecessor)
    with (root / "redis.log").open("wb") as log:
        # Unix socket only: cannot collide with, or contact, shared Redis.
        broker = subprocess.Popen([
            "redis-server", "--port", "0", "--unixsocket", str(cluster.socket),
            "--unixsocketperm", "700", "--save", "", "--appendonly", "no",
            "--dir", str(root),
        ], env={"PATH": os.environ["PATH"], "HOME": str(root), "LANG": "C.UTF-8"},
            stdout=log, stderr=subprocess.STDOUT)
        try:
            def ready():
                assert broker.poll() is None, (root / "redis.log").read_text()
                try:
                    return cluster.client.ping()
                except redis.RedisError:
                    return False
            eventually(ready, description="private Unix-socket Redis")
            yield cluster
        finally:
            try:
                cluster.close()
            finally:
                broker.terminate()
                try:
                    broker.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    broker.kill()
                    broker.wait(timeout=5)
                assert broker.poll() is not None
                assert all(p.poll() is not None for p in cluster.processes.values())
                # Redis removes its owned socket on orderly exit. Independently
                # check no server can answer, including when a test must fail.
                probe = redis.Redis(unix_socket_path=str(cluster.socket),
                                    socket_timeout=0.2)
                try:
                    try:
                        probe.ping()
                    except redis.RedisError:
                        pass
                    else:
                        raise AssertionError("disposable Redis survived teardown")
                finally:
                    probe.close()


if __name__ == "__main__":
    url, prefix, name = sys.argv[1:]
    assert url.startswith("redis+socket:///") and prefix.startswith("recovery-")
    application(url, prefix, client_name=name).worker_main([
        "worker", "--pool=solo", "--concurrency=1", "--hostname=" + name,
        "--queues=scrape,fixture-control", "--without-gossip", "--without-mingle",
        "--without-heartbeat", "--loglevel=INFO",
    ])
