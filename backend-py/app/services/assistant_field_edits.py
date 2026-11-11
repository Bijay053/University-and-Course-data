"""Route explicit existing-course field edits before any discovery inference."""
import json
import re
import uuid
from sqlalchemy import select
from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services.scraper.excelsia_source import facts, intake_months, owned


def edit_field(prompt):
    if re.search(r"\b(?:don't|do not|never)\s+(?:add|set|change|update|edit)\b", prompt, re.I):
        return None
    if re.search(r"\badd\s+(?:this\s+)?course\b", prompt, re.I):
        return None
    match = re.search(r"\b(?:add|set|change|update|edit)\s+(?:the\s+)?"
                      r"(intakes?|fees?|tuition|ielts|duration|location|campus|course name)\b", prompt, re.I)
    return match.group(1).lower() if match else None


def evidence_edit_field(prompt, messages=()):
    """Resolve a field-only follow-up, never reuse an assistant's numeric claim."""
    direct = edit_field(prompt)
    if direct:
        return direct
    if not re.search(r"\b(?:add|set|change|update|edit|fix|correct)\b", prompt, re.I):
        return None
    if re.search(r"\b(?:don't|do not|never|only explain|read.only)\b", prompt, re.I):
        return None
    if not re.search(r"\b(?:each|all|individual|missing)\s+(?:ielts\s+)?bands?\b", prompt, re.I):
        return None
    if re.search(r"\bielts\b", prompt, re.I):
        return "ielts"
    # Only the immediately preceding user topic can supply the test identity.
    # Page/assistant text is not an instruction or a staff-provided value.
    previous = [m.get("content", "") for m in messages if m.get("role") == "user"]
    if previous and previous[-1] == prompt:
        previous.pop()
    if previous and re.search(r"\bielts\b", previous[-1], re.I):
        return "ielts"
    return None


async def handle_field_edit(service, thread_id, turn_id, run_id, ctx, prompt, messages=()):
    # A document is evidence to investigate, not a literal replacement value.
    # In particular "update each band IELTS" used to fall into unsupported
    # manual editing before the source reader ever saw the supplied PDF.
    if document_research_requested(prompt):
        return False
    from app.services.assistant_eligibility import scope
    rows = scope(prompt, ctx["samples"])
    field = evidence_edit_field(prompt, messages)
    if not field:
        # On an existing course, an unrecognised field must not become an
        # implicit add-course operation. Explicit add-course requests retain
        # their separate discovery path.
        explicit_course = re.search(r"\badd\s+(?:this\s+)?course\b", prompt, re.I)
        from app.services.scrape_assistant_autorun import execution_requested
        if (rows and not explicit_course and execution_requested(prompt)
                and re.search(r"\b(?:add|set|change|update|edit)\s+\w+", prompt, re.I)):
            field = "unsupported"
        else:
            return False
    if not rows and field in {"intake", "intakes"}:
        # A recovery may have moved the course to another pending review.
        # Resolve it without creating yet another row; ambiguous matches stop.
        async with service.AsyncSessionLocal() as db:
            candidates = (await db.execute(select(ScrapedCourse).where(
                ScrapedCourse.university_id == ctx["university_id"],
                ScrapedCourse.status.in_(service.REVIEW_STATUSES),
            ))).scalars().all()
            mapped = [{"id": r.id, "name": r.course_name, "url": r.course_website,
                       "jobId": r.scrape_job_id} for r in candidates]
            rows = scope(prompt, mapped)
            selected = {r["id"] for r in rows}
            ctx = {**ctx, "versions": {**ctx["versions"], **{
                str(r.id): service.row_version(r) for r in candidates if r.id in selected
            }}}
    from app.services.scrape_assistant_autorun import execution_requested
    if not execution_requested(prompt):
        content = "This is an edit to an existing course field, not a request to add a course. Read-only request: no row changed or course fetched."
    elif not rows:
        # An edit never silently turns into discovery, even when the row moved.
        content = "No existing pending course matched this field edit. Give the current row number or exact course name; no course was fetched or created."
    elif field not in {"intake", "intakes"}:
        field_map = {"fee": "international_fee", "fees": "international_fee", "tuition": "international_fee",
                     "ielts": "ielts_overall", "duration": "duration", "location": "course_location", "campus": "course_location"}
        if field in field_map:
            action = service.action_proposal({"kind": "fix_fields", "fields": [field_map[field]]}, [r["id"] for r in rows])
            from app.services.assistant_source_gate import require_reachable_sources
            from app.services.assistant_source_cache import cached_source
            async def inspect(url,home):
                return await cached_source(service,thread_id,turn_id,run_id,url,home,
                                           service.inspect_source,12)
            try:
                await require_reachable_sources([r.get("url") for r in rows],ctx["home"],inspect)
            except ValueError as exc:
                await service.save_research_reply(thread_id,turn_id,run_id,{
                    "role":"assistant","createdAt":service.now().isoformat(),
                    "content":str(exc),"actions":[],"sources":[],
                },detail="Source access failed; no bulk fix queued.")
                return True
            action.update(_versions=ctx["versions"], _configVersion=ctx["config_version"],
                          requiresConfirmation=len(rows) > 10)
            await service.save_research_reply(thread_id, turn_id, run_id, {
                "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
                "content": (
                    "Prepared IELTS recovery for existing pending rows. Each course will be checked "
                    "against its own applicable official requirements, including Listening, Reading, "
                    "Writing and Speaking; the example course's scores will not be copied to other "
                    "courses. Unavailable or conflicting requirements remain unresolved."
                    if field == "ielts" else
                    "Checking and correcting the requested field on existing pending rows from official evidence. No new course will be created."
                ) + (" Confirm the batch before it runs." if len(rows) > 10 else ""),
                "actions": [action], "sources": [],
            }, detail="Existing-row field correction prepared.")
            return True
        content = "This field requires the existing course's edit form. No new course was fetched or created."
    elif len(rows) != 1:
        content = "This intake edit matched more than one pending row. Specify one row number; no values were changed."
    else:
        row = rows[0]
        requested = intake_months(prompt)
        from app.services.assistant_source_cache import cached_source
        from app.services.scrape_assistant_tools import inspect_source
        source = await cached_source(service, thread_id, turn_id, run_id, row["url"], ctx["home"], inspect_source, 12)
        proof = source.get("_excelsiaIntake", {})
        if (not requested or not source.get("verified") or not owned(row["url"]) or
            (proof.get("name") or "").casefold() != row["name"].casefold() or proof.get("months") != requested):
            content = "The requested intake values could not be confirmed for this course from its official Intake line. No row was changed or created."
        else:
            async with service.AsyncSessionLocal() as db:
                thread = (await db.execute(select(ScrapeAssistantThread).where(
                    ScrapeAssistantThread.id == thread_id, ScrapeAssistantThread.turn_id == turn_id,
                    ScrapeAssistantThread.run_id == run_id, ScrapeAssistantThread.status == "researching",
                ).with_for_update())).scalar_one_or_none()
                if not thread:
                    return True
                from app.models.user import User
                from app.permissions import user_has_permission
                from app.routers.auth import _payload_for, _perm_keys
                actor = await db.get(User, thread.actor_id)
                if not actor or not actor.is_active or not user_has_permission(_payload_for(actor, _perm_keys(actor)), "scraping.trigger"):
                    raise PermissionError("Permission to edit pending records was revoked")
                record = (await db.execute(select(ScrapedCourse).where(
                    ScrapedCourse.id == row["id"],
                    ScrapedCourse.scrape_job_id == row.get("jobId", thread.job_id),
                    ScrapedCourse.university_id == ctx["university_id"],
                ).with_for_update())).scalar_one_or_none()
                if not record or record.status not in service.REVIEW_STATUSES or service.row_version(record) != ctx["versions"][str(row["id"])]:
                    raise ValueError("The pending record changed during verification; no edit applied")
                before = list(record.intake_months or [])
                record.intake_months = requested
                db.add(CourseAuditLog(scraped_course_id=record.id, field_key="intake_months",
                    action="assistant_edit", old_value=json.dumps(before), new_value=json.dumps(requested),
                    reason=proof["quote"] + " Source: " + row["url"], actor=str(thread.actor_id)))
                await db.flush()
                reply = {"role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
                         "content": f"Updated {row['name']} (row {row['id']}): intake = {', '.join(requested)}. No new course created.",
                         "sources": [{"url": row["url"], "title": row["name"], "excerpt": proof["quote"]}],
                         "actions": [{"id": uuid.uuid4().hex, "kind": "fix_fields", "state": "completed",
                                      "label": "Updated intake", "fields": ["intake_months"], "courseCount": 1,
                                      "jobId": record.scrape_job_id,
                                      "resultSummary": f"{before or 'Empty'} → {', '.join(requested)}"}]}
                service.finish_running(thread.activity or [])
                thread.messages = list(thread.messages) + [reply]
                thread.status, thread.phase, thread.updated_at = "ready", "Intake saved and audited", service.now()
                await db.commit()
            return True
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "actions": [], "sources": [],
    }, detail="Field edit resolved without discovery.")
    return True


def document_research_requested(prompt):
    """Keep source-based updates on the research path, without changing URLs.

    Explicit staff values are handled by the staff-edit router before this
    handler. This gate must not fetch, parse scores from filenames, or create
    courses merely because a document URL appears in the request.
    """
    return bool(
        re.search(r"https://[^\s<>`]+\.pdf(?:[?#][^\s<>`]*)?(?=[\s`)>.,]|$)", prompt, re.I)
        or re.search(r"\b(?:this|the|attached|uploaded|official)\s+(?:\w+\s+){0,3}PDF\b", prompt, re.I)
        or re.search(r"\b(?:from|using|read|check|extract)\b.{0,80}\bPDF\b", prompt, re.I)
    )
