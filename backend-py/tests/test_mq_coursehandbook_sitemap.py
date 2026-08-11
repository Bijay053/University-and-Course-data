"""Tests for the coursehandbook.mq.edu.au sitemap-based MQ discovery path.

Pins the regex contract, year filter, and the early-return floor.  The
network-bound `_discover_from_coursehandbook_sitemap` function itself is
exercised only when ``MQ_LIVE_TEST=1`` (mirrors the existing live-test
convention in test_mq_browser_discover.py); CI must not hit the real
host.
"""
from __future__ import annotations

import os
import pytest

from app.services.scraper import mq_browser_discover as mq


class TestCoursehandbookRegexContract:
    """The /YYYY/courses/CXXXXXX shape is the ONLY thing we want to
    harvest from the handbook sitemap.  Units, areas-of-study, and
    double-degree URLs must NEVER match."""

    def test_matches_real_course_url_4digit_year_6digit_id(self):
        m = mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/2026/courses/C000001"
        )
        assert m is not None
        assert m.group(1) == "2026"

    def test_matches_real_course_url_with_trailing_slash(self):
        m = mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/2027/courses/C000352/"
        )
        assert m is not None
        assert m.group(1) == "2027"

    def test_rejects_unit_url(self):
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/2026/units/MATH1378"
        ) is None

    def test_rejects_aos_url(self):
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/2026/aos/N000003"
        ) is None

    def test_rejects_doubledegree_url(self):
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/2026/doubledegree/D000002"
        ) is None

    def test_rejects_wrong_host(self):
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://www.mq.edu.au/2026/courses/C000001"
        ) is None

    def test_rejects_http_scheme(self):
        # Belt-and-suspenders: handbook serves HTTPS only; reject plain
        # HTTP variants so a misconfigured scraper can't bypass TLS.
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "http://coursehandbook.mq.edu.au/2026/courses/C000001"
        ) is None

    def test_rejects_extra_path_segments(self):
        # Real course pages are flat: /YYYY/courses/CXXX (no trailing
        # subpages like /units or /requirements).  Reject these so a
        # sitemap drift can't silently inflate the harvest.
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/2026/courses/C000001/units"
        ) is None

    def test_rejects_two_digit_year(self):
        assert mq._COURSEHANDBOOK_COURSE_RE.match(
            "https://coursehandbook.mq.edu.au/26/courses/C000001"
        ) is None


class TestYearFilter:
    """The year set must contain today's year + next year (rolling
    window).  Older years (2020-2024) are still served by the handbook
    but represent expired offerings we don't want to stage."""

    def test_includes_this_year(self):
        import datetime as dt
        assert str(dt.date.today().year) in mq._COURSEHANDBOOK_YEARS

    def test_includes_next_year(self):
        import datetime as dt
        assert str(dt.date.today().year + 1) in mq._COURSEHANDBOOK_YEARS

    def test_excludes_two_years_ago(self):
        import datetime as dt
        assert str(dt.date.today().year - 2) not in mq._COURSEHANDBOOK_YEARS

    def test_only_three_years_total(self):
        # Window: previous + current + next year.  Including the previous
        # year recovers ~50-80 courses that are in the 2025 sitemap but
        # not yet re-published for 2026; duplicates deduplicate in the
        # resolver.  No further creep beyond these three years.
        assert len(mq._COURSEHANDBOOK_YEARS) == 3


class TestSitemapIndexUrl:
    """Hardcoded handbook index URL — pin so a future refactor can't
    silently re-point it."""

    def test_index_url_is_handbook_host(self):
        assert mq._COURSEHANDBOOK_SITEMAP_INDEX == (
            "https://coursehandbook.mq.edu.au/sitemap.xml"
        )


class TestEarlyReturnFloor:
    """browser_discover_mq tries the sitemap path first; when it
    returns ≥20 URLs, the widget sweep must be skipped entirely.  We
    can't easily inject a fake sitemap without a network mock, so this
    test patches `_discover_from_coursehandbook_sitemap` directly."""

    @pytest.mark.asyncio
    async def test_returns_early_when_sitemap_yields_enough(
        self, monkeypatch,
    ):
        fake_links = [
            {"url": f"https://coursehandbook.mq.edu.au/2026/courses/C{i:06d}",
             "name": ""}
            for i in range(1, 51)
        ]

        async def fake_sitemap(emit, *, max_courses):
            return fake_links[:max_courses]

        monkeypatch.setattr(
            mq, "_discover_from_coursehandbook_sitemap", fake_sitemap,
        )

        # Sentinel: if the widget sweep runs, it tries to import the
        # browser_pool — replace with a guard that fails the test.
        def _fail_import(*a, **kw):
            raise AssertionError(
                "Widget sweep ran despite sitemap returning ≥20 links"
            )

        # The widget sweep imports browser_pool AFTER the floor check,
        # so we can let the import live but assert by counting emits.
        emits: list[str] = []

        async def emit(kind, msg=None, **kw):
            emits.append(f"[{kind}] {msg}")

        result = await mq.browser_discover_mq(emit=emit, max_courses=300)

        assert len(result) == 50
        assert all(
            u["url"].startswith("https://coursehandbook.mq.edu.au/")
            for u in result
        )
        # Widget sweep emits "starting browser sweep across ... seed(s)"
        # — its absence proves we short-circuited.
        assert not any(
            "starting browser sweep across" in m for m in emits
        ), f"Widget sweep should NOT have started; emits: {emits}"

    @pytest.mark.asyncio
    async def test_falls_through_when_sitemap_returns_too_few(
        self, monkeypatch,
    ):
        # 19 URLs is under the floor of 20 → fall through to widget sweep.
        async def fake_sitemap(emit, *, max_courses):
            return [
                {"url": f"https://coursehandbook.mq.edu.au/2026/courses/C{i:06d}",
                 "name": ""}
                for i in range(19)
            ]

        monkeypatch.setattr(
            mq, "_discover_from_coursehandbook_sitemap", fake_sitemap,
        )

        # Force the widget-sweep code path to bail immediately so we
        # don't need a live browser, but prove it WAS entered by
        # observing the seed-start emit.  Replace pool.page with a
        # context manager whose __aenter__ raises.
        class _FailingCM:
            async def __aenter__(self):
                raise RuntimeError("simulated browser pool failure")

            async def __aexit__(self, *exc):
                return False

        import app.services.scraper.browser_pool as bp
        monkeypatch.setattr(
            bp.pool, "page", lambda *a, **kw: _FailingCM(), raising=False,
        )

        emits: list[str] = []

        async def emit(_evt, _msg=None, **kw):
            emits.append(str(_msg))

        result = await mq.browser_discover_mq(emit=emit, max_courses=300)

        # The widget sweep entered (we see its banner) and bailed → []
        assert result == []
        assert any(
            "starting browser sweep across" in m for m in emits
        ), f"Widget sweep should have started; emits: {emits}"


@pytest.mark.skipif(
    os.environ.get("MQ_LIVE_TEST") != "1",
    reason="MQ live test requires MQ_LIVE_TEST=1 (hits real network)",
)
class TestLiveCoursehandbookSitemap:
    """Network-bound end-to-end test — only runs with MQ_LIVE_TEST=1."""

    @pytest.mark.asyncio
    async def test_live_sitemap_returns_real_courses(self):
        emits = []

        async def emit(kind, msg=None, **kw):
            emits.append(msg)

        links = await mq._discover_from_coursehandbook_sitemap(
            emit, max_courses=400,
        )
        assert len(links) >= 50, f"expected 50+ links, got {len(links)}"
        for L in links[:5]:
            assert L["url"].startswith(
                "https://coursehandbook.mq.edu.au/"
            )
            assert "/courses/C" in L["url"]
