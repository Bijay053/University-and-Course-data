"""One explicitly scoped pending-row inventory for UI and assistant answers."""
from sqlalchemy import select,func
from app.models import ScrapedCourse


async def pending_counts(db,university_id,job_id):
    from app.services.scrape_assistant import REVIEW_STATUSES
    groups=(await db.execute(select(ScrapedCourse.scrape_job_id,func.count(ScrapedCourse.id))
        .where(ScrapedCourse.university_id==university_id,ScrapedCourse.status.in_(REVIEW_STATUSES))
        .group_by(ScrapedCourse.scrape_job_id))).all()
    return {"providerPendingRows":sum(count for _,count in groups),
            "thisJobPendingRows":next((count for job,count in groups if job==job_id),0),
            "pendingByJob":[{"jobId":job,"pendingRows":count} for job,count in groups],
            "scope":"Pending database rows, not unique courses or displayed/filtered groups"}
