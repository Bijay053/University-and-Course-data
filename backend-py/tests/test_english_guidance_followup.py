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
async def test_signed_assistant_dispatch_matches_existing_courses_and_preserves_intent(monkeypatch, text):
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
        assert request["_verificationPreview"] is False
    else:
        assert "1 of 1" in request["_readOnlyGuidanceContext"]
        assert "0 have verified" in request["_readOnlyGuidanceContext"]
    core.run_automatic_action.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt,actor,auto_run,mutation,automatic", [
    ("veifiy all course ilets score", 1, True, None, True),
    ("Verify all courses IELTS scores", 1, True, None, True),
    ("Check all courses IELTS scores", 1, True, None, True),
    ("Verify all courses IELTS scores", 2, True, None, False),
    ("Verify all courses IELTS scores", 1, False, None, False),
    ("Show a preview to verify all courses IELTS scores", 1, True, None, False),
    ("Read-only: verify all courses IELTS scores", 1, True, None, False),
    ("Verify all courses IELTS scores; do not run anything", 1, True, None, False),
    ("Verify all courses IELTS scores", 1, True, "published", False),
    ("Verify all courses IELTS scores", 1, True, "unmatched", False),
    ("Verify all courses IELTS scores", 1, True, "old-proof", False),
])
async def test_verified_document_batch_follows_actor_opt_in_not_a_forced_preview(
    monkeypatch, prompt, actor, auto_run, mutation, automatic,
):
    from app.services import assistant_pending_automation as policy
    from app.services.scrape_assistant_autorun import choose_action
    from app.services.english_html_tables import APPLICABILITY_VERSION
    request = {**user(prompt), "autoRun": auto_run}
    thread = SimpleNamespace(job_id="job", messages=[user(LINK), request])
    service = SimpleNamespace(context=AsyncMock(return_value={"home":HOME,"samples":[]}))
    action = {"id":"checked","kind":"document_update","state":"proposed","courseCount":45,
              "requiresConfirmation":True,"_sourceType":"html",
              "_applicabilityVersion":APPLICABILITY_VERSION,
              "_targets":[{"id":i,"kind":"pending","version":"reviewed",
                           "changes":[{"field":"ielts_overall","newValue":6}]} for i in range(45)]}
    if mutation == "published":
        action["_targets"][0]["kind"] = "published"
    elif mutation == "unmatched":
        action["_targets"][0]["changes"] = []
    elif mutation == "old-proof":
        action["_applicabilityVersion"] = -1
    async def reviewed(*args):
        # The real document reviewer maps this request marker to its immutable
        # confirmation-only action flag. Ordinary actor policy then decides.
        if args[-1].get("_verificationPreview"):
            action["_confirmationOnly"] = True
        policy.prepare({"actions":[action]},request,
                       SimpleNamespace(scrape_assistant_auto_pending_user_ids="1"),actor)
    monkeypatch.setattr(documents, "review", AsyncMock(side_effect=reviewed))
    handled = await follow.handle(service,("thread","turn","run"),thread,request,[])
    if not handled:
        from app.services.scrape_assistant_autorun import execution_requested
        assert not automatic and not execution_requested(prompt)
        documents.review.assert_not_awaited()
        return
    assert bool(action.get("automaticPending")) is automatic
    assert action["requiresConfirmation"] is not automatic
    assert (choose_action([action],prompt) is action) is automatic
    assert documents.review.await_args.args[-1]["_guidanceSource"] == LINK
