"""Tests for the coursehandbook.mq.edu.au sitemap-based MQ discovery path.

Pins the regex contract, year filter, and the early-return floor.  The
network-bound `_discover_from_coursehandbook_sitemap` function itself is
exercised only when ``MQ_LIVE_TEST=1`` (mirrors the existing live-test
convention in test_mq_browser_discover.py); CI must not hit the real
host.
"""
from __future__ import annotations

import asyncio
import html
import json
import os
import pytest

from app.services.scraper import mq_browser_discover as mq


class TestRenderedPageData:
    def _body(self) -> tuple[dict, str]:
        program = {
            "course_name": "Bachelor of International Studies",
            "fees": [{"student_type": "International", "amount": 42000}],
            "offering": [{"location": "North Ryde"}],
        }
        outer = {
            "result": {
                "data": {
                    "current": {
                        "fields": {
                            "json": json.dumps(program),
                        }
                    }
                }
            }
        }
        return program, json.dumps(outer)

    def test_rich_result_uses_page_data_study_level_and_duration(self):
        result = mq._build_scrapy_result(
            "Bachelor of Arts",
            "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts",
            {},
            {
                "study_level": "Undergraduate",
                "course_duration_in_years": {"label": "Full time: 3 years"},
            },
        )

        assert result["payload"]["degree_level"] == "Undergraduate"
        assert result["payload"]["academic_level"] == "Undergraduate"
        assert result["payload"]["duration"] == 3.0
        assert result["payload"]["duration_term"] == "year"
        methods = {item["method"] for item in result["evidence"]}
        assert "page_data:study_level" in methods
        assert "page_data:course_duration_in_years" in methods

    def test_funnelback_study_level_and_duration_keep_priority(self):
        result = mq._build_scrapy_result(
            "Master of Test",
            "https://www.mq.edu.au/study/find-a-course/courses/master-of-test",
            {
                "studyLevel": "Postgraduate",
                "courseDuration": "2 years",
            },
            {
                "study_level": "Undergraduate",
                "course_duration_in_years": {"label": "Full time: 3 years"},
            },
        )

        assert result["payload"]["degree_level"] == "Postgraduate"
        assert result["payload"]["duration"] == 2.0
        assert result["payload"]["duration_term"] == "year"
        methods = {item["method"] for item in result["evidence"]}
        assert "page_data:study_level" not in methods
        assert "page_data:course_duration_in_years" not in methods

    def test_rich_result_maps_only_international_offering_sessions_to_intakes(self):
        result = mq._build_scrapy_result(
            "Bachelor of Arts",
            "https://www.mq.edu.au/study/find-a-course/courses/bachelor-of-arts",
            {},
            {
                "offering": [
                    {
                        "student_types": ["Domestic students"],
                        "admission_calendar": "Session 3",
                        "location": "Off-campus",
                    },
                    {
                        "student_types": [
                            "International students studying within Australia on a visa",
                            "Domestic students",
                        ],
                        "admission_calendar": "Session 2",
                        "location": "North Ryde",
                    },
                    {
                        "student_types": [
                            "International students studying within Australia on a visa",
                        ],
                        "admission_calendar": "Session 1",
                        "location": "North Ryde",
                    },
                    {
                        "student_types": ["International students"],
                        "admission_calendar": "Session 2",
                        "location": "North Ryde",
                    },
                ]
            },
        )

        assert result["payload"]["intake_months"] == ["February", "July"]
        intake_evidence = [
            item for item in result["evidence"]
            if item["field_key"] == "intake_months"
        ]
        assert len(intake_evidence) == 1
        assert (
            intake_evidence[0]["method"]
            == "page_data:offering.admission_calendar"
        )

    def test_rich_result_leaves_intakes_blank_without_international_offering(self):
        result = mq._build_scrapy_result(
            "Domestic Test Course",
            "https://www.mq.edu.au/study/find-a-course/courses/domestic-test",
            {},
            {
                "offering": [
                    {
                        "student_types": ["Domestic students"],
                        "admission_calendar": "Session 1",
                        "location": "North Ryde",
                    }
                ]
            },
        )

        assert "intake_months" not in result["payload"]

    def test_extracts_plain_page_data_json(self):
        program, body = self._body()
        assert mq._extract_program_from_page_data(body) == program

    def test_extracts_chromium_wrapped_page_data_json(self):
        program, body = self._body()
        wrapped = (
            '<html><head><meta name="color-scheme" content="light dark">'
            '<meta charset="utf-8"></head><body><pre>'
            f"{html.escape(body)}</pre>"
            '<div class="json-formatter-container"></div></body></html>'
        )
        assert mq._extract_program_from_page_data(wrapped) == program

    def test_rejects_arbitrary_html_around_page_data(self):
        _program, body = self._body()
        wrapped = f"<html><body><div>untrusted</div><pre>{html.escape(body)}</pre></body></html>"
        assert mq._extract_program_from_page_data(wrapped) == {}


class TestCurrentAdmissionsUrl:
    def test_year_stamped_url_uses_current_course_route(self):
        assert mq._canonical_mq_admissions_url(
            "https://www.mq.edu.au/study/find-a-course/courses/2026/"
            "bachelor-of-psychology/"
        ) == (
            "https://www.mq.edu.au/study/find-a-course/courses/"
            "bachelor-of-psychology"
        )

    def test_current_url_is_unchanged(self):
        assert mq._canonical_mq_admissions_url(
            "https://www.mq.edu.au/study/find-a-course/courses/"
            "bachelor-of-psychology/"
        ) == (
            "https://www.mq.edu.au/study/find-a-course/courses/"
            "bachelor-of-psychology"
        )


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
    """Pin the current Funnelback-first tiering without live network calls."""

    @pytest.mark.asyncio
    async def test_returns_early_when_funnelback_yields_enough(
        self, monkeypatch,
    ):
        fake_links = [
            {"url": f"https://coursehandbook.mq.edu.au/2026/courses/C{i:06d}",
             "name": ""}
            for i in range(1, 301)
        ]

        async def fake_funnelback(emit, *, max_courses):
            return fake_links[:max_courses]

        monkeypatch.setattr(
            mq, "_discover_from_funnelback_api", fake_funnelback,
        )

        async def _fail_sitemap(*a, **kw):
            raise AssertionError(
                "Sitemap ran despite Funnelback returning a complete catalogue"
            )

        monkeypatch.setattr(
            mq, "_discover_from_coursehandbook_sitemap", _fail_sitemap,
        )

        emits: list[str] = []

        async def emit(kind, msg=None, **kw):
            emits.append(f"[{kind}] {msg}")

        result = await mq.browser_discover_mq(emit=emit, max_courses=300)

        assert len(result) == 300
        assert all(
            u["url"].startswith("https://coursehandbook.mq.edu.au/")
            for u in result
        )
        assert not any(
            "starting browser sweep across" in m for m in emits
        ), f"Widget sweep should NOT have started; emits: {emits}"

    @pytest.mark.asyncio
    async def test_falls_through_when_sitemap_returns_too_few(
        self, monkeypatch,
    ):
        async def fake_funnelback(emit, *, max_courses):
            return []

        # 19 URLs is under the floor of 20 → fall through to widget sweep.
        async def fake_sitemap(emit, *, max_courses):
            return [
                {"url": f"https://coursehandbook.mq.edu.au/2026/courses/C{i:06d}",
                 "name": ""}
                for i in range(19)
            ]

        async def fake_search_page(emit, *, max_courses):
            return []

        monkeypatch.setattr(
            mq, "_discover_from_funnelback_api", fake_funnelback,
        )
        monkeypatch.setattr(
            mq, "_discover_from_coursehandbook_sitemap", fake_sitemap,
        )
        monkeypatch.setattr(
            mq, "_discover_from_search_page", fake_search_page,
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

        # The widget sweep entered, then returned the structured partial
        # catalogue rather than discarding useful URLs.
        assert result == [
            {"url": f"https://coursehandbook.mq.edu.au/2026/courses/C{i:06d}",
             "name": ""}
            for i in range(19)
        ]
        assert any(
            "starting browser sweep across" in m for m in emits
        ), f"Widget sweep should have started; emits: {emits}"


class TestFunnelbackRichProvider:
    @pytest.mark.asyncio
    async def test_uses_rendered_transport_maps_fee_and_filters_subdegrees(
        self, monkeypatch,
    ):
        import html
        import httpx
        import json
        import app.services.scraper.http_fetcher as http_fetcher

        results = [
            {
                "title": f"Bachelor of Test {i}",
                "liveUrl": (
                    "https://www.mq.edu.au/study/find-a-course/"
                    f"courses/course-{i}"
                ),
                "metaData": {
                    "studyLevel": "Undergraduate",
                    "courseDuration": "3 years",
                },
            }
            for i in range(48)
        ]
        results.extend([
            {
                "title": "Major in Test",
                "liveUrl": (
                    "https://www.mq.edu.au/study/find-a-course/"
                    "courses/major/test"
                ),
                "metaData": {},
            },
            {
                "title": "Specialisation in Test",
                "liveUrl": (
                    "https://www.mq.edu.au/study/find-a-course/"
                    "courses/specialisation/test"
                ),
                "metaData": {},
            },
        ])
        funnelback_body = json.dumps({
            "response": {"resultPacket": {"results": results}},
        })
        page_data_body = json.dumps({
            "result": {
                "data": {
                    "current": {
                        "fields": {
                            "json": json.dumps({
                                "study_level": "Undergraduate",
                                "course_duration_in_years": {
                                    "label": "Full time: 3 years",
                                },
                                "fees": [{
                                    "fee_type": {"label": "International"},
                                    "estimated_annual_fee": "43200",
                                }],
                                "ielts_overall_score": "6.5",
                            }),
                        },
                    },
                },
            },
        })
        calls = []
        active_rendered = 0
        peak_rendered = 0

        async def fake_scrape_do(url, **kwargs):
            nonlocal active_rendered, peak_rendered
            calls.append((url, kwargs))
            if "s/search.json" in url:
                return (
                    "<html><head></head><body>"
                    '<pre style="word-wrap: break-word">'
                    f"{html.escape(funnelback_body)}"
                    "</pre></body></html>"
                )
            active_rendered += 1
            peak_rendered = max(peak_rendered, active_rendered)
            try:
                await asyncio.sleep(0)
                return page_data_body
            finally:
                active_rendered -= 1

        class FakeResponse:
            status_code = 403
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return FakeResponse()

        monkeypatch.setattr(
            http_fetcher, "fetch_html_scrape_do", fake_scrape_do,
        )
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(mq, "_RICH_COURSE_MIN_RESULTS", 1)

        async def emit(*args, **kwargs):
            return None

        links = await mq._discover_from_funnelback_api(
            emit, max_courses=100,
        )

        assert len(links) == 48
        assert 1 < peak_rendered <= mq._PAGE_DATA_RENDER_PARALLEL == 4
        assert all(call[1]["render"] is True for call in calls)
        page_data_calls = [call for call in calls if "s/search.json" not in call[0]]
        assert all(call[1]["rate_limit"] is True for call in page_data_calls)
        assert all(call[1]["max_retries"] == 0 for call in page_data_calls)
        assert all(
            "/major/" not in link["url"]
            and "/specialisation/" not in link["url"]
            for link in links
        )
        payload = links[0]["scrapy_result"]["payload"]
        assert payload["international_fee"] == 43200.0
        assert payload["ielts_overall"] == 6.5
        assert payload["degree_level"] == "Undergraduate"
        assert payload["duration"] == 3.0
        assert payload["duration_term"] == "year"

    @pytest.mark.asyncio
    async def test_paginates_past_funnelback_two_hundred_result_cap(
        self, monkeypatch,
    ):
        import httpx
        import json
        import app.services.scraper.http_fetcher as http_fetcher

        requested_starts = []
        page_data_body = json.dumps({
            "result": {
                "data": {
                    "current": {
                        "fields": {
                            "json": json.dumps({
                                "fees": [{
                                    "fee_type": {"label": "International"},
                                    "estimated_annual_fee": "43200",
                                }],
                            }),
                        },
                    },
                },
            },
        })

        def make_results(start, count):
            return [
                {
                    "title": f"Bachelor of Test {i}",
                    "liveUrl": (
                        "https://www.mq.edu.au/study/find-a-course/"
                        f"courses/course-{i}"
                    ),
                    "metaData": {},
                }
                for i in range(start, start + count)
            ]

        async def fake_scrape_do(url, **kwargs):
            if "s/search.json" not in url:
                return page_data_body
            if "start_rank=1&" in url:
                requested_starts.append(1)
                rows = make_results(0, 200)
            elif "start_rank=201&" in url:
                requested_starts.append(201)
                rows = make_results(200, 175)
            else:
                raise AssertionError(f"Unexpected Funnelback page: {url}")
            return json.dumps({
                "response": {"resultPacket": {"results": rows}},
            })

        class FakeResponse:
            status_code = 403
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return FakeResponse()

        async def emit(*args, **kwargs):
            return None

        monkeypatch.setattr(
            http_fetcher, "fetch_html_scrape_do", fake_scrape_do,
        )
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        links = await mq._discover_from_funnelback_api(
            emit, max_courses=500,
        )

        assert requested_starts == [1, 201]
        assert len(links) == 375

    @pytest.mark.asyncio
    async def test_fails_closed_when_page_data_enrichment_is_missing(
        self, monkeypatch,
    ):
        import httpx
        import json
        import app.services.scraper.http_fetcher as http_fetcher

        rows = [
            {
                "title": f"Bachelor of Test {i}",
                "liveUrl": (
                    "https://www.mq.edu.au/study/find-a-course/"
                    f"courses/course-{i}"
                ),
                "metaData": {},
            }
            for i in range(50)
        ]

        async def fake_scrape_do(url, **kwargs):
            if "s/search.json" in url:
                return json.dumps({
                    "response": {"resultPacket": {"results": rows}},
                })
            return None

        class FakeResponse:
            status_code = 403
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return FakeResponse()

        async def emit(*args, **kwargs):
            return None

        monkeypatch.setattr(
            http_fetcher, "fetch_html_scrape_do", fake_scrape_do,
        )
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(mq, "_RICH_COURSE_MIN_RESULTS", 1)

        with pytest.raises(mq.MqEnrichmentCoverageError, match="page-data coverage"):
            await mq._discover_from_funnelback_api(emit, max_courses=100)

    @pytest.mark.asyncio
    async def test_fails_closed_when_international_fee_coverage_is_low(
        self, monkeypatch,
    ):
        import httpx
        import app.services.scraper.http_fetcher as http_fetcher

        rows = [
            {
                "title": f"Bachelor of Test {i}",
                "liveUrl": (
                    "https://www.mq.edu.au/study/find-a-course/"
                    f"courses/course-{i}"
                ),
                "metaData": {},
            }
            for i in range(50)
        ]
        page_data_without_fee = json.dumps({
            "result": {
                "data": {
                    "current": {
                        "fields": {
                            "json": json.dumps({
                                "study_level": "Undergraduate",
                            }),
                        },
                    },
                },
            },
        })

        async def fake_scrape_do(url, **kwargs):
            if "s/search.json" in url:
                return json.dumps({
                    "response": {"resultPacket": {"results": rows}},
                })
            return page_data_without_fee

        class FakeResponse:
            status_code = 403
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return FakeResponse()

        async def emit(*args, **kwargs):
            return None

        monkeypatch.setattr(
            http_fetcher, "fetch_html_scrape_do", fake_scrape_do,
        )
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
        monkeypatch.setattr(mq, "_RICH_COURSE_MIN_RESULTS", 1)

        with pytest.raises(
            mq.MqEnrichmentCoverageError,
            match="international-fee coverage",
        ):
            await mq._discover_from_funnelback_api(emit, max_courses=100)

    @pytest.mark.asyncio
    async def test_fails_closed_when_second_funnelback_page_is_unreachable(
        self, monkeypatch,
    ):
        import httpx
        import json
        import app.services.scraper.http_fetcher as http_fetcher

        rows = [
            {
                "title": f"Bachelor of Test {i}",
                "liveUrl": (
                    "https://www.mq.edu.au/study/find-a-course/"
                    f"courses/course-{i}"
                ),
                "metaData": {},
            }
            for i in range(200)
        ]

        async def fake_scrape_do(url, **kwargs):
            if "start_rank=1&" in url:
                return json.dumps({
                    "response": {"resultPacket": {"results": rows}},
                })
            return None

        class FakeResponse:
            status_code = 403
            text = ""

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return FakeResponse()

        async def emit(*args, **kwargs):
            return None

        monkeypatch.setattr(
            http_fetcher, "fetch_html_scrape_do", fake_scrape_do,
        )
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        with pytest.raises(
            mq.MqEnrichmentCoverageError,
            match=r"page 2 .* was unreachable",
        ):
            await mq._discover_from_funnelback_api(emit, max_courses=500)

    @pytest.mark.asyncio
    async def test_reports_render_failure_when_direct_fallback_is_challenge(
        self, monkeypatch,
    ):
        import httpx
        import app.services.scraper.http_fetcher as http_fetcher

        async def fake_scrape_do(url, **kwargs):
            return None

        def fake_last_failure():
            return {
                "kind": "scrape_do_unavailable",
                "reason": "SECRET_TOKEN must never appear",
                "retryable": True,
                "transport": "scrape_do_render",
                "status_code": 200,
            }

        class FakeResponse:
            status_code = 200
            text = "<html><head><title>Just a moment...</title></head></html>"

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, *args, **kwargs):
                return FakeResponse()

        async def emit(*args, **kwargs):
            return None

        monkeypatch.setattr(
            http_fetcher, "fetch_html_scrape_do", fake_scrape_do,
        )
        monkeypatch.setattr(
            http_fetcher, "get_last_fetch_failure", fake_last_failure,
        )
        monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

        with pytest.raises(mq.MqEnrichmentCoverageError) as raised:
            await mq._discover_from_funnelback_api(emit, max_courses=500)

        message = str(raised.value)
        assert (
            "rendered Scrape.do transport returned an empty or suspiciously "
            "short HTTP 200 response"
        ) in message
        assert "direct HTTP fallback returned an anti-bot challenge" in message
        assert "Refusing to supplement with domestic-default HTML" in message
        assert "SECRET_TOKEN" not in message
        assert "search.json?" not in message


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
