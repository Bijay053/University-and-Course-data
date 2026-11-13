"""Exercise the full blocked-document branch, including deferred imports.

No network, model calls, course writes or real database are needed to reproduce
the release dependency failure observed on the LSBU production review.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import assistant_document_review as review_module
from app.services import assistant_source_cache, university_source_access

HOME = "https://www.lsbu.ac.uk"
PDF = HOME + "/_media_old/pdf/guidelines/Eng-lang-requirements.pdf"


class ReadOnlySession:
    def __init__(self, rows):
        self.rows = rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def execute(self, query):
        assert query.is_select, "This regression must not write course data"
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: self.rows))


@pytest.mark.asyncio
@pytest.mark.parametrize("supplied_url,prompt_tail", [
    (PDF, "update each band ielts for all courses"),
    (HOME + "/international/english-language-requirements",
     "ielts score are on this link update on all courses"),
])
async def test_robots_403_returns_complete_document_report_without_crashing(monkeypatch, supplied_url, prompt_tail):
    rows = [
        SimpleNamespace(id=i, course_name=f"Course {i}", degree_level="Bachelor",
                        academic_level="Undergraduate")
        for i in range(1, 228)
    ]
    service = review_module.service
    monkeypatch.setattr(service, "context", AsyncMock(return_value={
        "university_id": 1, "eligible_ids": [r.id for r in rows],
        "home": HOME, "config": None,
    }))
    monkeypatch.setattr(service, "AsyncSessionLocal", lambda: ReadOnlySession(rows))
    monkeypatch.setattr(service, "SOURCE_ACCESS_REGISTRY", True)
    save = AsyncMock()
    monkeypatch.setattr(service, "save_research_reply", save)
    source = {
        "url": supplied_url, "verified": False,
        "sourceType": "pdf" if supplied_url == PDF else "html",
        "failureStage": "robots", "accessIssue": "robots_unavailable",
        "httpStatus": 403, "sourceRequestAttempted": False,
        "error": "Cloudflare challenged the robots.txt request (HTTP 403); "
                 "the document was not requested.",
    }
    reader = AsyncMock(return_value=source)
    monkeypatch.setattr(assistant_source_cache, "cached_source", reader)
    from app.services import assistant_official_sources
    monkeypatch.setattr(assistant_official_sources, "configured_english_urls",
                        lambda *args: [HOME + "/unrelated-configured-english.pdf"])
    monkeypatch.setattr(assistant_official_sources, "english_guidance_alternatives",
                        lambda *args: [])
    monkeypatch.setattr(university_source_access, "entry", AsyncMock(return_value=None))
    # Keep the actual registry-discovery symbol/import under test. Its SELECT
    # uses a separate mocked result, with no learned links.
    original_discovery = university_source_access.discovered_guidance_urls

    async def discover(db, home):
        return await original_discovery(ReadOnlySession([]), home)

    monkeypatch.setattr(university_source_access, "discovered_guidance_urls", discover)
    monkeypatch.setattr(review_module, "inspect_source",
                        AsyncMock(side_effect=AssertionError("No network retry")))
    await review_module.review("thread", "turn", "run",
                               SimpleNamespace(job_id="job", actor_id=1), {
                                   "content": supplied_url + "\n" + prompt_tail,
                               })
    reader.assert_awaited_once()
    assert reader.call_args.args[4] == supplied_url
    review_module.inspect_source.assert_not_awaited()
    save.assert_awaited_once()
    reply = save.call_args.args[3]
    assert reply["sourceOutcome"] == "access_verification_failed"
    assert reply["actions"] == []
    assert reply["coursesUpdated"] == 0
    report = reply["documentReview"]
    assert (report["checked"], report["proposed"], report["notChecked"]) == (0, 0, 227)
    assert len(report["rows"]) == 227
    assert all(row["status"] == "not_checked" for row in report["rows"])
    assert report["accessError"]["url"] == supplied_url
    assert "HTTP 403" in report["accessError"]["message"]
