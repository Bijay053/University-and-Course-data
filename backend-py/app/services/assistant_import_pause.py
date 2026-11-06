"""Provider import holds: status and resume never approve or dispatch courses."""
from fastapi import HTTPException
from sqlalchemy import select
from app.models import University, ScrapeRuntimeJob
from app.models.user import User

IMPORT_KINDS = {"scan_catalogue", "recover_catalogue", "review_courses", "repair_catalogue"}
PAUSE_REASON = "Assistant imports are paused until provider consolidation is reviewed and complete. Pending field repairs remain available."


async def provider_for_job(db, job_id, *, lock=False):
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job or job.university_id is None:
        raise HTTPException(404, "Scrape job not found.")
    query = select(University).where(University.id == job.university_id)
    if lock:
        query = query.with_for_update()
    university = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
    if not university:
        raise HTTPException(404, "University not found.")
    return university


def is_paused(university):
    # Match the deployed SQL guard, including JSON string "true".
    value = (university.scrape_config or {}).get("assistant_imports_paused")
    return value is True or value == "true"


async def live_owner(db, actor, *, lock=False):
    query = select(User).where(User.id == int(actor["id"]))
    if lock:
        query = query.with_for_update()
    user = (await db.execute(query.execution_options(populate_existing=True))).scalar_one_or_none()
    return bool(user and user.is_active and user.is_super_admin)


def status(university, can_resume):
    paused = is_paused(university)
    return {
        "universityId": university.id,
        "universityName": university.name,
        "paused": paused,
        "reason": PAUSE_REASON if paused else None,
        "canResume": can_resume,
    }
