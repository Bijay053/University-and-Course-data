"""Bounded, explicitly labelled reuse of owned official HTML, never live access."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import logging
import re

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.models.page_snapshot import PageSnapshot
from app.services.assistant_source_gate import same_course_url

MAX_BYTES = 2_500_000
MAX_ROWS = 250
MAX_AGE = timedelta(hours=24)
METHODS = {"intake:selected_start_option", "intake:bedfordshire_admission_data"}


def snapshot_valid(snapshot, job_id, university_id, url, now=None):
    from app.services.snapshot_store import build_s3_key
    now = now or datetime.now(timezone.utc)
    captured = snapshot.fetched_at
    return bool(
        captured and captured.tzinfo is not None
        and timedelta(0) <= now - captured <= MAX_AGE
        and snapshot.scrape_job_id == job_id and snapshot.university_id == university_id
        and snapshot.snapshot_type == "html" and snapshot.status_code == 200
        and snapshot.fetch_method not in {"wayback", "gemini", "openai"}
        and same_course_url(snapshot.course_url, url)
        and snapshot.url_hash == hashlib.sha256(snapshot.course_url.encode()).hexdigest()[:16]
        and snapshot.storage_path == build_s3_key(university_id, job_id, snapshot.course_url, "html")
        and 0 < (snapshot.content_length or 0) <= MAX_BYTES
    )


async def captured_fact(snapshot, row):
    """Read a retained copy and prove identity; no source fetch or timestamp refresh."""
    from app.services.snapshot_store import download_snapshot
    from app.services.scraper.extractors.intake import extract
    try:
        raw = await asyncio.wait_for(download_snapshot(snapshot.storage_path), 8)
        if not raw or len(raw) != snapshot.content_length or len(raw) > MAX_BYTES:
            return None
        html = raw.decode("utf-8", errors="strict")
        heading = BeautifulSoup(html, "html.parser").find("h1")
        fold = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
        if not heading or fold(heading.get_text(" ", strip=True)) != fold(row["name"]):
            return None
        from app.services.scraper.extractors.bedfordshire_intakes import extract_admission_intakes
        handled, results = extract_admission_intakes(
            html, snapshot.course_url, location=row.get("location"))
        results = [r for r in (results if handled else await extract(html, snapshot.course_url))
                   if r.method in METHODS and r.snippet]
        if len(results) != 1:
            return None
        result = results[0]
        return {
            "months": result.normalized["intake_months"], "quote": result.snippet,
            "url": snapshot.course_url, "capturedAt": snapshot.fetched_at.isoformat(),
            "snapshotId": snapshot.id, "sha256": hashlib.sha256(raw).hexdigest(),
            "method": result.method,
            "selectedLocation": result.extras.get("selected_location"),
        }
    except (TimeoutError, UnicodeError, ValueError, KeyError):
        return None


async def saved_plans(service, job_id, university_id, rows):
    """None means no fresh copies; an incomplete result never authorises other rows."""
    if not rows or len(rows) > MAX_ROWS:
        return None
    now = datetime.now(timezone.utc)
    urls = {variant for row in rows if row.get("url")
            for variant in (row["url"].rstrip("/"), row["url"].rstrip("/") + "/")}
    async with service.AsyncSessionLocal() as db:
        snapshots = (await db.execute(select(PageSnapshot).where(
            PageSnapshot.university_id == university_id,
            PageSnapshot.scrape_job_id == job_id,
            PageSnapshot.course_url.in_(urls),
            PageSnapshot.snapshot_type == "html", PageSnapshot.status_code == 200,
            PageSnapshot.fetched_at >= now - MAX_AGE, PageSnapshot.fetched_at <= now,
        ).order_by(PageSnapshot.fetched_at.desc()).limit(2 * MAX_ROWS))).scalars().all()
    if not snapshots:
        return None
    output = [{"id": row["id"], "name": row["name"], "url": row.get("url"),
               "status": "unverified", "changes": [],
               "reason": "No supported course-matched saved intake comparison was established."}
              for row in rows]
    semaphore = asyncio.Semaphore(6)

    async def read(row, plan):
        if row.get("status", "pending") != "pending":
            plan["reason"] = "This record is not pending; no intake change was proposed."
            return
        snapshot = next((s for s in snapshots
                         if snapshot_valid(s, job_id, university_id, row.get("url"), now)), None)
        if snapshot is None:
            return
        try:
            async with semaphore:
                fact = await captured_fact(snapshot, row)
        except Exception as exc:
            logging.getLogger(__name__).error(
                "Saved intake extraction failed for course %s (%s)", row["id"], type(exc).__name__)
            plan["reason"] = "Saved-source extraction encountered an internal error; no intake update was proposed."
            return
        if fact:
            changes = [{"field": "intake_months", "oldValue": row.get("intakes") or [],
                        "newValue": fact["months"]}]
            plan.update(
                status="supported", changes=changes, reason="",
                source={"url": fact["url"], "quote": fact["quote"],
                        "capturedAt": fact["capturedAt"], "origin": "saved_official_html",
                        "selectedLocation": fact["selectedLocation"]},
                _snapshot=fact,
            )
    tasks = [asyncio.create_task(read(row, plan)) for row, plan in zip(rows, output)]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), 90)
    except TimeoutError:
        # gather cancels unfinished reads. Their visible plans stay unverified.
        pass
    return output


async def prepare_saved_review(service, thread_id, turn_id, run_id, ctx, rows):
    job_id = (ctx.get("job") or {}).get("id")
    if not job_id or not ctx.get("university_id"):
        return False
    plans = await saved_plans(service, job_id, ctx["university_id"], rows)
    if plans is None:
        return False
    supported = [p for p in plans if p["status"] == "supported"]
    proposed = [p for p in supported
                if p["changes"][0]["oldValue"] != p["changes"][0]["newValue"]]
    public = [{k: v for k, v in p.items() if not k.startswith("_")} for p in plans]
    actions = []
    if proposed:
        ids = [p["id"] for p in proposed]
        action = service.action_proposal({"kind": "fix_fields", "fields": ["intake_months"]}, ids)
        action.update(
            label="Apply intakes from saved official course pages",
            _versions=ctx["versions"], _configVersion=ctx["config_version"],
            _savedIntakeTargets=proposed, _savedIntakeJobId=job_id,
            requiresConfirmation=True, repairPreview=[p for p in public if p["id"] in ids],
            courseScope=[{"id": r["id"], "name": r["name"], "url": r["url"]}
                         for r in rows if r["id"] in ids],
        )
        actions.append(action)
    unverified = [p for p in public if p["status"] == "unverified"]
    exceptions = ("\n\nCourses left unverified and unchanged:\n" + "\n".join(
        f"- {p['name']}: {p['reason']}" for p in unverified)) if unverified else ""
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": (
            f"Assessed {len(rows)} existing pending courses using saved official HTML from this scrape. "
            f"{len(supported)} have supported course-owned intake evidence; "
            f"{len(proposed)} changes proposed, {len(supported)-len(proposed)} unchanged, "
            f"{len(rows)-len(supported)} unverified. "
            "These are captured pages, not fresh website checks; each source's capture date is shown. "
            "No university website request, catalogue discovery, course creation or publication was performed. "
            "Only the supported intake changes can run through the existing approval policy; other fields are preserved."
            + exceptions
        ),
        "actions": actions, "sources": [], "intakeReview": public,
    }, detail="Saved official intake evidence assessed; no live request.")
    return True
