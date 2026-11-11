from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.assistant_review_commands import saved_ielts_status


@pytest.mark.asyncio
@pytest.mark.parametrize("count", [1, 25])
async def test_saved_five_score_read_never_fetches_or_proposes_repair(count):
    service = SimpleNamespace(save_research_reply=AsyncMock(), now=lambda: datetime.now(timezone.utc))
    samples = [{"id":i, "name":"Physiotherapy BSc (Hons)", "ielts":6.5,
                "ieltsBands":dict(listening=6.0, reading=6.0, writing=6.0, speaking=6.0)}
               for i in range(count)]
    prompt = "Show the saved IELTS values for " + (
        "Physiotherapy BSc (Hons)" if count == 1 else "all courses")
    assert await saved_ielts_status(service, "t", "turn", "run", {"samples":samples}, prompt)
    reply = service.save_research_reply.call_args.args[3]
    assert not reply["actions"] and not reply["sources"]
    assert "IELTS overall 6.5; Listening 6.0; Reading 6.0; Writing 6.0; Speaking 6.0" in reply["content"]
    assert "No website was fetched" in reply["content"]
    if count > 20:
        assert "Showing 20 of 25" in reply["content"]


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt", [
    "Why are IELTS bands missing for Physiotherapy?",
    "Show official IELTS requirements for Physiotherapy",
    "Show saved IELTS and then fix the missing bands",
])
async def test_other_intents_stay_on_their_existing_routes(prompt):
    service = SimpleNamespace(save_research_reply=AsyncMock())
    assert not await saved_ielts_status(service, "t", "turn", "run", {"samples":[]}, prompt)
    service.save_research_reply.assert_not_awaited()
