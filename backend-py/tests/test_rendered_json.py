import html
import json

import pytest

from app.services.scraper.rendered_json import (
    RenderedJsonError,
    parse_rendered_json,
)


def test_plain_json_is_parsed_without_wrapper_handling():
    payload = {"courses": [{"name": "Law"}], "count": 1}

    assert parse_rendered_json(json.dumps(payload)) == payload


def test_escaped_pre_content_is_unwrapped_and_parsed():
    payload = {"title": 'Business & Law <International>', "active": True}
    wrapped = f"<html><body><pre>{html.escape(json.dumps(payload))}</pre></body></html>"

    assert parse_rendered_json(wrapped) == payload


def test_pre_attributes_are_supported():
    payload = [{"url": "/courses/one"}]
    wrapped = (
        '<html><body><pre style="word-wrap: break-word" data-source="json">'
        f"{html.escape(json.dumps(payload))}</pre></body></html>"
    )

    assert parse_rendered_json(wrapped) == payload


def test_chromium_json_formatter_container_after_pre_is_supported():
    payload = {"response": {"resultPacket": {"results": [{"title": "Law"}]}}}
    wrapped = (
        '<html><head><meta name="color-scheme" content="light dark">'
        '<meta charset="utf-8"></head><body><pre>'
        f"{html.escape(json.dumps(payload))}</pre>"
        '<div class="json-formatter-container"></div></body></html>'
    )

    assert parse_rendered_json(wrapped) == payload


@pytest.mark.parametrize(
    "raw",
    [
        "<html><body><pre>{not json}</pre></body></html>",
        "<html><body><pre>{\"truncated\": true}",
        "<html><body>ordinary non-JSON response</body></html>",
    ],
)
def test_malformed_or_non_json_responses_fail(raw):
    with pytest.raises(json.JSONDecodeError):
        parse_rendered_json(raw)


def test_challenge_page_is_an_explicit_failure():
    challenge = (
        "<html><head><title>Just a moment...</title></head>"
        "<body><pre>{}</pre></body></html>"
    )

    with pytest.raises(RenderedJsonError, match="anti-bot challenge"):
        parse_rendered_json(challenge)


@pytest.mark.parametrize(
    "raw",
    [
        (
            "<html><head></head><body>not a JSON endpoint"
            '<pre>{"accepted": true}</pre></body></html>'
        ),
        (
            "<html><head></head><body><div>unrelated content</div>"
            '<pre>{"accepted": true}</pre></body></html>'
        ),
        (
            "<html><head><title>API response</title></head><body>"
            '<pre>{"accepted": true}</pre></body></html>'
        ),
        (
            "<html><head></head><body>"
            '<pre>{"accepted": true}</pre>'
            '<div class="unrelated-container"></div></body></html>'
        ),
    ],
)
def test_valid_json_pre_inside_unrelated_html_is_rejected(raw):
    with pytest.raises(json.JSONDecodeError):
        parse_rendered_json(raw)