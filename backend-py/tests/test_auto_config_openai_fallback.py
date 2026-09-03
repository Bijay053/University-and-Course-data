from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.ai import gemini_client, openai_client
from app.services.scraper.auto_config_generator import _gemini_refine


def _profile() -> SimpleNamespace:
    return SimpleNamespace(
        url="https://example.edu",
        recommended_strategy="static_html",
        is_cloudflare_blocked=False,
        is_js_spa=False,
        has_sitemap=False,
        sitemap_course_count=0,
        wayback_course_count=0,
        detected_apis=[],
        notes=[],
    )


def test_openai_refines_config_when_gemini_is_skipped(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_client,
        "generate",
        AsyncMock(return_value=SimpleNamespace(skipped=True, text=None)),
    )
    openai_call = AsyncMock(return_value={
        "allow_url_patterns": ["/programme/"],
        "block_url_patterns": ["/category/"],
        "fees_on_course_page": True,
        "ielts_on_course_page": True,
        "fee_page_hint": None,
        "english_page_hint": None,
        "notes": "OpenAI fallback used",
    })
    monkeypatch.setattr(openai_client, "chat_json", openai_call)

    result = asyncio.run(_gemini_refine(
        {"discovery": {}, "extraction": {}},
        _profile(),
        "<h1>Example course</h1>",
        ["https://example.edu/programme/example"],
    ))

    assert result["discovery"]["allow_url_patterns"] == ["/programme/"]
    assert result["discovery"]["block_url_patterns"] == ["/category/"]
    openai_call.assert_awaited_once()


def test_openai_refines_config_when_gemini_returns_invalid_json(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_client,
        "generate",
        AsyncMock(return_value=SimpleNamespace(
            skipped=False,
            text="not valid json",
        )),
    )
    monkeypatch.setattr(
        openai_client,
        "chat_json",
        AsyncMock(return_value={
            "allow_url_patterns": ["/courses/"],
            "fees_on_course_page": True,
            "ielts_on_course_page": True,
        }),
    )

    result = asyncio.run(_gemini_refine(
        {"discovery": {}, "extraction": {}},
        _profile(),
        "<h1>Example course</h1>",
        [],
    ))

    assert result["discovery"]["allow_url_patterns"] == ["/courses/"]


def test_openai_refines_config_when_gemini_json_is_incomplete(monkeypatch) -> None:
    monkeypatch.setattr(
        gemini_client,
        "generate",
        AsyncMock(return_value=SimpleNamespace(
            skipped=False,
            text='{"allow_url_patterns": null, "block_url_patterns": []}',
        )),
    )
    openai_call = AsyncMock(return_value={
        "allow_url_patterns": ["/programme/"],
        "block_url_patterns": ["/academic-programmes/"],
        "fees_on_course_page": True,
        "ielts_on_course_page": True,
    })
    monkeypatch.setattr(openai_client, "chat_json", openai_call)

    result = asyncio.run(_gemini_refine(
        {"discovery": {}, "extraction": {}},
        _profile(),
        "<h1>Example course</h1>",
        [],
    ))

    assert result["discovery"]["allow_url_patterns"] == ["/programme/"]
    openai_call.assert_awaited_once()