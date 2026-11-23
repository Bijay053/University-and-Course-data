from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.services import assistant_documents as documents
from app.services import assistant_attachment_batch as batch
from app.services import assistant_document_review as document_review
from app.services.assistant_history import public_message
from app.routers import scrape_assistant as routes


def token(index, content_type="image/png", **overrides):
    data = dict(actor=1, job="job", key=f"assistant-documents/1/job/{index}",
                filename=f"file-{index}", size=100, contentType=content_type, expires=9999999999)
    return documents.sign({**data, **overrides})


def message(tokens):
    return {"role": "user", "turnId": "turn", "createdAt": "2026-11-23",
            "content": "Explain these files", "_documentTokens": tokens}


def test_contract_limits_and_legacy_compatibility():
    assert routes.PromptBody(prompt="Read", documentTokens=["a", "b"]).documentTokens == ["a", "b"]
    assert routes.PromptBody(prompt="Read", documentToken="a").documentToken == "a"
    for tokens in ([], ["a"] * 6):
        with pytest.raises(ValidationError):
            routes.PromptBody(prompt="Read", documentTokens=tokens)
    tokens = [token(i) for i in range(5)]
    assert len(documents.validate_tokens(tokens, 1, "job")) == 5
    for invalid in (tokens + [token(6)], [tokens[0], tokens[0]], [token(1, actor=2)],
                    [token(1, job="other")], [token(1, expires=0)], [token(1, size=2_000_001)],
                    [token(1, content_type="text/html")], ["x" * 4001]):
        with pytest.raises(ValueError):
            documents.validate_tokens(invalid, 1, "job")
    assert documents.document_tokens({"_documentToken": tokens[0]}) == [tokens[0]]


def test_private_history_keeps_every_file_and_original_indices_after_expiry():
    m = message([token(0, "application/pdf", expires=1), token(1, expires=1)])
    visible = public_message(m, thread_id="thread", job_id="job", position=1, actor_id=1)
    assert [a["index"] for a in visible["attachments"]] == [0, 1]
    assert [a["contentType"] for a in visible["attachments"]] == ["application/pdf", "image/png"]
    assert "_documentTokens" not in visible and "assistant-documents/" not in str(visible)
    assert not documents.public_attachments(m, 2, "job")
    assert not documents.public_attachments(m, 1, "other")
    assert documents.message_image(m, 1, "job")["contentType"] == "image/png"


@pytest.mark.asyncio
async def test_indexed_pdf_and_image_reads_enforce_owner_and_turn(monkeypatch):
    m = message([token(0, "application/pdf"), token(1)])
    monkeypatch.setattr(routes, "owned_thread", AsyncMock(return_value=(None, SimpleNamespace(messages=[m]))))
    monkeypatch.setattr(documents, "read_bytes", AsyncMock(return_value=b"private bytes"))
    for index, mime in [(0, "application/pdf"), (1, "image/png")]:
        result = await routes.message_attachment_content("job", "turn", index, None, {"id": 1})
        assert result.media_type == mime and result.body == b"private bytes"
        assert result.headers["cache-control"] == "private, no-store"
    for job, turn, index, actor in [("job", "turn", 0, 2), ("other", "turn", 0, 1),
                                     ("job", "wrong", 0, 1), ("job", "turn", -1, 1),
                                     ("job", "turn", 2, 1)]:
        with pytest.raises(HTTPException) as exc:
            await routes.message_attachment_content(job, turn, index, None, {"id": actor})
        assert exc.value.status_code == 404


def proposal(action_id, courses):
    return {
        "actions": [{"id": action_id, "kind": "document_update", "requiresConfirmation": True,
                     "courseCount": len(courses), "_targets": [
                         {"id": i, "version": "v", "changes": [{"field": "ielts_overall", "newValue": v}]}
                         for i, v in courses], "fields": ["ielts_overall"]}],
        "documentReview": {"rows": [
            {"id": i, "name": f"Course {i}", "status": "proposed", "applicability": "verified",
             "changes": [{"field": "ielts_overall", "newValue": v}],
             "currentValues": {"ielts_overall": None}, "proposedValues": {"ielts_overall": v}}
            for i, v in courses]},
    }


def test_conflicting_documents_withhold_only_conflicting_courses():
    replies = batch.reconcile([proposal("a", [(1, 6), (2, 6.5)]),
                               proposal("b", [(1, 7), (3, 6)])])
    assert [[t["id"] for a in r["actions"] for t in a["_targets"]] for r in replies] == [[2], [3]]
    assert [r["documentReview"]["rows"][0]["status"] for r in replies] == ["conflict", "conflict"]
    assert all(a["requiresConfirmation"] for r in replies for a in r["actions"])


def test_identical_proposals_are_not_applied_twice():
    replies = batch.reconcile([proposal("a", [(1, 6.5)]), proposal("b", [(1, 6.5)])])
    assert sum(a["courseCount"] for r in replies for a in r["actions"]) == 1
    assert not replies[1]["actions"]


def fake_service():
    return SimpleNamespace(record_activity=AsyncMock(), event=lambda *a, **kw: {},
                           now=lambda: datetime.now(timezone.utc), save_research_reply=AsyncMock())


@pytest.mark.asyncio
async def test_all_images_and_pdfs_are_read_without_persisting_binary(monkeypatch):
    service = fake_service()
    sources = [
        {"sourceType": "image", "_imageDataUrl": "data:image/png;base64,private", "filename": "one.png"},
        {"sourceType": "pdf", "verified": True, "_fullText": "Two", "filename": "two.pdf"},
        {"sourceType": "image", "_imageDataUrl": "data:image/png;base64,private", "filename": "three.png"},
    ]
    reader = AsyncMock(side_effect=deepcopy(sources))
    describe = AsyncMock(return_value="One, two and three.")
    monkeypatch.setattr(batch, "read_uploaded", reader)
    monkeypatch.setattr(batch, "describe", describe)
    assert await batch.review(service, "thread", "turn", "run",
                              SimpleNamespace(actor_id=1, job_id="job"), message(["a", "b", "c"]))
    assert reader.await_count == 3
    assert [s["filename"] for s in describe.call_args.args[-1]] == ["one.png", "two.pdf", "three.png"]
    reply = service.save_research_reply.call_args.args[3]
    assert reply["actions"] == [] and "3 of 3" in reply["content"]
    assert "data:image" not in str(reply) and "_documentTokens" not in str(reply)


@pytest.mark.asyncio
async def test_unreadable_file_does_not_hide_readable_files_or_start_a_repair(monkeypatch):
    service = fake_service()
    monkeypatch.setattr(batch, "read_uploaded", AsyncMock(side_effect=[
        {"sourceType": "pdf", "verified": True, "filename": "good.pdf"}, ValueError("private storage detail")]))
    monkeypatch.setattr(batch, "describe", AsyncMock(return_value="Good PDF read."))
    m = {**message(["a", "b"]), "content": "Update IELTS for all courses"}
    assert await batch.review(service, "thread", "turn", "run", SimpleNamespace(actor_id=1, job_id="job"), m)
    reply = service.save_research_reply.call_args.args[3]
    assert "1 of 2" in reply["content"] and "Attachment 2" in reply["content"]
    assert not reply["actions"] and "private storage detail" not in str(reply)


@pytest.mark.asyncio
async def test_all_pdf_matches_reconcile_before_one_reply_is_saved(monkeypatch):
    service = fake_service()
    monkeypatch.setattr(batch, "read_uploaded", AsyncMock(side_effect=[
        {"sourceType": "pdf", "verified": True, "filename": "one.pdf"},
        {"sourceType": "pdf", "verified": True, "filename": "two.pdf"},
    ]))
    async def match(tid, turn, run, thread, m, *, _source, _service):
        reply = proposal(_source["filename"], [(1, 6 if _source["filename"] == "one.pdf" else 7)])
        await _service.save_research_reply(tid, turn, run, reply)
    monkeypatch.setattr(document_review, "review", match)
    m = {**message(["a", "b"]), "content": "Update all IELTS bands"}
    assert await batch.review(service, "thread", "turn", "run", SimpleNamespace(actor_id=1, job_id="job"), m)
    service.save_research_reply.assert_awaited_once()
    reply = service.save_research_reply.call_args.args[3]
    assert not reply["actions"] and len(reply["documentReviews"]) == 2
    assert all(d["review"]["proposed"] == 0 for d in reply["documentReviews"])


@pytest.mark.asyncio
async def test_vision_request_contains_all_files_and_closes_client(monkeypatch):
    from app.services.ai import openai_client
    from app.services.scrape_agent_v2 import model
    service = fake_service()
    service.AsyncSessionLocal = object()
    client = SimpleNamespace(close=AsyncMock())
    monkeypatch.setattr(openai_client, "_client", lambda: client)
    invoke = AsyncMock(return_value={"message": {"content": "Both images and the PDF."}})
    monkeypatch.setattr(model, "complete", invoke)
    await batch.describe(service, "thread", {"content": "Compare"}, [
        {"filename": "one.png", "sourceType": "image", "_imageDataUrl": "data:image/png;base64,one"},
        {"filename": "two.jpg", "sourceType": "image", "_imageDataUrl": "data:image/jpeg;base64,two"},
        {"filename": "three.pdf", "sourceType": "pdf", "_fullText": "PDF text"},
    ])
    payload = invoke.call_args.args[1][1]["content"]
    assert len([p for p in payload if p["type"] == "input_image"]) == 2
    assert "three.pdf" in str(payload) and "PDF text" in str(payload)
    client.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_one_matching_failure_withholds_other_documents_proposals(monkeypatch):
    service = fake_service()
    monkeypatch.setattr(batch, "read_uploaded", AsyncMock(side_effect=[
        {"sourceType": "pdf", "verified": True, "filename": "one.pdf"},
        {"sourceType": "pdf", "verified": True, "filename": "two.pdf"},
    ]))
    async def match(tid, turn, run, thread, m, *, _source, _service):
        if _source["filename"] == "two.pdf":
            raise RuntimeError("private internal detail")
        await _service.save_research_reply(tid, turn, run, proposal("one", [(1, 6.5)]))
    monkeypatch.setattr(document_review, "review", match)
    await batch.review(service, "thread", "turn", "run", SimpleNamespace(actor_id=1, job_id="job"),
                       {**message(["a", "b"]), "content": "Update IELTS for all courses"})
    reply = service.save_research_reply.call_args.args[3]
    assert not reply["actions"] and reply["documentReviews"][0]["review"]["proposed"] == 0
    assert "private internal detail" not in str(reply)
