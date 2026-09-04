"""Tests for the three new features implemented in session:

  1. Tier-7 operator alert (discovery_failure_alerts table + alert delivery)
  2. Nightly sweep beat task registration
  3. Tier-2 per-uni subdomain probe in discover_course_links
"""
from __future__ import annotations

import importlib
import inspect
import asyncio
from datetime import datetime, timedelta, timezone
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, update


# ─────────────────────────────────────────────────────────────────────────────
# 1. Tier-7 — DiscoveryFailureAlert model + alert delivery helpers
# ─────────────────────────────────────────────────────────────────────────────

def test_discovery_failure_alert_model_importable() -> None:
    """DiscoveryFailureAlert must import cleanly and expose the expected columns."""
    from app.models.discovery_failure_alert import DiscoveryFailureAlert

    assert DiscoveryFailureAlert.__tablename__ == "discovery_failure_alerts"
    columns = {c.name for c in DiscoveryFailureAlert.__table__.columns}
    assert "id" in columns
    assert "university_id" in columns
    assert "candidates_found" in columns
    assert "diagnostic" in columns
    assert "created_at" in columns
    assert "resolved_at" in columns
    assert "resolved_by" in columns
    assert "delivery_status" in columns
    assert "delivery_attempts" in columns
    assert "delivery_detail" in columns
    assert "delivery_attempted_at" in columns


def test_discovery_failure_alert_in_models_init() -> None:
    """app.models must re-export DiscoveryFailureAlert for Alembic autogenerate."""
    from app import models
    assert hasattr(models, "DiscoveryFailureAlert"), (
        "DiscoveryFailureAlert not exported from app.models — "
        "Alembic autogenerate won't see the table."
    )


def test_deliver_discovery_failure_alert_noop_without_env(monkeypatch) -> None:
    """deliver_discovery_failure_alert must not raise when no transport is configured."""
    import app.services.scraper.alert_delivery as ad

    monkeypatch.setattr(ad, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(ad, "ALERT_EMAIL_TO", None)

    # Should be a silent no-op (no Slack, no SMTP configured)
    ad.deliver_discovery_failure_alert(
        uni_name="Test University",
        uni_id=99,
        scrape_url="https://test.edu.au/courses",
        candidates_found=0,
        diagnostic={"job_id": "abc", "fast_mode": False},
    )


def test_deliver_discovery_failure_alert_calls_slack(monkeypatch) -> None:
    """deliver_discovery_failure_alert must call _send_slack_raw when SLACK_WEBHOOK_URL set."""
    import app.services.scraper.alert_delivery as ad

    monkeypatch.setattr(ad, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    monkeypatch.setattr(ad, "ALERT_EMAIL_TO", None)

    calls: list[tuple] = []

    def _fake_slack(url, subject, body):
        calls.append((url, subject, body))
        return {"success": True, "detail": "ok"}

    monkeypatch.setattr(ad, "_send_slack_raw", _fake_slack)

    ad.deliver_discovery_failure_alert(
        uni_name="Bond University",
        uni_id=10,
        scrape_url="https://bond.edu.au/courses",
        candidates_found=1,
        diagnostic={"job_id": "xyz"},
    )

    assert len(calls) == 1, "Expected exactly one Slack call"
    url, subject, body = calls[0]
    assert "Tier-7" in subject
    assert "Bond University" in subject
    assert "1 candidate" in subject
    assert "bond.edu.au" in body


def test_discovery_delivery_records_all_configured_transport_failures(monkeypatch) -> None:
    import app.services.scraper.alert_delivery as ad

    monkeypatch.setattr(ad, "SLACK_WEBHOOK_URL", "https://hooks.slack.invalid/fake")
    monkeypatch.setattr(ad, "ALERT_EMAIL_TO", "ops@example.test")
    monkeypatch.setattr(ad, "SMTP_HOST", "smtp.invalid")
    monkeypatch.setattr(
        ad, "_send_slack_raw",
        lambda *args: {"success": False, "detail": "Slack unavailable"},
    )
    monkeypatch.setattr(
        ad, "_send_email",
        lambda **kwargs: {"success": False, "detail": "SMTP unavailable"},
    )

    result = ad.deliver_discovery_failure_alert(
        uni_name="Test University", uni_id=99,
        scrape_url="https://test.example/courses", candidates_found=0,
        diagnostic={"job_id": "job-test"},
    )

    assert result["status"] == "failed"
    assert result["transports"]["slack"]["success"] is False
    assert result["transports"]["email"]["success"] is False


def test_discovery_delivery_reports_no_configured_transport(monkeypatch) -> None:
    import app.services.scraper.alert_delivery as ad

    monkeypatch.setattr(ad, "SLACK_WEBHOOK_URL", None)
    monkeypatch.setattr(ad, "ALERT_EMAIL_TO", None)
    monkeypatch.setattr(ad, "SMTP_HOST", "")

    result = ad.deliver_discovery_failure_alert(
        uni_name="Test University", uni_id=99,
        scrape_url="https://test.example/courses", candidates_found=0,
        diagnostic={},
    )

    assert result == {"status": "not_configured", "transports": {}}


@pytest.mark.asyncio
@pytest.mark.parametrize("candidates_found", [0, 40])
async def test_discovery_failure_alert_uses_runtime_job_id_and_delivers(
    monkeypatch,
    candidates_found: int,
) -> None:
    """Zero-course and high-drop alerts persist and deliver without reading job.id."""
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.services.scraper import alert_delivery
    from app.services.scraper.orchestrator import (
        _persist_and_deliver_discovery_failure_alert,
    )

    db = MagicMock()
    db.commit = AsyncMock()
    delivered: list[dict] = []

    monkeypatch.setattr(
        alert_delivery,
        "deliver_discovery_failure_alert",
        lambda **kwargs: (
            delivered.append(kwargs)
            or {"status": "delivered", "transports": {"test": {"success": True}}}
        ),
    )

    async def _run_inline(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _run_inline)

    job = ScrapeRuntimeJob(
        runtime_job_id="stable-runtime-job",
        scraping_job_id=None,
        university_id=99,
        university_name="Test University",
        url="https://test.example/courses",
        job_type="scrape",
        status="running",
    )
    diagnostic = {"source": "regression-test"}

    await _persist_and_deliver_discovery_failure_alert(
        db,
        job=job,
        uni_id=99,
        uni_name="Test University",
        scrape_url="https://test.example/courses",
        candidates_found=candidates_found,
        diagnostic=diagnostic,
    )
    await asyncio.sleep(0)

    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    persisted = db.add.call_args.args[0]
    assert persisted.diagnostic["job_id"] == "stable-runtime-job"
    assert persisted.candidates_found == candidates_found
    assert persisted.delivery_attempts == 1
    assert persisted.delivery_attempted_at is not None
    assert delivered[0]["diagnostic"]["job_id"] == "stable-runtime-job"
    assert delivered[0]["candidates_found"] == candidates_found


@pytest.mark.asyncio
async def test_overlapping_discovery_alert_retry_keeps_newest_attempt_authoritative(
    monkeypatch,
) -> None:
    """A stale initial send cannot overwrite a completed retry."""
    from app.database import AsyncSessionLocal, engine
    from app.models.discovery_failure_alert import DiscoveryFailureAlert
    from app.models.scrape_runtime import ScrapeRuntimeJob
    from app.models.university import University
    from app.routers import discovery_failure_alerts as alerts_router
    from app.services.scraper import alert_delivery
    from app.services.scraper.orchestrator import (
        _persist_and_deliver_discovery_failure_alert,
    )

    await engine.dispose()
    suffix = uuid.uuid4().hex[:12]
    initial_started = asyncio.Event()
    release_initial = asyncio.Event()
    delivery_calls = 0

    async def _interleaved_to_thread(function, *args, **kwargs):
        nonlocal delivery_calls
        assert function is alert_delivery.deliver_discovery_failure_alert
        delivery_calls += 1
        if delivery_calls == 1:
            initial_started.set()
            await release_initial.wait()
            return {
                "status": "failed",
                "transports": {"test": {"success": False}},
                "generation": 1,
            }
        return {
            "status": "delivered",
            "transports": {"test": {"success": True}},
            "generation": 2,
        }

    monkeypatch.setattr(asyncio, "to_thread", _interleaved_to_thread)
    monkeypatch.setattr(
        alerts_router,
        "deliver_discovery_failure_alert",
        alert_delivery.deliver_discovery_failure_alert,
    )

    university_id: int | None = None
    alert_id: int | None = None
    try:
        async with AsyncSessionLocal() as db:
            university = University(
                name=f"Alert Interleaving University {suffix}",
                country="Test",
                city="Test",
                scrape_url="https://alerts.example.test/courses",
            )
            db.add(university)
            await db.commit()
            await db.refresh(university)
            university_id = university.id

            job = ScrapeRuntimeJob(
                runtime_job_id=f"alert_interleave_{suffix}",
                university_id=university.id,
                university_name=university.name,
                url=university.scrape_url,
                job_type="scrape",
                status="running",
            )
            tasks_before = set(asyncio.all_tasks())
            await _persist_and_deliver_discovery_failure_alert(
                db,
                job=job,
                uni_id=university.id,
                uni_name=university.name,
                scrape_url=university.scrape_url or "",
                candidates_found=0,
                diagnostic={"source": "overlap-regression"},
            )
            background_tasks = set(asyncio.all_tasks()) - tasks_before

        assert len(background_tasks) == 1
        initial_task = background_tasks.pop()
        await asyncio.wait_for(initial_started.wait(), timeout=2)

        async with AsyncSessionLocal() as db:
            alert = (
                await db.execute(
                    select(DiscoveryFailureAlert).where(
                        DiscoveryFailureAlert.university_id == university_id
                    )
                )
            ).scalar_one()
            alert_id = alert.id
            assert alert.delivery_status == "pending"
            assert alert.delivery_attempts == 1

        async with AsyncSessionLocal() as db:
            with pytest.raises(HTTPException) as conflict:
                await alerts_router.retry_discovery_failure_alert(
                    alert_id=alert_id,
                    _user={"id": "test"},
                    db=db,
                )
            assert conflict.value.status_code == 409
        assert delivery_calls == 1

        async with AsyncSessionLocal() as db:
            await db.execute(
                update(DiscoveryFailureAlert)
                .where(DiscoveryFailureAlert.id == alert_id)
                .values(
                    delivery_attempted_at=(
                        datetime.now(timezone.utc) - timedelta(seconds=31)
                    )
                )
            )
            await db.commit()

        async with AsyncSessionLocal() as db:
            retry_result = await alerts_router.retry_discovery_failure_alert(
                alert_id=alert_id,
                _user={"id": "test"},
                db=db,
            )
        assert retry_result["deliveryStatus"] == "delivered"
        assert retry_result["deliveryAttempts"] == 2
        assert retry_result["deliveryDetail"]["generation"] == 2

        release_initial.set()
        await asyncio.wait_for(initial_task, timeout=2)

        async with AsyncSessionLocal() as db:
            final_alert = await db.get(DiscoveryFailureAlert, alert_id)
            assert final_alert is not None
            assert final_alert.delivery_status == "delivered"
            assert final_alert.delivery_attempts == 2
            assert final_alert.delivery_detail["generation"] == 2
        assert delivery_calls == 2
    finally:
        release_initial.set()
        if alert_id is not None or university_id is not None:
            async with AsyncSessionLocal() as db:
                if alert_id is not None:
                    await db.execute(
                        delete(DiscoveryFailureAlert).where(
                            DiscoveryFailureAlert.id == alert_id
                        )
                    )
                if university_id is not None:
                    await db.execute(
                        delete(University).where(University.id == university_id)
                    )
                await db.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_concurrent_expired_discovery_alert_retries_start_delivery_once(
    monkeypatch,
) -> None:
    """Two retries of one stale pending alert serialize on the database row lock."""
    from app.database import AsyncSessionLocal, engine
    from app.models.discovery_failure_alert import DiscoveryFailureAlert
    from app.models.university import University
    from app.routers import discovery_failure_alerts as alerts_router

    await engine.dispose()
    suffix = uuid.uuid4().hex[:12]
    first_commit_reached = asyncio.Event()
    release_first_commit = asyncio.Event()
    delivery_started = asyncio.Event()
    release_delivery = asyncio.Event()
    delivery_calls = 0

    async def _blocked_to_thread(function, *args, **kwargs):
        nonlocal delivery_calls
        assert function is alerts_router.deliver_discovery_failure_alert
        delivery_calls += 1
        delivery_started.set()
        await release_delivery.wait()
        return {
            "status": "delivered",
            "transports": {"test": {"success": True}},
        }

    monkeypatch.setattr(asyncio, "to_thread", _blocked_to_thread)

    university_id: int | None = None
    alert_id: int | None = None
    first_retry: asyncio.Task | None = None
    second_retry: asyncio.Task | None = None
    try:
        async with AsyncSessionLocal() as db:
            university = University(
                name=f"Concurrent Alert Retry University {suffix}",
                country="Test",
                city="Test",
                scrape_url="https://concurrent-alert.example.test/courses",
            )
            db.add(university)
            await db.flush()
            university_id = university.id

            alert = DiscoveryFailureAlert(
                university_id=university.id,
                candidates_found=0,
                diagnostic={"source": "concurrent-retry-regression"},
                delivery_status="pending",
                delivery_attempts=1,
                delivery_attempted_at=(
                    datetime.now(timezone.utc) - timedelta(seconds=31)
                ),
            )
            db.add(alert)
            await db.commit()
            await db.refresh(alert)
            alert_id = alert.id

        async with AsyncSessionLocal() as first_db, AsyncSessionLocal() as second_db:
            original_first_commit = first_db.commit

            async def _hold_first_commit():
                first_commit_reached.set()
                await release_first_commit.wait()
                await original_first_commit()

            monkeypatch.setattr(first_db, "commit", _hold_first_commit)
            first_retry = asyncio.create_task(
                alerts_router.retry_discovery_failure_alert(
                    alert_id=alert_id,
                    _user={"id": "operator-one"},
                    db=first_db,
                )
            )
            await asyncio.wait_for(first_commit_reached.wait(), timeout=2)

            second_retry = asyncio.create_task(
                alerts_router.retry_discovery_failure_alert(
                    alert_id=alert_id,
                    _user={"id": "operator-two"},
                    db=second_db,
                )
            )
            await asyncio.sleep(0.1)
            assert not second_retry.done(), (
                "The concurrent retry must wait for the first transaction's "
                "row lock to be released"
            )

            release_first_commit.set()
            await asyncio.wait_for(delivery_started.wait(), timeout=2)
            with pytest.raises(HTTPException) as conflict:
                await asyncio.wait_for(second_retry, timeout=2)
            assert conflict.value.status_code == 409
            await second_db.rollback()

            release_delivery.set()
            first_result = await asyncio.wait_for(first_retry, timeout=2)

        assert first_result["deliveryStatus"] == "delivered"
        assert first_result["deliveryAttempts"] == 2
        assert delivery_calls == 1

        async with AsyncSessionLocal() as db:
            final_alert = await db.get(DiscoveryFailureAlert, alert_id)
            assert final_alert is not None
            assert final_alert.delivery_status == "delivered"
            assert final_alert.delivery_attempts == 2
    finally:
        release_first_commit.set()
        release_delivery.set()
        for task in (first_retry, second_retry):
            if task is not None and not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if alert_id is not None or university_id is not None:
            async with AsyncSessionLocal() as db:
                if alert_id is not None:
                    await db.execute(
                        delete(DiscoveryFailureAlert).where(
                            DiscoveryFailureAlert.id == alert_id
                        )
                    )
                if university_id is not None:
                    await db.execute(
                        delete(University).where(University.id == university_id)
                    )
                await db.commit()
        await engine.dispose()


def test_deliver_drift_alert_noop_when_clean(monkeypatch) -> None:
    """deliver_drift_alert must be a no-op when diffs and warnings are both empty."""
    import app.services.scraper.alert_delivery as ad

    monkeypatch.setattr(ad, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    calls: list = []
    monkeypatch.setattr(ad, "_send_slack_raw", lambda *a, **kw: calls.append(a))

    ad.deliver_drift_alert(
        before_date="20260430",
        after_date="20260501",
        diffs=[],
        warnings=[],
        summary="Regression sweep: 5 before / 5 after snapshots\nAll clean.",
    )

    assert calls == [], "deliver_drift_alert should not fire when no diffs"


def test_deliver_drift_alert_fires_with_diffs(monkeypatch) -> None:
    """deliver_drift_alert must call Slack with a summary when diffs exist."""
    import app.services.scraper.alert_delivery as ad

    monkeypatch.setattr(ad, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/fake")
    monkeypatch.setattr(ad, "ALERT_EMAIL_TO", None)

    calls: list[tuple] = []
    monkeypatch.setattr(ad, "_send_slack_raw", lambda url, subj, body: calls.append((subj, body)))

    ad.deliver_drift_alert(
        before_date="20260430",
        after_date="20260501",
        diffs=[{"slug": "acu", "field": "fee_international", "before": "32000", "after": "33000"}],
        warnings=[],
        summary="1 unexpected diff",
    )

    assert len(calls) == 1
    subject, body = calls[0]
    assert "Nightly Drift" in subject
    assert "1 error" in subject
    assert "20260430" in subject
    assert "acu" in body


# ─────────────────────────────────────────────────────────────────────────────
# 2. Nightly sweep beat task registration
# ─────────────────────────────────────────────────────────────────────────────

def test_nightly_sweep_task_registered() -> None:
    """scrape.nightly_sweep must appear in the Celery task registry."""
    from app.tasks.celery_app import celery_app

    celery_app.loader.import_default_modules()
    registered = {n for n in celery_app.tasks if not n.startswith("celery.")}
    assert "scrape.nightly_sweep" in registered, (
        f"scrape.nightly_sweep not registered. Got: {sorted(registered)}"
    )


def test_nightly_sweep_in_beat_schedule() -> None:
    """nightly-sweep-and-drift-alert must be present in the beat_schedule at 02:00 UTC."""
    from app.tasks.celery_app import celery_app

    schedule = celery_app.conf.beat_schedule or {}
    entry = schedule.get("nightly-sweep-and-drift-alert")
    assert entry is not None, (
        "nightly-sweep-and-drift-alert missing from beat_schedule. "
        "The nightly drift report will never run automatically."
    )
    assert entry["task"] == "scrape.nightly_sweep"
    # Verify it runs at 02:00 UTC (crontab hour=2, minute=0)
    sched = entry["schedule"]
    assert sched.hour == {2}, f"Expected hour=2, got {sched.hour}"
    assert sched.minute == {0}, f"Expected minute=0, got {sched.minute}"


def test_nightly_sweep_returns_skipped_no_baseline() -> None:
    """nightly_sweep_and_alert must return sweep=skipped_no_baseline when no previous
    snapshot directory exists (first run scenario).

    Strategy: patch subprocess.run (capture_baseline) to succeed and patch
    pathlib.Path.iterdir so that the nightly root appears empty, simulating
    a first-ever run where no previous date directory exists.
    """
    import subprocess
    import pathlib
    from app.tasks.scrape_tasks import nightly_sweep_and_alert

    def _fake_run(args, **kwargs):
        result = MagicMock()
        result.returncode = 0
        result.stdout = "Snapshot OK"
        result.stderr = ""
        return result

    _real_iterdir = pathlib.Path.iterdir

    def _fake_iterdir(self):
        # Return empty iterator for the nightly baselines dir so the task
        # sees no previous snapshots and returns sweep=skipped_no_baseline.
        if "nightly" in str(self):
            return iter([])
        return _real_iterdir(self)

    with patch("subprocess.run", side_effect=_fake_run), \
         patch.object(pathlib.Path, "mkdir"), \
         patch.object(pathlib.Path, "iterdir", _fake_iterdir):
        result = nightly_sweep_and_alert()

    assert result.get("sweep") == "skipped_no_baseline", (
        f"Expected sweep=skipped_no_baseline on first run, got: {result}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. Tier-2 subdomain probes in discover_course_links
# ─────────────────────────────────────────────────────────────────────────────

def test_discover_course_links_accepts_discovery_config_param() -> None:
    """discover_course_links must accept a discovery_config kwarg."""
    from app.services.scraper.discovery import discover_course_links

    sig = inspect.signature(discover_course_links)
    assert "discovery_config" in sig.parameters, (
        "discover_course_links is missing the discovery_config parameter — "
        "Tier-2 subdomain probes will never fire."
    )
    # Must be keyword-only with a default of None
    param = sig.parameters["discovery_config"]
    assert param.default is None, (
        f"discovery_config default should be None, got {param.default!r}"
    )


def test_tier2_subdomain_probe_fires_when_low_candidates() -> None:
    """Tier-2 subdomain probe must fire when BFS yields < 5 candidates and
    discovery_config.fallback_subdomains is non-empty."""
    from app.services.scraper.discovery import discover_course_links

    probed_urls: list[str] = []

    async def _fake_fetch_html(url: str, **kwargs) -> str | None:
        # Simulate that the primary origin returns only 2 links (below threshold)
        # and the subdomain returns real course links.
        probed_urls.append(url)
        if "study.myuni.edu.au" in url:
            return """
            <html><body>
              <a href="/course/bachelor-of-science">Bachelor of Science</a>
              <a href="/course/bachelor-of-arts">Bachelor of Arts</a>
              <a href="/course/master-of-engineering">Master of Engineering</a>
            </body></html>
            """
        if "myuni.edu.au" in url:
            # Primary origin returns almost nothing (1 link, below threshold)
            return """
            <html><body>
              <a href="/course/foundation">Foundation Program</a>
            </body></html>
            """
        return None

    class _FakeDiscoveryConfig:
        fallback_subdomains = ["study.{domain}"]

    _SITEMAP = "app.services.scraper.sitemap.discover_from_sitemap"
    _EXPAND = "app.services.scraper.home_page_redirect.expand_course_list_with_categories"
    with patch("app.services.scraper.discovery.fetch_html", side_effect=_fake_fetch_html), \
         patch(_SITEMAP, new_callable=AsyncMock, return_value=[]), \
         patch(_EXPAND, new_callable=AsyncMock, return_value=[]):
        links = asyncio.run(
            discover_course_links(
                "https://www.myuni.edu.au/courses",
                max_pages=1,
                max_courses=20,
                emit=None,
                discovery_config=_FakeDiscoveryConfig(),
            )
        )

    subdomain_probed = any("study.myuni.edu.au" in u for u in probed_urls)
    assert subdomain_probed, (
        f"Expected study.myuni.edu.au to be probed, but probed URLs were: {probed_urls}"
    )


def test_alt_listing_probe_skipped_when_explicit_sitemap_url_configured() -> None:
    """Alt-listing-path probe must NOT fire when the per-uni YAML sets an
    explicit ``discovery.sitemap_url``.

    2026-07-03 (Ulster job_ec86dc5866cb handoff): if the sitemap fetch
    leaves ``found`` below the alt-probe threshold (e.g. a Cloudflare-
    blocked host where the sitemap request itself fails), guessing at 7
    more generic listing paths on the SAME blocked host is pure wasted
    time (~25s each). An explicit sitemap_url means the operator has
    already identified the definitive course-catalogue source, so the
    alt-probe tier should be skipped entirely rather than attempted.
    """
    from app.services.scraper.discovery import discover_course_links

    alt_probed: list[str] = []

    async def _fake_fetch_html(url: str, **kwargs) -> str | None:
        if any(p in url for p in (
            "/our-courses", "/our-programs", "/courses/all",
            "/all-courses", "/study/all",
        )):
            alt_probed.append(url)
            return None
        # Primary page yields nothing — forces `found` below the alt-probe
        # threshold so the ONLY thing gating the alt-probe is sitemap_url.
        return "<html><body></body></html>"

    class _FakeDiscoveryConfig:
        sitemap_url = "https://www.ulster.ac.uk/site-maps/sitemap-courses.xml"

    _SITEMAP = "app.services.scraper.sitemap.discover_from_sitemap"
    _EXPAND = "app.services.scraper.home_page_redirect.expand_course_list_with_categories"
    with patch("app.services.scraper.discovery.fetch_html", side_effect=_fake_fetch_html), \
         patch(_SITEMAP, new_callable=AsyncMock, return_value=[]), \
         patch(_EXPAND, new_callable=AsyncMock, return_value=[]):
        asyncio.run(
            discover_course_links(
                "https://www.ulster.ac.uk/courses",
                max_pages=1,
                max_courses=20,
                emit=None,
                discovery_config=_FakeDiscoveryConfig(),
            )
        )

    assert alt_probed == [], (
        f"Expected no alt-listing-path probes when sitemap_url is configured, "
        f"but probed: {alt_probed}"
    )


def test_tier2_subdomain_probe_skipped_when_enough_candidates() -> None:
    """Tier-2 subdomain probe must NOT fire when BFS already found >= 5 candidates.

    We test the probe decision by patching the full pipeline at a higher level:
    the BFS + sitemap fallback + alt-probe all mocked to return 8 courses, and
    then we verify the subdomain fetch is never attempted.
    """
    from app.services.scraper.discovery import discover_course_links

    subdomain_fetched: list[str] = []
    call_count = 0

    async def _fake_fetch_html(url: str, **kwargs) -> str | None:
        nonlocal call_count
        call_count += 1
        if "handbook.myuni.edu.au" in url:
            subdomain_fetched.append(url)
        # Any URL on the primary domain: return 8 course links (above threshold=5).
        # Use /courses/bachelor-of-X slugs which _looks_like_course() accepts.
        if "myuni.edu.au" in url and "handbook" not in url:
            names = ["science", "arts", "nursing", "engineering", "law", "business", "it", "education"]
            courses = "\n".join(
                f'<a href="/courses/bachelor-of-{n}">Bachelor of {n.title()}</a>'
                for n in names
            )
            return f"<html><body>{courses}</body></html>"
        return None

    class _FakeDiscoveryConfig:
        fallback_subdomains = ["handbook.{domain}"]

    _SITEMAP = "app.services.scraper.sitemap.discover_from_sitemap"
    _EXPAND = "app.services.scraper.home_page_redirect.expand_course_list_with_categories"
    with patch("app.services.scraper.discovery.fetch_html", side_effect=_fake_fetch_html), \
         patch(_SITEMAP, new_callable=AsyncMock, return_value=[]), \
         patch(_EXPAND, new_callable=AsyncMock, return_value=[]):
        asyncio.run(
            discover_course_links(
                "https://www.myuni.edu.au/courses",
                max_pages=1,
                max_courses=20,
                emit=None,
                discovery_config=_FakeDiscoveryConfig(),
            )
        )

    assert not subdomain_fetched, (
        "Tier-2 subdomain probe fired even though primary URL found >= 5 candidates. "
        f"Probed: {subdomain_fetched}"
    )


def test_tier2_apex_domain_strips_www() -> None:
    """Tier-2 probe must expand 'handbook.{domain}' using the apex domain
    (www. stripped), not the raw netloc."""
    from app.services.scraper.discovery import discover_course_links

    probed_urls: list[str] = []

    async def _fake_fetch_html(url: str, **kwargs) -> str | None:
        probed_urls.append(url)
        if "handbook." in url:
            # Return a course link so the probe is counted as useful
            return "<html><body><a href='/course/x'>Course X</a></body></html>"
        # Primary origin returns nothing → triggers subdomain probe
        return "<html><body></body></html>"

    class _FakeDiscoveryConfig:
        fallback_subdomains = ["handbook.{domain}"]

    _SITEMAP = "app.services.scraper.sitemap.discover_from_sitemap"
    _EXPAND = "app.services.scraper.home_page_redirect.expand_course_list_with_categories"
    with patch("app.services.scraper.discovery.fetch_html", side_effect=_fake_fetch_html), \
         patch(_SITEMAP, new_callable=AsyncMock, return_value=[]), \
         patch(_EXPAND, new_callable=AsyncMock, return_value=[]):
        asyncio.run(
            discover_course_links(
                "https://www.biguni.edu.au/courses",
                max_pages=1,
                max_courses=20,
                emit=None,
                discovery_config=_FakeDiscoveryConfig(),
            )
        )

    # Must probe handbook.biguni.edu.au — NOT handbook.www.biguni.edu.au
    correct_probe = any("handbook.biguni.edu.au" in u for u in probed_urls)
    wrong_probe = any("handbook.www.biguni.edu.au" in u for u in probed_urls)
    assert correct_probe, (
        f"Expected handbook.biguni.edu.au to be probed. Got: {probed_urls}"
    )
    assert not wrong_probe, (
        f"www. was NOT stripped — probe hit handbook.www.biguni.edu.au. Got: {probed_urls}"
    )
