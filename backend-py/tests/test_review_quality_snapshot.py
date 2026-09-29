"""Quality in review must be derived from the row snapshot shown to the reviewer."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.models.scraped_course import ScrapedCourse
from app.routers import scrape
from app.services.scraper import replay_extraction


def test_running_stage_cannot_attach_old_degree_warning_to_new_review_row(monkeypatch):
    async def scope(_db, _job):
        return ["running-job"], 7, [], None

    async def no_op(*_args):
        pass

    monkeypatch.setattr(replay_extraction, "continuation_review_scope", scope)
    monkeypatch.setattr(scrape, "_attach_evidence_counts_bulk", no_op)
    monkeypatch.setattr(scrape, "_attach_recovery_counts_bulk", no_op)

    row = ScrapedCourse(
        id=101, scrape_job_id="running-job", university_id=7,
        status="pending", course_name="MA Banking and Finance",
        degree_level=None, course_website="https://example.test/ma-banking",
        international_fee=20000, currency="GBP", fee_term="Annual",
        ielts_overall=6.5, study_mode="On Campus", course_location="Cambridge",
        duration=1, duration_term="year", intake_months=["September"],
        auto_publish_status="pending_review",
    )

    # Reproduce the old two-request race: quality reads a running scrape's
    # incomplete row, then the scraper fills degree_level before review reads.
    old_quality = scrape._score_quality_rows([scrape._staged_row_to_dict(row)])["courses"][0]
    assert "missing_degree_level" in {issue["code"] for issue in old_quality["issues"]}
    row.degree_level = "Master"

    db = SimpleNamespace(
        get=AsyncMock(return_value=None),
        execute=AsyncMock(return_value=SimpleNamespace(
            scalars=lambda: SimpleNamespace(all=lambda: [row]),
        )),
    )
    review = asyncio.run(scrape.staged_one("running-job", db, view="summary"))
    course = review["courses"][0]
    assert course["degreeLevel"] == "Master"
    assert course["courseQuality"]["breakdown"]["degree_level"]["fill"] is True
    assert "missing_degree_level" not in {
        issue["code"] for issue in course["courseQuality"]["issues"]
    }
    assert course["courseQuality"]["score"] > old_quality["score"]