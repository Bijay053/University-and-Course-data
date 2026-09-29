"""Regression coverage for ARU's browser-mode merge exception."""
from __future__ import annotations

import asyncio

from app.services.scraper import per_course_browser
from app.services.scraper.extractors.base import ExtractionResult
from app.services.scraper.pipelines.single_course import (
    _is_aru_browser_keyword_online,
)


ARU_URL = "https://www.aru.ac.uk/study/postgraduate/banking-and-finance-mba"
HEADER_LOCATION = {
    "field_key": "course_location",
    "value": "Chelmsford",
    "method": "location.aru_course_header",
}
GENERIC_BROWSER_ONLINE = {
    "field_key": "study_mode",
    "value": "Online",
    "method": "per_course_browser_extended",
    "source_method": "study_mode:rule",
}


def test_aru_physical_header_ignores_only_generic_browser_online_keyword():
    assert _is_aru_browser_keyword_online(
        ARU_URL, GENERIC_BROWSER_ONLINE, [HEADER_LOCATION]
    )


def test_aru_location_online_is_not_physical_evidence():
    online_location = {**HEADER_LOCATION, "value": "Online"}
    assert not _is_aru_browser_keyword_online(
        ARU_URL, GENERIC_BROWSER_ONLINE, [online_location]
    )


def test_aru_exception_does_not_apply_to_other_hosts_or_non_detail_urls():
    assert not _is_aru_browser_keyword_online(
        "https://aru.ac.uk/study/postgraduate/banking-and-finance-mba",
        GENERIC_BROWSER_ONLINE,
        [HEADER_LOCATION],
    )
    assert not _is_aru_browser_keyword_online(
        "https://www.aru.ac.uk/study/postgraduate",
        GENERIC_BROWSER_ONLINE,
        [HEADER_LOCATION],
    )


def test_explicit_course_delivery_online_is_preserved():
    explicit_delivery = {
        **GENERIC_BROWSER_ONLINE,
        "source_method": "study_mode:label",
    }
    assert not _is_aru_browser_keyword_online(
        ARU_URL, explicit_delivery, [HEADER_LOCATION]
    )


def test_extended_browser_evidence_retains_extractor_method(monkeypatch):
    async def empty_extractor(_html, _url):
        return []

    for extractor in (
        per_course_browser.course_name_extractor,
        per_course_browser.fee,
        per_course_browser.english_test,
        per_course_browser.intake,
        per_course_browser.duration,
        per_course_browser.location,
    ):
        monkeypatch.setattr(extractor, "extract", empty_extractor)

    async def online_rule(_html, _url):
        return [
            ExtractionResult(
                field_key="study_mode",
                value="Online",
                normalized={"study_mode": "Online"},
                confidence=0.5,
                method="study_mode:rule",
            )
        ]

    monkeypatch.setattr(per_course_browser.study_mode, "extract", online_rule)
    filled, evidence = asyncio.run(
        per_course_browser._extended_extract(
            "<html>Apply online</html>", ARU_URL, {}, override=True
        )
    )

    assert filled == {"study_mode": "Online"}
    assert evidence[0]["method"] == "per_course_browser_extended"
    assert evidence[0]["source_method"] == "study_mode:rule"


def test_aru_rendered_award_fills_missing_degree_with_source_proof(monkeypatch):
    async def empty_extractor(_html, _url):
        return []

    for extractor in (
        per_course_browser.course_name_extractor,
        per_course_browser.fee,
        per_course_browser.english_test,
        per_course_browser.intake,
        per_course_browser.duration,
        per_course_browser.location,
        per_course_browser.study_mode,
    ):
        monkeypatch.setattr(extractor, "extract", empty_extractor)

    html = """<title>Brand Management - MSc - ARU</title>
    <h1 id="course-page-title">Brand Management</h1>
    <dl class="utopian-course-options__list"><dt>Award</dt><dd>MSc</dd></dl>"""
    filled, evidence = asyncio.run(per_course_browser._extended_extract(
        html, "https://www.aru.ac.uk/study/postgraduate/brand-management",
        {"course_name": "Brand Management"}, override=False,
    ))
    assert filled["degree_level"] == "Master's"
    assert evidence[0]["source_url"].endswith("/brand-management")
    assert evidence[0]["source_method"] == "degree_level:aru_award"
    assert evidence[0]["snippet"] == "Award MSc"
    filled, _ = asyncio.run(per_course_browser._extended_extract(
        html, "https://www.aru.ac.uk/study/postgraduate/brand-management",
        {"course_name": "Brand Management", "degree_level": "Bachelor's"}, override=False,
    ))
    assert "degree_level" not in filled