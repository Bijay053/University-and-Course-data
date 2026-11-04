"""Short, bounded assistant jobs reuse the scraper's event-loop cleanup."""
from app.tasks.celery_app import celery_app


async def _research(thread_id, turn_id):
    from app.tasks.scrape_tasks import _task_loop_resources
    from app.services.scrape_assistant import investigate
    async with _task_loop_resources(scraper=True):
        await investigate(thread_id, turn_id)


@celery_app.task(name="scrape.prompt_assistant", soft_time_limit=125, time_limit=145)
def research_scrape(thread_id: str, turn_id: str):
    from app.tasks.scrape_tasks import _run_in_fresh_loop
    _run_in_fresh_loop(_research(thread_id, turn_id))
