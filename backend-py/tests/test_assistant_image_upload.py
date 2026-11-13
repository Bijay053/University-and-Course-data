import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_documents as documents
from app.services import assistant_image_review as images

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aS1sAAAAASUVORK5CYII=")


@pytest.mark.parametrize("content,expected", [
    ("What is wrong in this screenshot?", True),
    ("add Sydney as the location", False),
])
def test_image_does_not_swallow_explicit_staff_values(content, expected):
    token = documents.sign(dict(actor=1, job="job", key="assistant-documents/1/job/a.png",
                                contentType="image/png", expires=9999999999))
    message = {"content":content, "_documentToken":token}
    assert images.image_read_requested(message, 1, "job", [message]) is expected


@pytest.mark.parametrize("mime,name", [
    ("image/png", "screenshot.png"), ("image/jpeg", "photo.jpeg"), ("image/jpeg", "photo.jpg"),
])
@pytest.mark.asyncio
async def test_private_image_grant_binds_type_owner_job_and_size(monkeypatch, mime, name):
    monkeypatch.setattr(documents.snapshot_store, "is_enabled", lambda: True)
    grant = await documents.request_upload(3, "job", name, len(PNG), mime)
    data = documents.verify(grant["documentToken"], 3, "job")
    assert data["contentType"] == mime and data["filename"] == name
    assert data["key"].startswith("assistant-documents/3/job/")
    assert grant["transport"] == "portal" and "uploadUrl" not in grant
    for actor, job in [(4, "job"), (3, "another-job")]:
        with pytest.raises(ValueError):
            documents.verify(grant["documentToken"], actor, job)


@pytest.mark.parametrize("mime,name", [
    ("image/svg+xml", "s.svg"), ("image/gif", "s.gif"),
    ("image/png", "s.pdf"), ("application/pdf", "s.png"),
])
@pytest.mark.asyncio
async def test_unsupported_or_disguised_upload_types_are_rejected(mime, name):
    with pytest.raises(ValueError):
        await documents.request_upload(1, "job", name, 10, mime)


@pytest.mark.parametrize("raw,mime", [
    (b"<svg/>", "image/png"), (b"%PDF-1.4", "image/jpeg"),
    (PNG[:16] + b"\xff" * 8 + PNG[24:], "image/png"),
    (PNG[:16] + b"\0" * 8 + PNG[24:], "image/png"),
])
def test_invalid_signatures_and_decompression_dimensions_rejected(raw, mime):
    with pytest.raises(ValueError):
        images.validate_image(raw, mime)


@pytest.mark.asyncio
async def test_image_upload_read_preserves_private_write_once_and_unverified_provenance(monkeypatch):
    import io
    writes = []
    client = SimpleNamespace(
        put_object=lambda **kw: writes.append(kw), close=lambda: None,
        get_object=lambda **kw: {"ContentLength": len(PNG), "Body": io.BytesIO(PNG)},
    )
    monkeypatch.setattr(documents.snapshot_store, "is_enabled", lambda: True)
    monkeypatch.setattr(documents.snapshot_store, "_make_client", lambda: client)
    monkeypatch.setattr(documents.snapshot_store, "_bucket", lambda: "private-bucket")
    grant = await documents.request_upload(1, "job", "s.png", len(PNG), "image/png")
    await documents.store_uploaded(grant["documentToken"], 1, "job", PNG)
    assert writes[0]["IfNoneMatch"] == "*" and "ACL" not in writes[0]
    assert writes[0]["ContentType"] == "image/png"
    source = await documents.read_uploaded(grant["documentToken"], 1, "job")
    assert source["verified"] is False and source["officialProvenanceVerified"] is False
    assert source["_imageDataUrl"] == "data:image/png;base64," + base64.b64encode(PNG).decode()


@pytest.mark.asyncio
async def test_image_review_uses_budgeted_vision_without_persisting_bytes_or_writing_courses(monkeypatch):
    from app.services.scrape_agent_v2 import model
    service = SimpleNamespace(
        AsyncSessionLocal=object(), record_activity=AsyncMock(), save_research_reply=AsyncMock(),
        event=lambda *a, **kw: {"label": a[0], **kw},
        now=lambda: SimpleNamespace(isoformat=lambda: "2026-11-13T00:00:00Z"),
    )
    source = {"_imageDataUrl": "data:image/png;base64,PRIVATE_IMAGE"}
    monkeypatch.setattr(documents, "read_uploaded", AsyncMock(return_value=source))
    complete = AsyncMock(return_value={"message": {"content": "The image shows a missing fee."}})
    monkeypatch.setattr(model, "complete", complete)
    await images.review(service, "thread", "turn", "run",
                        SimpleNamespace(actor_id=1, job_id="job"),
                        {"content": "Explain this error", "_documentToken": "token"})
    args, kwargs = complete.call_args
    assert args[1][1]["content"][1]["type"] == "input_image"
    assert kwargs["spend_context"][0] is service.AsyncSessionLocal
    assert kwargs["spend_context"][2] == "thread"
    reply = service.save_research_reply.call_args.args[3]
    assert reply["actions"] == [] and reply["sources"] == []
    assert "PRIVATE_IMAGE" not in str(service.save_research_reply.call_args)
    assert "PRIVATE_IMAGE" not in str(service.record_activity.call_args)


@pytest.mark.asyncio
async def test_budget_failure_does_not_save_a_successful_image_answer(monkeypatch):
    from app.services.scrape_agent_v2 import model
    from app.services.scrape_agent_v2.spend import SpendLimit
    monkeypatch.setattr(documents, "read_uploaded", AsyncMock(
        return_value={"_imageDataUrl": "data:image/png;base64,DATA"}))
    monkeypatch.setattr(model, "complete", AsyncMock(side_effect=SpendLimit("Daily cap reached")))
    service = SimpleNamespace(AsyncSessionLocal=object(), record_activity=AsyncMock(),
                              save_research_reply=AsyncMock(), event=lambda *a, **kw: {})
    with pytest.raises(SpendLimit):
        await images.review(service, "thread", "turn", "run",
                            SimpleNamespace(actor_id=1, job_id="job"),
                            {"content": "Explain", "_documentToken": "token"})
    service.save_research_reply.assert_not_called()
