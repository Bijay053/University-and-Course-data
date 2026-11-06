"""Dispatch one fenced pending-only operation after an authorized chat request."""
from copy import deepcopy
import re
from sqlalchemy import select


def execution_requested(prompt):
    return not re.search(r"\bread[- ]only\b|\b(?:just|only)\s+(?:explain|compare|research|investigate|check|show|report)\b|\b(?:do not|don't|dont|never)\s+(?:run|change|modify|repair|fix|scrape|update|apply|add|import|recover|execute)\b", prompt, re.I)


def choose_action(actions):
    proposed = [a for a in actions if a.get("state") == "proposed"
                and not a.get("requiresConfirmation")
                and a.get("courseCount", 0) <= 10 and a.get("kind") in {
        "scan_catalogue", "recover_catalogue", "review_courses", "fix_fields", "repair_catalogue"}]
    # A full discovery covers bounded recovery candidates too; never launch both.
    return next((a for a in proposed if a["kind"] == "scan_catalogue"), next(iter(proposed), None))


async def autorun(thread_id, turn_id):
    from app.services import scrape_assistant as service
    from app.models.scrape_assistant import ScrapeAssistantThread
    from app.models.user import User
    from app.permissions import user_has_permission
    from app.routers.scrape_assistant import confirm_action, ConfirmBody
    async with service.AsyncSessionLocal() as db:
        thread = (await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id == thread_id).with_for_update())).scalar_one_or_none()
        if not thread or thread.turn_id != turn_id or thread.status != "ready":
            return
        request = next((m for m in reversed(thread.messages) if m["role"] == "user"), {})
        if not request.get("autoRun"):
            return
        messages = deepcopy(thread.messages)
        actions = messages[-1].get("actions", [])
        from app.services.assistant_import_pause import provider_for_job, is_paused, IMPORT_KINDS
        university = await provider_for_job(db, thread.job_id)
        if is_paused(university):
            actions = [a for a in actions if a.get("kind") not in IMPORT_KINDS]
        action = choose_action(actions)
        if not action:
            return
        user = await db.get(User, thread.actor_id)
        from app.routers.auth import _payload_for, _perm_keys
        actor = _payload_for(user, _perm_keys(user)) if user else {"id": thread.actor_id}
        if not user or not user.is_active or not user_has_permission(actor, "scraping.trigger"):
            action["state"], action["error"] = "error", "Automatic work stopped: scraping permission is no longer available."
            thread.messages = messages
            await db.commit()
            return
        for alternative in actions:
            if alternative["id"] != action["id"] and alternative["state"] == "proposed":
                alternative["state"] = "superseded"
        thread.messages = messages
        await db.flush()
        try:
            urls = action.get("_requestedUrls", [])[:20] if action["kind"] == "recover_catalogue" else None
            await confirm_action(thread.job_id, action["id"], ConfirmBody(confirm=True, courseUrls=urls), db, actor)
        except Exception as exc:
            from fastapi import HTTPException
            await db.rollback()
            thread = (await db.execute(select(ScrapeAssistantThread).where(
                ScrapeAssistantThread.id == thread_id).with_for_update())).scalar_one()
            messages = deepcopy(thread.messages)
            for message in messages:
                for item in message.get("actions", []):
                    if item["id"] == action["id"]:
                        item["state"] = "error"
                        item["error"] = str(exc.detail) if isinstance(exc, HTTPException) and exc.status_code < 500 else "Automatic dispatch could not be verified. No success is claimed."
            thread.messages = messages
            await db.commit()
