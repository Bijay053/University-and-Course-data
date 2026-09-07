from __future__ import annotations

import pytest

from app.services.scraper.extractors import gemini_primary


@pytest.mark.asyncio
async def test_openai_primary_provider_never_calls_gemini(monkeypatch):
    calls: list[dict] = []

    async def fake_chat_json(**kwargs):
        calls.append(kwargs)
        return {
            "international_fee": 42000,
            "fee_term": "Annual",
        }

    async def fail_gemini(*_args, **_kwargs):
        raise AssertionError("Gemini must not run for OpenAI repair extraction")

    monkeypatch.setattr(
        "app.services.ai.openai_client.chat_json",
        fake_chat_json,
    )
    monkeypatch.setattr(gemini_primary.gemini_client, "generate", fail_gemini)

    filled, cost, input_tokens, output_tokens, debug = (
        await gemini_primary.extract_primary(
            "<main><h1>Master of Engineering</h1>"
            "<p>International annual tuition fee: AUD 42,000.</p>"
            "<p>This full-time postgraduate course is delivered on campus "
            "for international students and includes supervised projects.</p>"
            "</main>",
            "https://example.edu/courses/master-of-engineering",
            fields=["international_fee", "fee_term"],
            provider="openai",
            timeout=2,
        )
    )

    assert filled == {
        "international_fee": 42000.0,
        "fee_term": "Annual",
    }
    assert cost == 0
    assert input_tokens == 0
    assert output_tokens == 0
    assert debug["provider"] == "openai"
    assert len(calls) == 1
    assert "never infer or guess" in calls[0]["system"]