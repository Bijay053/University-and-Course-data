"""Opt-in, destructive ONLY to its own newly initialized local cluster.

Run from the repository root:
  python backend-py/tests/task564_acceptance.py --output /tmp/task564-evidence

Requires initdb, pg_ctl, redis-server, pnpm, backend dependencies and Playwright
Chromium. Never reads the application's configured DATABASE_URL.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import os
import re
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

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend-py"
SOURCE = "task564-source"
HOST = "task564.example.test"
CAP_SOURCE = "task564-cap-source"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def recipe_snapshot():
    root = BACKEND / "scraper_config"
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob("*")) if path.is_file()}


async def snapshot(url):
    import asyncpg
    db = await asyncpg.connect(url.replace("postgresql+asyncpg:", "postgresql:"))
    try:
        source = await db.fetchval("""
            SELECT coalesce(json_agg(row_to_json(s) ORDER BY id)::text, '[]')
            FROM scraped_courses s WHERE scrape_job_id = ANY($1::text[])
        """, [SOURCE, CAP_SOURCE])
        jobs = await db.fetch("""
            SELECT runtime_job_id, status, request_payload, discovered_config,
                   imported, current, total_found, errors, error_message,
                   total_gemini_cost_usd, worker_pid,
                   (SELECT count(*) FROM scraped_courses s
                    WHERE s.scrape_job_id = scrape_runtime_jobs.runtime_job_id) AS review_rows
            FROM scrape_runtime_jobs WHERE runtime_job_id <> ALL($1::text[])
        """, [SOURCE, CAP_SOURCE])
        claims = await db.fetch("SELECT * FROM autonomous_worker_claims")
        audits = await db.fetch("SELECT session_id, status, evidence FROM ai_repair_audits")
        rows = await db.fetch("""
            SELECT id, scrape_job_id, course_name, course_website, status
            FROM scraped_courses ORDER BY id
        """)
        return {
            "source_bytes": source,
            "jobs": [dict(row) for row in jobs],
            "claims": [dict(row) for row in claims],
            "audits": [dict(row) for row in audits],
            "review_rows": [dict(row) for row in rows],
            "published_courses": await db.fetchval("SELECT count(*) FROM courses"),
            "published_review_rows": await db.fetchval(
                "SELECT count(*) FROM scraped_courses WHERE status IN ('approved','published')"),
            "new_pending": await db.fetchval(
                "SELECT count(*) FROM scraped_courses WHERE scrape_job_id <> ALL($1::text[]) AND status = 'pending'",
                [SOURCE, CAP_SOURCE]),
        }
    finally:
        await db.close()


def snapshot_sync(url):
    # Sync Playwright owns an event loop in the calling thread. Keep asyncpg's
    # event loop and connection entirely in a short-lived independent thread.
    with ThreadPoolExecutor(max_workers=1) as executor:
        return executor.submit(lambda: asyncio.run(snapshot(url))).result(timeout=30)


def assert_review_identity(page, response, database, job_id, source_id, expect):
    """Prove exact child navigation and its explicit source+child review union."""
    payload = response.json()
    assert payload["lastScrape"]["jobId"] == job_id, "Review summary silently switched jobs"
    api_rows = payload["courses"]
    job = next(row for row in database["jobs"] if row["runtime_job_id"] == job_id)
    assert json.loads(job["request_payload"])["retrySourceJobId"] == source_id
    # Production deliberately includes the explicit continuation source. This
    # is an exact two-job set, NOT permission to include all university rows.
    expected = [row for row in database["review_rows"]
                if row["scrape_job_id"] in {job_id, source_id} and row["status"] == "pending"]
    child_rows = [row for row in expected if row["scrape_job_id"] == job_id]
    assert child_rows, "No pending rows for exact child"
    assert {row["id"] for row in api_rows if row["status"] == "pending"} == {
        row["id"] for row in expected
    }, "Review response differs from exact child plus declared-source DB identities"
    assert {row["id"]: row["scrapeJobId"] for row in api_rows} == {
        row["id"]: row["scrape_job_id"] for row in expected
    }, "Review row lineage identity differs from DB"
    for row in expected:
        visible_row = page.locator("tr").filter(has=page.locator(
            f'a[title="Verify: {row["course_website"]}"]'))
        expect(visible_row).to_have_count(1)
        expect(visible_row).to_be_visible()
        expect(visible_row).to_contain_text(row["course_name"])
    first = child_rows[0]
    visible_row = page.locator("tr").filter(has=page.locator(
        f'a[title="Verify: {first["course_website"]}"]'))
    with page.expect_response(lambda r: r.url.split("?")[0].endswith(
        f'/api/scrape/staged/{first["id"]}/review')) as details:
        visible_row.get_by_title("Review evidence", exact=True).click()
    assert details.value.status == 200, details.value.text()
    expect(page.get_by_role("dialog")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("dialog")).not_to_be_visible()
    return {"rendered_row_ids": [row["id"] for row in expected],
            "review_job_id": job_id, "explicit_source_job_id": source_id,
            "child_row_ids": [row["id"] for row in child_rows],
            "opened_evidence_row_id": first["id"]}


def run(output):
    output.mkdir(parents=True, exist_ok=True)
    for binary in ("initdb", "pg_ctl", "redis-server", "pnpm"):
        if not shutil.which(binary):
            raise RuntimeError(f"Required binary missing: {binary}")
    processes, logs = [], []
    pg_started = False
    fixture = None
    release = threading.Event()
    evidence = {"passed": False, "disclosure": (
        "Reserved task564.example.test is a synthetic official-source fixture. "
        "Only its DNS-safety decision and httpx transport destination are substituted. "
        "HTML is served by a real local HTTP server; no task dispatch, extraction, "
        "staging, auth, report router, or browser API response is mocked. "
        "This is not proof of any live university catalogue."
    )}
    temp = Path(tempfile.mkdtemp(prefix="task564-private-"))
    recipe_root = temp / "recipes"
    recipes_before = recipe_snapshot()
    pgport, redisport, apiport, webport, httpport = [port() for _ in range(5)]
    dburl = f"postgresql+asyncpg://task564@127.0.0.1:{pgport}/postgres"
    # Allowlist, not a copy of the ambient environment: no production credentials.
    env = {key: os.environ[key] for key in (
        "PATH", "HOME", "LANG", "LD_LIBRARY_PATH", "NIX_LD_LIBRARY_PATH",
        "PLAYWRIGHT_BROWSERS_PATH", "SSL_CERT_FILE",
    ) if key in os.environ}
    env.update({
        "DATABASE_URL": dburl, "DATABASE_REQUIRE_TLS": "false",
        "REDIS_URL": f"redis://127.0.0.1:{redisport}/0",
        "SESSION_SECRET": "task564-disposable-session-secret",
        "TASK564_ISOLATED": "yes", "TASK564_HTTP_PORT": str(httpport),
        "TASK564_API_PORT": str(apiport), "PYTHONPATH": str(BACKEND),
        "TASK564_RECIPE_ROOT": str(recipe_root),
        "API_PORT": str(apiport), "WEB_PORT": str(webport), "PORT": str(webport),
        "API_PROXY_TARGET": f"http://127.0.0.1:{apiport}", "BASE_PATH": "/",
        "GEMINI_API_KEY": "", "OPENAI_API_KEY": "", "SCRAPE_DO_TOKEN": "",
        "AI_INTEGRATIONS_OPENAI_API_KEY": "", "NODE_ENV": "test",
    })

    def start(name, command, cwd=BACKEND):
        log = (output / f"{name}.log").open("w")
        logs.append(log)
        child = subprocess.Popen(command, cwd=cwd, env=env, stdout=log,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        processes.append((name, child))
        return child

    def wait_http(url):
        for _ in range(240):
            try:
                with urlopen(url, timeout=1) as response:
                    if response.status < 500:
                        return
            except Exception:
                time.sleep(.25)
        raise AssertionError(f"Isolated server failed to become ready: {url}")

    counts = Counter()

    class OfficialFixture(BaseHTTPRequestHandler):
        def do_GET(self):
            counts[self.path] += 1
            if "/courses/" in self.path and counts[self.path] > 1:
                if not release.wait(120):
                    self.send_error(504, "Harness gate not released")
                    return
            bounded = self.path == "/bounded-catalogue"
            is_cap_course = self.path.startswith("/courses/degree-")
            name = ("Bachelor of Computer Science " + self.path.rsplit("-", 1)[-1]
                    if is_cap_course else
                    "Undergraduate degree courses" if bounded else
                    "Bachelor of Computer Science" if "computing" in self.path else "Master of Business")
            links = (
                "".join(f'<a href="https://{HOST}/courses/degree-{n}">'
                        f'Bachelor of Computer Science {n}</a>' for n in range(55))
                if bounded else ""
                if is_cap_course else
                f'<a href="https://{HOST}/courses/computing">Bachelor of Computer Science</a>'
                f'<a href="https://{HOST}/courses/business">Master of Business</a>'
            )
            html = f"""<!doctype html><html><head><title>{name}</title></head>
            <body><main><h1>{name}</h1>
            <p>International students: Applications open.</p>
            <p>Qualification: {'Bachelor degree' if 'Bachelor' in name else 'Master degree'}</p>
            <p>Duration: 2 years full-time on campus</p><p>Location: Sydney campus</p>
            <p>International tuition fee: AUD $35,000 per year</p>
            <p>Intakes: February and July 2027</p>
            <p>IELTS Academic overall 6.5 with no band below 6.0.</p>
            <p>This accredited degree prepares international students for professional
            careers through coursework, lectures and practical experience.</p>
            {links}
            </main></body></html>""".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def log_message(self, *args):
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
        assert seed.wait(timeout=90) == 0, "Schema/seed failed; see seed.log"
        before = snapshot_sync(dburl)
        start("worker", [sys.executable, "-m", "tests.task564_process", "worker"])
        start("api", [sys.executable, "-m", "tests.task564_process", "api"])
        start("web", ["pnpm", "exec", "vite", "--host", "127.0.0.1", "--strictPort"],
              ROOT / "artifacts/university-portal")
        wait_http(f"http://127.0.0.1:{apiport}/api/health")
        base = f"http://127.0.0.1:{webport}"
        wait_http(base)
        from playwright.sync_api import sync_playwright, expect
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(viewport={"width": 1440, "height": 1100})
            context.tracing.start(screenshots=True, snapshots=True, sources=True)
            page = context.new_page()
            try:
                page.goto(base + "/login")
                page.get_by_label("Email", exact=True).fill("task564@example.test")
                page.get_by_label("Password", exact=True).fill("isolated-task564-password")
                page.get_by_role("button", name="Sign in", exact=True).click()
                page.wait_for_url(lambda url: "/login" not in str(url))
                page.goto(base + "/scraping")
                source_card = page.locator(f"#scrape-history-{SOURCE}")
                report_button = source_card.get_by_test_id("button-report-courses")
                expect(report_button).to_be_visible(timeout=60000)
                report_button.click()
                source_card.get_by_test_id("input-report-urls").fill(
                    f"https://{HOST}/courses/computing\nhttps://{HOST}/courses/business")
                source_card.get_by_test_id("input-report-description").fill("Task564 browser acceptance")
                with page.expect_response(lambda r: r.request.method == "POST" and
                                          r.url.endswith(f"/{SOURCE}/course-reports")) as posted:
                    source_card.get_by_test_id("button-submit-report").click()
                response = posted.value
                assert response.status == 202, response.text()
                report = response.json()
                child_id = report["job_id"]
                evidence["child_job_id"] = child_id
                article = page.get_by_test_id(f"report-{child_id}")
                expect(article).to_be_visible()
                progress_deadline = time.monotonic() + 60
                while time.monotonic() < progress_deadline:
                    progress = snapshot_sync(dburl)
                    running = next((j for j in progress["jobs"]
                                    if j["runtime_job_id"] == child_id), None)
                    if running and running["status"] == "running":
                        break
                    time.sleep(.25)
                else:
                    raise AssertionError("Worker did not persist running progress before reload")
                # A fresh document must fetch the durable report, not retain React state.
                page.reload()
                reloaded = page.get_by_test_id(f"report-{child_id}")
                expect(reloaded).to_be_visible(timeout=60000)
                expect(reloaded.locator("p").first).to_contain_text(re.compile(r"— running$"))
                persisted = snapshot_sync(dburl)
                assert next(j for j in persisted["jobs"] if
                            j["runtime_job_id"] == child_id)["status"] == "running"
                evidence["reloaded_progress_status"] = "running"
                evidence["durable_reload_before_completion"] = True
                page.screenshot(path=str(output / "progress-reloaded.png"), full_page=True)
                release.set()
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    after = snapshot_sync(dburl)
                    target = next((j for j in after["jobs"] if j["runtime_job_id"] == child_id), None)
                    if target and target["status"] in {"completed", "completed_with_errors", "failed", "failed_degraded", "stopped"}:
                        break
                    time.sleep(1)
                else:
                    raise AssertionError("Real worker did not finish within 240 seconds")
                assert target["status"] in {"completed", "completed_with_errors"}, target
                assert target["imported"] > 0 and after["new_pending"] > 0, target
                # Let the real queued monitor finalize its durable audit as well.
                audit_deadline = time.monotonic() + 90
                while time.monotonic() < audit_deadline:
                    after = snapshot_sync(dburl)
                    if after["audits"] and all(
                        a["status"] not in {"queued", "running"} for a in after["audits"]
                    ):
                        break
                    time.sleep(1)
                else:
                    raise AssertionError("Production monitor did not finalize the report audit")
                evidence["database"] = after
                assert after["source_bytes"] == before["source_bytes"], "Source rows changed"
                assert after["published_courses"] == before["published_courses"] == 0
                assert after["published_review_rows"] == 0
                assert after["claims"], "No real fenced worker claim was persisted"
                payload = json.loads(target["request_payload"])
                policy = payload["autonomousVerification"]
                assert policy["max_courses"] == 50
                assert policy["time_budget_seconds"] == 600
                assert policy["cost_cap_usd"] == 2
                assert target["current"] <= 50 and target["total_gemini_cost_usd"] <= 2
                for audit in after["audits"]:
                    state = json.loads(audit["evidence"]).get("autonomous", {})
                    assert int(state.get("verification_requeues") or 0) <= 2
                    assert len(state.get("verification_job_ids", [])) <= 2
                # Verify server-side input bound, not only the browser's guidance text.
                oversized = context.request.post(
                    base + f"/api/scrape/jobs/{SOURCE}/course-reports",
                    data={"kind": "missing", "course_urls": [
                        f"https://{HOST}/courses/{n}" for n in range(51)]})
                assert oversized.status == 422, oversized.text()
                evidence["oversized_report_status"] = oversized.status
                page.reload()
                button = page.get_by_test_id(f"button-review-report-{child_id}")
                expect(button).to_be_visible(timeout=30000)
                with page.expect_response(lambda r: child_id in r.url and
                                          "staged" in r.url) as reviewed:
                    button.click()
                assert reviewed.value.status == 200, reviewed.value.text()
                assert reviewed.value.url.split("?")[0].endswith(f"/api/scrape/staged/{child_id}")
                evidence["exact_review_url"] = reviewed.value.url
                evidence["exact_review_identity"] = assert_review_identity(
                    page, reviewed.value, after, child_id, SOURCE, expect)
                page.screenshot(path=str(output / "exact-child-review.png"), full_page=True)
                evidence["source_rows_byte_identical"] = True
                # Independent report: discovery offers 55 eligible URLs, so the
                # actual worker must enforce its course-count boundary.
                page.goto(base + "/scraping")
                cap_card = page.locator(f"#scrape-history-{CAP_SOURCE}")
                cap_button = cap_card.get_by_test_id("button-report-courses")
                expect(cap_button).to_be_visible(timeout=30000)
                cap_button.click()
                cap_card.get_by_test_id("input-report-catalogue").fill(
                    f"https://{HOST}/bounded-catalogue")
                cap_card.get_by_test_id("input-report-expected").fill("55")
                with page.expect_response(lambda r: r.request.method == "POST" and
                                          r.url.endswith(f"/{CAP_SOURCE}/course-reports")) as cap_post:
                    cap_card.get_by_test_id("button-submit-report").click()
                assert cap_post.value.status == 202, cap_post.value.text()
                cap_id = cap_post.value.json()["job_id"]
                evidence["cap_job_id"] = cap_id
                cap_deadline = time.monotonic() + 660
                while time.monotonic() < cap_deadline:
                    final = snapshot_sync(dburl)
                    cap_job = next((j for j in final["jobs"] if j["runtime_job_id"] == cap_id), None)
                    if cap_job and cap_job["status"] in {
                        "completed", "completed_with_errors", "failed", "failed_degraded", "stopped",
                    }:
                        break
                    time.sleep(1)
                else:
                    raise AssertionError("Count-cap worker never reached a terminal state")
                evidence["cap_database"] = final
                assert cap_job["status"] in {"completed", "completed_with_errors"}, cap_job
                meta = json.loads(cap_job["discovered_config"])["autonomousVerification"]
                assert meta["effective_max_courses"] == 50, meta
                assert meta["selected_courses"] == 50, meta
                assert len(meta["selected_urls"]) == 50, meta
                assert meta["limit_reached"] is True, meta
                assert all("/courses/degree-" in url for url in meta["selected_urls"]), meta
                assert 0 < cap_job["imported"] <= 50 and cap_job["current"] <= 50, cap_job
                assert 0 < cap_job["review_rows"] <= 50, cap_job
                cap_audit_deadline = time.monotonic() + 90
                while time.monotonic() < cap_audit_deadline:
                    final = snapshot_sync(dburl)
                    if len(final["audits"]) == 2 and all(
                        audit["status"] not in {"queued", "running"} for audit in final["audits"]
                    ):
                        break
                    time.sleep(1)
                else:
                    raise AssertionError("Count-cap monitor did not finalize its audit")
                evidence["cap_database"] = final
                assert final["source_bytes"] == before["source_bytes"], "Source row changed during cap run"
                assert final["published_courses"] == final["published_review_rows"] == 0
                page.reload()
                cap_article = page.get_by_test_id(f"report-{cap_id}")
                expect(cap_article).to_be_visible(timeout=30000)
                expect(cap_article).to_contain_text("Catalogue coverage: not verified")
                if meta.get("capped"):
                    expect(cap_article).to_contain_text("recovery limit reached")
                with page.expect_response(lambda r: r.url.split("?")[0].endswith(
                    f"/api/scrape/staged/{cap_id}")) as cap_review:
                    page.get_by_test_id(f"button-review-report-{cap_id}").click()
                assert cap_review.value.status == 200, cap_review.value.text()
                evidence["cap_exact_review_url"] = cap_review.value.url
                evidence["cap_review_identity"] = assert_review_identity(
                    page, cap_review.value, final, cap_id, CAP_SOURCE, expect)
                evidence["cap_boundary_forced"] = True
                page.screenshot(path=str(output / "count-cap-review.png"), full_page=True)
                evidence["passed"] = True
            finally:
                page.screenshot(path=str(output / "final-browser.png"), full_page=True)
                context.tracing.stop(path=str(output / "browser-trace.zip"))
                browser.close()
    except Exception:
        evidence["error"] = traceback.format_exc()
        raise
    finally:
        release.set()
        if fixture:
            fixture.shutdown()
            fixture.server_close()
        for name, process in reversed(processes):
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=5)
        if pg_started:
            subprocess.run(["pg_ctl", "-D", str(temp / "pg"), "-m", "immediate", "-w", "stop"],
                           check=True, capture_output=True)
        for log in logs:
            log.close()
        private_recipes = sorted(str(path.relative_to(recipe_root))
                                 for path in recipe_root.rglob("*") if path.is_file())
        forbidden_path = recipe_root / "forbidden-writes.txt"
        forbidden_writes = forbidden_path.read_text().splitlines() if forbidden_path.exists() else []
        recipes_after = recipe_snapshot()
        recipe_changes = sorted(name for name in recipes_before.keys() | recipes_after.keys()
                                if recipes_before.get(name) != recipes_after.get(name))
        shutil.rmtree(temp)
        closed = {}
        for name, number in zip(("postgres", "redis", "api", "web", "official"), (
            pgport, redisport, apiport, webport, httpport,
        )):
            with socket.socket() as sock:
                closed[name] = sock.connect_ex(("127.0.0.1", number)) != 0
        evidence["fixture_requests"] = dict(counts)
        evidence["cleanup"] = {
            "closed_ports": closed, "private_directory_removed": not temp.exists(),
            "processes_exited": {name: child.poll() is not None for name, child in processes},
            "production_recipe_tree_unchanged": not recipe_changes,
            "production_recipe_changes": recipe_changes,
            "private_recipe_files_removed": private_recipes,
            "private_recipe_root_removed": not recipe_root.exists(),
            "forbidden_production_recipe_write_attempts": forbidden_writes,
        }
        if recipe_changes or forbidden_writes or not all(closed.values()) or recipe_root.exists():
            evidence["passed"] = False
            evidence["cleanup_failed"] = True
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2, default=str))
        assert all(closed.values()), f"Cleanup left listening sockets: {closed}"
        assert not recipe_changes, f"Production recipe filesystem changed: {recipe_changes}"
        assert not forbidden_writes, f"Attempted production recipe mutation: {forbidden_writes}"
        assert not recipe_root.exists(), "Private generated recipe root survived cleanup"
    return evidence


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.output.resolve())
    print(json.dumps({"passed": result["passed"], "evidence": str(args.output)}, indent=2))