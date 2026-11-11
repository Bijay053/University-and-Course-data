from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.routers import scrape
from app.services.scraper.smart_fix import run_smart_batch


@pytest.mark.asyncio
@pytest.mark.parametrize("filled", [True, False])
async def test_present_overall_does_not_hide_missing_band_and_resolution_requires_saved_scores(monkeypatch, filled):
    row = SimpleNamespace(ielts_overall=6.5, ielts_listening=None,
                          ielts_reading=None, ielts_writing=None, ielts_speaking=None)
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(return_value={
        "total": 1, "courses_with_url": 1, "issues": [],
    }))
    calls = []
    async def extract(body, db):
        calls.append(body)
        if filled:
            row.ielts_listening = row.ielts_reading = row.ielts_writing = row.ielts_speaking = 6.0
        return {"results": [{"id": 59504, "ok": True, "updated_fields": ["ielts_listening"] if filled else []}]}
    monkeypatch.setattr(scrape, "re_extract_staged", extract)
    result = (await run_smart_batch(scrape.ReExtractBody(
        ids=[59504], universityId=116, targetFields=["ielts_overall"], smart=True), db))["results"][0]
    assert len(calls) == 1
    assert calls[0].target_fields == ["ielts_overall"]
    assert result["attempted"]
    assert result["made_progress"] is filled
    assert result["resolved_fields"] == (["ielts_overall"] if filled else [])
    assert result["unresolved_fields"] == ([] if filled else ["ielts_overall"])
    assert result["reason_code"] != "already_resolved"


@pytest.mark.asyncio
async def test_complete_ielts_bundle_is_already_resolved(monkeypatch):
    row = SimpleNamespace(**{f"ielts_{part}": 6.5 for part in
        ("overall", "listening", "reading", "writing", "speaking")})
    db = SimpleNamespace(get=AsyncMock(return_value=row))
    monkeypatch.setattr(scrape, "analyze_staged", AsyncMock(return_value={
        "total": 1, "courses_with_url": 1, "issues": [],
    }))
    extract = AsyncMock()
    monkeypatch.setattr(scrape, "re_extract_staged", extract)
    result = (await run_smart_batch(scrape.ReExtractBody(
        ids=[59504], universityId=116, targetFields=["ielts_overall"], smart=True), db))["results"][0]
    assert not result["attempted"] and result["reason_code"] == "already_resolved"
    extract.assert_not_awaited()
