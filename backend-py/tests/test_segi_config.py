from __future__ import annotations

import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.scraper.config.loader import load_uni_config


def test_segi_production_config_uses_current_official_catalogue() -> None:
    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
        db_scrape_config={
            "auto_config": {
                "discovery": {
                    "use_wayback": True,
                    "allow_url_patterns": [r"^/programs/dis_.*\\.htm$"],
                }
            }
        },
    )

    assert config.discovery.bfs_page_budget == 1
    assert config.discovery.skip_home_page_redirect is True
    assert config.discovery.skip_sitemap_fallback is True
    assert config.discovery.skip_browser_discovery is True
    assert config.discovery.use_wayback is False
    assert config.discovery.archive_only is False
    assert config.discovery.seed_urls == [
        "https://university.segi.edu.my/site-map/"
    ]
    assert config.discovery.allowed_extra_hostnames == [
        "university.segi.edu.my",
        "www.segi.edu.my",
    ]
    assert config.discovery.segi_wordpress_supplement is True
    assert config.discovery.scrape_do_skip_fallbacks is False
    assert config.discovery.scrape_do_render is False
    assert config.discovery.insecure_tls_direct_hostnames == [
        "university.segi.edu.my"
    ]
    assert config.discovery.allow_url_patterns == [
        r"^https?://university\.segi\.edu\.my/course/[^/?#]+/?$",
        r"^https://www\.segi\.edu\.my/[^/?#]+/$",
    ]
    assert re.search(
        config.discovery.allow_url_patterns[0],
        "https://university.segi.edu.my/course/bachelor-of-psychology-honours/",
    )
    assert not re.search(
        config.discovery.allow_url_patterns[0],
        "https://university.segi.edu.my/course-search/",
    )
    assert re.search(
        config.discovery.allow_url_patterns[1],
        "https://www.segi.edu.my/diploma-in-nursing-pg/",
    )
    assert config.extraction.scrape_do_render is False
    assert config.extraction.scrape_do_render_hostnames == [
        "www.segi.edu.my",
    ]
    assert config.extraction.max_parallel_fetch == 1
    assert config.extraction.per_course_timeout_seconds == 60
    assert config.extraction.html_compaction_enabled is True
    assert config.extraction.scrape_do_wait_for_ms == 500
    assert config.extraction.scrape_do_request_timeout_seconds == 20
    assert config.extraction.scrape_do_render_max_retries == 1
    assert config.extraction.scrape_do_skip_fallbacks is False
    assert config.extraction.staging.require_international_fee is False
    assert config.extraction.staging.stage_on_parser_error is True
    assert config.extraction.fees.default_currency == "MYR"
    assert config.extraction.fees.currency_override == "MYR"


@pytest.mark.asyncio
async def test_segi_course_fetch_uses_exact_host_tls_exception() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        patch.object(
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=200,
                    text="<html>" + ("course " * 200) + "</html>",
                )
            ),
        ) as direct,
        patch.object(
            http_fetcher,
            "fetch_html_scrape_do",
            new=AsyncMock(side_effect=AssertionError("proxy must not run")),
        ) as scrape_do,
        patch.object(
            http_fetcher,
            "fetch_html_cffi",
            new=AsyncMock(side_effect=AssertionError("curl_cffi must not run")),
        ) as cffi,
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/bachelor-of-psychology-honours/"
        )

    assert html
    direct.assert_awaited_once()
    scrape_do.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_tls_exception_failure_never_uses_proxy_or_wayback() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        patch.object(
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                side_effect=httpx.ConnectError(
                    "official host temporarily unavailable"
                )
            ),
        ) as direct,
        patch.object(http_fetcher, "fetch_html_scrape_do", new=AsyncMock()) as scrape_do,
        patch.object(http_fetcher, "fetch_html_wayback", new=AsyncMock()) as wayback,
        patch.object(http_fetcher, "fetch_html_cffi", new=AsyncMock()) as cffi,
        patch.object(http_fetcher.asyncio, "sleep", new=AsyncMock()),
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html is None
    assert direct.await_count == 3
    scrape_do.assert_not_awaited()
    wayback.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_tls_exception_rejects_cross_host_redirect() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        patch.object(
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                return_value=SimpleNamespace(
                    status_code=302,
                    headers={"location": "https://attacker.example/internal"},
                    text="",
                )
            ),
        ) as direct,
        patch.object(http_fetcher, "fetch_html_scrape_do", new=AsyncMock()) as scrape_do,
        patch.object(http_fetcher, "fetch_html_wayback", new=AsyncMock()) as wayback,
        patch.object(http_fetcher, "fetch_html_cffi", new=AsyncMock()) as cffi,
    ):
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html is None
    direct.assert_awaited_once()
    assert http_fetcher.get_last_fetch_failure() == {
        "kind": "unsafe_redirect",
        "reason": (
            "Exact-host TLS exception rejected redirect from "
            "'https://university.segi.edu.my/course/current-course/' "
            "to 'https://attacker.example/internal'."
        ),
        "retryable": False,
        "transport": "direct_insecure_tls",
        "terminal": True,
        "status_code": 302,
    }
    scrape_do.assert_not_awaited()
    wayback.assert_not_awaited()
    cffi.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_tls_exception_allows_same_host_https_redirect() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    final_html = "<html>" + ("course " * 200) + "</html>"

    with patch.object(
        http_fetcher.httpx.AsyncClient,
        "get",
        new=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    status_code=301,
                    headers={"location": "/course/canonical/"},
                    text="",
                ),
                SimpleNamespace(status_code=200, headers={}, text=final_html),
            ]
        ),
    ) as direct:
        html = await http_fetcher.fetch_html(
            "https://university.segi.edu.my/course/current-course/"
        )

    assert html == final_html
    assert [call.args[0] for call in direct.await_args_list] == [
        "https://university.segi.edu.my/course/current-course/",
        "https://university.segi.edu.my/course/canonical/",
    ]


@pytest.mark.asyncio
async def test_segi_tls_exception_does_not_apply_to_other_hosts() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import http_fetcher

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)

    with (
        patch.object(
            http_fetcher.httpx.AsyncClient,
            "get",
            new=AsyncMock(
                side_effect=AssertionError(
                    "one-off insecure client must not run for another host"
                )
            ),
        ) as direct,
        patch.object(
            http_fetcher,
            "_get_shared_client",
            side_effect=RuntimeError("normal verified path reached"),
        ) as shared_client,
    ):
        html = await http_fetcher.fetch_html("https://other.example/course/x/")

    assert html is None
    assert shared_client.call_count == 3
    direct.assert_not_awaited()


@pytest.mark.asyncio
async def test_segi_online_mode_title_skips_expensive_enrichment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper import per_course_vision
    from app.services.scraper.extractors import ai_fallback, gemini_primary
    from app.services.scraper.pipelines.single_course import extract_course

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    html = """
    <html>
      <head><title>Bachelor of Psychology (Honours) – ODL (Online Mode)</title></head>
      <body>
        <main>
          <h1>Bachelor of Psychology (Honours) – ODL (Online Mode)</h1>
          <section><h2>Campus</h2><p>SEGi University</p></section>
          <section><h2>Entry requirements</h2><p>IELTS 5.0 or PTE 36</p></section>
        </main>
      </body>
    </html>
    """

    primary = AsyncMock(side_effect=AssertionError("Gemini primary must be skipped"))
    vision = AsyncMock(side_effect=AssertionError("vision OCR must be skipped"))

    def fail_fallback(*args, **kwargs):
        raise AssertionError("AI fallback must be skipped")

    monkeypatch.setattr(gemini_primary, "extract_primary", primary)
    monkeypatch.setattr(per_course_vision, "maybe_vision_refetch", vision)
    monkeypatch.setattr(ai_fallback, "fill_missing", fail_fallback)
    result = await extract_course(
        "https://university.segi.edu.my/course/bachelor-of-psychology-honours-odl/",
        country="Malaysia",
        html=html,
        use_ai_fallback=True,
    )

    assert result.get("error") is None
    assert result["payload"]["study_mode"] == "Online"
    assert result["payload"]["online_only"] is True
    assert result["payload"]["online_only_segi"] is True
    assert result["payload"]["online_only_authoritative"] is True
    assert result["_perf"]["ai_skipped_online_only"] is True
    assert result["_perf"]["vision_skipped"] is True
    primary.assert_not_awaited()
    vision.assert_not_awaited()
    assert any(
        evidence.get("field_key") == "study_mode"
        and evidence.get("method") == "study_mode:title_keyword"
        and evidence.get("value") == "Online"
        for evidence in result["evidence"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    [
        "Bachelor of Business (Online Mode or On Campus)",
        "Bachelor of Business (Online Learning and On Campus)",
    ],
)
async def test_segi_mixed_title_does_not_take_online_fast_path(title: str) -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines.single_course import extract_course
    from app.services.scraper.guards import should_stage_course

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    events: list[dict] = []

    async def emit(*args, **kwargs):
        events.append(kwargs)

    result = await extract_course(
        "https://university.segi.edu.my/course/bachelor-of-business/",
        country="Malaysia",
        html=f"""
        <html><body>
          <h1>{title}</h1>
          <dl><dt>Study mode</dt><dd>Online or On Campus</dd></dl>
          <dl><dt>Campus</dt><dd>SEGi University</dd></dl>
          <p>International tuition fee: MYR 20,000</p>
        </body></html>
        """,
        use_ai_fallback=False,
        emit=emit,
    )

    # The new fast path must not classify either explicit mixed title as
    # Online-only.  This also proves the title authority was not moved ahead
    # of the existing mixed/on-campus correction.
    assert result["payload"].get("online_only_authoritative") is not True
    assert result["payload"].get("online_only_segi") is not True
    assert result["_perf"].get("ai_skipped_online_only") is not True
    assert not any(event.get("kind") == "online_only_skip" for event in events)

    eligible_payload = dict(result["payload"])
    eligible_payload.update(
        {
            "course_name": title,
            "study_mode": "Blended",
            "international_fee": 20_000,
        }
    )
    accepted, reason = should_stage_course(
        title,
        eligible_payload,
        source_url="https://university.segi.edu.my/course/bachelor-of-business/",
    )
    assert accepted is True, reason


@pytest.mark.asyncio
async def test_segi_rule_online_with_physical_campus_does_not_take_fast_path() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    result = await extract_course(
        "https://university.segi.edu.my/course/bachelor-of-business/",
        country="Malaysia",
        html="""
        <html><body>
          <h1>Bachelor of Business Administration</h1>
          <p>Online course information and resources are available.</p>
          <p>Campus: SEGi University</p>
          <p>International tuition fee: MYR 20,000</p>
        </body></html>
        """,
        use_ai_fallback=False,
    )

    # A broad study_mode:rule Online result is not authoritative.  The
    # physical campus route must remain eligible for the normal pipeline.
    assert result["payload"].get("online_only_authoritative") is not True
    assert result["payload"].get("online_only_segi") is not True
    assert result["_perf"].get("ai_skipped_online_only") is not True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("slug", "score"),
    [
        (
            "master-of-science-in-environmental-sustainability-with-artificial-intelligence",
            5.0,
        ),
        ("master-of-science-pharmaceutical-sciences", 6.0),
    ],
)
async def test_segi_verified_postgraduate_brochure_rules_fill_missing_ielts(
    slug: str,
    score: float,
) -> None:
    """Only explicitly mapped SEGi programmes may use the official brochure.

    These current official pages expose programme entry prose but no numeric
    IELTS value in their HTML.  The linked SEGi Postgraduate Studies brochure
    gives the score for each named programme.  This is intentionally not a
    university-wide default.
    """
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.extractors import english_test

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    html = """
    <html><body>
      <h1>Postgraduate programme</h1>
      <h2>English Requirements</h2>
      <p>MUET Band 4 or equivalent to CEFR Mid B2.</p>
    </body></html>
    """

    results = await english_test.extract(
        html,
        f"https://university.segi.edu.my/course/{slug}/",
    )

    assert [(r.value, r.method) for r in results if r.field_key == "ielts_overall"] == [
        (score, "segi_programme_rule")
    ]


@pytest.mark.asyncio
async def test_segi_brochure_rule_does_not_become_universal_or_rescue_odl() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.extractors import english_test

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    html = "<html><body><p>MUET Band 4 or equivalent to CEFR Mid B2.</p></body></html>"

    # A different university must never inherit SEGi's programme map.
    other_host = await english_test.extract(
        html,
        "https://other.example/course/master-of-accountancy/",
    )
    assert not any(r.field_key == "ielts_overall" for r in other_host)

    # ODL has its own online-only exclusion.  The conventional programme's
    # brochure score must not fill an ODL URL as a side effect.
    odl = await english_test.extract(
        html,
        "https://university.segi.edu.my/course/master-of-accountancy-odl/",
    )
    assert not any(
        r.field_key == "ielts_overall" and r.method == "segi_programme_rule"
        for r in odl
    )


@pytest.mark.asyncio
async def test_segi_college_page_uses_course_owned_metadata_campus() -> None:
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.extractors import location

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    html = """
    <html><head>
      <meta property="og:description"
            content="Programme ID : (R2/1022/4/0033)(06/28) (FA1779)
                     Campus: SEGi College Kuala Lumpur
                     Levels of Study: Diploma">
    </head><body>
      <footer>Visit our campuses in Kota Damansara, Penang and Sarawak.</footer>
    </body></html>
    """

    results = await location.extract(
        html,
        "https://www.segi.edu.my/"
        "diploma-in-occupational-safety-and-health-kl/",
    )

    assert len(results) == 1
    assert results[0].value == "SEGi College Kuala Lumpur"
    assert results[0].method == "location.segi_course_campus"


@pytest.mark.asyncio
async def test_segi_transition_from_university_to_college_keeps_course_owned_english() -> None:
    """The first Colleges result remains extractable after University pages."""
    from app.services.scraper.config import set_uni_config
    from app.services.scraper.pipelines.single_course import extract_course

    config = load_uni_config(
        slug="segi",
        scrape_url="https://www.segi.edu.my/",
        university_id=13,
        name="SEGi University & Colleges",
    )
    set_uni_config(config)
    university = await extract_course(
        "https://university.segi.edu.my/course/master-of-accountancy/",
        country="Malaysia",
        html="<h1>Master of Accountancy</h1><p>Campus: SEGi University</p><p>IELTS 5.0</p>",
        use_ai_fallback=False,
    )
    college = await extract_course(
        "https://www.segi.edu.my/bachelor-of-accounting-and-finance-honours-1/",
        country="Malaysia",
        html=(
            "<html><head><meta property='og:description' content='"
            "Programme ID: ABC Campus: SEGi College Kuala Lumpur "
            "Level of Study: Bachelor Degree'></head><body>"
            "<h1>Bachelor of Accounting and Finance (Honours)</h1>"
            "<h2>English requirements</h2><p>IELTS 5.5</p></body></html>"
        ),
        use_ai_fallback=False,
    )

    assert university["payload"]["ielts_overall"] == 5.0
    assert college["payload"]["ielts_overall"] == 5.5
    assert college["payload"]["course_location"] == "SEGi College Kuala Lumpur"


@pytest.mark.asyncio
async def test_wayback_discovery_uses_configured_cdx_prefix() -> None:
    from app.services.scraper.wayback_discover import wayback_discover

    captured_params: dict[str, str] = {}
    rows = [
        ["original", "timestamp"],
        ["https://www.segi.edu.my/course/master-of-accountancy/", "20250101000000"],
    ]

    class CdxResponse:
        status_code = 200
        text = json.dumps(rows)
        request = httpx.Request("GET", "http://web.archive.org/cdx/search/cdx")

        @staticmethod
        def raise_for_status() -> None:
            return None

    async def fake_cdx_get(self, endpoint_url, **kwargs):
        captured_params.update(kwargs["params"])
        return CdxResponse()

    with patch(
        "app.services.scraper.wayback_discover.httpx.AsyncClient.get",
        new=fake_cdx_get,
    ):
        discovered = await wayback_discover(
            "https://www.segi.edu.my/",
            max_courses=10,
            cdx_url_prefix="www.segi.edu.my/course/*",
        )

    assert captured_params["url"] == "www.segi.edu.my/course/*"
    assert discovered == [
        {
            "url": "https://www.segi.edu.my/course/master-of-accountancy/",
            "name": "",
        }
    ]


@pytest.mark.asyncio
async def test_wayback_discovery_rejects_cross_host_prefix() -> None:
    from app.services.scraper.wayback_discover import wayback_discover

    with patch(
        "app.services.scraper.wayback_discover.httpx.AsyncClient.get",
        side_effect=AssertionError("cross-host prefix must fail before network access"),
    ):
        discovered = await wayback_discover(
            "https://www.segi.edu.my/",
            cdx_url_prefix="other-university.example/course/*",
        )

    assert discovered == []