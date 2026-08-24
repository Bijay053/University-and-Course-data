import asyncio
import html
import json

import pytest

from app.services.scraper.extractors import latrobe_json
from app.services.scraper.extractors.latrobe_json import _decode_json_response


def test_decode_json_response_accepts_raw_json():
    assert _decode_json_response('{"availability": true, "data": {"duration": "3 years"}}') == {
        "availability": True,
        "data": {"duration": "3 years"},
    }


def test_decode_json_response_unwraps_chromium_pre_document():
    payload = {
        "availability": True,
        "data": {
            "entryReq": {
                "engReq": '<p><span>6.5 IELTS with no band below 6.0.</span></p>'
            }
        },
    }
    wrapped = (
        '<html><head><meta charset="utf-8"></head><body><pre>'
        + html.escape(json.dumps(payload))
        + "</pre></body></html>"
    )

    assert _decode_json_response(wrapped) == payload


def test_decode_json_response_rejects_non_json_html():
    with pytest.raises(json.JSONDecodeError):
        _decode_json_response("<html><body>challenge page</body></html>")


def test_fetch_course_bundle_uses_top_level_navigation(monkeypatch):
    detail_doc = {
        "availability": True,
        "data": {
            "awardTitle": "Bachelor of Business",
            "duration": "3 years full-time",
            "entryReq": {"engReq": "IELTS 6.5 with no band below 6.0"},
        },
    }
    course_html = (
        '<html><script>{"allDetailUrls":{"2026":{"international":{"BU":'
        '"https://www.latrobe.edu.au/courses/data/2026/international/bu/test"}}}'
        "}}</script></html>"
    )
    bundle = {
        "courseHtml": course_html,
        "detailUrl": "https://www.latrobe.edu.au/courses/data/2026/international/bu/test",
        "detailText": json.dumps(detail_doc),
    }
    wrapped = "<html><body><pre>" + html.escape(json.dumps(bundle)) + "</pre></body></html>"
    captured = {}

    async def fake_scrape_do(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return wrapped

    monkeypatch.setattr(latrobe_json, "fetch_html_scrape_do", fake_scrape_do)
    actual_html, actual_doc, actual_url = asyncio.run(
        latrobe_json.fetch_course_bundle(
            "https://www.latrobe.edu.au/courses/bachelor-of-business",
            wait_for_ms=3000,
            local_concurrency_limit=8,
        )
    )

    assert actual_html == course_html
    assert actual_doc == detail_doc
    assert actual_url == bundle["detailUrl"]
    assert captured["render"] is True
    assert captured["wait_for_ms"] == 3000
    assert captured["unescape_json_html"] is False
    assert captured["local_concurrency_limit"] == 8
    browser_script = json.dumps(captured["play_with_browser"])
    assert "window.location.assign" in browser_script
    assert "fetch(" not in browser_script


def test_apply_overrides_uses_prefetched_document_without_fetch(monkeypatch):
    course_html = (
        '<script>{"allDetailUrls":{"2026":{"international":{"BU":'
        '"https://www.latrobe.edu.au/courses/data/2026/international/bu/test"}}}'
        "}}</script>"
    )
    detail_doc = {
        "availability": True,
        "data": {
            "duration": "3 years full-time",
            "entryReq": {"engReq": "IELTS 6.5 with no band below 6.0"},
        },
    }

    async def forbidden_fetch(_url, **_kwargs):
        raise AssertionError("prefetched detail must avoid a second provider request")

    monkeypatch.setattr(
        latrobe_json,
        "fetch_html_scrape_do",
        forbidden_fetch,
    )
    payload = {}
    applied = asyncio.run(
        latrobe_json.apply_overrides(
            payload,
            course_html,
            url="https://www.latrobe.edu.au/courses/test",
            prefetched_doc=detail_doc,
            prefetched_url=(
                "https://www.latrobe.edu.au/courses/data/2026/"
                "international/bu/test"
            ),
        )
    )

    assert "duration" in applied
    assert payload["duration"] == 3


def test_apply_overrides_restores_on_campus_from_authoritative_delivery_code(monkeypatch):
    """An OC JSON variant must override the SPA shell's weak Online guess."""
    detail_url = "https://www.latrobe.edu.au/courses/data/2026/international/ci/test"
    course_html = (
        '<script>{"allDetailUrls":{"2026":{"international":{"CI":'
        f'"{detail_url}"'
        "}}}}</script>"
    )
    payload = {"study_mode": "Online"}
    evidence: list[dict] = []
    detail_doc = {
        "data": {
            "duration": "3 years full-time",
            "deliveryModeCode": "OC",
            "deliveryModeDescription": "On Campus",
            "entryReq": {"engReq": "IELTS 6.5"},
        }
    }

    async def forbidden_fetch(_url, **_kwargs):
        raise AssertionError("prefetched detail must avoid a second provider request")

    monkeypatch.setattr(latrobe_json, "fetch_html_scrape_do", forbidden_fetch)
    applied = asyncio.run(
        latrobe_json.apply_overrides(
            payload,
            course_html,
            url="https://www.latrobe.edu.au/courses/test",
            evidence=evidence,
            prefetched_doc=detail_doc,
            prefetched_url=detail_url,
        )
    )

    assert applied["study_mode"] == {"old": "Online", "new": "On Campus"}
    assert payload["study_mode"] == "On Campus"
    assert evidence[-1]["method"] == "latrobe_json"
    assert evidence[-1]["value"] == "On Campus"


def test_pick_international_url_prefers_earliest_year_and_canonical_campus():
    manifest = {
        "2027": {
            "international": {
                "ON": "https://example.test/2027/on",
                "CI": "https://example.test/2027/ci",
            }
        },
        "2026": {
            "international": {
                "ON": "https://example.test/2026/on",
                "BU": "https://example.test/2026/bu",
                "CI": "https://example.test/2026/ci",
            }
        },
    }

    assert (
        latrobe_json.pick_international_url(manifest)
        == "https://example.test/2026/ci"
    )


def test_mismatched_prefetch_uses_rendered_canonical_detail(monkeypatch):
    canonical_url = "https://example.test/2026/ci"
    course_html = (
        '<script>{"allDetailUrls":{"2026":{"international":{'
        f'"CI":"{canonical_url}","ON":"https://example.test/2026/on"'
        "}}}}</script>"
    )
    wrong_doc = {
        "data": {
            "duration": "9 years full-time",
            "entryReq": {"engReq": "IELTS 9.0"},
        }
    }
    canonical_doc = {
        "data": {
            "duration": "3 years full-time",
            "entryReq": {"engReq": "IELTS 6.5 with no band below 6.0"},
        }
    }
    calls = []

    async def fake_rendered_fetch(url, **kwargs):
        calls.append((url, kwargs))
        return "<html><body><pre>" + html.escape(json.dumps(canonical_doc)) + "</pre></body></html>"

    monkeypatch.setattr(
        latrobe_json,
        "fetch_html_scrape_do",
        fake_rendered_fetch,
    )
    payload = {}
    asyncio.run(
        latrobe_json.apply_overrides(
            payload,
            course_html,
            url="https://www.latrobe.edu.au/courses/test",
            prefetched_doc=wrong_doc,
            prefetched_url="https://example.test/2027/on",
            local_concurrency_limit=8,
        )
    )

    assert payload["duration"] == 3
    assert calls == [
        (
            canonical_url,
            {
                "render": True,
                "wait_for_ms": 0,
                "local_concurrency_limit": 8,
            },
        )
    ]


def test_missing_prefetch_uses_rendered_detail_not_plain_fetch(monkeypatch):
    canonical_url = "https://example.test/2026/ci"
    course_html = (
        '<script>{"allDetailUrls":{"2026":{"international":{'
        f'"CI":"{canonical_url}"'
        "}}}}</script>"
    )
    canonical_doc = {
        "data": {
            "duration": "2 years full-time",
            "entryReq": {"engReq": "IELTS 6.5"},
        }
    }
    captured = {}

    async def fake_rendered_fetch(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return json.dumps(canonical_doc)

    monkeypatch.setattr(
        latrobe_json,
        "fetch_html_scrape_do",
        fake_rendered_fetch,
    )
    payload = {}
    asyncio.run(
        latrobe_json.apply_overrides(
            payload,
            course_html,
            url="https://www.latrobe.edu.au/courses/test",
            local_concurrency_limit=8,
        )
    )

    assert payload["duration"] == 2
    assert captured == {
        "url": canonical_url,
        "render": True,
        "wait_for_ms": 0,
        "local_concurrency_limit": 8,
    }