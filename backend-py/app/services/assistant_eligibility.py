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
    """Resolve positive course scope without treating preservation as expansion."""
    normal = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
    preservation = re.compile(
        r"\b(?:keep|leave|preserve)\b(?:(?![.;]\s)[^;\n])*?\b"
        r"(?:unchanged|untouched|alone|as\s+is|as\s+they\s+are)\b"
        r"|\b(?:do\s+not|don't|never|without)\s+"
        r"(?:change|changing|update|updating|edit|editing|modify|modifying|affect|affecting)\b"
        r".*?(?=[.;]\s|\n|$)", re.I,
    )
    protected_text = " ".join(m.group(0) for m in preservation.finditer(prompt))
    positive = preservation.sub(" ", prompt)

    def references(text):
        ids = {int(v) for v in re.findall(r"\b(?:row|record)\s*#?\s*(\d+)\b", text, re.I)}
        urls = {u.rstrip("/.,") for u in re.findall(r"https://[^\s)]+", text)}
        return [r for r in samples if r["id"] in ids
                or (r["url"] or "").rstrip("/") in urls
                or (normal(r["name"]) and normal(r["name"]) in normal(text))]

    wide = re.search(
        r"\b(?:all|every|each|other|others|rest|these|those)\s+(?:\d+\s+)?"
        r"(?:(?:the|of|existing|remaining|selected|pending|university|online|online-only|"
        r"domestic|domestic-only|international|other)\s+)*"
        r"(?:courses?|records?|rows?|programmes?|programs?|degrees?)\b"
        r"|\b(?:check|review|verify|remove|exclude|reject|fix|repair|update)\s+"
        r"(?:the\s+)?(?:all|others?|rest|these|those)\b"
        r"(?!\s+(?:(?:the|four|five|\d+|missing|existing|current)\s+)*"
        r"(?:IELTS|bands?|scores?|components?|fields?|skills?)\b)",
        positive, re.I,
    )
    protected = {r["id"] for r in references(protected_text)}
    return [r for r in (samples if wide else references(positive))
            if r["id"] not in protected]


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
    online = ctx.get("_eligibilityKind") == "online"
    label = "online-only" if online else "domestic-only"
    if online and ctx.get("job", {}).get("id") and re.search(r"\b(?:all|every)\s+(?:pending\s+)?courses?\b", prompt, re.I):
        ctx = {**await service.context(ctx["job"]["id"], provider_wide=True),
               "_eligibilityKind": "online"}
    rows = scope(prompt, ctx["samples"])
    if online:
        from app.services.assistant_online_exclusion import selection
        rows = selection(prompt, ctx["samples"])
    if not rows:
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": ("No unambiguous pending-course selection was established. " if online else
                        "No pending course matched this request. ") + "Name the course or say "
                       f"'check all pending courses and exclude {label} courses'. "
                       "No published courses were changed.",
            "actions": [], "sources": [],
        }, detail="No matching pending course; nothing changed.")
        return
    await service.record_activity(thread_id, turn_id, run_id, service.event(
        f"Checking {label} eligibility", status="running",
        detail=f"Checking all {len(rows)} requested pending records against their official pages.",
    ))
    from app.models.scrape_assistant import ScrapeAssistantThread
    async with service.AsyncSessionLocal() as db:
        thread = (await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id == thread_id,
            ScrapeAssistantThread.turn_id == turn_id,
            ScrapeAssistantThread.run_id == run_id,
            ScrapeAssistantThread.status == "researching",
        ).with_for_update())).scalar_one_or_none()
        if thread is None:
            return
        if online:
            from copy import deepcopy
            messages = deepcopy(thread.messages)
            request = next(m for m in reversed(messages) if m["role"] == "user")
            snapshot = request.get("_onlineEligibilityScope")
            if snapshot is None:
                snapshot = {
                    "universityId": ctx["university_id"],
                    "rows": [{key: r[key] for key in ("id", "name", "url")} for r in rows],
                    "versions": {str(r["id"]): ctx["versions"][str(r["id"])] for r in rows},
                }
                request["_onlineEligibilityScope"] = snapshot
                thread.messages = messages
                await db.commit()
            if snapshot["universityId"] != ctx["university_id"]:
                raise ValueError("The pending-review scope changed; no exclusions were applied.")
            rows = snapshot["rows"]
            ctx = {**ctx, "versions": snapshot["versions"]}
        cached = {}
        for message in thread.messages:
            if not online or message.get("turnId") == turn_id:
                cached.update(message.get("_onlineEligibilityChecks" if online else "_eligibilityChecks", {}))
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
                if online:
                    from app.services.assistant_online_exclusion import check
                    results[row["id"]] = await asyncio.wait_for(
                        check(service, thread_id, turn_id, run_id, row, ctx["home"]),
                        timeout=min(13, deadline - time.monotonic()))
                else:
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
                request.setdefault("_onlineEligibilityChecks" if online else "_eligibilityChecks", {})[str(row["id"])] = {
                    "version": ctx["versions"][str(row["id"])],
                    "result": results[row["id"]],
                }
                owner.messages = messages
                owner.updated_at = service.now()
                await db.commit()
            await service.record_activity(thread_id, turn_id, run_id, service.event(
                "Eligibility page checked", status="completed",
                url=row["url"], detail=f"{row['name']}: " + (
                    f"explicit {label} evidence found"
                    if results[row["id"]][0] else
                    f"record retained ({results[row['id']][1]}); no exclusion established"
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

    online = ctx.get("_eligibilityKind") == "online"
    label = "online-only" if online else "domestic-only"
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
        if online:
            from app.services.assistant_online_exclusion import intent
            instruction = intent(request.get("content", ""), thread.messages)
            needs_preview = bool(instruction and not confirmed and (
                not instruction["execute"] or not request.get("autoRun")))
            allowed = allowed and bool(instruction) and (
                confirmed or bool(request.get("autoRun")) and instruction["execute"])
        else:
            needs_preview = False
            allowed = allowed and bool(request.get("autoRun")) and requested(request.get("content", ""))
        candidates = sum(bool(quote) for quote, _ in results.values())
        update_delivery = online and (ctx.get("_updateDelivery") or (
            bool(re.search(r"\b(?:add|update|correct|set)\b", request.get("content", ""), re.I))
            and bool(re.search(r"\b(?:delivery|mode|location|campus)\b", request.get("content", ""), re.I))))
        operation_count = (sum(state in {"on_campus", "mixed_mode", "checked"} for _, state in results.values())
                           if update_delivery else candidates)
        from app.services.assistant_pending_automation import enabled
        from app.config import settings
        owner_authorized = online and allowed and not needs_preview and enabled(settings, thread.actor_id)
        bulk = operation_count > 10 and not confirmed and not owner_authorized
        confirmation = bulk or needs_preview and operation_count > 0
        allowed = allowed and not confirmation
        excluded, details, sources, undo = [], [], [], []
        updated = []
        ids = [r["id"] for r in rows]
        locked = (await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id.in_(ids),
            True if ctx.get("_providerWide") else ScrapedCourse.scrape_job_id == thread.job_id,
            ScrapedCourse.university_id == ctx["university_id"],
        ).order_by(ScrapedCourse.id).with_for_update())).scalars().all()
        current = {r.id: r for r in locked}
        for candidate in rows:
            quote, state = results.get(candidate["id"], (None, "not checked"))
            row = current.get(candidate["id"])
            unchanged = (row is not None and row.status in service.REVIEW_STATUSES
                         and service.row_version(row) == ctx["versions"][str(row.id)])
            message = f"Retained: no explicit course-owned {label} evidence."
            if state != "checked":
                message = f"Retained: source {state}."
            if online and state == "mixed_mode":
                message = "Retained: mixed delivery or compulsory campus teaching; not online-only."
            elif online and state == "on_campus":
                message = "Retained: course-owned delivery indicates on-campus study."
            if update_delivery and allowed and unchanged and state in {"on_campus", "mixed_mode", "checked"}:
                from app.services.scrape_assistant_tools import quote_in_source
                from app.services.assistant_online_exclusion import SCHEMA
                source = next((m.get("_sourceChecks", {}).get(candidate["url"])
                               for m in reversed(thread.messages)
                               if m.get("_sourceChecks", {}).get(candidate["url"])), {})
                proof = source.get("_onlineExclusion") or {}
                evidence_quote = proof.get("quote") or proof.get("deliveryQuote")
                mode = "Online" if state == "checked" else proof.get("studyMode")
                if (source.get("verified") and source.get("_onlineExclusionSchema") == SCHEMA
                        and mode in {"Online", "On Campus", "Blended"}
                        and evidence_quote and quote_in_source(evidence_quote, source)):
                    updates = {"study_mode": mode}
                    if state == "on_campus" and proof.get("courseLocation"):
                        updates["course_location"] = proof["courseLocation"]
                    changes = {k: {"before": getattr(row, k), "after": v} for k, v in updates.items()
                               if getattr(row, k) != v}
                    if changes:
                        updated.append(row.id)
                        import json
                        for k in changes:
                            setattr(row, k, changes[k]["after"])
                        from app.models.audit import CourseAuditLog
                        for key, change in changes.items():
                            db.add(CourseAuditLog(scraped_course_id=row.id, field_key=key,
                                action="assistant_verified_delivery",
                                old_value=json.dumps(change["before"]),
                                new_value=json.dumps(change["after"]), actor=str(thread.actor_id),
                                reason=json.dumps({"source": candidate["url"], "quote": evidence_quote})))
                        message += " Updated verified " + ", ".join(changes) + "."
            if quote and allowed and unchanged:
                undo.append({"id": row.id, "status": row.status,
                             "reason": row.rejection_reason,
                             "reviewedAt": row.reviewed_at.isoformat() if row.reviewed_at else None})
                row.status = "rejected"
                row.rejection_reason = f"{'online_only' if online else 'domestic_only'}: {quote} Source: {candidate['url']}"
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
                sources.append({"url": candidate["url"], "title": candidate["name"],
                                "excerpt" if online else "quote": quote})
            elif quote:
                message = (f"Confirmed {label}; awaiting confirmation."
                           if confirmation else "Retained: permission changed or the record was edited/approved during checking.")
            if online:
                source = request.get("_sourceChecks", {}).get(candidate["url"], {})
                proof = source.get("_onlineExclusion", {})
                if source.get("verified") and proof.get("state") == state:
                    from app.services.scrape_assistant_tools import quote_in_source
                    for excerpt in dict.fromkeys(filter(None, (
                        proof.get("quote"), proof.get("deliveryQuote"), proof.get("campusQuote"),
                    ))):
                        if quote_in_source(excerpt, source):
                            message += f' Evidence: “{excerpt}”'
                            if not any(s.get("url") == candidate["url"] and s.get("excerpt") == excerpt
                                       for s in sources):
                                sources.append({"url": candidate["url"], "title": candidate["name"],
                                                "excerpt": excerpt})
            details.append({"id": candidate["id"], "url": candidate["url"],
                            "status": "excluded" if candidate["id"] in excluded else "retained",
                            "message": candidate["name"] + " — " + message})
        await db.flush()
        # Read back saved statuses before writing the completion receipt.
        if excluded:
            saved = (await db.execute(select(ScrapedCourse.id).where(
                ScrapedCourse.id.in_(excluded), ScrapedCourse.status == "rejected",
            ))).scalars().all()
            if set(saved) != set(excluded):
                raise RuntimeError("Exclusion persistence could not be verified")
        checked = sum(state in {"checked", "mixed_mode", "on_campus", "unverified"}
                      for _, state in results.values())
        summary = (f"Checked {checked}/{len(rows)} requested pending records. "
                   f"Excluded {len(excluded)} confirmed {label} records; "
                   f"retained {len(rows) - len(excluded)}. "
                   "Retained records are not automatically verified as internationally eligible. "
                   "Nothing was permanently deleted or published.")
        if online:
            counts = {state: sum(s == state for _, s in results.values())
                      for state in {"mixed_mode", "on_campus", "unverified", "unavailable"}}
            summary += (f" Mixed delivery: {counts['mixed_mode']}; on-campus: {counts['on_campus']}; "
                        f"unverified: {counts['unverified']}; unavailable: {counts['unavailable']}. "
                        + ("All existing pending reviews for this university were in scope; "
                         if ctx.get("_providerWide") else "Only this existing pending review was checked; ")
                        + "no catalogue discovery was run.")
            if update_delivery:
                summary += f" Verified mode/location fields updated on {len(updated)} records."
        if not allowed and (candidates or not online):
            summary += (" Review and confirm the evidenced exclusions below." if confirmation else
                        " Changes were blocked because automatic execution or permission was unavailable.")
        items = [dict(i) for i in thread.activity]
        service.finish_running(items)
        items.append(service.event("Eligibility results saved", status="completed", detail=summary))
        reply = {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": summary, "sources": sources, "activity": service.public_activity(items[-40:]),
            "actions": [{
                "id": uuid.uuid4().hex, "kind": "exclude_online" if online else "exclude_domestic",
                "jobId": thread.job_id,
                "label": ("Update verified delivery and exclude online-only courses" if update_delivery
                          else f"Exclude confirmed {label} pending courses"),
                "courseCount": operation_count if online else len(rows),
                "fields": ["study_mode", "course_location"] if update_delivery else [],
                "state": "proposed" if confirmation else (
                    "needs_review" if online and any(state in {"unverified", "unavailable", "not checked"}
                                                    for _, state in results.values()) else "completed"),
                "resultSummary": summary, "courseResults": details,
                "requiresConfirmation": confirmation, "_ctx": {
                    "university_id": ctx["university_id"], "versions": ctx["versions"],
                    **({"_eligibilityKind": "online"} if online else {}),
                    "_providerWide": bool(ctx.get("_providerWide")),
                    "_updateDelivery": bool(update_delivery),
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
        messages = list(thread.messages)
        if online and ctx.get("_confirmedActionId"):
            from copy import deepcopy
            messages = deepcopy(messages)
            for message in messages:
                for action in message.get("actions", []):
                    if action.get("id") == ctx["_confirmedActionId"] and action.get("kind") == "exclude_online":
                        action["state"] = "superseded"
        thread.messages = messages + [reply]
        thread.activity = items[-40:]
        thread.status, thread.phase, thread.error = "ready", "Eligibility check finished", None
        thread.updated_at = service.now()
        # Rows, feedback and receipt commit together: retries cannot double-apply.
        await db.commit()
