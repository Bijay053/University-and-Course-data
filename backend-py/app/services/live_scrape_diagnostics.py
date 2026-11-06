"""Bounded, read-only live-job diagnostics. Logs are data, never instructions."""
import re
import asyncio
import ast
import time
from collections import Counter
from datetime import datetime, timezone
from sqlalchemy import select, func
from app.models.scrape_runtime import ScrapeRuntimeJob, ScrapeRuntimeLog

_worker_cache = {"at": 0, "data": None}
_worker_lock = asyncio.Lock()


def _read_workers():
    from app.tasks.celery_app import celery_app
    observer = celery_app.control.inspect(timeout=1)
    return {name: getattr(observer, name)() for name in ("active", "reserved", "scheduled")}


async def worker_observation(job_id):
    async with _worker_lock:
        if time.monotonic() - _worker_cache["at"] > 20:
            try:
                data = await asyncio.wait_for(asyncio.to_thread(_read_workers), timeout=4)
            except Exception:
                data = None
            _worker_cache.update(at=time.monotonic(), data=data)
    data = _worker_cache["data"]
    if not data or any(value is None for value in data.values()):
        return {"workerStatus": "unavailable", "queueStatus": "unavailable"}
    for state, workers in data.items():
        for tasks in workers.values():
            for raw in tasks:
                task = raw.get("request", raw)
                args = task.get("args", [])
                if isinstance(args, str):
                    try:
                        args = ast.literal_eval(args)
                    except (ValueError, SyntaxError):
                        args = []
                kwargs = task.get("kwargs", {})
                if isinstance(kwargs, str):
                    try:
                        kwargs = ast.literal_eval(kwargs)
                    except (ValueError, SyntaxError):
                        kwargs = {}
                if ((isinstance(args, (tuple, list)) and args and args[0] == job_id)
                    or (isinstance(kwargs, dict) and kwargs.get("job_id") == job_id)):
                    return {"workerStatus": "task_observed", "queueStatus": state}
    return {"workerStatus": "task_not_observed", "queueStatus": "not_observed"}


def summarize(job, logs, now=None, log_count=None):
    now = now or datetime.now(timezone.utc)
    def seconds(value):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return max(0, (now - value).total_seconds())
    elapsed = seconds(job.started_at)
    processed, total = job.current or 0, job.total_found or 0
    speed = processed * 60 / elapsed if elapsed and processed else None
    urls, timeouts, retries, ai, fees = Counter(), 0, 0, 0, 0
    for event, payload in logs:
        payload = payload if isinstance(payload, dict) else {}
        message = str(payload.get("message") or "")
        timeout = event in {"course_timeout", "per_course_timeout"} or "[COURSE TIMEOUT]" in message
        timeouts += int(timeout)
        retries += int(event in {"retry", "course_retry", "extract_retry"})
        ai += int("[FALLBACK] AI enriching" in message or event == "ai_enrichment")
        fees += int(bool(re.search(r"\bfee (?:is )?(?:missing|blank)\b|\bmissing:\s*fee\b", message, re.I)))
        # Repeated mentions in warning/debug lines do not prove repeat requests.
        if event in {"extract_start", "course_fetch_start"} and payload.get("url"):
            urls[str(payload["url"])] += 1
    duplicates = sum(n - 1 for n in urls.values() if n > 1)
    issues = []
    if timeouts:
        issues.append({"code": "timeouts", "title": "Course extraction timeouts",
                       "detail": f"{timeouts} timeout events in the retained log window; inspect the affected source URLs before changing deadlines."})
    if ai:
        issues.append({"code": "ai_fallback", "title": "AI enrichment fallbacks",
                       "detail": f"{ai} enrichment starts observed. This count alone does not prove the calls were unnecessary."})
    if fees:
        issues.append({"code": "missing_fee", "title": "Missing fee observations",
                       "detail": f"{fees} log lines report missing or blank tuition. These observations are not a count of unique courses and do not establish that fees are unpublished."})
    if duplicates:
        issues.append({"code": "repeated_fetch", "title": "Repeated fetch starts",
                       "detail": f"{duplicates} repeated URL starts observed; retries can be legitimate."})
    heartbeat_age = seconds(job.heartbeat_at)
    if job.status == "running" and heartbeat_age is not None and heartbeat_age > 120:
        issues.append({"code": "heartbeat", "title": "Worker heartbeat is old",
                       "detail": "The stored heartbeat is over two minutes old. This does not prove the worker is dead; do not restart or reclaim it automatically."})
    return {
        "jobId": job.runtime_job_id, "status": job.status,
        "processed": processed, "total": total, "elapsedSeconds": elapsed,
        "coursesPerMinute": speed,
        "etaSeconds": (total - processed) / speed * 60 if speed and total > processed else None,
        "timeouts": timeouts, "retries": retries, "duplicateRequests": duplicates,
        "aiEnrichments": ai, "heartbeatAgeSeconds": heartbeat_age,
        "logWindow": {"observed": len(logs), "total": log_count,
                      "truncated": log_count is not None and log_count > len(logs)},
        "issues": issues, "capabilities": {"runtimeChanges": False},
        "workerStatus": "heartbeat_observed" if heartbeat_age is not None else "unavailable",
        "queueStatus": "not_inspected",
    }


async def inspect(db, job_id):
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise ValueError("Scrape job not found")
    query = select(ScrapeRuntimeLog.event, ScrapeRuntimeLog.payload).where(
        ScrapeRuntimeLog.runtime_job_id == job_id)
    logs = (await db.execute(query.order_by(ScrapeRuntimeLog.sequence.desc()).limit(2000))).all()
    total = await db.scalar(select(func.count()).select_from(ScrapeRuntimeLog).where(
        ScrapeRuntimeLog.runtime_job_id == job_id))
    result = summarize(job, logs, log_count=total)
    result.update(await worker_observation(job_id))
    return result


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    if not re.search(r"\b(?:slow|timeouts?|performance|optimis[ez]|optimiz[ez]|worker|queue|repeated|retry.*failed)\b", prompt, re.I):
        return False
    async with service.AsyncSessionLocal() as db:
        result = await inspect(db, ctx["job"]["id"])
    lines = [
        f"Live job: {result['processed']}/{result['total']} completed; status: {result['status']}.",
        f"Observed log events: {result['timeouts']} timeouts, {result['retries']} retries, "
        f"{result['aiEnrichments']} AI enrichment starts, {result['duplicateRequests']} repeated URL starts.",
    ]
    lines += [i["title"] + ": " + i["detail"] for i in result["issues"]]
    if result["logWindow"]["truncated"]:
        lines.append("Counts cover only the latest 2,000 log events, not the entire run.")
    lines.append(f"Worker observation: {result['workerStatus']}; queue observation: {result['queueStatus']}. "
                 "Worker observations are cached for up to 20 seconds; not observed does not mean dead.")
    lines.append("No course data, worker settings or job progress were changed. Runtime tuning and selective retry are not available through this live panel yet; I have not claimed to apply them.")
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": "\n".join(lines), "sources": [], "actions": [],
    }, detail="Live job diagnostics read without source research or job restart.")
    return True
