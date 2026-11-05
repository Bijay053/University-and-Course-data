"""Private conversation-scoped source reuse with generation-fenced writes."""
import asyncio
from copy import deepcopy
from sqlalchemy import select
from app.models.scrape_assistant import ScrapeAssistantThread


async def cached_source(service, thread_id, turn_id, run_id, url, home, inspect, timeout, *, fresh=False):
    if not fresh:
        async with service.AsyncSessionLocal() as db:
            thread = await db.get(ScrapeAssistantThread, thread_id)
            if thread:
                for message in reversed(thread.messages):
                    result = message.get("_sourceChecks", {}).get(url)
                    if result and result.get("verified"):
                        return result
    result = await asyncio.wait_for(inspect(url, home), timeout=timeout)
    if result.get("verified"):
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
                saved["_fullText"] = result.get("text", "") + "\n" + "\n".join(
                    item.get("text", "") for item in result.get("excerpts", [])
                )
                request.setdefault("_sourceChecks", {})[url] = saved
                thread.messages = messages
                await db.commit()
    return result
