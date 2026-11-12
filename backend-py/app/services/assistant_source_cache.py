"""Private conversation-scoped source reuse with generation-fenced writes."""
import asyncio
from copy import deepcopy
from time import time as wall_time
from sqlalchemy import select
from app.models.scrape_assistant import ScrapeAssistantThread

ACCESS_ISSUES = frozenset({"robots_unavailable", "robots_disallowed", "source_access_denied"})
FAILURE_REUSE_SECONDS = 120
IELTS_FACTS_VERSION = 2


async def refresh_ielts_facts(source, url):
    """Reparse retained verified text after parser changes, without another fetch.

    This refreshes derived facts, not the age or authority of the source.
    Historic messages and their evidence remain unchanged.
    """
    if (not source.get("verified") or source.get("sourceType") == "pdf"
            or source.get("_ieltsFactsVersion") == IELTS_FACTS_VERSION):
        return source
    from html import escape
    from app.services.scraper.extractors.english_test import extract
    text = source.get("_fullText") or source.get("text") or ""
    results = await extract("<main>" + escape(text) + "</main>", url) if text else []
    facts = [
        {"field": r.field_key, "value": r.value, "normalized": r.normalized,
         "quote": r.snippet, "method": r.method}
        for r in results if r.field_key == "ielts_overall"
    ]
    if facts:
        source["facts"] = [f for f in source.get("facts", [])
                           if not str(f.get("field", "")).startswith("ielts_")] + facts
    source["_ieltsFactsVersion"] = IELTS_FACTS_VERSION
    return source


async def cached_source(service, thread_id, turn_id, run_id, url, home, inspect, timeout, *, fresh=False):
    from app.services.scrape_assistant_tools import official_url
    try:
        url = official_url(url, home)
    except ValueError:
        # Invalid/foreign URLs must still be rejected by the normal reader,
        # never satisfied from another provider's cached data.
        return await asyncio.wait_for(inspect(url, home), timeout=timeout)
    registry_enabled = getattr(service, "SOURCE_ACCESS_REGISTRY", False) is True
    if registry_enabled:
        from app.services.university_source_access import entry, refusal
        async with service.AsyncSessionLocal() as db:
            held = refusal(await entry(db, url, home))
        if held:
            # Apply policy before even private-session evidence reuse.
            return held
    if not fresh:
        async with service.AsyncSessionLocal() as db:
            thread = await db.get(ScrapeAssistantThread, thread_id)
            if thread:
                for message in reversed(thread.messages):
                    result = message.get("_sourceChecks", {}).get(url)
                    if not result or result.get("_sourceScopeVersion") != 2:
                        continue
                    if result.get("sourceOrigin") == "retained_scrape_page_text":
                        from app.services.assistant_retained_source import recent_capture
                        if not recent_capture(result.get("sourceCapturedAt")):
                            break
                    checked_at = result.get("_sourceCheckedAt")
                    age = wall_time() - checked_at if isinstance(checked_at, (int, float)) else None
                    reusable_failure = (
                        not result.get("verified")
                        and age is not None and 0 <= age < FAILURE_REUSE_SECONDS
                    )
                    if result.get("verified") or reusable_failure:
                        cached = deepcopy(result)
                        cached.setdefault("requestedUrl", url)
                        cached.setdefault("urlHandling", "preserved")
                        if cached.get("accessIssue") in {"robots_unavailable", "robots_disallowed"}:
                            cached.setdefault("failureStage", "robots")
                            cached.setdefault("sourceRequestAttempted", False)
                        cached["_sourceCacheHit"] = True
                        if reusable_failure:
                            from app.services.assistant_retained_source import retained_course_source
                            retained = await retained_course_source(
                                service, thread_id, turn_id, run_id, url, cached)
                            if retained:
                                retained["_sourceCacheHit"] = True
                                return retained
                        return await refresh_ielts_facts(cached, url)
                    # An expired latest failure cannot revive an older success.
                    break
    if registry_enabled:
        from app.services.university_source_access import guarded_read
        result = await guarded_read(service.AsyncSessionLocal, url, home, inspect, timeout)
    else:
        result = await asyncio.wait_for(inspect(url, home), timeout=timeout)
    if not fresh:
        from app.services.assistant_retained_source import retained_course_source
        retained = await retained_course_source(service, thread_id, turn_id, run_id, url, result)
        if retained:
            result = retained
    if isinstance(result.get("verified"), bool) and result.get("failureStage") != "access_in_progress":
        async with service.AsyncSessionLocal() as db:
            thread = (await db.execute(select(ScrapeAssistantThread).where(
                ScrapeAssistantThread.id == thread_id, ScrapeAssistantThread.turn_id == turn_id,
                ScrapeAssistantThread.run_id == run_id, ScrapeAssistantThread.status == "researching",
            ).with_for_update())).scalar_one_or_none()
            if thread:
                messages = deepcopy(thread.messages)
                request = next(m for m in reversed(messages) if m["role"] == "user")
                # Keep cited text, not a second unbounded copy of the whole page.
                saved = {k: v for k, v in result.items() if k != "_fullText"}
                saved["_sourceScopeVersion"] = 2
                saved["_ieltsFactsVersion"] = IELTS_FACTS_VERSION
                saved["_sourceCheckedAt"] = wall_time()
                saved["_fullText"] = result.get("text", "") + "\n" + "\n".join(
                    item.get("text", "") for item in result.get("excerpts", [])
                )
                request.setdefault("_sourceChecks", {})[url] = saved
                thread.messages = messages
                await db.commit()
    return result
