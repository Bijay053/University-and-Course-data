"""Course-matched location comparisons from explicitly dated retained HTML."""
import asyncio
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import urldefrag, urlsplit

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.page_snapshot import PageSnapshot
from app.services.assistant_saved_intake_sources import MAX_AGE, MAX_BYTES, MAX_ROWS, snapshot_valid

PLACEHOLDERS = {"all locations", "all campuses"}


def resolve_request(prompt, messages=()):
    """Continue an uninterrupted user location repair, never another topic."""
    short = re.compile(
        r"\s*(?:please\s+)?(?:fix|repair|correct)\s+"
        r"(?:(?:this|that|the|same)\s+)?(?:it|bug|issue|problem)"
        r"(?:\s+(?:please|again))?[.!?\s]*", re.I)
    if not short.fullmatch(prompt):
        return prompt
    previous = [m.get("content", "") for m in messages if m.get("role") == "user"]
    if previous and previous[-1] == prompt:
        previous.pop()
    for prior in reversed(previous):
        if short.fullmatch(prior):
            continue
        if (re.search(r"\ball\s+(?:locations|campuses)\b", prior, re.I)
                and re.search(r"\b(?:fix|correct|repair)\b", prior, re.I)
                and not re.search(r"\b(?:do not|don't|never|read[- ]only)\b", prior, re.I)):
            return prior
        break
    return prompt


def is_placeholder(value):
    return isinstance(value, str) and " ".join(value.casefold().split()) in PLACEHOLDERS


def requested_rows(prompt, rows, university_name=""):
    """A generic placeholder complaint targets bad values, not a course name."""
    if (not re.search(r"\ball\s+(?:locations|campuses)\b", prompt, re.I)
            or re.search(r"\bread[- ]only\b|\b(?:do not|don't|never|only explain)\b", prompt, re.I)
            or not re.search(r"\b(?:fix|correct|repair)\b", prompt, re.I)
            or re.search(r"\b(?:set|replace|change|add)\b|\bto\s+\w+", prompt, re.I)):
        return None
    ids = {int(value) for value in re.findall(
        r"\b(?:row|record)\s*#?\s*(\d+)\b", prompt, re.I)}
    urls = {urldefrag(value.rstrip(".,)"))[0].rstrip("/")
            for value in re.findall(r"https?://[^\s<>]+", prompt)}
    fold = lambda value: " ".join(re.findall(r"\w+", value.casefold()))
    if ids:
        if not ids <= {r["id"] for r in rows}:
            return []
        selected = [r for r in rows if r["id"] in ids]
    elif urls:
        selected = [r for r in rows if urldefrag(
            r.get("url") or "")[0].rstrip("/") in urls]
    else:
        # "All locations" is the erroneous stored label, not an instruction
        # to broaden a named or numbered course selector to every record.
        selected = [r for r in rows if r.get("name") and
                    f" {fold(r['name'])} " in f" {fold(prompt)} "]
    if not selected and not (ids or urls):
        text = re.sub(r"\ball\s+(?:locations|campuses)\b", "", prompt, flags=re.I)
        for name in filter(None, (university_name, re.sub(
                r"\b(?:university|of|the)\b", "", university_name, flags=re.I).strip())):
            text = re.sub(r"(?<!\w)" + re.escape(name) + r"(?!\w)", "", text, flags=re.I)
        words = set(re.findall(r"\w+", text.casefold()))
        allowed = set(
            "what why is are this that these those the a an in for of on all "
            "course courses location locations campus campuses field fields value values "
            "fix correct repair bug bugs issue issues wrong incorrect please "
            "current review pending existing showing show displayed as recorded stored "
            "only using official saved source sources page pages".split())
        if not words <= allowed:
            return []
        selected = rows
    return [r for r in selected if is_placeholder(r.get("location"))]


async def captured_location(snapshot, row):
    from app.services.snapshot_store import download_snapshot
    from app.services.scraper.extractors.bedfordshire_intakes import extract_admission_location
    raw = await asyncio.wait_for(download_snapshot(snapshot.storage_path), 8)
    if not raw or len(raw) != snapshot.content_length or len(raw) > MAX_BYTES:
        return None
    html = raw.decode("utf-8", errors="strict")
    heading = BeautifulSoup(html, "html.parser").find("h1")
    fold = lambda value: " ".join(re.findall(r"\w+", value.casefold()))
    if not heading or fold(heading.get_text(" ", strip=True)) != fold(row["name"]):
        return None
    handled, results = extract_admission_location(html, snapshot.course_url)
    if not handled or len(results) != 1 or not results[0].snippet:
        return None
    result = results[0]
    return {"value": result.value, "quote": result.snippet,
            "url": snapshot.course_url, "capturedAt": snapshot.fetched_at.isoformat(),
            "snapshotId": snapshot.id, "sha256": hashlib.sha256(raw).hexdigest(),
            "method": result.method}


async def saved_plans(service, job_id, university_id, rows):
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
            PageSnapshot.fetched_at <= now,
        ).order_by(PageSnapshot.fetched_at.desc()).limit(2 * MAX_ROWS))).scalars().all()
        methods = dict((await db.execute(select(
            ScrapedCourse.id, ScrapedCourse.extraction_method,
        ).where(ScrapedCourse.id.in_([r["id"] for r in rows]),
                ScrapedCourse.university_id == university_id,
                ScrapedCourse.scrape_job_id == job_id))).all())
    if not snapshots:
        return None
    output = [{"id": r["id"], "name": r["name"], "url": r.get("url"),
               "status": "unverified", "changes": [],
               "reason": "No unambiguous course-owned campus was established from an eligible saved page."}
              for r in rows]
    semaphore = asyncio.Semaphore(6)

    async def read(row, plan):
        method = str((methods.get(row["id"]) or {}).get("course_location", "")).casefold()
        if ("staff" in method or "manual" in method or row.get("status", "pending") != "pending"
                or row["id"] not in methods or not is_placeholder(row.get("location"))):
            plan["reason"] = "This row is not an automatically repairable pending location placeholder."
            return
        snapshot = next((s for s in snapshots if snapshot_valid(
            s, job_id, university_id, row.get("url") or "")), None)
        if snapshot is None:
            expired = next((s for s in snapshots if s.fetched_at < now - MAX_AGE
                and snapshot_valid(s, job_id, university_id, row.get("url") or "",
                                   now=s.fetched_at)), None)
            if expired:
                plan["reason"] = (
                    f"The saved official page was captured {expired.fetched_at.isoformat()} "
                    "and exceeds the 24-hour automatic-repair limit. Its content was not "
                    "read or treated as current evidence. A permitted fresh source is required."
                )
            return
        try:
            async with semaphore:
                fact = await captured_location(snapshot, row)
            if fact:
                plan.update(status="supported", reason="", _snapshot=fact,
                    changes=[{"field": "course_location", "oldValue": row["location"],
                              "newValue": fact["value"]}],
                    source={"url": fact["url"], "quote": fact["quote"],
                            "capturedAt": fact["capturedAt"], "origin": "saved_official_html"})
        except Exception:
            plan["reason"] = "The saved course page could not be read completely; this row remains unchanged."

    tasks = [asyncio.create_task(read(row, plan)) for row, plan in zip(rows, output)]
    try:
        await asyncio.wait_for(asyncio.gather(*tasks), 90)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return output


async def prepare_review(service, thread_id, turn_id, run_id, ctx, rows):
    job_id = (ctx.get("job") or {}).get("id")
    if not job_id or not ctx.get("university_id") or not rows:
        return False
    # Other providers keep their existing live-source path until they have a
    # registered course-owned retained-location extractor of their own.
    if urlsplit(ctx.get("home") or "").hostname not in {"beds.ac.uk", "www.beds.ac.uk"}:
        return False
    plans = await saved_plans(service, job_id, ctx["university_id"], rows)
    if plans is None:
        return False
    supported = [p for p in plans if p["status"] == "supported"]
    public = [{k: v for k, v in p.items() if not k.startswith("_")} for p in plans]
    actions = []
    if supported:
        ids = [p["id"] for p in supported]
        action = service.action_proposal(
            {"kind": "fix_fields", "fields": ["course_location"]}, ids)
        action.update(label="Correct course locations from saved official pages",
            _versions=ctx["versions"], _configVersion=ctx["config_version"],
            _savedLocationTargets=supported, _savedLocationJobId=job_id,
            requiresConfirmation=True,
            repairPreview=[p for p in public if p["id"] in ids],
            courseScope=[{"id": r["id"], "name": r["name"], "url": r["url"]}
                         for r in rows if r["id"] in ids])
        actions.append(action)
    exceptions = [p for p in plans if p["status"] != "supported"]
    detail = "\n".join(f"- {p['name']}: {p['reason']}" for p in exceptions)
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": (
            f"Assessed {len(rows)} pending location placeholders using saved-source metadata "
            "and any eligible official course pages. "
            f"{len(supported)} course-specific location corrections prepared; "
            f"{len(exceptions)} unverified and unchanged. "
            "These are dated captured pages, not fresh website checks. "
            "Only the supported location corrections can run under the existing approval policy. "
            "Other locations, fees, IELTS, intakes, delivery modes and publication status are preserved. "
            "No website requests, catalogue discovery or course creation were performed."
            + ("\n\nNot safely matched:\n" + detail if detail else "")
        ), "actions": actions, "sources": [],
    }, detail="Saved official course locations assessed; no live request.")
    return True
