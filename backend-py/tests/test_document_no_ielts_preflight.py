from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import assistant_document_review as review


@pytest.mark.parametrize("source,expected", [
    ({"text": "International tuition fees, postgraduate masters £17,600"}, False),
    ({"_fullText": "IELTS Academic overall 6.5, components 6.0"}, True),
    ({"_pages": [{"text": "International English Language Testing System score: 6.5"}]}, True),
    ({"text": "PTE 65"}, False),
])
def test_fee_and_other_test_documents_are_not_ielts_evidence(source, expected):
    assert review.contains_ielts_requirements(source) is expected


@pytest.mark.asyncio
async def test_fee_source_stops_before_course_reads_or_model_matching(monkeypatch):
    from app.services import assistant_documents
    from app.services.scrape_agent_v2 import model
    ctx = {"university_id": 114, "eligible_ids": [1], "home": "https://www.cardiffmet.ac.uk"}
    monkeypatch.setattr(review.service, "context", AsyncMock(return_value=ctx))
    result = MagicMock()
    result.scalars.return_value.all.return_value = [SimpleNamespace(id=1, course_name="MRes Management")]
    session = AsyncMock()
    session.execute.return_value = result
    manager = AsyncMock()
    manager.__aenter__.return_value = session
    monkeypatch.setattr(review.service, "AsyncSessionLocal", lambda: manager)
    monkeypatch.setattr(assistant_documents, "read_uploaded", AsyncMock(return_value={
        "verified": True, "sourceType": "pdf", "url": "https://university.example/fees.pdf",
        "_fullText": "Full-time International Students. Postgraduate Masters including MRes £17,600",
    }))
    complete = AsyncMock()
    monkeypatch.setattr(model, "complete", complete)
    save = AsyncMock()
    monkeypatch.setattr(review.service, "save_research_reply", save)
    inspect = AsyncMock()
    monkeypatch.setattr(review, "inspect_source", inspect)
    await review.review("thread", "turn", "run", SimpleNamespace(job_id="job", actor_id=1),
                        {"content": "Read-only: check fees and IELTS from this document",
                         "_documentToken": "staff-upload"})
    complete.assert_not_awaited()
    inspect.assert_not_awaited()
    reply = save.await_args.args[3]
    assert reply["sourceOutcome"] == "no_ielts_requirements"
    assert reply["actions"] == []
    assert reply["documentReview"]["checked"] == 0
    assert reply["documentReview"]["notChecked"] == 1
