from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_english_guidance_followup as follow
from app.services import assistant_document_review as documents

HOME = "https://www.aston.ac.uk"
LINK = HOME + "/international/english-language-requirements"
WHY = "why ielts still says in all courses unverified"
SHARED = "i have shared you the english requirment link"
CHECK = LINK + "\n\nis one is verified scourse"


def user(text):
    return {"role": "user", "content": text}


def test_transcript_resolves_guidance_not_english_degrees():
    messages = [user(LINK), user(WHY), user(SHARED)]
    assert follow.resolve(SHARED, messages, HOME, []) == ("reminder", True, LINK)
    assert follow.resolve(CHECK, [*messages, user(CHECK)], HOME, []) == ("source", True, LINK)
    assert follow.resolve(CHECK, [user(CHECK)], HOME, []) == ("source", False, LINK)
    assert follow.resolve(CHECK, [*messages, user("check the MBA fee"), user(CHECK)],
                          HOME, []) == ("source", False, LINK)


def test_ignore_assistant_foreign_and_course_links():
    history = [{"role": "assistant", "content": LINK},
               user(LINK.replace("aston.ac.uk", "other.example")),
               user(HOME + "/study/courses/english-language-ba"), user(SHARED)]
    assert follow.resolve(SHARED, history, HOME, []) == ("reminder", False, None)
    assert follow.resolve(CHECK.replace("aston.ac.uk", "other.example"), [], HOME, [LINK]) is None
    assert follow.kind("Which English Language BA courses are available?") is None
    assert follow.kind("Update IELTS using the source I shared") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [SHARED, CHECK, "veifiy all course ilets score", "review it and verified it"])
async def test_signed_assistant_dispatch_matches_existing_courses_read_only(monkeypatch, text):
    from app.services import scrape_assistant as core, assistant_conversation_context
    from tests.test_assistant_image_intakes import DB
    messages = [user(LINK), user(WHY), user(SHARED)]
    if text != SHARED:
        messages.append(user(CHECK))
    if text not in {SHARED, CHECK}:
        messages.append(user(text))
    thread = SimpleNamespace(id="thread", actor_id=1, job_id="job", messages=messages)
    monkeypatch.setattr(core, "claim", AsyncMock(return_value=True))
    monkeypatch.setattr(core, "AsyncSessionLocal", lambda: DB(thread))
    monkeypatch.setattr(core, "context", AsyncMock(return_value={
        "home": HOME, "samples": [{"ielts": 6, "englishVerification": "unknown"}]}))
    monkeypatch.setattr(assistant_conversation_context, "university_context", AsyncMock(return_value=[]))
    monkeypatch.setattr(documents, "review", AsyncMock())
    monkeypatch.setattr(core, "run_automatic_action", AsyncMock())
    await core.investigate("thread", "turn")
    documents.review.assert_awaited_once()
    request = documents.review.await_args.args[-1]
    assert request["_guidanceSource"] == LINK
    if text.startswith("veifiy"):
        assert request["_verificationPreview"] is True
        from app.services.assistant_pending_automation import supported
        assert not supported({"state":"proposed", "kind":"document_update", "_confirmationOnly":True})
    else:
        assert "1 of 1" in request["_readOnlyGuidanceContext"]
        assert "0 have verified" in request["_readOnlyGuidanceContext"]
    core.run_automatic_action.assert_not_awaited()
