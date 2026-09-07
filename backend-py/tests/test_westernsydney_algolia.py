import json
import re

import httpx
import pytest

from app.services.scraper.algolia_provider import (
    _wsu_english_values,
    fetch_algolia_links,
    merge_algolia_payload,
)
from app.services.scraper.config.schema import AlgoliaDiscoveryConfig
from app.services.scraper.config.loader import load_uni_config
from app.services.scraper.orchestrator import (
    _link_matches_post_discovery_allow,
    _link_matches_post_discovery_block,
)


WSU_URL = (
    "https://www.westernsydney.edu.au/future/study/courses/"
    "postgraduate/master-of-example"
)


def test_wsu_config_preserves_authoritative_algolia_fields() -> None:
    config = load_uni_config(
        slug="westernsydney",
        name="Western Sydney University",
        scrape_url="https://www.westernsydney.edu.au/future/study/courses",
        create_missing_stub=False,
    )

    assert config.discovery.algolia is not None
    assert config.discovery.algolia.payload_fields == [
        "internationalFees",
        "duration",
        "intakeSessions",
        "campuses",
        "cricosCode",
    ]


def test_algolia_research_link_bypasses_stale_post_discovery_filters() -> None:
    research = {
        "url": "https://www.westernsydney.edu.au/future/study/courses/research/master-of-research",
        "payload": {"_provider": "algolia", "internationalFees": "To be advised."},
    }
    block = [re.compile(r"/research/")]
    allow = [re.compile(r"/undergraduate/")]

    assert not _link_matches_post_discovery_block(research, block)
    assert _link_matches_post_discovery_allow(research, allow)


@pytest.mark.parametrize(
    "link",
    [
        {
            "url": "https://www.westernsydney.edu.au/future/study/courses/research/master-of-research",
            "name": "BFS fallback",
        },
        {
            "url": "https://www.westernsydney.edu.au/future/study/courses/research/master-of-research",
            "name": "Targeted retry",
        },
    ],
)
def test_non_provider_research_links_keep_post_discovery_filters(link: dict) -> None:
    block = [re.compile(r"/research/")]
    allow = [re.compile(r"/undergraduate/")]

    assert _link_matches_post_discovery_block(link, block)
    assert not _link_matches_post_discovery_allow(link, allow)


def test_wsu_algolia_overrides_domestic_fee_and_noisy_ai_duration() -> None:
    result = {
        "payload": {
            "international_fee": None,
            "duration": 5,
            "duration_term": "Years",
            "course_location": None,
        },
        "evidence": [
            {
                "field_key": "duration",
                "value": 5,
                "method": "openai_fallback",
                "decision_status": "selected",
            }
        ],
    }
    provider = {
        "_provider": "algolia",
        "_course_name": "Master of Example",
        "_source_url": "https://example-dsn.algolia.net/1/indexes/courses/query",
        "internationalFees": "AUD $38,833\u00a0",
        "duration": "Full Time: 1.5 Years (Available Part Time)*",
        "intakeSessions": [
            {"name": "Autumn 2027", "startDate": "01 March 2027"},
            {"name": "Spring 2027", "startDate": "19 July 2027"},
        ],
        "campuses": ["Parramatta City", "Online", "Parramatta City"],
        "cricosCode": "012345A",
    }

    merged = merge_algolia_payload(result, provider, url=WSU_URL)
    payload = merged["payload"]

    assert payload["international_fee"] == 38_833
    assert payload["fee_currency"] == "AUD"
    assert payload["fee_term"] == "Annual"
    assert payload["duration"] == 1.5
    assert payload["duration_term"] == "Years"
    assert payload["intake_months"] == ["March", "July"]
    assert payload["course_location"] == "Parramatta City"
    assert payload["cricos_code"] == "012345A"
    assert payload["ielts_overall"] == 6.5
    assert payload["ielts_listening"] == 6.0
    assert payload["pte_overall"] == 58
    assert payload["toefl_overall"] == 82
    assert result["evidence"][0]["decision_status"] == "superseded"
    methods = {row["method"] for row in merged["evidence"]}
    assert "algolia:internationalFees" in methods
    assert "algolia:duration" in methods
    fee_evidence = next(
        row for row in merged["evidence"]
        if row["method"] == "algolia:internationalFees"
    )
    assert fee_evidence["page_type"] == "api"
    assert fee_evidence["source_url"].endswith("/indexes/courses/query")
    assert fee_evidence["source_url"] != WSU_URL


def test_wsu_algolia_does_not_replace_existing_location_or_cricos() -> None:
    result = {
        "payload": {
            "course_location": "Bankstown City",
            "cricos_code": "999999Z",
        },
        "evidence": [],
    }
    provider = {
        "_provider": "algolia",
        "_course_name": "Master of Example",
        "campuses": ["Campbelltown"],
        "cricosCode": "012345A",
    }

    merged = merge_algolia_payload(result, provider, url=WSU_URL)

    assert merged["payload"]["course_location"] == "Bankstown City"
    assert merged["payload"]["cricos_code"] == "999999Z"


def test_wsu_named_english_exception_and_existing_score_authority() -> None:
    result = {
        "payload": {
            "course_name": "Master of Teaching (Primary)",
            "ielts_overall": 8.0,
        },
        "evidence": [],
    }
    provider = {
        "_provider": "algolia",
        "_course_name": "Master of Teaching (Primary)",
    }

    merged = merge_algolia_payload(result, provider, url=WSU_URL)

    assert merged["payload"]["ielts_overall"] == 8.0
    assert merged["payload"]["ielts_listening"] == 8.0
    assert merged["payload"]["ielts_reading"] == 7.0
    assert merged["payload"]["pte_overall"] == 78
    assert merged["payload"]["toefl_overall"] == 105


@pytest.mark.parametrize(
    ("course_name", "profile", "ielts", "pte", "toefl"),
    [
        ("Bachelor of Nursing", "nursing", 7.0, 65, 94),
        (
            "Bachelor of Occupational Therapy (Honours)",
            "allied_health",
            7.0,
            65,
            94,
        ),
        (
            "Bachelor of Clinical Science (Medicine)/Doctor of Medicine",
            "medicine",
            7.0,
            65,
            100,
        ),
        ("Bachelor of Education (Primary)", "teaching", 7.5, 78, 105),
        (
            "Bachelor of Criminal and Community Justice/Bachelor of Social Work",
            "social_work",
            7.0,
            65,
            94,
        ),
        (
            "Master of Professional Psychology",
            "psychology",
            7.0,
            65,
            100,
        ),
        ("Master of Business Administration", "standard", 6.5, 58, 82),
    ],
)
def test_wsu_official_english_profiles(
    course_name: str,
    profile: str,
    ielts: float,
    pte: int,
    toefl: int,
) -> None:
    matched = _wsu_english_values(course_name, None)

    assert matched is not None
    values, matched_profile = matched
    assert matched_profile == profile
    assert values["ielts_overall"] == ielts
    assert values["pte_overall"] == pte
    assert values["toefl_overall"] == toefl


def test_wsu_pathway_does_not_inherit_standard_english_profile() -> None:
    result = {
        "payload": {
            "course_name": "University Foundation Studies",
            "degree_level": "Foundation",
        },
        "evidence": [],
    }

    merged = merge_algolia_payload(
        result,
        {
            "_provider": "algolia",
            "_course_name": "University Foundation Studies",
        },
        url=WSU_URL,
    )

    assert "ielts_overall" not in merged["payload"]


def test_algolia_payload_is_ignored_for_other_hosts() -> None:
    result = {"payload": {"duration": 2}, "evidence": []}

    merged = merge_algolia_payload(
        result,
        {"_provider": "algolia", "duration": "Full Time: 5 Years"},
        url="https://example.edu/course",
    )

    assert merged["payload"]["duration"] == 2


@pytest.mark.asyncio
async def test_algolia_fetch_carries_configured_provider_payload(
    monkeypatch,
) -> None:
    seen_body = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "nbHits": 1,
                "nbPages": 1,
                "hits": [{
                    "title": "Master of Example",
                    "coursePageUrl": WSU_URL,
                    "internationalFees": "AUD $40,000",
                    "duration": "Full Time: 2 Years",
                }],
            },
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=transport, **kwargs),
    )
    emitted = []

    async def emit(*args, **kwargs) -> None:
        emitted.append((args, kwargs))

    config = AlgoliaDiscoveryConfig(
        app_id="EXAMPLE",
        api_key="public-search-key",
        index_name="courses",
        payload_fields=["internationalFees", "duration"],
    )
    links = await fetch_algolia_links(config, emit)

    assert seen_body["attributesToRetrieve"] == [
        "coursePageUrl",
        "title",
        "internationalFees",
        "duration",
    ]
    assert links == [{
        "name": "Master of Example",
        "url": WSU_URL,
        "payload": {
            "_provider": "algolia",
            "_course_name": "Master of Example",
            "_source_url": (
                "https://EXAMPLE-dsn.algolia.net/1/indexes/courses/query"
            ),
            "internationalFees": "AUD $40,000",
            "duration": "Full Time: 2 Years",
        },
    }]