import html
import json

import pytest

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