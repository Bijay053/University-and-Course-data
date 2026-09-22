"""Disposable real-worker acceptance for reviewed course-report continuation.

Run from the repository root:
  python backend-py/tests/task568_acceptance.py --output /tmp/task568-evidence

The harness creates private PostgreSQL and Redis instances and never reads the
application's configured DATABASE_URL. It uses the production API, prefork
Celery worker, extraction pipeline, staging, and workflow monitor.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from urllib.request import urlopen

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.task564_acceptance import port, recipe_snapshot

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend-py"
SOURCE = "task564-source"
HOST = "task564.example.test"
URLS = [f"https://{HOST}/courses/degree-{n}" for n in range(60)]
TERMINAL = {"completed", "completed_with_errors", "failed", "failed_degraded", "stopped"}


async def database_snapshot(url: str) -> dict:
    import asyncpg
    db = await asyncpg.connect(url.replace("postgresql+asyncpg:", "postgresql:"))
    try:
        jobs = await db.fetch("""
            SELECT runtime_job_id, status, request_payload, discovered_config,
                   imported, current, total_found, errors, completed_at
            FROM scrape_runtime_jobs ORDER BY created_at, runtime_job_id
        """)
        rows = await db.fetch("""
            SELECT id, scrape_job_id, course_website, course_name, status
            FROM scraped_courses ORDER BY id
        """)
        audits = await db.fetch("SELECT session_id, status, evidence FROM ai_repair_audits")
        claims = await db.fetch("""
            SELECT claim_key, generation, state, task_id, process_identity
            FROM autonomous_worker_claims ORDER BY claimed_at
        """)
        return {
            "jobs": [dict(row) for row in jobs],
            "rows": [dict(row) for row in rows],
            "audits": [dict(row) for row in audits],
            "claims": [dict(row) for row in claims],
            "published": await db.fetchval("SELECT count(*) FROM courses"),
        }
    finally:
        await db.close()


def snapshot(url: str) -> dict:
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(database_snapshot(url))).result(timeout=30)


def decoded(value):
    return json.loads(value) if isinstance(value, str) else (value or {})


def job(data, job_id):
    return next(item for item in data["jobs"] if item["runtime_job_id"] == job_id)


def terminal_job(state, job_id):
    candidate = next((item for item in state["jobs"]
                      if item["runtime_job_id"] == job_id), None)
    return candidate if candidate and candidate["status"] in TERMINAL else None


def wait_for(dburl, predicate, timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = snapshot(dburl)
        value = predicate(state)
        if value:
            return state, value
        time.sleep(.25)
    raise AssertionError("Timed out waiting for disposable acceptance state")


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    for binary in ("initdb", "pg_ctl", "redis-server"):
        if not shutil.which(binary):
            raise RuntimeError(f"Required binary missing: {binary}")
    evidence = {"task": 568, "passed": False}
    processes, logs = [], []
    fixture = None
    pg_started = False
    temp = Path(tempfile.mkdtemp(prefix="task568-private-"))
    recipe_root = temp / "recipes"
    recipes_before = recipe_snapshot()
    pgport, redisport, apiport, httpport = [port() for _ in range(4)]
    dburl = f"postgresql+asyncpg://task564@127.0.0.1:{pgport}/postgres"
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "NIX_LD_LIBRARY_PATH", "SSL_CERT_FILE",
    ) if key in os.environ}
    env.update({
        "DATABASE_URL": dburl, "DATABASE_REQUIRE_TLS": "false",
        "REDIS_URL": f"redis://127.0.0.1:{redisport}/0",
        "SESSION_SECRET": "task568-disposable-session-secret",
        "TASK564_ISOLATED": "yes", "TASK564_HTTP_PORT": str(httpport),
        "TASK564_API_PORT": str(apiport), "TASK564_RECIPE_ROOT": str(recipe_root),
        "PYTHONPATH": str(BACKEND), "GEMINI_API_KEY": "", "OPENAI_API_KEY": "",
        "SCRAPE_DO_TOKEN": "", "AI_INTEGRATIONS_OPENAI_API_KEY": "",
        "NODE_ENV": "test",
    })

    def start(name, command):
        log = (output / f"{name}.log").open("w")
        logs.append(log)
        child = subprocess.Popen(command, cwd=BACKEND, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((name, child))
        return child

    def stop(process):
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)

    def wait_http(url):
        for _ in range(240):
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status < 500:
                        return
            except Exception:
                time.sleep(.25)
        raise AssertionError(f"Server did not become ready: {url}")

    counts = Counter()
    counts_lock = threading.Lock()

    class OfficialFixture(BaseHTTPRequestHandler):
        def do_GET(self):
            with counts_lock:
                counts[self.path] += 1
                visit = counts[self.path]
            number = int(self.path.rsplit("-", 1)[-1]) if "/degree-" in self.path else -1
            # Visit one is API validation. During extraction, five URLs settle
            # quickly and every other request blocks beyond the 2-second budget.
            if number == 5 and visit == 2:
                time.sleep(8)
            name = f"Bachelor of Computer Science {number}"
            html = f"""<!doctype html><html><head><title>{name}</title></head>
            <body><main><h1>{name}</h1><p>International students may apply.</p>
            <p>Bachelor degree, full-time on campus for 2 years.</p>
            <p>Location: Sydney campus. International tuition fee: AUD $35,000 per year.</p>
            <p>Intakes: February and July 2027. IELTS Academic overall 6.5 with no band below 6.0.</p>
            </main></body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            try:
                self.wfile.write(html)
            except BrokenPipeError:
                pass

        def log_message(self, *_):
            pass

    try:
        subprocess.run(["initdb", "-D", str(temp / "pg"), "-A", "trust", "-U", "task564"],
                       check=True, capture_output=True)
        subprocess.run(["pg_ctl", "-D", str(temp / "pg"), "-l", str(output / "postgres.log"),
                        "-o", f"-h 127.0.0.1 -p {pgport} -k {temp}", "-w", "start"],
                       check=True, capture_output=True)
        pg_started = True
        fixture = ThreadingHTTPServer(("127.0.0.1", httpport), OfficialFixture)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        start("redis", ["redis-server", "--bind", "127.0.0.1", "--port", str(redisport),
                        "--save", "", "--appendonly", "no", "--dir", str(temp)])
        seed = start("seed", [sys.executable, "-m", "tests.task564_process", "seed"])
        assert seed.wait(timeout=90) == 0
        before = snapshot(dburl)
        source_rows_before = [row for row in before["rows"] if row["scrape_job_id"] == SOURCE]
        start("api", [sys.executable, "-m", "tests.task564_process", "api"])
        wait_http(f"http://127.0.0.1:{apiport}/api/health")
        base = f"http://127.0.0.1:{apiport}"
        with httpx.Client(base_url=base, timeout=40) as client:
            login = client.post("/api/auth/login", json={
                "email": "task564@example.test", "password": "isolated-task564-password"})
            assert login.status_code == 200, login.text
            posted = client.post(f"/api/scrape/jobs/{SOURCE}/course-reports", json={
                "kind": "missing", "course_urls": URLS,
                "description": "Task568 continuation acceptance",
            })
            assert posted.status_code == 202, posted.text
            first_id = posted.json()["job_id"]
            env["TASK568_REPORT_JOB_ID"] = first_id
            env["TASK568_TIME_BUDGET_SECONDS"] = "2"
            budget = start("budget", [sys.executable, "-m", "tests.task564_process",
                                      "set-report-budget"])
            assert budget.wait(timeout=30) == 0
            first_worker = start(
                "worker-first", [sys.executable, "-m", "tests.task564_process", "worker"])

            first_state, first = wait_for(
                dburl, lambda state: terminal_job(state, first_id), timeout=180)
            first_meta = decoded(first["discovered_config"]).get("autonomousVerification", {})
            assert first["status"] == "failed_degraded", first
            assert first_meta["budget_exhausted"] == "time_budget_exhausted"
            completed = first_meta.get("completed_urls", [])
            assert 0 < len(completed) < 50, first_meta

            # Wait for the real monitor to expose an acknowledged review state.
            def reviewable(state):
                for audit in state["audits"]:
                    data = decoded(audit["evidence"])
                    auto = data.get("autonomous", {})
                    if auto.get("verification_job_id") == first_id and auto.get("phase") == "needs_review":
                        return data
                return None
            reviewed_state, audit = wait_for(dburl, reviewable, timeout=120)
            first_claim = next(
                claim for claim in reviewed_state["claims"]
                if claim["claim_key"] == f"verification:{first_id}"
            )
            first_identity = first_claim["process_identity"]
            first_pid = int(first_identity.rsplit(":", 2)[-2])
            stop(first_worker)
            second_worker = start(
                "worker-second", [sys.executable, "-m", "tests.task564_process", "worker"])
            listing = client.get(f"/api/scrape/jobs/{SOURCE}/course-reports")
            assert listing.status_code == 200, listing.text
            report = listing.json()["reports"][0]
            exact_remaining = report["continuation"]["remaining_urls"]
            assert exact_remaining == [url for url in URLS if url not in set(completed)]
            assert report["continuation"]["remaining_count"] > 10

            # Separate clients issue simultaneous reviewed submissions.
            endpoint = f"{base}/api/scrape/jobs/{SOURCE}/course-reports/{first_id}/continue"
            cookies = dict(client.cookies)
            barrier = threading.Barrier(2)
            def submit():
                with httpx.Client(cookies=cookies, timeout=40) as contender:
                    barrier.wait()
                    response = contender.post(endpoint, json={"reviewed": True})
                    return response.status_code, response.text
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: submit(), range(2)))
            assert sorted(code for code, _ in outcomes) == [202, 409], outcomes
            success = json.loads(next(text for code, text in outcomes if code == 202))
            second_id = success["job_id"]
            stale = client.post(endpoint, json={"reviewed": True})
            assert stale.status_code == 409, stale.text

            after_race = snapshot(dburl)
            report_jobs = [
                item for item in after_race["jobs"]
                if decoded(item["request_payload"]).get("courseReport", {}).get("id")
                == report["report_id"]
            ]
            assert [item["runtime_job_id"] for item in report_jobs] == [first_id, second_id]
            second_payload = decoded(job(after_race, second_id)["request_payload"])
            assert second_payload["retrySourceJobId"] == first_id
            assert second_payload["courseReportRemainingUrls"] == exact_remaining
            assert second_payload["course_urls"] == exact_remaining[:50]
            assert not set(second_payload["course_urls"]) & set(completed)
            second_policy = second_payload["autonomousVerification"]
            assert second_policy["parent_job_id"] == SOURCE
            assert second_policy["session_id"] == report["report_id"]
            assert second_policy["max_courses"] == 50
            assert second_policy["time_budget_seconds"] == 600
            assert second_policy["cost_cap_usd"] == 2
            assert second_policy["round_index"] == 1

            final, second = wait_for(
                dburl, lambda state: terminal_job(state, second_id), timeout=240)
            assert second["status"] == "completed", second
            second_claim = next(
                claim for claim in final["claims"]
                if claim["claim_key"] == f"verification:{second_id}"
            )
            second_identity = second_claim["process_identity"]
            second_pid = int(second_identity.rsplit(":", 2)[-2])
            assert second_identity != first_identity and second_pid != first_pid, (
                first_identity, second_identity)
            final_listing = client.get(f"/api/scrape/jobs/{SOURCE}/course-reports")
            assert final_listing.status_code == 200
            final_report = final_listing.json()["reports"][0]
            source_rows_after = [row for row in final["rows"] if row["scrape_job_id"] == SOURCE]
            first_rows_before = [row for row in reviewed_state["rows"]
                                 if row["scrape_job_id"] == first_id]
            first_rows_after = [row for row in final["rows"] if row["scrape_job_id"] == first_id]
            assert source_rows_after == source_rows_before
            assert first_rows_after == first_rows_before
            assert first_rows_after, "Timed-out child produced no preserved review rows"
            assert final["published"] == 0
            # Settled first-child URLs must not be extracted by the continuation.
            second_rows = [row for row in final["rows"] if row["scrape_job_id"] == second_id]
            assert len(second_rows) == 50, second_rows
            assert {row["course_website"] for row in second_rows} == set(exact_remaining[:50])
            assert not ({row["course_website"] for row in first_rows_after}
                        & {row["course_website"] for row in second_rows})
            assert all(counts[url.replace(f"https://{HOST}", "")] == 2 for url in completed)
            assert final_report["children"][0]["job_id"] == first_id
            assert final_report["children"][1]["job_id"] == second_id
            assert final_report["continuation"]["remaining_urls"] == URLS[55:]
            evidence.update({
                "passed": True, "first_job_id": first_id, "second_job_id": second_id,
                "submitted_urls": len(URLS), "first_completed_urls": completed,
                "exact_remaining_after_reload": exact_remaining,
                "race_statuses": sorted(code for code, _ in outcomes),
                "stale_status": stale.status_code,
                "first_status": first["status"], "second_status": second["status"],
                "first_worker_pid": first_pid, "second_worker_pid": second_pid,
                "first_process_identity": first_identity,
                "second_process_identity": second_identity,
                "distinct_worker_processes": True,
                "first_budget_seconds": 2,
                "second_budget_seconds": second_payload["autonomousVerification"]["time_budget_seconds"],
                "source_rows_preserved": True, "prior_child_rows_preserved": True,
                "duplicate_settled_extractions": 0,
                "final_remaining_urls": final_report["continuation"]["remaining_urls"],
                "fixture_requests": dict(counts),
            })
    except Exception:
        evidence["error"] = traceback.format_exc()
        raise
    finally:
        if fixture:
            fixture.shutdown()
            fixture.server_close()
        for _, process in reversed(processes):
            stop(process)
        if pg_started:
            subprocess.run(["pg_ctl", "-D", str(temp / "pg"), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        for log in logs:
            log.close()
        recipes_after = recipe_snapshot()
        recipe_changes = sorted(name for name in recipes_before.keys() | recipes_after.keys()
                                if recipes_before.get(name) != recipes_after.get(name))
        shutil.rmtree(temp)
        evidence["cleanup"] = {
            "private_directory_removed": not temp.exists(),
            "production_recipe_changes": recipe_changes,
            "processes_exited": {name: child.poll() is not None for name, child in processes},
        }
        if recipe_changes or temp.exists():
            evidence["passed"] = False
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str))
        assert not recipe_changes
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.output.resolve())
    print(json.dumps({"passed": result["passed"], "evidence": str(args.output)}, indent=2))