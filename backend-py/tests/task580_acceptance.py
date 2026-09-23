"""Disposable real-worker acceptance for all-filtered retries and report continuations.

Run from the repository root:
  python backend-py/tests/task580_acceptance.py --output /tmp/task580-evidence
"""
from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import select
import signal
import socket
import subprocess
import sys
import tempfile
import time
import traceback
from urllib.request import urlopen

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.task564_acceptance import port, recipe_snapshot

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend-py"
SOURCE = "task564-source"
REPORT_PARENT = "task582-report-parent"
SELECTED = [
    "https://task564.example.test/courses/blocked-alpha",
    "https://task564.example.test/courses/blocked-beta",
]
ERROR_SELECTED = [
    "https://task564.example.test/courses/exhausted-fetch",
]
PRIOR = [
    "https://task564.example.test/courses/prior-reviewed",
    "https://task564.example.test/courses/blocked-prior-checkpoint",
]
TERMINAL = {
    "completed", "completed_with_errors", "completed_with_warnings",
    "failed", "failed_degraded", "stopped",
}


async def database_snapshot(url: str) -> dict:
    import asyncpg
    db = await asyncpg.connect(url.replace("postgresql+asyncpg:", "postgresql:"))
    try:
        jobs = await db.fetch("""
            SELECT runtime_job_id, status, request_payload, discovered_config,
                   imported, errors, error_message, completed_at, worker_pid
            FROM scrape_runtime_jobs ORDER BY created_at, runtime_job_id
        """)
        return {
            "jobs": [dict(row) for row in jobs],
            "earlier_reviews": {
                job_id: {
                    "job": await db.fetchval(
                        "SELECT row_to_json(j)::text FROM scrape_runtime_jobs j "
                        "WHERE runtime_job_id=$1", job_id,
                    ),
                    "rows": await db.fetchval(
                        "SELECT coalesce(json_agg(row_to_json(s) ORDER BY id)::text, '[]') "
                        "FROM scraped_courses s WHERE scrape_job_id=$1", job_id,
                    ),
                    "logs": await db.fetchval(
                        "SELECT coalesce(json_agg(row_to_json(l) ORDER BY sequence)::text, '[]') "
                        "FROM scrape_runtime_logs l WHERE runtime_job_id=$1", job_id,
                    ),
                }
                for job_id in (SOURCE, REPORT_PARENT)
            },
            "source_errors": await db.fetchval(
                "SELECT errors FROM scrape_runtime_jobs WHERE runtime_job_id=$1", SOURCE
            ),
            "source_approved": await db.fetchval(
                "SELECT count(*) FROM scraped_courses "
                "WHERE scrape_job_id=$1 AND status='approved'", SOURCE
            ),
            "source_approved_bytes": await db.fetchval("""
                SELECT coalesce(json_agg(row_to_json(s) ORDER BY id)::text, '[]')
                FROM scraped_courses s
                WHERE scrape_job_id=$1 AND status='approved'
            """, SOURCE),
            "published": await db.fetchval(
                "SELECT count(*) FROM courses WHERE approval_status='approved'"
            ),
            "published_bytes": await db.fetchval("""
                SELECT coalesce(json_agg(row_to_json(c) ORDER BY id)::text, '[]')
                FROM courses c WHERE approval_status='approved'
            """),
            "retry_rows": await db.fetch("""
                SELECT id, scrape_job_id, course_website, status
                FROM scraped_courses WHERE scrape_job_id <> $1 ORDER BY id
            """, SOURCE),
            "child_rows_bytes": await db.fetchval("""
                SELECT coalesce(json_agg(row_to_json(s) ORDER BY id)::text, '[]')
                FROM scraped_courses s WHERE scrape_job_id='job_task582_continuation'
            """),
            "diagnostic_logs": [
                dict(row) for row in await db.fetch("""
                    SELECT runtime_job_id, sequence, event, payload
                    FROM scrape_runtime_logs
                    WHERE payload->>'kind'='targeted_retry_all_filtered'
                    ORDER BY sequence
                """)
            ],
        }
    finally:
        await db.close()


def snapshot(url: str) -> dict:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(
            lambda: asyncio.run(database_snapshot(url))
        ).result(timeout=30)


def decoded(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def run(
    output: Path, *, continuation: bool = False, resume: str | None = None,
    interruption: bool = False, error_interruption: bool = False,
) -> dict:
    assert resume in (None, "mixed", "resolved")
    assert not resume or continuation
    assert not (interruption and error_interruption)
    assert not (interruption or error_interruption) or (continuation and resume is None)
    selected = (
        ERROR_SELECTED if error_interruption else
        PRIOR + (SELECTED if resume == "mixed" else []) if resume else SELECTED
    )
    blocked = resume != "resolved"
    output.mkdir(parents=True, exist_ok=True)
    for binary in ("initdb", "pg_ctl", "redis-server"):
        if not shutil.which(binary):
            raise RuntimeError(f"Required binary missing: {binary}")

    evidence = {
        "task": 591 if error_interruption else 587 if interruption else 580,
        "scenario": (
            "course_report_error_checkpoint_interruption" if error_interruption else
            "course_report_checkpoint_interruption" if interruption else
            f"course_report_{resume or 'continuation'}" if continuation else "retry_unresolved"
        ),
        "passed": False,
        "production_boundaries": (
            "Production FastAPI router, Celery prefork task, scraper orchestrator, "
            "URL filters, and PostgreSQL persistence are used without mocks."
        ),
    }
    processes: list[tuple[str, subprocess.Popen]] = []
    logs = []
    pg_started = False
    temp = Path(tempfile.mkdtemp(prefix="task580-private-"))
    recipe_root = temp / "recipes"
    postrun_root = temp / "task-postrun"
    checkpoint_marker_path = temp / "checkpoint-interruption.json"
    recipes_before = recipe_snapshot()
    pgport, redisport, apiport = [port() for _ in range(3)]
    dburl = f"postgresql+asyncpg://task564@127.0.0.1:{pgport}/postgres"
    env = {
        key: os.environ[key]
        for key in (
            "PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "NIX_LD_LIBRARY_PATH",
            "SSL_CERT_FILE",
        )
        if key in os.environ
    }
    env.update({
        "DATABASE_URL": dburl,
        "DATABASE_REQUIRE_TLS": "false",
        "REDIS_URL": f"redis://127.0.0.1:{redisport}/0",
        "SESSION_SECRET": "task580-disposable-session-secret",
        "TASK564_ISOLATED": "yes",
        "TASK564_HTTP_PORT": str(port()),
        "TASK564_API_PORT": str(apiport),
        "TASK564_RECIPE_ROOT": str(recipe_root),
        "TASK580_POSTRUN_DIR": str(postrun_root),
        "PYTHONPATH": str(BACKEND),
        "GEMINI_API_KEY": "",
        "OPENAI_API_KEY": "",
        "SCRAPE_DO_TOKEN": "",
        "AI_INTEGRATIONS_OPENAI_API_KEY": "",
        "NODE_ENV": "test",
    })
    if interruption or error_interruption:
        env["TASK587_CHECKPOINT_MARKER"] = str(checkpoint_marker_path)
    if error_interruption:
        env["TASK591_ERROR_CASE"] = "yes"

    def start(name: str, command: list[str]) -> subprocess.Popen:
        log = (output / f"{name}.log").open("w")
        logs.append(log)
        child = subprocess.Popen(
            command,
            cwd=BACKEND,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        processes.append((name, child))
        return child

    def stop(child: subprocess.Popen) -> None:
        if child.poll() is None:
            os.killpg(child.pid, signal.SIGTERM)
            try:
                child.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait(timeout=5)

    def wait_http(url: str) -> None:
        for _ in range(240):
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status < 500:
                        return
            except Exception:
                time.sleep(.25)
        raise AssertionError(f"Server did not become ready: {url}")

    def login(client: httpx.Client) -> None:
        response = client.post("/api/auth/login", json={
            "email": "task564@example.test",
            "password": "isolated-task564-password",
        })
        assert response.status_code == 200, response.text

    def submit_unresolved(base: str) -> str:
        with httpx.Client(base_url=base, timeout=40) as client:
            login(client)
            response = client.post(
                f"/api/scrape/history/{SOURCE}/retry-unresolved",
                json={"urls": SELECTED},
            )
            assert response.status_code == 202, response.text
            posted = response.json()
            return posted.get("job_id") or posted["jobId"]

    try:
        subprocess.run(
            ["initdb", "-D", str(temp / "pg"), "-A", "trust", "-U", "task564"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [
                "pg_ctl", "-D", str(temp / "pg"), "-l",
                str(output / "postgres.log"), "-o",
                f"-h 127.0.0.1 -p {pgport} -k {temp}", "-w", "start",
            ],
            check=True,
            capture_output=True,
        )
        pg_started = True
        start("redis", [
            "redis-server", "--bind", "127.0.0.1", "--port", str(redisport),
            "--save", "", "--appendonly", "no", "--dir", str(temp),
        ])
        seed = start("seed", [
            sys.executable, "-m", "tests.task580_process",
            f"seed-{resume}" if resume else ("seed-continuation" if continuation else "seed"),
        ])
        assert seed.wait(timeout=90) == 0, "Schema/seed failed; see seed.log"
        before = snapshot(dburl)
        assert before["source_errors"] == 7
        assert before["source_approved"] > 0
        assert before["published"] > 0
        if continuation:
            assert REPORT_PARENT != SOURCE
            for review in before["earlier_reviews"].values():
                assert review["job"] is not None
                assert decoded(review["rows"])
        if resume:
            child_before = next(row for row in before["jobs"]
                                if row["runtime_job_id"] == "job_task582_continuation")
            assert decoded(child_before["discovered_config"])["autonomousVerification"]["completed_urls"] == [PRIOR[1]]
            assert [row["course_website"] for row in decoded(before["child_rows_bytes"])] == [PRIOR[0]]

        worker = start(
            "worker", [sys.executable, "-m", "tests.task580_process", "worker"]
        )
        api = start("api-first", [
            sys.executable, "-m", "tests.task580_process", "api",
        ])
        wait_http(f"http://127.0.0.1:{apiport}/api/health")
        base = f"http://127.0.0.1:{apiport}"
        if continuation:
            retry_id = "job_task582_continuation"
            dispatch = start("dispatch", [
                sys.executable, "-m", "tests.task580_process", "dispatch-continuation",
            ])
            assert dispatch.wait(timeout=30) == 0, "Dispatch failed; see dispatch.log"
        else:
            retry_id = submit_unresolved(base)

        retry_parent = REPORT_PARENT if continuation else SOURCE

        if interruption or error_interruption:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if checkpoint_marker_path.exists():
                    checkpoint_marker = json.loads(checkpoint_marker_path.read_text())
                    break
                assert worker.poll() is None, "Worker exited before checkpoint pause"
                time.sleep(.1)
            else:
                raise AssertionError("First exclusion checkpoint pause was not reached")
            interrupted_snapshot = snapshot(dburl)
            interrupted_child = next(
                row for row in interrupted_snapshot["jobs"]
                if row["runtime_job_id"] == retry_id
            )
            interrupted_config = decoded(interrupted_child["discovered_config"])
            assert interrupted_child["status"] not in TERMINAL
            assert set(interrupted_config["autonomousVerification"]["completed_urls"]) == set(selected)
            if error_interruption:
                assert interrupted_config["autonomousVerification"]["url_outcomes"] == {
                    selected[0]: "error",
                }
            assert not interrupted_config.get("targeted_retry_diagnostic")
            assert not interrupted_snapshot["diagnostic_logs"]
            assert not (postrun_root / f"{retry_id}.json").exists()
            assert interrupted_snapshot["earlier_reviews"] == before["earlier_reviews"]
            assert interrupted_snapshot["published_bytes"] == before["published_bytes"]
            assert interrupted_snapshot["child_rows_bytes"] == before["child_rows_bytes"]
            evidence["interruption"] = {
                "checkpoint_marker": checkpoint_marker,
                "committed_child_before_kill": interrupted_child,
                "diagnostic_absent_before_kill": True,
            }

            # Freeze the supervisor first: it must not auto-redeliver this
            # lost task before we have observed death and revoked its fence.
            os.kill(worker.pid, signal.SIGSTOP)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                state = Path(f"/proc/{worker.pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if state == "T":
                    break
                time.sleep(.01)
            else:
                raise AssertionError("Could not freeze prefork supervisor")
            from app.services.worker_fencing import process_identity
            victim_pid = checkpoint_marker["worker_pid"]
            assert victim_pid != worker.pid
            assert os.getpgid(victim_pid) == worker.pid
            pidfd = os.pidfd_open(victim_pid)
            try:
                assert process_identity(victim_pid) == checkpoint_marker["process_identity"]
                poller = select.poll()
                poller.register(pidfd, select.POLLIN)
                os.kill(victim_pid, signal.SIGKILL)
                assert any(
                    flags & select.POLLIN for _, flags in poller.poll(15000)
                ), "Kernel did not confirm interrupted prefork child's exit"
            finally:
                os.close(pidfd)
            death = {
                "worker_pid": victim_pid,
                "process_identity": checkpoint_marker["process_identity"],
                "pidfd_exit_observed": True,
            }
            checkpoint_marker_path.with_suffix(".death.json").write_text(json.dumps(death))
            evidence["interruption"]["death"] = death
            os.killpg(worker.pid, signal.SIGKILL)
            worker.wait(timeout=15)
            assert not (postrun_root / f"{retry_id}.json").exists()

            requeue = start("requeue-interrupted", [
                sys.executable, "-m", "tests.task580_process", "requeue-interrupted",
            ])
            assert requeue.wait(timeout=30) == 0, "Interrupted requeue failed"
            requeued = snapshot(dburl)
            matching = [row for row in requeued["jobs"] if row["runtime_job_id"] == retry_id]
            assert len(matching) == 1
            assert matching[0]["status"] == "queued"
            assert matching[0]["request_payload"] == interrupted_child["request_payload"]
            assert matching[0]["discovered_config"] == interrupted_child["discovered_config"]
            # The durable marker makes the test hook one-shot across workers.
            worker = start("worker-redelivery", [
                sys.executable, "-m", "tests.task580_process", "worker",
            ])
            dispatch = start("dispatch-interrupted-redelivery", [
                sys.executable, "-m", "tests.task580_process", "dispatch-continuation",
            ])
            assert dispatch.wait(timeout=30) == 0, "Interrupted redelivery dispatch failed"

        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            final = snapshot(dburl)
            retry = next(
                (row for row in final["jobs"]
                 if row["runtime_job_id"] == retry_id),
                None,
            )
            if retry and retry["status"] in TERMINAL:
                break
            time.sleep(.25)
        else:
            raise AssertionError("Real prefork worker did not reach terminal state")

        # A terminal database row precedes the production recovery post-pass.
        # The task_postrun marker is emitted only after scrape.university has
        # returned, so all durability assertions below observe the complete
        # Celery task lifecycle rather than its intermediate terminal commit.
        marker_path = postrun_root / f"{retry_id}.json"
        marker_deadline = time.monotonic() + 120
        while time.monotonic() < marker_deadline:
            if marker_path.exists():
                postrun_marker = json.loads(marker_path.read_text())
                break
            if worker.poll() is not None:
                raise AssertionError("Prefork worker exited before task_postrun")
            time.sleep(.1)
        else:
            raise AssertionError("Celery task_postrun marker was not persisted")
        assert postrun_marker["task_name"] == "scrape.university"
        assert postrun_marker["runtime_job_id"] == retry_id
        assert postrun_marker["task_id"]
        assert postrun_marker["state"] == "SUCCESS", postrun_marker
        assert postrun_marker["return_value"]["ok"] is True, postrun_marker
        assert postrun_marker["return_value"]["id"] == retry_id, postrun_marker
        if interruption or error_interruption:
            assert postrun_marker["task_id"] != checkpoint_marker["task_id"]
            assert postrun_marker["worker_pid"] != checkpoint_marker["worker_pid"]
            evidence["interruption"]["redelivery_postrun"] = postrun_marker

        # Discard the intermediate terminal snapshot. This is the authoritative
        # post-task state used by every persistence and API-restart assertion.
        final = snapshot(dburl)
        retry = next(
            row for row in final["jobs"]
            if row["runtime_job_id"] == retry_id
        )
        payload = decoded(retry["request_payload"])
        config = decoded(retry["discovered_config"])
        diagnostic = config.get("targeted_retry_diagnostic")
        evidence["terminal_database_debug"] = {
            "retry": retry,
            "discovered_config": config,
            "diagnostic_logs": final["diagnostic_logs"],
        }
        expected_status = (
            "failed_degraded" if error_interruption else
            "failed" if blocked else "completed"
        )
        assert retry["status"] == expected_status, retry
        assert (
            retry["errors"] >= 0 if error_interruption else
            retry["errors"] > 0 if blocked else retry["errors"] == 0
        ), retry
        assert payload["courseUrls"] == selected
        assert payload["course_urls"] == selected
        assert payload["retrySourceJobId"] == retry_parent
        if continuation:
            assert payload["courseReport"]["source_job_id"] == SOURCE
        if blocked and not error_interruption:
            assert diagnostic["selected_urls"] == selected
            assert diagnostic["filtered_urls"] == SELECTED
            assert diagnostic["selected_count"] == len(selected)
            assert diagnostic["filtered_count"] == len(SELECTED)
            assert diagnostic["resolved_count"] == (len(PRIOR) if resume else 0)
            assert diagnostic["retry_source_job_id"] == retry_parent
            assert diagnostic["source_review_job_id"] == SOURCE
            assert diagnostic["processed_count"] == 0
            assert retry["error_message"] == diagnostic["message"]
        elif not blocked:
            assert diagnostic is None
            assert not retry["error_message"]
        else:
            assert diagnostic is None
            assert retry["status"] == "failed_degraded"
            assert config["autonomousVerification"]["url_outcomes"][selected[0]] == "error"
        assert final["source_errors"] == before["source_errors"] == 7
        assert final["source_approved"] == before["source_approved"] > 0
        assert final["source_approved_bytes"] == before["source_approved_bytes"]
        assert final["published"] == before["published"] > 0
        assert final["published_bytes"] == before["published_bytes"]
        assert final["earlier_reviews"] == before["earlier_reviews"]
        persisted_logs = [
            row for row in final["diagnostic_logs"]
            if row["runtime_job_id"] == retry_id
        ]
        assert bool(persisted_logs) == (blocked and not error_interruption)
        for row in persisted_logs:
            logged = decoded(row["payload"])
            for key, value in diagnostic.items():
                assert logged[key] == value, (key, logged)
        retry_rows = [
            dict(row) for row in final["retry_rows"]
            if row["scrape_job_id"] == retry_id
        ]
        assert len(retry_rows) == (1 if resume else 0), retry_rows
        # Imported counts review rows, not completed URL acknowledgements.
        # A filtered checkpoint is settled work but has no row to import.
        expected_imported = len(retry_rows)
        assert retry["imported"] == expected_imported, retry
        assert final["child_rows_bytes"] == before["child_rows_bytes"]
        if resume or interruption or error_interruption:
            completed = config["autonomousVerification"]["completed_urls"]
            if resume:
                assert PRIOR[1] in completed
            if blocked and not error_interruption:
                assert set(SELECTED) <= set(completed)
        assert worker.poll() is None, "Prefork worker exited before lifecycle completion"

        # Force a fresh API process and prove both projections reload persisted state.
        stop(api)
        api = start("api-restarted", [
            sys.executable, "-m", "tests.task580_process", "api",
        ])
        wait_http(f"http://127.0.0.1:{apiport}/api/health")
        with httpx.Client(base_url=base, timeout=40) as reloaded:
            login(reloaded)
            status_response = reloaded.get(f"/api/scrape/status/{retry_id}")
            history_response = reloaded.get(f"/api/scrape/history/{retry_id}")
            assert status_response.status_code == 200, status_response.text
            assert history_response.status_code == 200, history_response.text
            status_json = status_response.json()
            history_json = history_response.json()
        assert status_json["status"] == retry["status"]
        assert status_json["errors"] == retry["errors"]
        assert status_json["imported"] == expected_imported, status_json
        assert status_json.get("targetedRetryDiagnostic") == diagnostic
        assert status_json.get("errorMessage") == retry["error_message"]
        if blocked and not error_interruption:
            assert status_json["extractionQuality"]["errorCount"] > 0
        assert history_json["job"]["status"] == retry["status"]
        assert history_json["job"]["errors"] == retry["errors"]
        assert history_json["job"]["imported"] == expected_imported, history_json["job"]
        diagnostic_history = [
            row for row in history_json["logs"]
            if row.get("kind") == "targeted_retry_all_filtered"
        ]
        assert bool(diagnostic_history) == (blocked and not error_interruption)
        for logged in diagnostic_history:
            for key, value in diagnostic.items():
                assert logged[key] == value, (key, logged)
        assert len(history_json["stagedCourses"]) == (1 if resume else 0)
        if resume:
            assert history_json["stagedCourses"][0]["id"] == retry_rows[0]["id"]
        reloaded_snapshot = snapshot(dburl)
        assert reloaded_snapshot["earlier_reviews"] == before["earlier_reviews"]
        assert reloaded_snapshot["published_bytes"] == before["published_bytes"]
        assert reloaded_snapshot["child_rows_bytes"] == before["child_rows_bytes"]

        redelivery_evidence = None
        if (
            continuation
            and resume != "resolved"
            and not interruption
            and not error_interruption
        ):
            # Simulate at-least-once redelivery after the first task has fully
            # returned. This is the exact same durable child, not a newly
            # generated continuation. Its current-run exclusion checkpoints
            # are therefore pre-run checkpoints on the second delivery.
            first_delivery = {
                "status": retry["status"],
                "errors": retry["errors"],
                "error_message": retry["error_message"],
                "diagnostic": diagnostic,
                "diagnostic_logs": persisted_logs,
                "completed_urls": config["autonomousVerification"]["completed_urls"],
                "postrun": postrun_marker,
                "status_api": status_json,
                "history_diagnostics": diagnostic_history,
            }
            marker_path.unlink()
            requeue = start("requeue", [
                sys.executable, "-m", "tests.task580_process",
                "requeue-continuation",
            ])
            assert requeue.wait(timeout=30) == 0, "Requeue failed; see requeue.log"
            pre_redelivery = snapshot(dburl)
            redelivered_rows = [
                row for row in pre_redelivery["jobs"]
                if row["runtime_job_id"] == retry_id
            ]
            assert len(redelivered_rows) == 1
            redelivered_child = redelivered_rows[0]
            assert redelivered_child["status"] == "queued"
            assert decoded(redelivered_child["request_payload"]) == payload
            assert (
                decoded(redelivered_child["discovered_config"])
                ["targeted_retry_diagnostic"]
                == diagnostic
            )
            pre_redelivery_checkpoint_urls = (
                decoded(redelivered_child["discovered_config"])
                ["autonomousVerification"]["completed_urls"]
            )
            assert set(selected) <= set(pre_redelivery_checkpoint_urls)

            dispatch = start("dispatch-redelivery", [
                sys.executable, "-m", "tests.task580_process",
                "dispatch-continuation",
            ])
            assert dispatch.wait(timeout=30) == 0, (
                "Redelivery dispatch failed; see dispatch-redelivery.log"
            )
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                redelivered = snapshot(dburl)
                redelivered_child = next(
                    row for row in redelivered["jobs"]
                    if row["runtime_job_id"] == retry_id
                )
                if redelivered_child["status"] in TERMINAL:
                    break
                time.sleep(.25)
            else:
                raise AssertionError(
                    "Redelivered real prefork task did not reach terminal state"
                )

            marker_deadline = time.monotonic() + 120
            while time.monotonic() < marker_deadline:
                if marker_path.exists():
                    redelivery_postrun = json.loads(marker_path.read_text())
                    break
                if worker.poll() is not None:
                    raise AssertionError(
                        "Prefork worker exited before redelivery task_postrun"
                    )
                time.sleep(.1)
            else:
                raise AssertionError(
                    "Redelivery Celery task_postrun marker was not persisted"
                )
            assert redelivery_postrun["task_name"] == "scrape.university"
            assert redelivery_postrun["runtime_job_id"] == retry_id
            assert redelivery_postrun["task_id"]
            assert redelivery_postrun["task_id"] != postrun_marker["task_id"]
            assert redelivery_postrun["state"] == "SUCCESS", redelivery_postrun
            assert redelivery_postrun["return_value"]["ok"] is True
            assert redelivery_postrun["return_value"]["id"] == retry_id

            final = snapshot(dburl)
            matching_jobs = [
                row for row in final["jobs"]
                if row["runtime_job_id"] == retry_id
            ]
            assert len(matching_jobs) == 1
            retry = matching_jobs[0]
            payload = decoded(retry["request_payload"])
            config = decoded(retry["discovered_config"])
            diagnostic = config.get("targeted_retry_diagnostic")
            persisted_logs = [
                row for row in final["diagnostic_logs"]
                if row["runtime_job_id"] == retry_id
            ]
            assert retry["status"] == first_delivery["status"] == "failed"
            assert retry["errors"] > 0
            assert retry["error_message"] == first_delivery["error_message"]
            assert diagnostic == first_delivery["diagnostic"]
            assert payload["courseUrls"] == selected
            assert payload["course_urls"] == selected
            assert payload["retrySourceJobId"] == REPORT_PARENT
            assert set(selected) <= set(
                config["autonomousVerification"]["completed_urls"]
            )
            assert len(persisted_logs) > len(first_delivery["diagnostic_logs"])
            for old_log in first_delivery["diagnostic_logs"]:
                assert old_log in persisted_logs
            for row in persisted_logs:
                logged = decoded(row["payload"])
                for key, value in diagnostic.items():
                    assert logged[key] == value, (key, logged)
            assert final["source_errors"] == before["source_errors"]
            assert final["source_approved_bytes"] == before["source_approved_bytes"]
            assert final["published_bytes"] == before["published_bytes"]
            assert final["earlier_reviews"] == before["earlier_reviews"]
            assert final["child_rows_bytes"] == before["child_rows_bytes"]

            # A second fresh API process must project the post-redelivery row
            # and both durable diagnostics without relying on process memory.
            stop(api)
            api = start("api-redelivery-restarted", [
                sys.executable, "-m", "tests.task580_process", "api",
            ])
            wait_http(f"http://127.0.0.1:{apiport}/api/health")
            with httpx.Client(base_url=base, timeout=40) as reloaded:
                login(reloaded)
                status_response = reloaded.get(f"/api/scrape/status/{retry_id}")
                history_response = reloaded.get(f"/api/scrape/history/{retry_id}")
                assert status_response.status_code == 200, status_response.text
                assert history_response.status_code == 200, history_response.text
                status_json = status_response.json()
                history_json = history_response.json()
            diagnostic_history = [
                row for row in history_json["logs"]
                if row.get("kind") == "targeted_retry_all_filtered"
            ]
            assert status_json["status"] == "failed"
            assert status_json["errors"] == retry["errors"]
            assert status_json["targetedRetryDiagnostic"] == diagnostic
            assert status_json["errorMessage"] == retry["error_message"]
            assert len(diagnostic_history) > len(
                first_delivery["history_diagnostics"]
            )
            for logged in diagnostic_history:
                for key, value in diagnostic.items():
                    assert logged[key] == value, (key, logged)
            assert len(history_json["stagedCourses"]) == (1 if resume else 0)
            reloaded_snapshot = snapshot(dburl)
            assert reloaded_snapshot["earlier_reviews"] == before["earlier_reviews"]
            assert reloaded_snapshot["published_bytes"] == before["published_bytes"]
            assert reloaded_snapshot["child_rows_bytes"] == before["child_rows_bytes"]
            postrun_marker = redelivery_postrun
            redelivery_evidence = {
                "same_runtime_job_id": retry_id,
                "job_row_count": len(matching_jobs),
                "pre_redelivery_checkpoint_urls": pre_redelivery_checkpoint_urls,
                "first_delivery": first_delivery,
                "second_delivery_postrun": redelivery_postrun,
                "second_delivery_status": retry["status"],
                "second_delivery_diagnostic": diagnostic,
                "diagnostic_log_count_before_after": [
                    len(first_delivery["diagnostic_logs"]),
                    len(persisted_logs),
                ],
                "fresh_api_status": status_json,
                "fresh_api_history_diagnostic_count": len(diagnostic_history),
            }

        evidence.update({
            "passed": True,
            "retry_job_id": retry_id,
            "celery_worker_process_pid": worker.pid,
            "celery_task_postrun": postrun_marker,
            "terminal_status": retry["status"],
            "errors": retry["errors"],
            "selected_urls": selected,
            "persisted_request_payload": payload,
            "persisted_diagnostic": diagnostic,
            "diagnostic_log": persisted_logs[-1] if persisted_logs else None,
            "fresh_api_status": status_json,
            "fresh_api_history_diagnostic": diagnostic_history[-1] if diagnostic_history else None,
            "same_child_rows_byte_identical": True,
            "prior_checkpoint_urls": [PRIOR[1]] if resume else [],
            "post_task_checkpoint_urls": config.get("autonomousVerification", {}).get("completed_urls", []),
            "source_errors_before_after": [
                before["source_errors"], final["source_errors"],
            ],
            "approved_review_rows_before_after": [
                before["source_approved"], final["source_approved"],
            ],
            "published_approved_before_after": [
                before["published"], final["published"],
            ],
            "retry_staged_rows": len(retry_rows),
            "imported_counts": {
                "expected_review_rows": expected_imported,
                "persisted_worker": retry["imported"],
                "fresh_status": status_json["imported"],
                "fresh_history": history_json["job"]["imported"],
            },
            "approved_review_rows_byte_identical": True,
            "published_records_byte_identical": True,
            "earlier_reviews_before": before["earlier_reviews"],
            "earlier_reviews_after_reload": reloaded_snapshot["earlier_reviews"],
            "both_earlier_review_sets_byte_identical": True,
            "delivery_count": 2 if redelivery_evidence or interruption or error_interruption else 1,
            "redelivery": redelivery_evidence,
        })
    except Exception:
        evidence["error"] = traceback.format_exc()
        raise
    finally:
        for _, child in reversed(processes):
            stop(child)
        if pg_started:
            subprocess.run(
                [
                    "pg_ctl", "-D", str(temp / "pg"), "-m", "immediate",
                    "-w", "stop",
                ],
                check=True,
                capture_output=True,
            )
        for log in logs:
            log.close()
        recipes_after = recipe_snapshot()
        recipe_changes = sorted(
            name for name in recipes_before.keys() | recipes_after.keys()
            if recipes_before.get(name) != recipes_after.get(name)
        )
        forbidden = recipe_root / "forbidden-writes.txt"
        forbidden_writes = (
            forbidden.read_text().splitlines() if forbidden.exists() else []
        )
        shutil.rmtree(temp)
        closed = {}
        for name, number in (
            ("postgres", pgport), ("redis", redisport), ("api", apiport),
        ):
            with socket.socket() as sock:
                closed[name] = sock.connect_ex(("127.0.0.1", number)) != 0
        evidence["cleanup"] = {
            "closed_ports": closed,
            "private_directory_removed": not temp.exists(),
            "processes_exited": {
                name: child.poll() is not None for name, child in processes
            },
            "production_recipe_changes": recipe_changes,
            "forbidden_production_recipe_write_attempts": forbidden_writes,
        }
        if (
            recipe_changes
            or forbidden_writes
            or not all(closed.values())
            or temp.exists()
        ):
            evidence["passed"] = False
        (output / "evidence.json").write_text(
            json.dumps(evidence, indent=2, default=str)
        )
        assert all(closed.values()), f"Cleanup left listening sockets: {closed}"
        assert not recipe_changes, recipe_changes
        assert not forbidden_writes, forbidden_writes
        assert not temp.exists()
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--interruption-only", action="store_true",
        help="Run only the task-587 first-exclusion checkpoint interruption scenario",
    )
    parser.add_argument(
        "--error-interruption-only", action="store_true",
        help="Run only the task-591 exhausted-error checkpoint interruption scenario",
    )
    args = parser.parse_args()
    assert not (args.interruption_only and args.error_interruption_only)
    results = []
    if not args.interruption_only and not args.error_interruption_only:
        results.append(run(args.output.resolve()))
        results.append(run(args.output.resolve() / "continuation", continuation=True))
        results.extend(
            run(args.output.resolve() / scenario, continuation=True, resume=scenario)
            for scenario in ("mixed", "resolved")
        )
    if not args.error_interruption_only:
        results.append(run(
            args.output.resolve() if args.interruption_only else args.output.resolve() / "interruption",
            continuation=True, interruption=True,
        ))
    if not args.interruption_only:
        results.append(run(
            args.output.resolve() if args.error_interruption_only else args.output.resolve() / "error-interruption",
            continuation=True, error_interruption=True,
        ))
    print(json.dumps({
        "passed": all(item["passed"] for item in results),
        "evidence": str(args.output),
    }, indent=2))
