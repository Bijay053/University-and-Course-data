"""Evidence-backed pending-only exclusions, committed with their chat receipt."""
import asyncio
from contextvars import ContextVar
import re
import time
import uuid

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.services.scrape_assistant_tools import official_url, public_request


def requested(prompt):
    return bool(
        re.search(r"\bdomestic(?:[- ]only)?\b", prompt, re.I)
        and re.search(r"\b(remove|exclude|reject|delete)\b", prompt, re.I)
    ) and not re.search(
        r"\b(do not|don't|dont|never)\s+(remove|exclude|reject|delete|change)\b"
        r"|\b(read[- ]only|just explain|only explain)\b", prompt, re.I
    )


def scope(prompt, samples):
    """A named example must not shrink an explicit request to check others."""
    if re.search(r"\b(all|every|other|others|rest|these|those)\b", prompt, re.I):
        return samples
    ids = {int(value) for value in re.findall(r"\b(?:row|record)\s*#?\s*(\d+)\b", prompt, re.I)}
    if ids:
        return [r for r in samples if r["id"] in ids]
    urls = re.findall(r"https://[^\s)]+", prompt)
    if urls:
        return [r for r in samples if (r["url"] or "").rstrip("/") in
                {u.rstrip("/.,") for u in urls}]
    normal = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
    return [r for r in samples if normal(r["name"]) in normal(prompt)]


def domestic_evidence(html, name):
    """Only explicit eligibility labels on the named course, never fee text."""
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(
        "script,style,noscript,nav,footer,header,aside,form,"
        "[class*='related'],[id*='related'],[class*='recommend']"
    ):
        node.decompose()
    root = soup.select_one("main") or soup.select_one("article")
    if not root:
        return None
    # Some sites put the course hero immediately before <main>; navigation
    # copies were removed above. Multiple remaining headings fail closed.
    headings = soup.find_all("h1")
    normal = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
    if len(headings) != 1 or normal(headings[0].get_text(" ", strip=True)) != normal(name):
        return None
    pattern = re.compile(
        r"^(?:Available to|Student eligibility|Eligible students|Student type)\s*:?\s*"
        r"Domestic(?: students)? only[.!]?$", re.I,
    )
    for node in root.find_all(["p", "li", "tr", "div", "section"]):
        text = node.get_text(" ", strip=True)
        if len(text) < 160 and pattern.fullmatch(text):
            return text
    return None


_source_context = ContextVar("eligibility_source_context", default=None)


async def check_course(row, home):
    url = official_url(row["url"], home)
    context = _source_context.get()
    if context:
        from app.services.assistant_source_cache import cached_source
        from app.services.scrape_assistant_tools import inspect_source
        source = await cached_source(*context, url, home, inspect_source, 12)
        if not source.get("verified"):
            raise ValueError("Official page was not verified")
        if re.sub(r"\W+", "", source.get("_eligibilityName", "")).casefold() == re.sub(r"\W+", "", row["name"]).casefold():
            return source.get("_eligibilityQuote")
        return None
    raw = await asyncio.wait_for(public_request(url), timeout=12)
    if raw.lstrip().startswith(b"%PDF"):
        return None
    return domestic_evidence(raw.decode("utf-8", errors="replace"), row["name"])


async def run(service, thread_id, turn_id, run_id, ctx, prompt):
    rows = scope(prompt, ctx["samples"])
    if not rows:
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": "No pending course matched this request. Name the course or say "
                       "'check all pending courses and exclude domestic-only courses'. "
                       "No published courses were changed.",
            "actions": [], "sources": [],
        }, detail="No matching pending course; nothing changed.")
        return
    await service.record_activity(thread_id, turn_id, run_id, service.event(
        "Checking domestic-only eligibility", status="running",
        detail=f"Checking all {len(rows)} requested pending records against their official pages.",
    ))
    from app.models.scrape_assistant import ScrapeAssistantThread
    async with service.AsyncSessionLocal() as db:
        thread = await db.get(ScrapeAssistantThread, thread_id)
        cached = {}
        for message in thread.messages:
            cached.update(message.get("_eligibilityChecks", {}))
    results = {r["id"]: tuple(cached[str(r["id"])]["result"]) for r in rows
               if str(r["id"]) in cached and
               cached[str(r["id"])]["version"] == ctx["versions"][str(r["id"])]}
    queue = asyncio.Queue()
    for row in rows:
        if row["id"] not in results:
            queue.put_nowait(row)
    # Leave time for a fenced transaction inside the assistant's overall budget.
    deadline = time.monotonic() + 48

    async def worker():
        while not queue.empty() and time.monotonic() < deadline:
            row = queue.get_nowait()
            token = _source_context.set((service, thread_id, turn_id, run_id))
            try:
                quote = await asyncio.wait_for(
                    check_course(row, ctx["home"]), timeout=min(13, deadline - time.monotonic())
                )
                results[row["id"]] = (quote, "checked")
            except Exception:
                results[row["id"]] = (None, "unavailable")
            finally:
                _source_context.reset(token)
            async with service.AsyncSessionLocal() as db:
                owner = (await db.execute(select(ScrapeAssistantThread).where(
                    ScrapeAssistantThread.id == thread_id,
                    ScrapeAssistantThread.turn_id == turn_id,
                    ScrapeAssistantThread.run_id == run_id,
                    ScrapeAssistantThread.status == "researching",
                ).with_for_update())).scalar_one_or_none()
                if owner is None:
                    return
                from copy import deepcopy
                messages = deepcopy(owner.messages)
                request = next(m for m in reversed(messages) if m["role"] == "user")
                request.setdefault("_eligibilityChecks", {})[str(row["id"])] = {
                    "version": ctx["versions"][str(row["id"])],
                    "result": results[row["id"]],
                }
                owner.messages = messages
                owner.updated_at = service.now()
                await db.commit()
            await service.record_activity(thread_id, turn_id, run_id, service.event(
                "Eligibility page checked", status="completed",
                url=row["url"], detail=f"{row['name']}: " + (
                    "explicit domestic-only evidence found"
                    if results[row["id"]][0] else "no exclusion established; record retained"
                ),
            ))

    await asyncio.gather(*(worker() for _ in range(min(4, len(rows)))))
    if len(results) < len(rows):
        async with service.AsyncSessionLocal() as db:
            owner = (await db.execute(select(ScrapeAssistantThread).where(
                ScrapeAssistantThread.id == thread_id, ScrapeAssistantThread.turn_id == turn_id,
                ScrapeAssistantThread.run_id == run_id,
                ScrapeAssistantThread.status == "researching",
            ).with_for_update())).scalar_one_or_none()
            if owner:
                owner.status, owner.run_id = "queued", None
                owner.phase = f"Continuing eligibility checks: {len(results)}/{len(rows)}"
                await db.commit()
        return
    await persist(service, thread_id, turn_id, run_id, ctx, rows, results)


async def persist(service, thread_id, turn_id, run_id, ctx, rows, results, *, confirmed=False):
    from app.models import ScrapedCourse, ScrapeFeedback
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.models.user import User
    from app.permissions import user_has_permission
    from app.routers.auth import _payload_for, _perm_keys

    async with service.AsyncSessionLocal() as db:
        thread = (await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id == thread_id,
            ScrapeAssistantThread.turn_id == turn_id,
            ScrapeAssistantThread.run_id == run_id,
            ScrapeAssistantThread.status == "researching",
        ).with_for_update())).scalar_one_or_none()
        if thread is None:
            return
        user = await db.get(User, thread.actor_id)
        allowed = bool(user and user.is_active and user_has_permission(
            _payload_for(user, _perm_keys(user)), "scraping.trigger"
        ))
        request = next((m for m in reversed(thread.messages) if m["role"] == "user"), {})
        allowed = allowed and bool(request.get("autoRun")) and requested(request.get("content", ""))
        bulk = sum(bool(quote) for quote, _ in results.values()) > 10 and not confirmed
        allowed = allowed and not bulk
        excluded, details, sources, undo = [], [], [], []
        ids = [r["id"] for r in rows]
        locked = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id.in_(ids),
            ScrapedCourse.scrape_job_id == thread.job_id,
            ScrapedCourse.university_id == ctx["university_id"],
        ).order_by(ScrapedCourse.id).with_for_update())).scalars().all()
        current = {r.id: r for r in locked}
        for candidate in rows:
            quote, state = results.get(candidate["id"], (None, "not checked"))
            row = current.get(candidate["id"])
            unchanged = (row is not None and row.status in service.REVIEW_STATUSES
                         and service.row_version(row) == ctx["versions"][str(row.id)])
            message = "Retained: no explicit course-owned domestic-only evidence."
            if state != "checked":
                message = f"Retained: source {state}."
            if quote and allowed and unchanged:
                undo.append({"id": row.id, "status": row.status,
                             "reason": row.rejection_reason,
                             "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None})
                row.status = "rejected"
                row.rejection_reason = f"domestic_only: {quote} Source: {candidate['url']}"
                row.reviewed_at = service.now()
                undo[-1]["excludedVersion"] = service.row_version(row)
                undo[-1]["excludedReason"] = row.rejection_reason
                undo[-1]["excludedAt"] = row.reviewed_at.isoformat()
                import json
                from app.models.audit import CourseAuditLog
                db.add(CourseAuditLog(
                    scraped_course_id=row.id, field_key="status", action="assistant_exclude",
                    old_value=json.dumps(undo[-1]), new_value=json.dumps({"status": "rejected"}),
                    reason=row.rejection_reason, actor=str(thread.actor_id),
                ))
                db.add(ScrapeFeedback(
                    university_id=row.university_id, scraped_course_id=row.id,
                    course_name=row.course_name, field_key="eligibility",
                    issue_type="manual_reject", reason=row.rejection_reason, status="active",
                ))
                excluded.append(row.id)
                message = "Excluded from pending approval: " + quote
                sources.append({"url": candidate["url"], "title": candidate["name"], "quote": quote})
            elif quote:
                message = ("Confirmed domestic-only; awaiting confirmation of more than 10 exclusions."
                           if bulk else "Retained: permission changed or the record was edited/approved during checking.")
            details.append({"url": candidate["url"], "status": "excluded" if candidate["id"] in excluded else "retained",
                            "message": candidate["name"] + " — " + message})
        await db.flush()
        # Read back saved statuses before writing the completion receipt.
        if excluded:
            saved = (await db.execute(select(ScrapedCourse.id).where(
                ScrapedCourse.id.in_(excluded), ScrapedCourse.status == "rejected",
            ))).scalars().all()
            if set(saved) != set(excluded):
                raise RuntimeError("Exclusion persistence could not be verified")
        checked = sum(state == "checked" for _, state in results.values())
        summary = (f"Checked {checked}/{len(rows)} requested pending records. "
                   f"Excluded {len(excluded)} confirmed domestic-only records; "
                   f"retained {len(rows) - len(excluded)}. "
                   "Retained records are not automatically verified as internationally eligible. "
                   "Nothing was permanently deleted or published.")
        if not allowed:
            summary += (" More than 10 exclusions require confirmation." if bulk else
                        " Changes were blocked because automatic execution or permission was unavailable.")
        items = [dict(i) for i in thread.activity]
        service.finish_running(items)
        items.append(service.event("Eligibility results saved", status="completed", detail=summary))
        reply = {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": summary, "sources": sources, "activity": service.public_activity(items[-40:]),
            "actions": [{
                "id": uuid.uuid4().hex, "kind": "exclude_domestic", "label": "Pending eligibility check",
                "courseCount": len(rows), "fields": [], "state": "proposed" if bulk else "completed",
                "resultSummary": summary, "courseResults": details,
                "requiresConfirmation": bulk, "_ctx": {
                    "university_id": ctx["university_id"], "versions": ctx["versions"],
                }, "_rows": rows, "_results": results, "_turnId": turn_id,
                "_configVersion": ctx.get("config_version"), "_undo": undo,
            }],
        }
        if undo:
            reply["actions"].append({
                "id": uuid.uuid4().hex, "kind": "undo_exclusion",
                "label": "Undo these exclusions", "courseCount": len(undo),
                "fields": [], "state": "proposed", "requiresConfirmation": True,
                "_undo": undo, "_universityId": ctx["university_id"],
            })
        thread.messages = (list(thread.messages) + [reply])[-20:]
        thread.activity = items[-40:]
        thread.status, thread.phase, thread.error = "ready", "Eligibility check finished", None
        thread.updated_at = service.now()
        # Rows, feedback and receipt commit together: retries cannot double-apply.
        await db.commit()
