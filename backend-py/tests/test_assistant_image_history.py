"""Sent images survive reload/expiry, without public storage or upload grants."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from app.services import assistant_documents as documents
from app.routers import scrape_assistant as routes


def message(**overrides):
    data = dict(actor=1, job="job", key="assistant-documents/1/job/test.png",
                filename="screenshot.png", size=3, contentType="image/png", expires=1)
    data.update(overrides)
    return {"role":"user", "turnId":"turn", "_documentToken":documents.sign(data)}


def test_saved_image_remains_owner_bound_after_upload_grant_expires():
    m = message()
    assert documents.message_image(m, 1, "job")["filename"] == "screenshot.png"
    # Expiry is relaxed only for a stored message's authenticated read.
    with pytest.raises(ValueError):
        documents.verify(m["_documentToken"], 1, "job")
    assert documents.message_image(m, 2, "job") is None
    assert documents.message_image(m, 1, "other") is None
    assert documents.message_image({**m, "role":"assistant"}, 1, "job") is None
    assert documents.message_image(message(contentType="application/pdf"), 1, "job") is None
    assert documents.message_image(message(key="another-user/test.png"), 1, "job") is None
    assert documents.message_image({**m, "_documentToken":"tampered"}, 1, "job") is None


@pytest.mark.asyncio
async def test_history_image_endpoint_is_private_binary_and_safe_when_unavailable(monkeypatch):
    thread = SimpleNamespace(messages=[message()])
    monkeypatch.setattr(routes, "owned_thread", AsyncMock(return_value=(None,thread)))
    read = AsyncMock(return_value=b"png")
    monkeypatch.setattr(documents, "read_bytes", read)
    response = await routes.message_image_content("job", "turn", None, {"id":1})
    assert response.body == b"png" and response.media_type == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    for actor, job, turn in [(2,"job","turn"),(1,"other","turn"),(1,"job","wrong")]:
        with pytest.raises(HTTPException) as exc:
            await routes.message_image_content(job, turn, None, {"id":actor})
        assert exc.value.status_code == 404
    assert read.await_count == 1
    read.side_effect = RuntimeError("private storage credentials must not be disclosed")
    with pytest.raises(HTTPException) as exc:
        await routes.message_image_content("job", "turn", None, {"id":1})
    assert exc.value.status_code == 404 and "credentials" not in exc.value.detail


@pytest.mark.asyncio
async def test_history_image_endpoint_requires_sign_in():
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        result = await client.get("/jobs/job/assistant/messages/turn/image")
    assert result.status_code in (401,403)
