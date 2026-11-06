"""Database-only status and explicit, scoped standing rules."""
import re
from sqlalchemy import select
from app.models import ScrapedCourse, University


async def handle_read_or_rule(service, thread_id, turn_id, run_id, ctx, prompt, messages):
    from app.services.assistant_staff_locations import handle as staff_locations
    if await staff_locations(service,thread_id,turn_id,run_id,ctx,prompt,messages):
        return True
    from app.services.assistant_field_edits import handle_field_edit
    if await handle_field_edit(service, thread_id, turn_id, run_id, ctx, prompt):
        return True
    from app.services.selective_retry import handle as retry_failures
    if await retry_failures(service, thread_id, turn_id, run_id, ctx, prompt):
        return True
    from app.services.live_scrape_diagnostics import handle as live_diagnostics
    if await live_diagnostics(service, thread_id, turn_id, run_id, ctx, prompt):
        return True
    reextract = bool(re.match(r"^\s*(?:please\s+)?(?:re-?scrape|re-?extract)\b", prompt, re.I))
    if reextract:
        from app.services.assistant_eligibility import scope
        rows = scope(prompt, ctx["samples"])
        action = service.action_proposal(
            {"kind": "fix_fields", "fields": sorted(service.FIELDS)},
            [r["id"] for r in rows],
        ) if rows else None
        if action:
            action["_versions"] = ctx["versions"]
            action["_configVersion"] = ctx.get("config_version")
            action["requiresConfirmation"] = len(rows) > 10
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": ("Prepared source-backed re-extraction for the requested pending rows."
                        if action else "Name a pending course, provide its row number, or request all pending rows."),
            "sources": [], "actions": [action] if action else [],
        }, detail="Scoped pending re-extraction prepared.")
        return True
    approve = bool(re.match(r"^\s*(?:please\s+)?approve\b", prompt, re.I))
    if approve:
        from app.services.assistant_eligibility import scope
        import uuid
        rows = scope(prompt, ctx["samples"])
        if len(rows) == 1:
            row = rows[0]
            actions = [{
                "id": uuid.uuid4().hex, "kind": "approve_course",
                "label": "Confirm publication: " + row["name"],
                "courseCount": 1, "fields": [], "state": "proposed",
                "requiresConfirmation": True, "_ids": [row["id"]],
                "_versions": ctx["versions"], "_turnId": turn_id,
            }]
            content = "Approval publishes this course. Confirm publication to proceed through the normal approval checks."
        else:
            actions = []
            content = "Name one pending course or give its official URL to prepare approval. Publishing always requires confirmation."
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": content, "sources": [], "actions": actions,
        }, detail="Publication requires explicit confirmation.")
        return True
    status_question = bool(re.search(
        r"\b(is|was|has|have|how many|why|show|list)\b.*\b(removed|excluded|rejected|pending|unapproved|approved|published)\b",
        prompt, re.I,
    )) and not re.search(r"\b(remove|exclude|reject|approve|fix|rescrape)\b", prompt, re.I)
    rule = bool(re.search(r"\b(we don.t need|never include|always exclude|remember|standing rule)\b", prompt, re.I)
                and re.search(r"\bdomestic.only\b", prompt, re.I))
    if not status_question and not rule:
        return False
    if rule:
        from app.models.scrape_assistant import ScrapeAssistantThread
        async with service.AsyncSessionLocal() as db:
            owner = (await db.execute(select(ScrapeAssistantThread).where(
                ScrapeAssistantThread.id == thread_id, ScrapeAssistantThread.turn_id == turn_id,
                ScrapeAssistantThread.run_id == run_id, ScrapeAssistantThread.status == "researching",
            ).with_for_update())).scalar_one_or_none()
            if owner is None:
                return True
            from app.models.user import User
            from app.permissions import user_has_permission
            from app.routers.auth import _payload_for, _perm_keys
            actor = await db.get(User, owner.actor_id)
            if not actor or not actor.is_active or not user_has_permission(
                _payload_for(actor, _perm_keys(actor)), "scraping.trigger"
            ):
                raise PermissionError("Permission to save a standing rule is no longer available")
            uni = (await db.execute(select(University).where(
                University.id == ctx["university_id"]).with_for_update())).scalar_one()
            config = dict(uni.scrape_config or {})
            config["assistant_rules"] = {**config.get("assistant_rules", {}),
                "exclude_confirmed_domestic_only": True,
                "recorded_by": owner.actor_id, "recorded_at": service.now().isoformat(),
                "instruction": prompt[:1000]}
            uni.scrape_config = config
            await db.commit()
        content = ("Saved for this university: exclude courses with confirmed course-level domestic-only "
                   "eligibility. Missing CRICOS or domestic fee labels alone do not prove ineligibility. "
                   "This rule does not remove published records.")
    else:
        async with service.AsyncSessionLocal() as db:
            rows = (await db.execute(select(ScrapedCourse).where(
                ScrapedCourse.scrape_job_id == ctx["job"]["id"],
                ScrapedCourse.university_id == ctx["university_id"],
            ).order_by(ScrapedCourse.id))).scalars().all()
        numbers = set(int(n) for n in re.findall(r"\b(?:row|record)\s*#?\s*(\d+)\b", prompt, re.I))
        selected = [r for r in rows if r.id in numbers or r.course_name.casefold() in prompt.casefold()]
        if not selected and re.search(r"\b(this|it|that)\b", prompt, re.I):
            prior = next((a for m in reversed(messages[:-1]) for a in reversed(m.get("actions", []))
                          if a.get("kind") == "exclude_domestic"), None)
            ids = {r["id"] for r in (prior or {}).get("_undo", [])}
            selected = [r for r in rows if r.id in ids]
        if numbers and not selected:
            content = "No matching row belongs to this university's scrape review. No website was fetched."
        elif selected:
            content = "\n".join(f"- Row {r.id}: {r.course_name} — {r.status}"
                                + (f". {r.rejection_reason}" if r.rejection_reason else "")
                                for r in selected)
            content += "\nChecked the review database only; this is not the university website's publication status."
        elif re.search(r"\b(how many|count|total)\b",prompt,re.I):
            from app.services.review_counts import pending_counts
            async with service.AsyncSessionLocal() as db:
                totals=await pending_counts(db,ctx["university_id"],ctx["job"]["id"])
            content=(f"Provider total: {totals['providerPendingRows']} pending rows across all reviews. "
                     f"This job: {totals['thisJobPendingRows']} pending rows. "
                     "These are database rows, not unique course groups or filtered screen results. No website was fetched.")
        elif re.search(r"\b(this|it|that)\b", prompt, re.I):
            content = "I could not identify the record from the saved action. Provide its row number or course name. No website was fetched."
        else:
            counts = {}
            for row in rows:
                counts[row.status] = counts.get(row.status, 0) + 1
            content = "Current review database: " + ", ".join(f"{n} {s}" for s, n in sorted(counts.items())) + ". No website was fetched."
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "sources": [], "actions": [],
    }, detail="Database-only request completed.")
    return True
