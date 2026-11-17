"""Date-suffix cleanup is not permission to merge conflicting offerings."""
from copy import deepcopy
import json
import re
import uuid

from sqlalchemy import select

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.user import User
from app.services.assistant_intake_consolidation import family
from app.services.assistant_staff_edits import permissions, state, version


def requested(prompt):
    return bool(
        not re.search(r"\b(?:merge|consolidate|combine)\b|\bmake\b.*\bmultiple\s+intakes?\b", prompt, re.I)
        and not re.search(r"\bonly\s+(?:fix\s+)?(?:ielts|fees?|delivery|location)\b", prompt, re.I)
        and re.search(r"\bcourses?\s+(?:names?|titles?)\b|\b(?:names?|titles?)\s+of\b", prompt, re.I)
        and re.search(r"\b(?:intakes?|dates?|jan(?:uary)?|sep(?:tember)?|apr(?:il)?)\b", prompt, re.I)
        # Generic "fix this all" retains the existing consolidation workflow.
        # Name-only cleanup is an explicit remove/clean/strip operation.
        and re.search(r"\b(?:clean|remove|strip|correct)\b", prompt, re.I)
    )


def clean_title(row, home):
    identity = family(row, home)
    if not identity:
        return None
    # A date in a name/URL alone must never invent an intake.
    months = row.intake_months or []
    if not isinstance(months, list) or identity[3].casefold() not in {
        str(month).strip().casefold() for month in months
    }:
        return None
    return identity[0]


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    if not requested(prompt):
        return False
    from app.services.scrape_assistant_autorun import execution_requested
    if re.search(r"\b(?:except|excluding|but not|not all)\b", prompt, re.I):
        content = "Please name the courses to include. No names or other values were changed."
        actions = []
    else:
        words = (ctx.get("name") or "").split()
        providers = "|".join(re.escape(" ".join(words[:length]))
                             for length in range(len(words), 0, -1))
        provider = rf"(?:(?:{providers})\s+)?" if providers else ""
        all_courses = bool(re.search(
            rf"\b(?:all|every)\s+(?:(?:the|pending|existing|other|remaining|these)\s+)*"
            + provider + r"courses?\b", prompt, re.I,
        ))
        async with service.AsyncSessionLocal() as db:
            query = select(ScrapedCourse).where(
                ScrapedCourse.university_id == ctx["university_id"],
                ScrapedCourse.status.in_(service.REVIEW_STATUSES),
            )
            if not all_courses:
                query = query.where(ScrapedCourse.scrape_job_id == ctx["job"]["id"])
            rows = list((await db.execute(query.order_by(ScrapedCourse.id).limit(5001))).scalars())
            if len(rows) > 5000:
                raise ValueError("The cleanup exceeds the bounded review limit; no names changed")
            if not all_courses:
                from app.services.assistant_course_names import matches
                choices = [{"title": row.course_name, "url": row.course_website} for row in rows
                           if row.course_name and row.course_website]
                named = matches(prompt, choices)
                from app.services.assistant_course_names import result, clarification
                if not named or result(named)["state"] != "resolved":
                    await service.save_research_reply(thread_id, turn_id, run_id, {
                        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
                        "content": clarification(result(named)) if named else
                                   "Which course should I clean? Give its name, or say all pending courses. Nothing changed.",
                        "actions": [], "sources": [],
                    }, detail="Course-name cleanup needs a clear course scope.")
                    return True
                selected = {(item["title"], item["url"]) for item in named}
                rows = [row for row in rows if (row.course_name, row.course_website) in selected]
            targets = []
            unresolved = 0
            for row in rows:
                title = clean_title(row, ctx["home"])
                if title is None:
                    from app.services.assistant_course_names import DATED_TITLE
                    unresolved += bool(DATED_TITLE.fullmatch(row.course_name or ""))
                    continue
                before, related = await state(db, row, "pending", ["course_name"])
                targets.append({
                    "id": row.id, "kind": "pending", "beforeName": row.course_name,
                    "afterName": title, "intakes": deepcopy(row.intake_months),
                    "version": version(service, row, "pending", before, related),
                })
            action = {
                "id": uuid.uuid4().hex, "kind": "cleanup_names", "state": "proposed",
                "label": "Remove intake dates from pending course names",
                "fields": ["course_name"], "courseCount": len(targets),
                "requiresConfirmation": len(targets) > 10 or not execution_requested(prompt),
                "nameCleanup": [{key: target[key] for key in ("beforeName", "afterName", "intakes")}
                                for target in targets],
                "_targets": targets, "_home": ctx["home"], "_turnId": turn_id,
                "_instruction": prompt, "_universityId": ctx["university_id"],
            }
            actions = [action] if targets else []
            content = (
                f"Found {len(targets)} pending course names with matching stored intakes. "
                "This operation only removes the intake-date suffix from the name. "
                "All records, intake values, fee years, delivery variants and source evidence are retained. "
                "No course is merged, excluded, approved or published."
                + (f" {unresolved} dated names remain unverified because their stored intake or URL does not agree."
                   if unresolved else "")
            )
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "actions": actions, "sources": [],
    }, detail="Prepared name-only cleanup without discovery or page fetching.")
    return True


async def apply(service, db, action, university_id, actor_id, *, undo=False):
    permissions(await db.get(User, actor_id))
    targets = action["_targets"]
    if (action["_universityId"] != university_id or not targets
            or len({target["id"] for target in targets}) != len(targets)):
        raise ValueError("The name cleanup scope is invalid")
    locked = []
    for target in sorted(targets, key=lambda item: item["id"]):
        if target.get("kind") != "pending":
            raise ValueError("Name cleanup cannot change published courses")
        row = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id == target["id"], ScrapedCourse.university_id == university_id,
        ).with_for_update())).scalar_one_or_none()
        if not row or row.status not in service.REVIEW_STATUSES:
            raise ValueError("A cleanup record changed; refresh the request")
        before, related = await state(db, row, "pending", ["course_name"])
        expected = target.get("afterVersion") if undo else target["version"]
        if version(service, row, "pending", before, related) != expected:
            raise ValueError("A cleanup record changed; no name was overwritten")
        if not undo and (row.course_name != target["beforeName"]
                         or clean_title(row, action["_home"]) != target["afterName"]):
            raise ValueError("The stored intake no longer supports this name cleanup")
        if undo and row.course_name != target["afterName"]:
            raise ValueError("A cleaned name changed; undo refused")
        locked.append((row, target))
    # Fence the entire batch before changing any member.
    for row, target in locked:
        old = row.course_name
        row.course_name = target["beforeName"] if undo else target["afterName"]
        db.add(CourseAuditLog(
            scraped_course_id=row.id, field_key="course_name",
            action="assistant_name_cleanup_undo" if undo else "assistant_name_cleanup",
            old_value=json.dumps(old), new_value=json.dumps(row.course_name),
            actor=str(actor_id), reason="Staff-requested name normalization; not new official verification: "
                                      + action["_instruction"],
        ))
    await db.flush()
    for row, target in locked:
        await db.refresh(row)
        expected = target["beforeName"] if undo else target["afterName"]
        if row.course_name != expected:
            raise ValueError("Name cleanup read-back failed")
        values, related = await state(db, row, "pending", ["course_name"])
        target["afterVersion"] = version(service, row, "pending", values, related)
    action.update(
        state="completed",
        resultSummary=f"{'Restored' if undo else 'Cleaned'} {len(locked)} pending course names. "
                      "Intakes, fees, delivery options and records retained. Nothing merged or published.",
    )


def undo_proposal(action):
    return {
        **{key: deepcopy(action[key]) for key in
           ("_targets", "_home", "_turnId", "_instruction", "_universityId", "courseCount")},
        "id": uuid.uuid4().hex, "kind": "undo_name_cleanup", "state": "proposed",
        "label": "Undo course-name cleanup", "fields": ["course_name"], "requiresConfirmation": True,
    }
