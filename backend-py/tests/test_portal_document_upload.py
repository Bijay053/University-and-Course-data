"""Same-origin private uploads: no S3 CORS or expanded permissions."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from starlette.requests import Request
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from app.services import assistant_documents as documents
from app.routers import scrape_assistant as routes


@pytest.mark.asyncio
async def test_grant_and_write_are_private_write_once_and_owner_bound(monkeypatch):
    writes = []
    client = SimpleNamespace(put_object=lambda **kw: writes.append(kw), close=lambda: None)
    monkeypatch.setattr(documents.snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(documents.snapshot_store, "_make_client", lambda: client)
    monkeypatch.setattr(documents.snapshot_store, "_bucket", lambda: "private-bucket")
    raw = b"%PDF-1.4\nexample"
    grant = await documents.request_upload(1, "job", "requirements.pdf", len(raw), "application/pdf")
    assert grant["transport"] == "portal" and "uploadUrl" not in grant
    with pytest.raises(ValueError):
        await documents.store_uploaded(grant["documentToken"], 2, "job", raw)
    with pytest.raises(ValueError):
        await documents.store_uploaded(grant["documentToken"], 1, "other", raw)
    with pytest.raises(ValueError):
        await documents.store_uploaded(grant["documentToken"], 1, "job", b"x" * len(raw))
    assert writes == []
    assert await documents.store_uploaded(grant["documentToken"], 1, "job", raw) == {"uploaded": True}
    assert len(writes) == 1 and writes[0]["IfNoneMatch"] == "*"
    assert writes[0]["ContentType"] == "application/pdf" and "ACL" not in writes[0]
    assert writes[0]["Key"].startswith("assistant-documents/1/job/")


def request(raw, token, content_type="application/pdf", length=None):
    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    headers = [(b"content-type", content_type.encode()), (b"x-document-token", token.encode())]
    if length is not None:
        headers.append((b"content-length", str(length).encode()))
    return Request({"type": "http", "headers": headers}, receive)


@pytest.mark.asyncio
async def test_endpoint_bounds_stream_and_hides_storage_errors(monkeypatch):
    monkeypatch.setattr(routes, "owned_thread", AsyncMock())
    monkeypatch.setattr(documents.settings, "assistant_document_uploads_enabled", True)
    token = documents.sign(dict(actor=1, job="job", key="assistant-documents/1/job/a.pdf",
                                size=5, expires=9999999999))
    store = AsyncMock(return_value={"uploaded": True})
    monkeypatch.setattr(documents, "store_uploaded", store)
    assert await routes.document_content("job", request(b"%PDF-", token), None, {"id": 1}) == {"uploaded": True}
    for raw, content_type, length, code in [
        (b"%PDF-too-long", "application/pdf", None, 413),
        (b"%PDF-", "text/plain", None, 422),
        (b"%PDF-", "application/pdf", 8, 422),
    ]:
        with pytest.raises(routes.HTTPException) as exc:
            await routes.document_content("job", request(raw, token, content_type, length), None, {"id": 1})
        assert exc.value.status_code == code
    assert store.await_count == 1
    store.side_effect = RuntimeError("signed credential and private key must never appear")
    with pytest.raises(routes.HTTPException) as exc:
        await routes.document_content("job", request(b"%PDF-", token), None, {"id": 1})
    assert exc.value.status_code == 503 and "credential" not in exc.value.detail


@pytest.mark.asyncio
async def test_upload_endpoint_requires_authentication():
    app = FastAPI()
    app.include_router(routes.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/jobs/job/assistant/documents/content", content=b"%PDF-")
    assert response.status_code in (401, 403)
