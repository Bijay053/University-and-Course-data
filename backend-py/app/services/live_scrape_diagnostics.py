"""Bounded live-job diagnostics. Logs are data, never instructions."""
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


def summarize(job, logs, now=None, log_count=None, runtime=None, *, stall_seconds=180,last_completion_at=None):
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
    active, prepared, preparation_errors = {}, set(), []
    waiting_for_parse=set()
    preparation_total=0
    last_progress=last_completion_at if isinstance(last_completion_at,datetime) else None
    last_prepared=None
    active_extractions={}
    extraction_began=None
    for row in logs:
        event, payload = row[:2]
        observed_at = row[2] if len(row)>2 else None
        payload = payload if isinstance(payload, dict) else {}
        message = str(payload.get("message") or "")
        kind=payload.get("kind") or event
        url=payload.get("url")
        if kind=="course_prepare_start":
            active[url]=observed_at
            preparation_total=max(preparation_total,int(payload.get("total") or 0))
        elif kind=="course_source_fetched":
            active.pop(url,None)
            waiting_for_parse.add(url)
            last_prepared=observed_at or last_prepared
        elif kind=="course_prepare_parse_start":
            waiting_for_parse.discard(url)
            active[url]=observed_at
        elif kind in {"course_prepare_done","course_prepare_error"}:
            active.pop(url,None)
            if kind=="course_prepare_done":
                prepared.add(url)
                last_prepared=observed_at or last_prepared
            else:
                preparation_errors.append({"url":url,"reason":payload.get("error") or "Preparation failed"})
        if kind=="course_extraction_start":
            active_extractions[url]=observed_at
        if payload.get("phase")=="extract" and extraction_began is None:
            extraction_began=observed_at
        if event=="progress" and payload.get("phase")=="extract":
            active_extractions.pop(url,None)
            if observed_at and (last_progress is None or observed_at>last_progress):
                last_progress=observed_at
        timeout = event in {"course_timeout", "per_course_timeout"} or "[COURSE TIMEOUT]" in message
        timeouts += int(timeout)
        retries += int(event in {"retry", "course_retry", "extract_retry"}
                       or payload.get("kind") == "selective_retry_attempt")
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
    no_progress_age=seconds(last_progress or ((extraction_began or job.started_at) if processed==0 else None))
    if preparation_total and (active or len(prepared)<preparation_total):
        issues.append({"code":"course_preparation","title":"Course options are being prepared before extraction",
            "detail":f"{len(prepared)}/{preparation_total} source pages prepared; {len(active)} active source operations; {len(waiting_for_parse)} waiting for option parsing; {len(preparation_errors)} failures. Preparation does not increment completed-course progress."})
    if job.status=="running" and total and processed<total and no_progress_age is not None and no_progress_age>=stall_seconds:
        prep_age=seconds(last_prepared)
        if prep_age is None or prep_age>=stall_seconds:
            issues.append({"code":"no_extraction_progress","title":"No recent completed extraction observed",
                "detail":f"No completion observed for at least {int(no_progress_age)} seconds in available telemetry. A healthy heartbeat does not prove course progress. The worker is not assumed dead; no restart or lease release was performed."})
    if job.status == "running" and heartbeat_age is not None and heartbeat_age > 120:
        issues.append({"code": "heartbeat", "title": "Worker heartbeat is old",
                       "detail": "The stored heartbeat is over two minutes old. This does not prove the worker is dead; do not restart or reclaim it automatically."})
    from app.services.scrape_runtime_controls import available, generation
    runtime = runtime or {}
    if runtime:
        runtime = {**runtime, "current": runtime.get("generation") == generation(job)}
    return {
        "jobId": job.runtime_job_id, "status": job.status,
        "processed": processed, "total": total, "elapsedSeconds": elapsed,
        "coursesPerMinute": speed,
        "etaSeconds": (total - processed) / speed * 60 if speed and total > processed else None,
        "timeouts": timeouts, "retries": retries, "duplicateRequests": duplicates,
        "aiEnrichments": ai, "heartbeatAgeSeconds": heartbeat_age,
        "logWindow": {"observed": len(logs), "total": log_count,
                      "truncated": log_count is not None and log_count > len(logs)},
        "issues": issues, "capabilities": {"runtimeChanges": available(job, runtime)},
        "runtime": runtime,
        "workerStatus": "heartbeat_observed" if heartbeat_age is not None else "unavailable",
        "queueStatus": "not_inspected",
        "lastProgressAt":last_progress.isoformat() if last_progress else None,
        "noProgressSeconds":no_progress_age,
        "preparation":{"total":preparation_total,"completed":len(prepared),
            "waitingForParse":len(waiting_for_parse),
            "active":[{"url":u,"elapsedSeconds":seconds(t)} for u,t in active.items()],
            "failed":preparation_errors},
        "activeExtractions":[{"url":u,"elapsedSeconds":seconds(t)} for u,t in active_extractions.items()],
    }


async def inspect(db, job_id):
    job = await db.get(ScrapeRuntimeJob, job_id)
    if not job:
        raise ValueError("Scrape job not found")
    query = select(ScrapeRuntimeLog.event, ScrapeRuntimeLog.payload, ScrapeRuntimeLog.created_at).where(
        ScrapeRuntimeLog.runtime_job_id == job_id)
    logs = list(reversed((await db.execute(query.order_by(ScrapeRuntimeLog.sequence.desc()).limit(2000))).all()))
    total = await db.scalar(select(func.count()).select_from(ScrapeRuntimeLog).where(
        ScrapeRuntimeLog.runtime_job_id == job_id))
    last_completion=await db.scalar(select(ScrapeRuntimeLog.created_at).where(
        ScrapeRuntimeLog.runtime_job_id==job_id,ScrapeRuntimeLog.event=="progress",
        ScrapeRuntimeLog.payload["phase"].as_string()=="extract"
    ).order_by(ScrapeRuntimeLog.sequence.desc()).limit(1))
    threshold=180
    configuration_error=None
    if getattr(job,"university_id",None):
        from app.models import University
        university=await db.get(University,job.university_id)
        if university:
            from app.services.scraper.config.loader import load_uni_config
            try:
                config=load_uni_config(slug=getattr(university,"slug","") or "",name=university.name,
                    scrape_url=university.scrape_url,university_id=university.id,
                    db_scrape_config=university.scrape_config or {},create_missing_stub=False)
                if config is not None:
                    threshold=config.extraction.live_stall_seconds
            except ValueError:
                configuration_error="Provider diagnostics configuration could not be validated; using the explicit 180-second default."
    from app.services.scrape_runtime_controls import control
    result = summarize(job, logs, log_count=total, runtime=await control(db, job),
                       stall_seconds=threshold,last_completion_at=last_completion)
    result["stallThresholdSeconds"]=threshold
    if configuration_error:
        result["issues"].append({"code":"diagnostics_configuration","title":"Diagnostics configuration unavailable","detail":configuration_error})
    result.update(await worker_observation(job_id))
    if result["workerStatus"]=="unavailable":
        result["issues"].append({"code":"worker_visibility","title":"Worker state could not be verified",
            "detail":"The queue observer did not provide current worker state. This is not proof of worker death; no lease was released or job restarted."})
    return result


def requested(prompt):
    return bool(re.search(r"\b(?:concurrency|retries|backoff(?:Seconds)?|restart|discard|slow|stuck|stall(?:ed|ing)?|timeouts?|performance|optimis[ez]|optimiz[ez]|worker|queue|repeated|retry.*failed|taking\s+(?:so\s+)?(?:long|time)|zero\s+(?:progress|extractions?))\b|\b(?:0|no)\s*(?:/\s*\d+|extractions?|progress)\b",prompt,re.I))


def followup_requested(prompt,messages,job_id):
    fields=r"\b(?:fees?|tuition|IELTS|intakes?|duration|location|campus|rename|bachelor|master)\b"
    if re.search(fields,prompt,re.I) or not re.search(r"\b(?:fix|do it|bug|repair|investigate|still)\b",prompt,re.I):
        return False
    previous_users=[m for m in messages[:-1] if m.get("role")=="user"]
    if previous_users and re.search(fields,previous_users[-1].get("content",""),re.I) and not requested(previous_users[-1].get("content","")):
        return False
    last_assistant=next((m for m in reversed(messages) if m.get("role")=="assistant"),{})
    marker=last_assistant.get("liveDiagnostics")
    if isinstance(marker,dict) and marker.get("jobId")==job_id:
        return True
    for message in reversed(previous_users[-4:]):
        text=message.get("content","")
        if requested(text):
            return True
        if re.search(fields,text,re.I) or not re.search(r"\b(?:fix|do it|bug|repair|investigate|still)\b",text,re.I):
            break
    return False


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    async with service.AsyncSessionLocal() as db:
        if not requested(prompt):
            if not re.search(r"\b(?:fix|do it|bug|repair|investigate|still)\b",prompt,re.I):
                return False
            from app.models.scrape_assistant import ScrapeAssistantThread
            thread=await db.get(ScrapeAssistantThread,thread_id)
            if not thread or thread.job_id!=ctx["job"]["id"]:
                return False
            if not followup_requested(prompt,thread.messages or [],ctx["job"]["id"]):
                return False
        try:
            result = await inspect(db, ctx["job"]["id"])
        except Exception as exc:
            from app.services.worker_fencing import OwnershipLost
            if isinstance(exc,OwnershipLost):
                raise
            result=None
            failure=type(exc).__name__
    if result is None:
        await service.save_research_reply(thread_id,turn_id,run_id,{
            "role":"assistant","turnId":turn_id,"createdAt":service.now().isoformat(),
            "content":f"Live scraping diagnostics could not be read ({failure}). No recovery action was performed. Worker ownership and current progress remain unverified; an official course URL is not needed for this pipeline issue.",
            "sources":[],"actions":[],"liveDiagnostics":{"jobId":ctx["job"]["id"],"unavailable":True},
        },detail="Live pipeline diagnostics unavailable; no course research or restart attempted.")
        return True
    lines = [
        f"Live job: {result['processed']}/{result['total']} completed; status: {result['status']}.",
        f"Observed log events: {result['timeouts']} timeouts, {result['retries']} retries, "
        f"{result['aiEnrichments']} AI enrichment starts, {result['duplicateRequests']} repeated URL starts.",
    ]
    lines += [i["title"] + ": " + i["detail"] for i in result["issues"]]
    for item in result.get("preparation",{}).get("active",[])[:5]:
        lines.append(f"Preparing {item['url']}; observed elapsed: {item['elapsedSeconds']} seconds.")
    for item in result.get("activeExtractions",[])[:5]:
        lines.append(f"Extracting {item['url']}; observed elapsed: {item['elapsedSeconds']} seconds.")
    if result["logWindow"]["truncated"]:
        lines.append("Counts cover only the latest 2,000 log events, not the entire run.")
    lines.append(f"Worker observation: {result['workerStatus']}; queue observation: {result['queueStatus']}. "
                 "Worker observations are cached for up to 20 seconds; not observed does not mean dead.")
    actions = []
    from app.services.scrape_runtime_controls import proposal
    changes = {("backoffSeconds" if key.lower().startswith("backoff") else key.lower()): value
               for key, value in re.findall(
                   r"\b(concurrency|retries|backoff(?:Seconds| seconds)?)\s*(?:=|to|:)\s*(-?\d+(?:\.\d+)?)\b",
                   prompt, re.I)}
    if changes:
        try:
            changes = {key: int(value) for key, value in changes.items()}
            async with service.AsyncSessionLocal() as db:
                job = await db.get(ScrapeRuntimeJob, ctx["job"]["id"])
                values = {**result["runtime"].get("applied", {}), **changes}
                from app.services.scrape_runtime_controls import control
                actions = [proposal(job, values, turn_id, await control(db, job))]
            lines.append("Confirm the old/new settings below. Only this job's future attempts change; active requests finish normally.")
        except ValueError as exc:
            lines.append(str(exc))
    if re.search(r"\b(?:restart|discard)\b", prompt, re.I):
        actions = []
        lines.append("Restart and progress discard are destructive and are not supported by live tuning. "
                     "Neither can be inferred from approval of a tuning change; each would require its own explicit approval.")
    lines.append("No settings or progress were changed by this diagnosis. "
                 "Use job-only runtime controls or request e.g. concurrency to 2, retries to 1, backoffSeconds to 30. "
                 "Selected failures can be retried through chat after this source job settles; running jobs are never restarted or retried concurrently. "
                 "Restart and publication are not performed here.")
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": "\n".join(lines), "sources": [], "actions": actions, "liveDiagnostics":result,
    }, detail="Live job diagnostics read without source research or job restart.")
    return True
