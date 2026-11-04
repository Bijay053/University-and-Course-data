"""Bounded, read-only research runs independently of the long scraping queue.

Database claims arbitrate API/worker deliveries. Corrective actions still use
the existing Celery jobs; this runner never publishes or changes course data.
"""
import asyncio
import logging

log = logging.getLogger(__name__)
_tasks: dict[tuple[str, str], asyncio.Task] = {}
MAX_LOCAL_RESEARCH = 2


def schedule(thread_id: str, turn_id: str) -> bool:
    key = (thread_id, turn_id)
    if key in _tasks:
        return True
    if len(_tasks) >= MAX_LOCAL_RESEARCH:
        return False
    from app.services.scrape_assistant import investigate
    task = asyncio.create_task(investigate(thread_id, turn_id))
    _tasks[key] = task
    def finished(done):
        _tasks.pop(key, None)
        if not done.cancelled() and done.exception():
            log.warning("Assistant research runner stopped (%s)", type(done.exception()).__name__)
    task.add_done_callback(finished)
    return True


async def shutdown():
    tasks = list(_tasks.values())
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    _tasks.clear()
