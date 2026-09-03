"""Task #229 — checkpoint/resume + contention-bounding regression tests.

These lock in the behaviour added to fix large (500+ course) scrapes that
previously died on the hard 45-min Celery ceiling, restarted from course 0, and
stormed the shared Scrape.do/Gemini accounts with 429s.

The tests avoid live Redis/Postgres: the rate limiter's disabled and fail-open
paths are exercised directly, the URL-normaliser and resume filter are pure
Python, the ``_clear_stale_dedup`` SQL-shaping is asserted on the generated
clause, and the Celery time-limit wiring is asserted against ``settings``.
"""
from __future__ import annotations

import asyncio

import pytest

from app.config import settings
from app.services.scraper import rate_limiter
from app.services.scraper.orchestrator import (
    _matched_resume_provenance,
    _normalize_course_url,
)


def _run(coro):
    """Drive a coroutine on a fresh event loop.

    The suite uses a session-scoped asyncio loop (asyncpg pool affinity); a
    standalone loop keeps these no-Redis tests isolated from that pool.
    """
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── URL normalisation (resume-checkpoint matching) ───────────────────────────

def test_normalize_strips_scheme_www_and_trailing_slash():
    a = _normalize_course_url("https://www.uni.edu/course/x/")
    b = _normalize_course_url("http://uni.edu/course/x")
    assert a == b == "uni.edu/course/x"


def test_normalize_preserves_query_string():
    # Some universities key the international-fee view off a query param; those
    # are genuinely distinct course pages and must NOT collapse together.
    intl = _normalize_course_url("https://uni.edu/course/x?international=true")
    dom = _normalize_course_url("https://uni.edu/course/x")
    assert intl != dom
    assert intl == "uni.edu/course/x?international=true"


def test_normalize_handles_none_and_empty():
    assert _normalize_course_url(None) == ""
    assert _normalize_course_url("") == ""
    assert _normalize_course_url("   ") == ""


# ── Resume filter logic (skip already-staged, re-attempt rejected) ───────────

def test_resume_filter_skips_already_staged_keeps_new():
    """Reproduce the run_scrape resume filter against a normalised done-set."""
    done = {
        _normalize_course_url("https://www.uni.edu/a"),
        _normalize_course_url("https://www.uni.edu/b/"),
    }
    links = [
        {"url": "https://uni.edu/a", "name": "A"},      # already staged → skip
        {"url": "http://www.uni.edu/b", "name": "B"},   # already staged → skip
        {"url": "https://uni.edu/c", "name": "C"},      # new → keep
    ]
    remaining = [
        lk for lk in links
        if _normalize_course_url(lk.get("url")) not in done
    ]
    assert [lk["name"] for lk in remaining] == ["C"]


def test_resume_filter_noop_when_nothing_staged():
    done: set[str] = set()
    links = [{"url": "https://uni.edu/a", "name": "A"}]
    remaining = [
        lk for lk in links
        if _normalize_course_url(lk.get("url")) not in done
    ]
    assert remaining == links


def test_resume_provenance_records_only_checkpoint_rows_used_by_current_links():
    links = [
        {"url": "https://uni.edu/course/a/"},
        {"url": "https://uni.edu/course/b"},
        {"url": "https://uni.edu/course/new"},
    ]
    checkpoints = {
        _normalize_course_url("http://www.uni.edu/course/a"): (101, "job_failed_a"),
        _normalize_course_url("https://uni.edu/course/b/"): (102, "job_failed_b"),
        _normalize_course_url("https://uni.edu/course/unrelated"): (
            999,
            "job_unrelated",
        ),
    }

    keys, course_ids, source_job_ids = _matched_resume_provenance(
        links, checkpoints
    )

    assert keys == {
        _normalize_course_url("https://uni.edu/course/a"),
        _normalize_course_url("https://uni.edu/course/b"),
    }
    assert course_ids == [101, 102]
    assert source_job_ids == ["job_failed_a", "job_failed_b"]


# ── _clear_stale_dedup SQL shaping (reviewer-rejection + resume preservation) ─

def test_clear_stale_dedup_preserves_current_and_resumable(monkeypatch):
    """The DELETE must (a) exclude the current job's own rows and (b) exclude
    recently-interrupted resumable jobs when resume is enabled, while never
    touching rejected rows (status='pending' only)."""
    captured: dict = {}

    class _FakeResult:
        rowcount = 0

    class _FakeDB:
        async def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _FakeResult()

        async def commit(self):
            captured["committed"] = True

    from app.services.scraper import orchestrator as orch

    monkeypatch.setattr(settings, "scrape_resume_enabled", True, raising=False)
    _run(orch._clear_stale_dedup(_FakeDB(), 42, current_job_id="job_abc"))

    sql = captured["sql"]
    assert "sc.status = 'pending'" in sql          # rejected rows untouched
    assert "sc.scrape_job_id <> :cur_job" in sql   # current job preserved
    assert "'queued', 'failed', 'stopped'" in sql  # resumable jobs preserved
    assert captured["params"]["cur_job"] == "job_abc"
    assert captured["params"]["uid"] == 42


def test_clear_stale_dedup_preserves_manually_stopped_jobs(monkeypatch):
    """Manual-stop checkpoints must survive: a deliberately STOPPED run's pending
    rows must be preserved (within the resume window) so a re-trigger resumes
    instead of restarting from course 0.  Regression for the manual-stop gap."""
    captured: dict = {}

    class _FakeResult:
        rowcount = 0

    class _FakeDB:
        async def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _FakeResult()

        async def commit(self):
            pass

    from app.services.scraper import orchestrator as orch

    monkeypatch.setattr(settings, "scrape_resume_enabled", True, raising=False)
    _run(orch._clear_stale_dedup(_FakeDB(), 99))
    sql = captured["sql"]
    # All three interrupted statuses (timeout=failed, restart=queued,
    # manual=stopped) must be in the preservation clause.
    assert "'queued', 'failed', 'stopped'" in sql
    # The window bound must be applied so genuinely-old leftovers are still wiped.
    assert "updated_at > NOW() - (:rw || ' minutes')::interval" in sql
    assert captured["params"]["rw"] == str(settings.scrape_resume_window_minutes)


def test_clear_stale_dedup_skips_resumable_clause_when_disabled(monkeypatch):
    captured: dict = {}

    class _FakeResult:
        rowcount = 0

    class _FakeDB:
        async def execute(self, stmt, params):
            captured["sql"] = str(stmt)
            captured["params"] = params
            return _FakeResult()

        async def commit(self):
            pass

    from app.services.scraper import orchestrator as orch

    monkeypatch.setattr(settings, "scrape_resume_enabled", False, raising=False)
    _run(orch._clear_stale_dedup(_FakeDB(), 7))
    sql = captured["sql"]
    assert "('queued', 'failed')" not in sql       # resume clause omitted
    assert "sc.status = 'pending'" in sql


# ── Rate limiter: disabled + fail-open paths ─────────────────────────────────

def test_rate_limiter_disabled_returns_true_without_redis():
    # rate <= 0 must short-circuit before any Redis connect.
    assert _run(rate_limiter.acquire("scrape_do", 0)) is True
    assert _run(rate_limiter.acquire("gemini", 0.0)) is True
    assert _run(rate_limiter.acquire("x", -5)) is True


def test_rate_limiter_fail_open_on_unreachable_redis(monkeypatch):
    # A positive rate with an unreachable Redis URL must still proceed (fail
    # open) — a Redis outage can never block scraping.
    monkeypatch.setattr(
        settings, "redis_url", "redis://127.0.0.1:6390/0", raising=False
    )
    monkeypatch.setattr(rate_limiter, "_MAX_WAIT_S", 1.0, raising=False)
    assert _run(rate_limiter.acquire("scrape_do", 5.0)) is True


def test_acquire_helpers_are_disabled_by_default():
    # gemini_rate_limit_per_sec ships off (0.0) by default.
    # scrape_do_rate_limit_per_sec ships ENABLED (3.0/sec) as of the QMUL
    # job_4fb674e585b2 fix (2026-07-06): cross-process semaphores alone
    # cannot bound Scrape.do contention across 8 prefork Celery workers
    # sharing one account, so the fleet-wide Redis token bucket must be on
    # by default to smooth bursts. Either way, `acquire_scrape_do()` never
    # raises and always returns True (real or fail-open pass-through).
    assert settings.gemini_rate_limit_per_sec == 0.0
    assert settings.scrape_do_rate_limit_per_sec > 0.0
    assert _run(rate_limiter.acquire_scrape_do()) is True
    assert _run(rate_limiter.acquire_gemini()) is True


# ── Celery time-limit wiring ─────────────────────────────────────────────────

def test_celery_time_limits_sourced_from_settings():
    from app.tasks.celery_app import celery_app

    assert celery_app.conf.task_soft_time_limit == settings.scrape_task_soft_time_limit_s
    assert celery_app.conf.task_time_limit == settings.scrape_task_hard_time_limit_s
    # Hard limit must exceed soft limit so SIGKILL only follows an unhandled
    # SoftTimeLimitExceeded.
    assert settings.scrape_task_hard_time_limit_s > settings.scrape_task_soft_time_limit_s


def test_default_ceiling_is_raised_above_old_45min():
    # Regression guard: the old hardcoded soft limit was 2700s (45 min) which
    # killed healthy large scrapes. The new default must be materially higher.
    assert settings.scrape_task_soft_time_limit_s >= 7200
