import pytest
from app.models import ScrapedCourse
from app.services.review_counts import pending_counts
from tests.test_scrape_prompt_assistant import store,assistant_postgres


@pytest.mark.asyncio
async def test_provider_counts_are_not_filtered_review_counts(store):
    async with store() as db:
        db.add(ScrapedCourse(id=990,university_id=1,course_name="Another course",
            scrape_job_id="different-review",status="pending"))
        db.add(ScrapedCourse(id=991,university_id=1,course_name="Excluded course",
            scrape_job_id="different-review",status="rejected"))
        await db.commit()
        counts=await pending_counts(db,1,"source")
        assert counts["providerPendingRows"]==2
        assert counts["thisJobPendingRows"]==1
        assert sum(r["pendingRows"] for r in counts["pendingByJob"])==2
