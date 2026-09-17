import json
import re

import httpx
import pytest

from app.services.scraper.algolia_provider import (
    fetch_algolia_links,
    merge_algolia_payload,
)
from app.services.scraper.config.schema import AlgoliaDiscoveryConfig
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper import orchestrator
from app.services.scraper.extractors import fee
from app.services.scraper.orchestrator import _link_matches_post_discovery_allow


def _load_op(db_scrape_config=None):
    return load_uni_config(
        slug="op",
        scrape_url="https://www.op.ac.nz",
        university_id=68,
        name="Otago Polytechnic",
        db_scrape_config=db_scrape_config,
    )


def _provider_payload(**overrides):
    payload = {
        "_provider": "algolia",
        "_course_name": "Bachelor of Applied Management (Accounting)",
        "_source_url": "https://mqp0hwtgj8-dsn.algolia.net/1/indexes/opmarketing_live_Page/query",
        "objectClassName": "App\\Pages\\ProgrammeInfoPage",
        "Duration": {
            "domestic": {"partTime": {"number": 6, "unit": "Years"}},
            "international": {"fullTime": {"number": 3, "unit": "Years"}},
        },
        "Intake": {
            "domestic": ["March"],
            "international": ["February", "July", "September"],
        },
        "Delivery": {
            "domestic": ["Online"],
            "international": ["On campus"],
        },
        "Locations": {
            "domestic": ["Distance"],
            "international": ["Dunedin"],
        },
    }
    payload.update(overrides)
    return payload


def test_op_tracked_recipe_replaces_stale_probe_defaults():
    cfg = _load_op({
        "auto_config": {
            "_auto_generated": True,
            "_strategy": "search_api",
            "discovery": {"allow_url_patterns": [r"/study/"]},
            "extraction": {"fees": {"default_currency": "USD"}},
        }
    })

    assert cfg.discovery.algolia.index_name == "opmarketing_live_Page"
    assert cfg.discovery.algolia.url_field == "objectLink"
    assert cfg.discovery.algolia.name_field == "objectTitle"
    assert cfg.discovery.algolia.required_field_values == {
        "objectClassName": "App\\Pages\\ProgrammeInfoPage"
    }
    assert cfg.discovery.expected_min_courses == 120
    assert cfg.discovery.use_wayback is False
    assert cfg.discovery.skip_sitemap_fallback is True
    assert cfg.extraction.staging.skip_degree_qualifier_check is True
    assert cfg.extraction.fees.default_currency == "NZD"
    assert cfg.extraction.fees.currency_override == "NZD"


def test_op_allows_only_typed_nzqa_detail_route():
    cfg = _load_op()
    patterns = [
        re.compile(pattern, re.IGNORECASE)
        for pattern in cfg.discovery.allow_url_patterns
    ]

    assert _link_matches_post_discovery_allow(
        {
            "url": (
                "https://www.op.ac.nz/programmes/nzqa/"
                "new-zealand-certificate-in-mechanical-engineering-level-3"
            )
        },
        patterns,
    )
    for url in (
        "https://www.op.ac.nz/programmes",
        "https://www.op.ac.nz/study/adventure",
        "https://www.op.ac.nz/study/animal-healthcare/veterinary-nursing",
        "https://www.op.ac.nz/study/business/accounting",
        "https://www.op.ac.nz/programmes/short-course/example",
    ):
        assert not _link_matches_post_discovery_allow({"url": url}, patterns)


def test_op_algolia_international_metadata_overrides_domestic_page_state():
    result = merge_algolia_payload(
        {
            "payload": {
                "duration": 6,
                "duration_term": "Years",
                "study_load": "Part Time",
                "study_mode": "Online",
                "course_location": "Distance",
                "intake_months": ["March"],
            },
            "evidence": [],
        },
        _provider_payload(),
        url="https://www.op.ac.nz/programmes/nzqa/bachelor-of-applied-management-accounting-2",
    )

    assert result["payload"] | {
        "duration": 3,
        "duration_term": "Years",
        "study_load": "Full Time",
        "study_mode": "On Campus",
        "course_location": "Dunedin",
        "intake_months": ["February", "July", "September"],
    } == result["payload"]
    assert result["payload"].get("domestic_only") is not True
    assert result["payload"].get("online_only") is not True


def test_op_algolia_marks_programmes_without_international_offering_domestic_only():
    result = merge_algolia_payload(
        {"payload": {"international_fee": 9695}, "evidence": []},
        _provider_payload(
            Duration={"domestic": {"fullTime": {"number": 1, "unit": "Year"}}, "international": []},
            Intake={"domestic": ["February"], "international": []},
            Delivery={"domestic": ["On campus"], "international": []},
            Locations={"domestic": ["Dunedin"], "international": []},
        ),
        url="https://www.op.ac.nz/programmes/nzqa/domestic-only-programme",
    )

    assert result["payload"]["domestic_only"] is True


def test_op_algolia_marks_international_online_only_programmes():
    result = merge_algolia_payload(
        {"payload": {}, "evidence": []},
        _provider_payload(
            Duration={"international": {"partTime": {"number": 14, "unit": "Weeks"}}},
            Intake={"international": ["Flexible"]},
            Delivery={"international": ["Online"]},
            Locations={"international": ["Online"]},
        ),
        url="https://www.op.ac.nz/programmes/nzqa/online-programme",
    )

    assert result["payload"]["study_load"] == "Part Time"
    assert result["payload"]["study_mode"] == "Online"
    assert result["payload"]["online_only"] is True


def test_op_algolia_partial_metadata_does_not_invent_mode_or_load():
    result = merge_algolia_payload(
        {
            "payload": {
                "study_load": "Full Time",
                "study_mode": "Blended",
            },
            "evidence": [],
        },
        _provider_payload(
            Duration={"international": {}},
            Intake={"international": ["February"]},
            Delivery={"international": []},
            Locations={"international": []},
        ),
        url="https://www.op.ac.nz/programmes/nzqa/partial-programme",
    )

    assert result["payload"]["study_load"] == "Full Time"
    assert result["payload"]["study_mode"] == "Blended"


@pytest.mark.asyncio
async def test_op_algolia_filters_route_and_record_type_before_emitting(
    monkeypatch,
):
    hits = [
        {
            "objectTitle": "Master of Architecture",
            "objectLink": "https://www.op.ac.nz/programmes/nzqa/master-of-architecture",
            "objectClassName": "App\\Pages\\ProgrammeInfoPage",
        },
        {
            "objectTitle": "Adventure",
            "objectLink": "https://www.op.ac.nz/study/adventure",
            "objectClassName": "App\\Pages\\StudyAreaPage",
        },
        {
            "objectTitle": "Misrouted programme record",
            "objectLink": "https://www.op.ac.nz/study/misrouted",
            "objectClassName": "App\\Pages\\ProgrammeInfoPage",
        },
        {
            "objectTitle": "Wrong type on NZQA route",
            "objectLink": "https://www.op.ac.nz/programmes/nzqa/wrong-type",
            "objectClassName": "Page",
        },
    ]

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"nbHits": len(hits), "nbPages": 1, "hits": hits},
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )

    async def emit(*_args, **_kwargs):
        return None

    cfg = AlgoliaDiscoveryConfig(
        app_id="EXAMPLE",
        api_key="public-search-key",
        index_name="pages",
        url_field="objectLink",
        name_field="objectTitle",
        payload_fields=["objectClassName"],
        allow_url_patterns=[
            r"^https?://(?:www\.)?op\.ac\.nz/programmes/nzqa/[^/?#]+/?$"
        ],
        required_field_values={
            "objectClassName": "App\\Pages\\ProgrammeInfoPage"
        },
    )

    links = await fetch_algolia_links(cfg, emit)

    assert [link["url"] for link in links] == [
        "https://www.op.ac.nz/programmes/nzqa/master-of-architecture"
    ]
    assert links[0]["payload"]["objectClassName"] == (
        "App\\Pages\\ProgrammeInfoPage"
    )


def test_op_algolia_payload_survives_timeout_recovery_queue():
    provider = _provider_payload(
        Duration={"domestic": {"fullTime": {"number": 1, "unit": "Year"}}, "international": []},
        Intake={"domestic": ["February"], "international": []},
        Delivery={"domestic": ["On campus"], "international": []},
        Locations={"domestic": ["Dunedin"], "international": []},
    )
    link = {
        "name": "Domestic-only programme",
        "url": "https://www.op.ac.nz/programmes/nzqa/domestic-only-programme",
        "payload": provider,
    }

    timeout_result = orchestrator._per_course_timeout_result(link, 30)
    sweep_links: list[dict] = []
    assert orchestrator._queue_recovery_sweep_candidate(
        timeout_result,
        counter="fetch_failed",
        links=sweep_links,
        url_keys=set(),
    )
    assert sweep_links[0]["payload"] == provider

    recovered = merge_algolia_payload(
        {"payload": {"international_fee": 9695}, "evidence": []},
        sweep_links[0]["payload"],
        url=sweep_links[0]["url"],
    )
    assert recovered["payload"]["domestic_only"] is True


@pytest.mark.asyncio
async def test_op_fee_parser_selects_standard_international_full_course_amount():
    html = """
    <div class="programme-fee-grid">
      <div class="programme-fee-box">
        <div><h3>Domestic fees</h3>
          <div class="programme-fee-boxes">
            <div>Full tuition</div><div>Standard</div>
            <div class="programme-fee">$4,021</div>
          </div>
        </div>
      </div>
      <div class="programme-fee-box">
        <div><h3>International fees</h3>
          <div class="programme-fee-boxes">
            <div>Full tuition</div><div>With scholarship applied</div>
            <div class="programme-fee">$11,800</div>
          </div>
          <div class="programme-fee-boxes">
            <div>Full tuition</div><div>Standard</div>
            <div class="programme-fee">$13,800</div>
          </div>
        </div>
      </div>
    </div>
    """

    results = await fee.extract(
        html,
        "https://www.op.ac.nz/programmes/nzqa/"
        "new-zealand-certificate-in-bicycle-servicing-level-3",
        country="New Zealand",
    )

    assert len(results) == 1
    assert results[0].normalized == {
        "international_fee": 13800.0,
        "currency": "NZD",
        "fee_term": "Full Course",
        "fee_year": None,
    }
    assert results[0].method == "fee.otago_polytechnic_international_card"


@pytest.mark.asyncio
async def test_op_fee_parser_marks_first_year_amount_as_annual():
    html = """
    <div><h3>International fees</h3>
      <div class="programme-fee-boxes">
        <div>First year</div><div>With scholarship applied</div>
        <div class="programme-fee">$23,400</div>
      </div>
      <div class="programme-fee-boxes">
        <div>First year</div><div>Standard</div>
        <div class="programme-fee">$26,900</div>
      </div>
      <div class="programme-fee-boxes">
        <div>Second year</div><div>Standard</div>
        <div class="programme-fee">$26,900</div>
      </div>
    </div>
    """

    results = await fee.extract(
        html,
        "https://www.op.ac.nz/programmes/nzqa/bachelor-of-construction",
        country="New Zealand",
    )

    assert results[0].normalized["international_fee"] == 26900.0
    assert results[0].normalized["fee_term"] == "Annual"


@pytest.mark.asyncio
async def test_op_fee_parser_uses_first_standard_doctorate_course_amount():
    html = """
    <div><h3>International fees</h3>
      <div class="programme-fee-boxes">
        <div>First year</div><div>With scholarship applied</div>
        <div class="programme-fee">$26,600</div>
      </div>
      <div class="programme-fee-boxes">
        <div>Course one</div><div>Standard</div>
        <div class="programme-fee">$29,600</div>
      </div>
      <div class="programme-fee-boxes">
        <div>Course two</div><div>Standard</div>
        <div class="programme-fee">$29,600</div>
      </div>
    </div>
    """

    results = await fee.extract(
        html,
        "https://www.op.ac.nz/programmes/nzqa/doctor-of-professional-practice",
        country="New Zealand",
    )

    assert results[0].normalized["international_fee"] == 29600.0
    assert results[0].normalized["fee_term"] == "Annual"