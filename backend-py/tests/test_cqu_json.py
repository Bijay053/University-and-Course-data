"""Regression coverage for CQU's deterministic structured-data fast path."""

from __future__ import annotations

import html
import json
from typing import Any
from unittest.mock import patch

import pytest

from app.services.scraper.config.context import set_uni_config
from app.services.scraper.config.loader import get_config_for_host
from app.services.scraper.extractors import cqu_json
from app.services.scraper.guards import should_stage_course


def _course_schema_html(
    prerequisites: str = "",
    *,
    audience_type: str | None = None,
) -> str:
    data = {
        "@context": "https://schema.org",
        "@type": "Course",
        "name": "Bachelor of Example",
        "coursePrerequisites": html.escape(prerequisites),
    }
    if audience_type is not None:
        data["audience"] = {"audienceType": audience_type}
    return (
        "<html><head><title>Bachelor of Example - CQUniversity</title>"
        f'<script type="application/ld+json">{json.dumps(data)}</script>'
        "</head><body></body></html>"
    )


def _aims_html(aims: dict) -> str:
    data = {
        "props": {
            "pageProps": {
                "layoutData": {
                    "sitecore": {
                        "context": {
                            "route": {
                                "fields": {
                                    "customRouteContent": {
                                        "value": {"AIMSData": aims}
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    return f'<script id="__NEXT_DATA__">{json.dumps(data)}</script>'


def test_generic_finder_shell_is_not_course_data() -> None:
    shell = (
        "<html><head><title>Find a Course - CQUniversity</title></head>"
        "<body><h1>Find a Course</h1></body></html>"
    )

    assert cqu_json.is_generic_course_finder_shell(shell) is True
    assert cqu_json.has_course_structured_data(shell) is False


def test_course_schema_prevents_false_shell_detection() -> None:
    page = _course_schema_html(
        "IELTS Academic overall band score of at least 6.5"
    )

    assert cqu_json.has_course_structured_data(page) is True
    assert cqu_json.is_generic_course_finder_shell(page) is False


def test_schema_english_applies_without_aims_data() -> None:
    page = _course_schema_html(
        "IELTS Academic overall band score of at least 6.5 and "
        "no individual band lower than 6.0. "
        "PTE Academic overall score of at least 58. "
        "TOEFL iBT Requires 79 or better overall."
    )
    payload: dict = {}
    evidence: list[dict] = []

    applied = cqu_json.apply_overrides(
        payload,
        page,
        url="https://www.cqu.edu.au/courses/cc00/bachelor-of-example",
        evidence=evidence,
    )

    assert payload["ielts_overall"] == 6.5
    assert payload["ielts_listening"] == 6.0
    assert payload["pte_overall"] == 58.0
    assert payload["toefl_overall"] == 79.0
    assert {"ielts", "pte_overall", "toefl_overall"} <= applied.keys()
    assert {row["field_key"] for row in evidence} == {
        "ielts_overall",
        "pte_overall",
        "toefl_overall",
    }


def test_aims_domestic_classification_requires_explicit_flag_pair() -> None:
    assert cqu_json.is_domestic_only(
        cqu_json.parse_aims_data(
            _aims_html({"is_international": False, "is_domestic": True})
        )
        or {}
    )
    assert not cqu_json.is_domestic_only(
        cqu_json.parse_aims_data(
            _aims_html({"is_international": False})
        )
        or {}
    )


def test_course_schema_domestic_audience_overrides_misleading_aims_flags() -> None:
    page = (
        _course_schema_html(audience_type="DOMESTIC")
        + _aims_html(
            {
                "is_international": True,
                "is_domestic": True,
                "availabilities": [
                    {
                        "locations": [
                            {
                                "location": "Rockhampton",
                                "is_international": True,
                            }
                        ]
                    }
                ],
            }
        )
    )
    payload: dict = {}
    evidence: list[dict] = []

    applied = cqu_json.apply_overrides(
        payload,
        page,
        url="https://www.cqu.edu.au/courses/cc12/bachelor-of-education-primary",
        evidence=evidence,
    )

    assert cqu_json.is_domestic_only(
        cqu_json.parse_aims_data(page) or {},
        cqu_json.parse_course_schema(page),
    )
    assert payload["domestic_only"] is True
    assert applied["domestic_only"]["new"] is True
    assert any(
        row["field_key"] == "domestic_only"
        and row["method"] == "cqu_json:schema_org_audience"
        and row["confidence"] == 1.0
        for row in evidence
    )
    should_stage, reason = should_stage_course(
        "Bachelor of Education (Primary)",
        payload,
        "https://www.cqu.edu.au/courses/cc12/bachelor-of-education-primary",
    )
    assert should_stage is False
    assert reason == "domestic_only"


def test_course_schema_international_audience_overrides_domestic_aims_flags() -> None:
    page = (
        _course_schema_html(audience_type="INTERNATIONAL")
        + _aims_html({"is_international": False, "is_domestic": True})
    )
    payload: dict = {}
    evidence: list[dict] = []

    applied = cqu_json.apply_overrides(
        payload,
        page,
        url="https://www.cqu.edu.au/courses/cc00/international-example",
        evidence=evidence,
    )

    assert not cqu_json.is_domestic_only(
        cqu_json.parse_aims_data(page) or {},
        cqu_json.parse_course_schema(page),
    )
    assert "domestic_only" not in payload
    assert "domestic_only" not in applied
    assert not any(
        row.get("field_key") == "domestic_only"
        for row in evidence
    )


def test_course_meta_domestic_overrides_misleading_aims_flags() -> None:
    page = (
        '<html><head><meta name="studentType" content="DOMESTIC" '
        'data-next-head=""/></head><body>'
        + _aims_html(
            {
                "is_international": True,
                "is_domestic": True,
                "availabilities": [
                    {
                        "locations": [
                            {
                                "location": "Rockhampton",
                                "is_international": True,
                            }
                        ]
                    }
                ],
            }
        )
        + "</body></html>"
    )
    payload: dict = {}
    evidence: list[dict] = []

    applied = cqu_json.apply_overrides(
        payload,
        page,
        url="https://www.cqu.edu.au/courses/cc12/bachelor-of-education-primary",
        evidence=evidence,
    )

    assert cqu_json.is_domestic_only(
        cqu_json.parse_aims_data(page) or {},
        cqu_json.parse_course_schema(page),
        html=page,
    )
    assert payload["domestic_only"] is True
    assert applied["domestic_only"]["new"] is True
    assert any(
        row["field_key"] == "domestic_only"
        and row["method"] == "cqu_json:student_type_meta"
        and row["confidence"] == 1.0
        for row in evidence
    )


def test_aims_online_only_sets_online_mode_without_weakening_gate() -> None:
    page = _aims_html(
        {
            "availabilities": [
                {
                    "is_international": True,
                    "term_year_begin_date": "2027-03-01",
                    "locations": [
                        {"location": "Online", "is_international": True}
                    ],
                }
            ]
        }
    )
    payload: dict = {}

    cqu_json.apply_overrides(
        payload,
        page,
        url="https://www.cqu.edu.au/courses/cc00/online-example",
    )

    assert payload["study_mode"] == "Online"
    assert payload["course_location"] == "Online"
    should_stage, reason = should_stage_course(
        "Graduate Certificate of Online Example",
        payload,
        "https://www.cqu.edu.au/courses/cc00/online-example",
    )
    assert should_stage is False
    assert reason == "online_only"


@pytest.mark.asyncio
async def test_finder_shell_recovers_bare_structured_page_without_remote_fallbacks() -> None:
    cfg = get_config_for_host(
        hostname="www.cqu.edu.au",
        name="CQUniversity",
        scrape_url="https://www.cqu.edu.au",
        university_id=22,
        create_missing_stub=False,
    )
    set_uni_config(cfg)
    aims = {
        "is_international": True,
        "is_domestic": True,
        "availabilities": [
            {
                "is_international": True,
                "term_year_begin_date": "2027-03-01",
                "locations": [
                    {"location": "Rockhampton", "is_international": True}
                ],
            }
        ],
        "fees": {"2027": {"IFYF": {"amount": 12000}}},
        "duration": {"full_time_years": 3},
        "english_proficiency_text": (
            "IELTS Academic overall band score of at least 6.5 and no "
            "individual band lower than 6.0. PTE Academic overall score "
            "of at least 58. TOEFL iBT Requires 79 or better overall."
        ),
    }
    bare_page = (
        "<html><head><title>Bachelor of Example - CQUniversity</title></head>"
        "<body><h1>Bachelor of Example</h1><main>"
        "<p>Duration 1 year full-time.</p>"
        "<p>International tuition fee AUD 9999 per year.</p>"
        "<p>Study at Sydney. IELTS 7.5. PTE Academic 76. TOEFL iBT 105.</p>"
        + ("Course information. " * 40)
        + "</main>"
        + _aims_html(aims)
        + "</body></html>"
    )
    finder_shell = (
        "<html><head><title>Find a Course - CQUniversity</title></head>"
        "<body><h1>Find a Course</h1></body></html>"
    )
    fetch_calls: list[str] = []

    async def _bare_fetch(url: str, *args: Any, **kwargs: Any) -> str:
        fetch_calls.append(url)
        return bare_page

    async def _must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("CQU remote fallback must not run")

    async def _emit(*args: Any, **kwargs: Any) -> None:
        return None

    with (
        patch(
            "app.services.scraper.pipelines.single_course.fetch_html",
            side_effect=_bare_fetch,
        ),
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.extractors.gemini_primary.extract_primary",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.extractors.ai_fallback.fill_missing",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.per_course_vision.maybe_vision_refetch",
            side_effect=_must_not_run,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.cqu.edu.au/courses/cc00/bachelor-of-example",
            html=finder_shell,
            country="Australia",
            emit=_emit,
        )

    assert fetch_calls == [
        "https://www.cqu.edu.au/courses/cc00/bachelor-of-example"
    ]
    assert result.get("error") is None
    assert result["payload"]["course_name"] == "Bachelor of Example"
    assert result["payload"]["course_website"] == (
        "https://www.cqu.edu.au/courses/cc00/bachelor-of-example"
    )
    assert result["payload"]["international_fee"] == 12000
    assert result["payload"]["course_location"] == "Rockhampton"
    assert result["payload"]["ielts_overall"] == 6.5
    assert result["payload"]["pte_overall"] == 58
    assert result["payload"]["toefl_overall"] == 79
    cqu_evidence = [
        row for row in result["evidence"]
        if str(row.get("method") or "").startswith("cqu_json:")
    ]
    assert cqu_evidence
    assert {
        row["source_url"] for row in cqu_evidence
    } == {"https://www.cqu.edu.au/courses/cc00/bachelor-of-example"}


@pytest.mark.asyncio
async def test_explicit_aims_domestic_only_exits_before_remote_fallbacks() -> None:
    cfg = get_config_for_host(
        hostname="www.cqu.edu.au",
        name="CQUniversity",
        scrape_url="https://www.cqu.edu.au",
        university_id=22,
        create_missing_stub=False,
    )
    set_uni_config(cfg)
    page = (
        "<html><head><title>Associate Degree of Example</title></head>"
        "<body><h1>Associate Degree of Example</h1>"
        + _aims_html({"is_international": False, "is_domestic": True})
        + "</body></html>"
    )

    async def _must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("domestic-only CQU course must exit immediately")

    with (
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.extractors.gemini_primary.extract_primary",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.extractors.ai_fallback.fill_missing",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.per_course_vision.maybe_vision_refetch",
            side_effect=_must_not_run,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.cqu.edu.au/courses/cc01/associate-degree-of-example",
            html=page,
            country="Australia",
        )

    assert result["payload"]["domestic_only"] is True


@pytest.mark.asyncio
async def test_schema_domestic_only_exits_before_remote_fallbacks() -> None:
    cfg = get_config_for_host(
        hostname="www.cqu.edu.au",
        name="CQUniversity",
        scrape_url="https://www.cqu.edu.au",
        university_id=22,
        create_missing_stub=False,
    )
    set_uni_config(cfg)
    page = (
        "<html><head><title>Bachelor of Education (Primary)</title></head>"
        "<body><h1>Bachelor of Education (Primary)</h1>"
        + _course_schema_html(audience_type="DOMESTIC")
        + "</body></html>"
    )

    async def _must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("schema domestic-only course must exit immediately")

    with (
        patch(
            "app.services.scraper.browser_pool.pool.fetch_html",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.extractors.gemini_primary.extract_primary",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.extractors.ai_fallback.fill_missing",
            side_effect=_must_not_run,
        ),
        patch(
            "app.services.scraper.per_course_vision.maybe_vision_refetch",
            side_effect=_must_not_run,
        ),
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.cqu.edu.au/courses/cc12/bachelor-of-education-primary",
            html=page,
            country="Australia",
        )

    assert result["payload"]["domestic_only"] is True
    assert result["payload"].get("international_fee") is None


@pytest.mark.asyncio
async def test_finder_shell_does_not_adopt_bare_page_without_course_json() -> None:
    cfg = get_config_for_host(
        hostname="www.cqu.edu.au",
        name="CQUniversity",
        scrape_url="https://www.cqu.edu.au",
        university_id=22,
        create_missing_stub=False,
    )
    set_uni_config(cfg)
    finder_shell = (
        "<html><head><title>Find a Course - CQUniversity</title></head>"
        "<body><h1>Find a Course</h1></body></html>"
    )
    bare_without_course_json = (
        "<html><head><title>Bachelor of Wrongly Adopted</title></head>"
        "<body><h1>Bachelor of Wrongly Adopted</h1>"
        + ("Generic page text. " * 40)
        + "</body></html>"
    )

    async def _bare_fetch(url: str, *args: Any, **kwargs: Any) -> str:
        return bare_without_course_json

    async def _emit(*args: Any, **kwargs: Any) -> None:
        return None

    with patch(
        "app.services.scraper.pipelines.single_course.fetch_html",
        side_effect=_bare_fetch,
    ):
        from app.services.scraper.pipelines.single_course import extract_course

        result = await extract_course(
            "https://www.cqu.edu.au/courses/cc00/bachelor-of-example",
            html=finder_shell,
            country="Australia",
            emit=_emit,
        )

    assert result["payload"]["course_name"] == "Find A Course"
    assert result["payload"]["course_website"].endswith(
        "?audience=INTERNATIONAL"
    )


@pytest.mark.asyncio
async def test_aims_online_only_remains_rejected_after_full_pipeline() -> None:
    cfg = get_config_for_host(
        hostname="www.cqu.edu.au",
        name="CQUniversity",
        scrape_url="https://www.cqu.edu.au",
        university_id=22,
        create_missing_stub=False,
    )
    set_uni_config(cfg)
    aims = {
        "is_international": True,
        "is_domestic": True,
        "availabilities": [
            {
                "is_international": True,
                "term_year_begin_date": "2027-03-01",
                "locations": [
                    {"location": "Online", "is_international": True}
                ],
            }
        ],
        "fees": {"2027": {"IFYF": {"amount": 22000}}},
        "duration": {"full_time_years": 1},
        "english_proficiency_text": (
            "IELTS Academic overall band score of at least 6.5."
        ),
    }
    page = (
        "<html><head><title>Graduate Certificate of Online Example</title></head>"
        "<body><h1>Graduate Certificate of Online Example</h1><main>"
        "<p>Study online or at the Rockhampton campus.</p>"
        + ("Course information. " * 40)
        + "</main>"
        + _aims_html(aims)
        + "</body></html>"
    )

    async def _emit(*args: Any, **kwargs: Any) -> None:
        return None

    from app.services.scraper.pipelines.single_course import extract_course

    result = await extract_course(
        "https://www.cqu.edu.au/courses/cc00/online-example",
        html=page,
        country="Australia",
        emit=_emit,
    )

    assert result["payload"]["study_mode"] == "Online"
    assert result["payload"]["course_location"] == "Online"
    should_stage, reason = should_stage_course(
        result["payload"]["course_name"],
        result["payload"],
        result["payload"]["course_website"],
    )
    assert should_stage is False
    assert reason == "online_only"