"""Celery entry point. Run worker with:

    celery -A app.tasks.celery_app worker --concurrency=4 --loglevel=info

If Redis isn't reachable, the FastAPI process still boots — only the
``.delay()`` call from the API will quietly fail (and the job stays in
``queued`` state for manual retry).
"""
from __future__ import annotations

from celery import Celery

from app.config import settings

celery_app = Celery(
    "uniportal",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.scrape_tasks"],
)

celery_app.conf.update(
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="scrape",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
)
