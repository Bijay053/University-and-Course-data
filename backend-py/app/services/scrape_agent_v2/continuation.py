"""Bounded, generation-fenced continuation after a worker time slice."""
from copy import deepcopy
from sqlalchemy import select
from app.database import AsyncSessionLocal
from app.models.scrape_assistant import ScrapeAssistantThread


async def enqueue(thread_id,turn_id,run_id):
    from app.services.scrape_assistant import now
    async with AsyncSessionLocal() as db:
        thread=(await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id==thread_id,ScrapeAssistantThread.turn_id==turn_id,
            ScrapeAssistantThread.run_id==run_id,ScrapeAssistantThread.status=="researching",
        ).with_for_update())).scalar_one_or_none()
        if not thread:
            return
        messages=deepcopy(thread.messages)
        request=next(m for m in reversed(messages) if m["role"]=="user")
        count=request.get("_agentContinuations",0)+1
        request["_agentContinuations"]=count
        thread.messages,thread.run_id,thread.updated_at=messages,None,now()
        if count>40:
            thread.status="error"
            thread.error="Background time budget reached. Completed checks and audited changes are retained."
            await db.commit()
            return
        thread.status,thread.phase="queued","Continuing from the last completed course"
        await db.commit()
    try:
        from app.tasks.scrape_assistant import research_scrape
        research_scrape.apply_async(args=[thread_id,turn_id],countdown=1)
    except Exception:
        # Durable queued state can also be picked up by the authenticated
        # assistant polling runner if the broker is temporarily unavailable.
        import logging
        logging.getLogger(__name__).warning("Agent continuation retained in queued state; worker publish unavailable")
