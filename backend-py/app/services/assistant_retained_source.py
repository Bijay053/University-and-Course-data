"""Recover recorded course text, not model output, after an incomplete live read."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_assistant import ScrapeAssistantThread

MAX_AGE = timedelta(hours=24)
MAX_BYTES = 100_000


def recent_capture(captured, now=None):
    try:
        captured = datetime.fromisoformat(captured)
        return captured.tzinfo is not None and timedelta(0) <= (
            (now or datetime.now(timezone.utc)) - captured) <= MAX_AGE
    except (TypeError, ValueError):
        return False


def recorded_page_text(raw, url, expected_length):
    if not raw or len(raw) > MAX_BYTES or len(raw) != expected_length:
        return None
    try:
        prompt = raw.decode("utf-8", errors="strict")
    except UnicodeError:
        return None
    for cue in ('Course page text (truncated):\n"""\n',
                'Course page text (may be truncated):\n"""\n'):
        if cue not in prompt:
            continue
        header, body = prompt.split(cue, 1)
        # Only the page-input block is evidence. Template instructions, field
        # hints and model responses must never become source facts.
        if f"\nCourse URL: {url}\n" not in header or not body.endswith('\n"""\n'):
            return None
        return body[:-5]
    return None


async def retained_course_source(service, thread_id, turn_id, run_id, url, failure):
    if (failure.get("verified") or failure.get("accessIssue")
            or failure.get("sourceType") == "pdf"
            or failure.get("failureStage") != "source_body"
            or failure.get("httpStatus") != 200
            or failure.get("restrictionKind") != "incomplete_response"):
        return None
    value = urlsplit(url)
    variants = [value._replace(path=value.path.rstrip("/") + suffix).geturl() for suffix in ("", "/")]
    observed_at = datetime.now(timezone.utc)
    cutoff = observed_at - MAX_AGE
    async with service.AsyncSessionLocal() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        if not thread or thread.turn_id != turn_id or thread.run_id != run_id:
            return None
        rows = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.scrape_job_id == thread.job_id,
            ScrapedCourse.course_website.in_(variants),
            ScrapedCourse.status == "pending",
        ))).scalars().all()
        if len(rows) != 1:
            return None
        row = rows[0]
        snapshots = (await db.execute(select(PageSnapshot).where(
            PageSnapshot.university_id == row.university_id,
            PageSnapshot.scrape_job_id == thread.job_id,
            PageSnapshot.course_url.in_(variants),
            PageSnapshot.snapshot_type == "ai_prompt",
            PageSnapshot.status_code == 200,
            PageSnapshot.content_length <= MAX_BYTES,
            PageSnapshot.fetched_at >= cutoff,
            PageSnapshot.fetched_at <= observed_at,
        ).order_by(PageSnapshot.fetched_at.desc()).limit(4))).scalars().all()
        name = row.course_name or ""
        job_id, university_id = thread.job_id, row.university_id
    from app.services.snapshot_store import build_s3_key, download_snapshot
    seen = set()
    for snapshot in snapshots:
        chunk = (snapshot.original_extraction or {}).get("chunk")
        if type(chunk) is not int or not 1 <= chunk <= 10:
            continue
        expected = build_s3_key(university_id, job_id, snapshot.course_url, "ai_prompt", page_number=chunk)
        digest = hashlib.sha256(snapshot.course_url.encode()).hexdigest()[:16]
        if (snapshot.storage_path != expected or snapshot.url_hash != digest
                or expected in seen or snapshot.fetch_method not in {"gemini", "openai"}):
            continue
        seen.add(expected)
        try:
            raw = await asyncio.wait_for(download_snapshot(expected), 2)
        except TimeoutError:
            continue
        body = recorded_page_text(raw, snapshot.course_url, snapshot.content_length)
        normalized = lambda s: re.sub(r"\W+", " ", s.casefold()).strip()
        if not body or len(name) < 6 or normalized(name) not in normalized(body[:1200]):
            continue
        from app.services.scrape_assistant_tools import evidence_excerpts
        from app.services.assistant_source_cache import refresh_ielts_facts
        captured = snapshot.fetched_at.isoformat()
        source = await refresh_ielts_facts({
            "url":url, "sourceType":"html", "verified":True,
            "title":f"{name} — saved course text ({captured})", "courseTitle":name,
            "text":body[:24000], "_fullText":body, "facts":[],
            "sourceOrigin":"retained_scrape_page_text", "sourceCapturedAt":captured,
            "_retainedSnapshotId":snapshot.id,
            "_liveReadFailure":{"error":failure.get("error"),"failureStage":"source_body","httpStatus":200},
        }, url)
        # Retained input is not a generic substitute for an unreadable source:
        # it must actually contain a directly quoted complete IELTS bundle.
        from app.services.assistant_repair_proposals import _course_ielts
        if not _course_ielts(source):
            continue
        source["excerpts"] = evidence_excerpts(body, source["facts"])
        return source
    return None
