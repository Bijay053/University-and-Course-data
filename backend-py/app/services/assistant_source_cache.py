"""Private conversation-scoped source reuse with generation-fenced writes."""
import asyncio
from copy import deepcopy
from time import time as wall_time
from sqlalchemy import select
from app.models.scrape_assistant import ScrapeAssistantThread

ACCESS_ISSUES = frozenset({"robots_unavailable", "robots_disallowed", "source_access_denied"})
FAILURE_REUSE_SECONDS = 120


async def cached_source(service, thread_id, turn_id, run_id, url, home, inspect, timeout, *, fresh=False):
    from app.services.scrape_assistant_tools import official_url
    try:
        url = official_url(url, home)
    except ValueError:
        # Invalid/foreign URLs must still be rejected by the normal reader,
        # never satisfied from another provider's cached data.
        return await asyncio.wait_for(inspect(url, home), timeout=timeout)
    if not fresh:
        async with service.AsyncSessionLocal() as db:
            thread = await db.get(ScrapeAssistantThread, thread_id)
            if thread:
                for message in reversed(thread.messages):
                    result = message.get("_sourceChecks", {}).get(url)
                    if not result or result.get("_sourceScopeVersion") != 2:
                        continue
                    checked_at = result.get("_sourceCheckedAt")
                    age = wall_time() - checked_at if isinstance(checked_at, (int, float)) else None
                    reusable_failure = (
                        result.get("accessIssue") in ACCESS_ISSUES
                        and age is not None and 0 <= age < FAILURE_REUSE_SECONDS
                    )
                    if result.get("verified") or reusable_failure:
                        cached = deepcopy(result)
                        cached["_sourceCacheHit"] = True
                        return cached
                    # An expired latest failure cannot revive an older success.
                    break
    result = await asyncio.wait_for(inspect(url, home), timeout=timeout)
    if result.get("verified") or result.get("accessIssue") in ACCESS_ISSUES:
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
                saved["_sourceCheckedAt"] = wall_time()
                saved["_fullText"] = result.get("text", "") + "\n" + "\n".join(
                    item.get("text", "") for item in result.get("excerpts", [])
                )
                request.setdefault("_sourceChecks", {})[url] = saved
                thread.messages = messages
                await db.commit()
    return result
