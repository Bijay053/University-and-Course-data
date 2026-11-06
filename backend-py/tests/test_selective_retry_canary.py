"""Opt-in browser -> OTP -> assistant -> Redis/Celery -> official HTML -> staging.

No application database connections, shared broker, production writes, model
calls or publication are permitted. Only the mail DELIVERY is a local outbox:
challenge generation/consumption, password hashing and permissions are real.
The one injected transient happens before transport, not at the provider.
"""
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import threading
import time

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import Engine

from app.config import settings
from app.models import ScrapeRuntimeJob, ScrapedCourse, University, User
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_runtime import ScrapeRuntimeLog
from app.services import scrape_assistant as assistant
from app.services import scrape_assistant_runner
from app.services.scraper import orchestrator, result_reuse
from app.services.scraper.url_identity import canonical_course_url_key
from tests.test_scrape_prompt_assistant import store, assistant_postgres

ROOT = Path(__file__).resolve().parents[2]
HOME = "https://www.winchester.ac.uk"
COURSE = HOME + "/study/undergraduate/courses/llb-hons-law/"
SUCCESS = HOME + "/study/undergraduate/courses/ba-hons-history/"
LIVE = os.environ.get("RUN_SELECTIVE_RETRY_CANARY") == "1"
REAL_SCHEDULE = scrape_assistant_runner.schedule


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def snapshot(factory):
    """Compare complete stored values, including timestamps and provenance."""
    async with factory() as db:
        return {
            "job": await db.scalar(text(
                "SELECT to_jsonb(j) FROM scrape_runtime_jobs j WHERE runtime_job_id='source'")),
            "row": await db.scalar(text(
                "SELECT to_jsonb(c) FROM scraped_courses c WHERE id=1")),
            "evidence": (await db.execute(text(
                "SELECT to_jsonb(e) FROM scraped_field_evidence e WHERE scraped_course_id=1 ORDER BY id"
            ))).scalars().all(),
            "logs": (await db.execute(text(
                "SELECT to_jsonb(l) FROM scrape_runtime_logs l WHERE runtime_job_id='source' ORDER BY sequence"
            ))).scalars().all(),
        }


@pytest.mark.asyncio
@pytest.mark.skipif(not LIVE, reason="Opt-in official-source browser/worker canary")
@pytest.mark.parametrize("cache_case", ["valid", "mismatch"])
async def test_browser_selected_retry_recovers_official_course(store, monkeypatch, cache_case, assistant_postgres, request):
    from celery.contrib.testing.worker import start_worker
    from celery.signals import task_postrun
    from fastapi import FastAPI
    import httpx
    from playwright.async_api import async_playwright
    import uvicorn
    from tests.helpers.run_disposable_redis import private_broker
    from app import database
    from app.routers import auth, scrape_assistant as routes
    from app.services import scrape_assistant_runner, scrape_assistant_tools
    from app.services.passwords import hash_password
    from app.tasks import scrape_tasks
    from app.tasks.celery_app import celery_app
    from app.services.scrape_agent_v2 import model
    from app.services.ai import openai_client, gemini_client
    from app.services.scraper.config import loader

    # Prohibit accidental shared DB access, including captured session factories.
    forbidden, worker_done = [], []
    def task_finished(sender=None, task_id=None, args=None, retval=None, state=None, **kwargs):
        if sender and sender.name == "scrape.university":
            worker_done.append({"taskId": task_id, "jobId": args[0], "state": state, "result": retval})
    task_postrun.connect(task_finished, weak=False)
    def deny_shared(dialect, record, args, params):
        if params.get("host") != assistant_postgres["host"]:
            forbidden.append("shared_database")
            raise AssertionError("Canary attempted a non-disposable database connection")
    def only_private_sql(connection, cursor, statement, parameters, context, many):
        if connection.engine.pool is not store.kw["bind"].sync_engine.pool:
            forbidden.append("shared_database_sql")
            raise AssertionError("Canary SQL escaped its private schema")
        if re.search(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+(?:\w+\.)?\"?(?:courses|fees|intakes|english_requirements)\"?\b",
                     statement, re.I):
            forbidden.append("publication")
            raise AssertionError("Canary attempted to write published course data")
    event.listen(Engine, "do_connect", deny_shared)
    event.listen(Engine, "before_cursor_execute", only_private_sql)
    def remove_guards():
        task_postrun.disconnect(task_finished)
        for name, handler in (("do_connect", deny_shared), ("before_cursor_execute", only_private_sql)):
            if event.contains(Engine, name, handler):
                event.remove(Engine, name, handler)
    request.addfinalizer(remove_guards)
    monkeypatch.setattr(database, "AsyncSessionLocal", store)
    monkeypatch.setattr(orchestrator, "AsyncSessionLocal", store)
    monkeypatch.setattr(scrape_tasks, "AsyncSessionLocal", store)
    monkeypatch.setattr(scrape_tasks, "engine", store.kw["bind"])
    from app.services import scrape_runtime_controls
    gate = scrape_runtime_controls.RuntimeGate
    class IsolatedRuntimeGate(gate):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, session_factory=store, **kwargs)
    monkeypatch.setattr(scrape_runtime_controls, "RuntimeGate", IsolatedRuntimeGate)
    monkeypatch.setattr(settings, "scrape_agent_v2_enabled", True)
    monkeypatch.setattr(settings, "scrape_agent_v2_user_ids", "843")
    load_config = loader.load_uni_config
    def canary_config(*args, **kwargs):
        kwargs["create_missing_stub"] = False
        config = load_config(*args, **kwargs)
        # Explicit bounded test configuration, shared by proposal and worker.
        config.extraction.skip_remote_ai_enrichment = True
        config.extraction.skip_per_course_browser = True
        return config
    monkeypatch.setattr(loader, "load_uni_config", canary_config)
    # Restore the production scheduler disabled by the legacy store fixture.
    monkeypatch.setattr(scrape_assistant_runner, "schedule", REAL_SCHEDULE)
    async def deny_model(*args, **kwargs):
        forbidden.append("model")
        raise AssertionError("Deterministic retry must not call a model")
    monkeypatch.setattr(model, "complete", deny_model)
    monkeypatch.setattr(assistant, "chat_json", deny_model)
    def deny_client(*args, **kwargs):
        import traceback
        forbidden.append("model_client:" + "".join(traceback.format_stack(limit=5)))
        raise AssertionError("Live models disabled: no USD reservation authorized")
    monkeypatch.setattr(openai_client, "_client", deny_client)
    monkeypatch.setattr(gemini_client, "_client", deny_client)
    # The optional archival subsystem must not write to the real object store.
    from app.services.scraper import snapshot_save
    async def no_archive(*args, **kwargs):
        return None
    monkeypatch.setattr(snapshot_save, "save_extraction_snapshot", no_archive)
    monkeypatch.setattr(snapshot_save, "save_api_json_snapshot", no_archive)
    # The bootstep still executes the real readiness query, on OUR connection.
    from app import schema_readiness
    async def private_readiness():
        async with store.kw["bind"].connect() as connection:
            await schema_readiness.require_qualification_guidance_schema(connection)
    monkeypatch.setattr(schema_readiness, "check_worker_schema", private_readiness)

    outbox = []
    async def mail(to, subject, body, html_body=None):
        assert to == "retry-canary@example.test"
        outbox.append(re.search(r"\b[0-9]{6}\b", body).group())
        return True
    monkeypatch.setattr(auth, "smtp_is_configured", lambda: True)
    monkeypatch.setattr(auth, "send_email", mail)

    async with store() as db:
        university = await db.get(University, 1)
        university.name = "Isolated Retry Canary"
        university.scrape_url = HOME
        university.scrape_config = {"extraction": {
            "skip_remote_ai_enrichment": True, "skip_per_course_browser": True,
        }}
        db.add(User(id=843, email="retry-canary@example.test",
                    password_hash=hash_password("test-only-canary-password"),
                    full_name="Isolated Canary", is_active=True, is_super_admin=True))
        job = await db.get(ScrapeRuntimeJob, "source")
        job.url, job.total_found, job.current, job.errors = HOME, 2, 2, 1
        job.completed_at = datetime.now(timezone.utc)
        job.worker_id = "settled-canary-source"
        row = await db.get(ScrapedCourse, 1)
        row.course_website = SUCCESS
        db.add(ScrapedFieldEvidence(scraped_course_id=1, field_key="course_name",
            source_url=SUCCESS, extraction_method="staff", raw_text="Preserved success",
            selected=True, decision_status="selected"))
        db.add(ScrapeRuntimeLog(runtime_job_id="source", sequence=1, event="status",
            payload={"kind": "extract_error", "url": COURSE, "retryable": True,
                     "reason": "per_course_timeout"}))
        # Fixture uses explicit primary key 1; advance its actual sequence.
        await db.execute(text("SELECT setval(pg_get_serial_sequence('scraped_courses','id'),1)"))
        await assistant.ensure_schema(db)
        await result_reuse.ensure_schema(db)
        await db.commit()
    from tests.test_campus_fee_split import migrate_offerings_in_transaction
    async with store.kw["bind"].begin() as connection:
        await migrate_offerings_in_transaction(connection)
        from importlib.util import module_from_spec, spec_from_file_location
        from types import SimpleNamespace
        for filename in ("382_autonomous_worker_claims.py", "383_worker_claim_lock_lineage.py"):
            spec = spec_from_file_location("canary_" + filename[:-3],
                ROOT / "backend-py/alembic/versions" / filename)
            migration = module_from_spec(spec)
            spec.loader.exec_module(migration)
            migration.op = SimpleNamespace(execute=lambda sql: connection.sync_connection.execute(text(sql)))
            await connection.run_sync(lambda _: migration.upgrade())
    before = await snapshot(store)
    reads, attempts, cache_receipts, fresh_extractions, reservation_receipts = [], [], [], [], []
    seeded = {}
    real_extract = orchestrator._extract_with_hard_timeout
    real_reuse = result_reuse.reuse
    warm = False

    async def authorized_extract(link, *args, **kwargs):
        assert link["url"] == COURSE, "Successful URL must never be extracted"
        attempts.append(time.monotonic())
        # Independently read the committed reservation before ANY transport.
        from app.services.worker_fencing import current_owner
        owner = current_owner.get()
        assert owner and owner.key.startswith("selective-retry:")
        async with store() as db:
            receipt = (await db.execute(text(
                "SELECT id,ordinal,outcome FROM scrape_retry_attempts WHERE job_id=:id ORDER BY ordinal DESC LIMIT 1"
            ), {"id": owner.key.removeprefix("selective-retry:")})).one()
        assert receipt.ordinal == len(attempts) and receipt.outcome == "started"
        reservation_receipts.append(receipt.id)
        # A controlled pre-transport failure proves the worker's real backoff.
        if len(attempts) == 1:
            return {"url": COURSE, "error": "per_course_timeout", "retryable": True}
        raw = await scrape_assistant_tools.public_request(
            scrape_assistant_tools.official_url(COURSE, HOME))
        html = raw.decode("utf-8")
        (proof_dir / f"{cache_case}-official.html").write_text(html)
        verified = await scrape_assistant_tools.inspect_source(COURSE, HOME, raw_body=raw)
        assert verified.get("verified"), verified.get("error")
        reads.append({"url": COURSE, "sha256": hashlib.sha256(raw).hexdigest(),
                      "robotsChecked": True, "bytes": len(raw)})
        kwargs["timeout_seconds"] = min(45, kwargs.get("timeout_seconds", 45))
        return await real_extract({**link, "_surrey_html": html}, *args, **kwargs)

    # Observe the real cache at the worker boundary. The warm entry comes from
    # actual deterministic extraction, never a fabricated payload or pending row.
    async def observed_reuse(url, html, scope, extract, **kwargs):
        nonlocal warm
        if not warm:
            # Seed a legitimate partial deterministic result using the REAL
            # course-title and tuition extractors on these exact live bytes.
            # Missing fields remain unknown; do not copy successful pending rows.
            from app.services.scraper.extractors import course_name, fee
            title = max(await course_name.extract(html, url), key=lambda r: r.confidence)
            tuition = max(await fee.extract(html, url), key=lambda r: r.confidence)
            payload = {**title.normalized, **tuition.normalized}
            assert payload.get("course_name") and payload.get("international_fee")
            prior = {"url": url, "payload": payload, "gemini_calls": 0, "evidence": [
                {"field_key": candidate.field_key, "normalized_value": payload[candidate.field_key],
                 "raw_value": candidate.snippet, "source_url": url,
                 "extraction_method": candidate.method, "method": candidate.method,
                 "value": candidate.value, "snippet": candidate.snippet,
                 "decision_status": "selected", "selected": True, "confidence": candidate.confidence}
                for candidate in (title, tuition)]}
            assert result_reuse.authoritative(prior, url, html), ("Unproven live cache seed", prior)
            async def seed_result():
                return prior
            previous_scope = scope if cache_case == "valid" else {
                **scope, "audience": "domestic", "year": 1900,
                "config": "stale-config", "central": "stale-source"}
            previous_html = html if cache_case == "valid" else html + "<!-- older source bytes -->"
            await real_reuse(url, previous_html, previous_scope, seed_result, **kwargs)
            seeded.update(key=result_reuse.cache_key(url, previous_html, previous_scope),
                          result=prior, scope=previous_scope)
            warm = True
        async def counted_extraction():
            fresh_extractions.append(url)
            return await extract()
        result = await real_reuse(url, html, scope, counted_extraction, **kwargs)
        cache_receipts.append(bool(result.get("_result_cache_hit")))
        return result
    monkeypatch.setattr(orchestrator, "_extract_with_hard_timeout", authorized_extract)
    monkeypatch.setattr(result_reuse, "reuse", observed_reuse)
    # Current workspace debug builds intentionally cannot reuse results. Use
    # a content-addressed test release identity, not a fake production receipt.
    from app import release_info
    monkeypatch.setattr(settings, "debug", False)
    monkeypatch.setattr(release_info, "get_release_revision",
                        lambda: result_reuse._BUILD_SOURCE_HASH[:40])

    app = FastAPI()
    app.include_router(auth.router, prefix="/api/auth")
    app.include_router(routes.router, prefix="/api/scrape")
    async def db_dependency():
        async with store() as db:
            yield db
    app.dependency_overrides[database.get_db] = db_dependency
    api_port, web_port = free_port(), free_port()
    api = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=api_port, log_level="warning"))
    api_thread = threading.Thread(target=api.run, daemon=True)
    web = None
    browser = None
    report = {"environment": "workspace", "production": {"executed": False,
              "reason": "Task fork; production access and publication not authorized"},
              "case": cache_case, "passed": False,
              "parserSourceHash": result_reuse._BUILD_SOURCE_HASH,
              "canarySourceHash": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    proof_dir = ROOT / "generated-artifacts/selective-retry"
    proof_dir.mkdir(parents=True, exist_ok=True)
    try:
        with private_broker() as broker:
            broker_url = f"redis://127.0.0.1:{broker}/0"
            monkeypatch.setattr(settings, "redis_url", broker_url)
            # start_worker is a real non-eager Celery consumer, no beat/reaper.
            monkeypatch.setitem(celery_app.conf, "broker_url", broker_url)
            monkeypatch.setitem(celery_app.conf, "result_backend", broker_url)
            monkeypatch.setitem(celery_app.conf, "task_always_eager", False)
            # Celery caches producer pools independently of conf.broker_url.
            # Never publish case two into case one's already-stopped broker.
            monkeypatch.setattr(celery_app, "_pool", None)
            monkeypatch.setattr(celery_app.amqp, "_producer_pool", None)
            api_thread.start()
            deadline = time.monotonic() + 20
            while not api.started:
                assert api_thread.is_alive() and time.monotonic() < deadline
                await asyncio.sleep(.05)
            log = (proof_dir / f"{cache_case}-vite.log").open("w")
            web = subprocess.Popen(
                ["node", "--input-type=module", "-e",
                 (ROOT / "backend-py/tests/helpers/selective_retry_browser.mjs").read_text(),
                 str(web_port), str(api_port)],
                cwd=ROOT / "artifacts/university-portal", stdout=log, stderr=subprocess.STDOUT)
            async with httpx.AsyncClient() as client:
                deadline = time.monotonic() + 40
                while True:
                    assert web.poll() is None, "Canary Vite exited; inspect generated proof log"
                    try:
                        if (await client.get(f"http://127.0.0.1:{web_port}/__retry_canary")).status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    assert time.monotonic() < deadline, "Canary Vite startup timed out"
                    await asyncio.sleep(.2)
            with start_worker(celery_app, pool="solo", concurrency=1, perform_ping_check=False,
                              queues=["scrape"], shutdown_timeout=30):
                async with async_playwright() as playwright, AsyncExitStack() as browser_cleanup:
                    browser = await playwright.chromium.launch(headless=True)
                    # Close Chromium before stopping its driver, even if an
                    # OTP, transport, chat, or database assertion fails.
                    browser_cleanup.push_async_callback(browser.close)
                    page = await browser.new_page()
                    await page.goto(f"http://127.0.0.1:{web_port}/__retry_canary")
                    await page.locator("#email").fill("retry-canary@example.test")
                    await page.locator("#password").fill("test-only-canary-password")
                    await page.get_by_role("button", name="Send verification code", exact=True).click()
                    await page.locator("#verification-code").wait_for()
                    assert not any(c["name"] == "session" for c in await page.context.cookies())
                    denied = await page.request.post(f"http://127.0.0.1:{web_port}/api/scrape/jobs/source/assistant",
                                                    data={"prompt": "Retry failed courses"})
                    assert denied.status == 401
                    assert len(outbox) == 1
                    await page.locator("#verification-code").fill(outbox.pop())
                    await page.get_by_role("button", name="Verify and sign in", exact=True).click()
                    await page.get_by_test_id("input-assistant-prompt").fill(
                        "Retry only failed courses with 2 attempts and backoff 1s. Don't retry successful courses.")
                    await page.get_by_test_id("button-assistant-submit").click()
                    deadline = time.monotonic() + 150
                    action = None
                    while time.monotonic() < deadline:
                        response = await page.request.get(
                            f"http://127.0.0.1:{web_port}/api/scrape/jobs/source/assistant")
                        assert response.ok
                        reply = await response.json()
                        actions = [a for m in reply["messages"] for a in m.get("actions", [])]
                        if actions:
                            action = actions[-1]
                            if action["state"] in {"completed", "error"}:
                                break
                        assert reply["status"] != "error", reply.get("error")
                        await asyncio.sleep(.5)
                    if not action or action["state"] != "completed":
                        async with store() as db:
                            report["workerLogs"] = (await db.execute(text(
                                "SELECT payload FROM scrape_runtime_logs WHERE runtime_job_id!='source' ORDER BY sequence"
                            ))).scalars().all()
                            report["jobs"] = (await db.execute(text(
                                "SELECT runtime_job_id,status,error_message,discovered_config FROM scrape_runtime_jobs"
                            ))).mappings().all()
                            report["jobs"] = [dict(item) for item in report["jobs"]]
                    assert action and action["state"] == "completed", action or reply
                    assert action["resultSummary"] == "1 URLs stored pending, 0 skipped, 0 unverified. Nothing published."
                    await page.get_by_text(action["resultSummary"], exact=True).wait_for(timeout=5000)
                    await page.reload()
                    await page.get_by_text(action["resultSummary"], exact=True).wait_for(timeout=10000)
                    await page.screenshot(path=str(proof_dir / f"{cache_case}-browser.png"))
            assert len(worker_done) == 1 and worker_done[0]["jobId"] == action["jobId"]
            assert worker_done[0]["state"] == "SUCCESS" and worker_done[0]["result"]["ok"] is True
            assert await snapshot(store) == before, "Successful row/source progress/evidence changed"
            async with store() as db:
                child = await db.get(ScrapeRuntimeJob, action["jobId"])
                assert child.request_payload["courseUrls"] == [COURSE]
                assert child.request_payload["selectiveRetry"]["budgets"] == {COURSE: 2}
                assert child.current == child.total_found == child.imported == 1
                rows = (await db.execute(select(ScrapedCourse).where(
                    ScrapedCourse.scrape_job_id == child.runtime_job_id))).scalars().all()
                assert len(rows) == 1 and rows[0].status == "pending" and rows[0].auto_publish_status == "review"
                assert canonical_course_url_key(rows[0].course_website) == canonical_course_url_key(COURSE)
                assert rows[0].course_name == seeded["result"]["payload"]["course_name"]
                assert rows[0].international_fee == seeded["result"]["payload"]["international_fee"]
                evidence = (await db.execute(select(ScrapedFieldEvidence).where(
                    ScrapedFieldEvidence.scraped_course_id == rows[0].id))).scalars().all()
                assert evidence and any(e.selected for e in evidence)
                assert all(canonical_course_url_key(e.source_url) == canonical_course_url_key(COURSE)
                           for e in evidence)
                receipts = (await db.execute(text(
                    "SELECT ordinal,outcome,created_at,result_hash FROM scrape_retry_attempts WHERE job_id=:id ORDER BY ordinal"
                ), {"id": child.runtime_job_id})).all()
                assert [(r.ordinal, r.outcome) for r in receipts] == [(1, "error"), (2, "extracted")]
                assert all(r.result_hash for r in receipts)
                assert (receipts[1].created_at - receipts[0].created_at).total_seconds() >= 1
                assert await db.scalar(text("SELECT count(*) FROM courses")) == 0
                saved = await db.scalar(text(
                    "SELECT payload FROM scrape_extraction_result_cache WHERE cache_key=:key"),
                    {"key": seeded["key"]})
                assert saved["scope"] == seeded["scope"] and saved["result"] == seeded["result"]
                worker_logs = (await db.execute(select(ScrapeRuntimeLog.payload).where(
                    ScrapeRuntimeLog.runtime_job_id == child.runtime_job_id))).scalars().all()
                assert [p["attempt"] for p in worker_logs if p.get("kind") == "selective_retry_attempt"] == [1, 2]
                assert sum(p.get("kind") == "selective_retry_cache_hit" for p in worker_logs) == (cache_case == "valid")
                assert all(p.get("url") != SUCCESS for p in worker_logs)
            assert len(attempts) == 2 and attempts[1] - attempts[0] >= 1
            assert len(reads) == 1 and cache_receipts == [cache_case == "valid"]
            assert fresh_extractions == ([] if cache_case == "valid" else [COURSE])
            assert not forbidden
            report.update(reads=reads, attempts=[{"ordinal": r.ordinal,
                "outcome": r.outcome, "at": r.created_at.isoformat(), "resultHash": r.result_hash} for r in receipts],
                cacheHits=cache_receipts, successfulStateUnchanged=True, pendingRows=1,
                fullExtractions=len(fresh_extractions), reservationIds=reservation_receipts,
                childJobId=child.runtime_job_id, pendingRowId=rows[0].id,
                selectedEvidenceIds=[e.id for e in evidence if e.selected],
                workerCompletion=worker_done[0],
                publishedWrites=0, modelCalls=0, modelBudget="No model request; reservation not needed",
                otpRequired=True, summary=action["resultSummary"])
        # Do not certify success until the private broker's teardown succeeds.
        report["passed"] = True
    finally:
        report["forbiddenOperations"] = forbidden
        try:
            api.should_exit = True
            if api_thread.ident:
                await asyncio.to_thread(api_thread.join, 10)
                assert not api_thread.is_alive(), "Canary API did not stop"
            if web:
                web.terminate()
                await asyncio.to_thread(web.wait, 10)
                log.close()
            await scrape_assistant_runner.shutdown()
            report["cleanupVerified"] = True
        except BaseException:
            report.update(passed=False, cleanupVerified=False)
            raise
        finally:
            remove_guards()
            (proof_dir / f"{cache_case}-workspace.json").write_text(json.dumps(report, indent=2))
            (proof_dir / "production.json").write_text(json.dumps({
                "environment": "production", "executed": False, "passed": None,
                "courseWrites": 0, "publication": False,
                "reason": "Workspace canary is not production acceptance; no production connection or deploy attempted."
            }, indent=2))
