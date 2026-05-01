"""Tests for the three new features implemented in session:

  1. Tier-7 operator alert (discovery_failure_alerts table + alert delivery)
  2. Nightly sweep beat task registration
  3. Tier-2 per-uni subdomain probe in discover_course_links
"""
from __future__ import annotations

import importlib
import inspect
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch


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
