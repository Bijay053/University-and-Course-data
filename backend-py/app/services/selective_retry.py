"""Selected, settled failure recovery. No discovery, root reclaim or publication."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from sqlalchemy import select, text, or_
from app.models import ScrapeRuntimeJob, ScrapedCourse, University
from app.models.scrape_runtime import ScrapeRuntimeLog

ACTIVE = {"queued", "running", "awaiting_approval"}
MAX_ATTEMPTS = 3


def retry_requested(prompt):
    negative = re.search(r"\b(?:do not|don't|never|stop)\s+(?:retry|queue)\b(?!\s+(?:(?:the|any)\s+)?(?:successful|successes|resolved|completed)\b)", prompt, re.I)
    return not negative and bool(
        re.search(r"(?:^|[.;\n])\s*(?:(?:please|can you|could you|would you|will you)\s+)*retry\b", prompt, re.I)
        and re.search(r"\b(?:failed|failures|timeouts?|unresolved|errors?)\b", prompt, re.I))


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def url_key(value):
    parts = urlsplit(value.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("Only official HTTP course URLs from the failure ledger are supported.")
    # Query parameters identify independently selectable course variants.
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, ""))


def limits(attempts=1, backoff=30):
    if type(attempts) is not int or not 1 <= attempts <= MAX_ATTEMPTS:
        raise ValueError("Select between 1 and 3 extraction attempts per URL.")
    if type(backoff) is not int or not 1 <= backoff <= 300:
        raise ValueError("Select backoff between 1 and 300 seconds.")
    return attempts, backoff


def owner_proof(job):
    return digest({k: getattr(job, k, None) for k in (
        "runtime_job_id", "university_id", "url", "request_payload", "status",
        "worker_id", "worker_pid", "claimed_at", "completed_at",
    )})


async def ensure_schema(db):
    await db.execute(text("""
        CREATE TABLE IF NOT EXISTS scrape_retry_attempts (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES scrape_runtime_jobs(runtime_job_id) ON DELETE CASCADE,
            url TEXT NOT NULL, url_hash TEXT NOT NULL, ordinal INTEGER NOT NULL,
            outcome TEXT NOT NULL DEFAULT 'started', result_hash TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (job_id, url_hash, ordinal)
        )
    """))


async def reserve_attempt(job_id, url):
    """Durable pre-network reservation; interrupted / unknown attempts stay spent."""
    from app.database import AsyncSessionLocal
    key = url_key(url)
    async with AsyncSessionLocal() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": -1 - int(digest([job_id, key])[:15], 16)})
        job = await db.get(ScrapeRuntimeJob, job_id)
        policy = (job.request_payload or {}).get("selectiveRetry") if job else None
        if not policy or job.status != "running" or key not in policy["budgets"]:
            raise ValueError("Selective retry no longer owns this running URL.")
        await guard_source(db, job)
        count = await db.scalar(text(
            "SELECT count(*) FROM scrape_retry_attempts WHERE job_id=:job AND url_hash=:hash"
        ), {"job": job_id, "hash": digest(key)})
        if count >= policy["budgets"][key]:
            return None
        attempt_id = uuid4().hex
        await db.execute(text(
            "INSERT INTO scrape_retry_attempts (id,job_id,url,url_hash,ordinal) VALUES (:id,:job,:url,:hash,:ordinal)"
        ), {"id": attempt_id, "job": job_id, "url": key, "hash": digest(key), "ordinal": count + 1})
        await db.commit()
        return attempt_id, count + 1


async def finish_attempt(attempt_id, result):
    from app.database import AsyncSessionLocal
    from app.services.scraper.orchestrator import _retry_failure_details
    failure = _retry_failure_details(result)
    outcome = "error" if failure else ("skipped" if isinstance(result, dict) and result.get("skip") else "extracted")
    async with AsyncSessionLocal() as db:
        await db.execute(text("UPDATE scrape_retry_attempts SET outcome=:outcome,result_hash=:hash WHERE id=:id"),
            {"id": attempt_id, "outcome": outcome, "hash": digest(result),})
        await db.commit()


async def effective_config(db, university):
    from app.services.scraper.config.loader import load_uni_config
    cfg = load_uni_config(slug=getattr(university, "slug", "") or "", name=university.name,
        university_id=university.id, scrape_url=university.scrape_url,
        db_scrape_config=university.scrape_config or {}, create_missing_stub=False)
    from app.release_info import get_release_revision
    from app.services.scraper.result_reuse import _BUILD_SOURCE_HASH
    return digest({"yaml": cfg.model_dump(mode="json") if cfg else None,
                   "database": university.scrape_config or {}, "url": university.scrape_url,
                   "parser": {"revision": get_release_revision(), "source": _BUILD_SOURCE_HASH}})


async def inventory(db, source_id):
    from app.routers.scrape import _unresolved_history_entries
    source = await db.get(ScrapeRuntimeJob, source_id)
    if not source or not source.university_id:
        raise ValueError("Source scrape no longer exists.")
    # Never reclaim a worker or race its primary pass / recovery sweep.
    if source.status in ACTIVE or not source.completed_at:
        raise ValueError("This scrape has not settled. Its worker may still recover these URLs; no retry was queued.")
    root_id = ((source.request_payload or {}).get("selectiveRetry") or {}).get("rootJobId") or source_id
    children = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.university_id == source.university_id,
        or_(ScrapeRuntimeJob.request_payload["retrySourceJobId"].astext == root_id,
            ScrapeRuntimeJob.request_payload["selectiveRetry"]["rootJobId"].astext == root_id),
    ))).scalars().all()
    logs = (await db.execute(select(ScrapeRuntimeLog.payload, ScrapeRuntimeLog.created_at).where(
        ScrapeRuntimeLog.runtime_job_id == source_id).order_by(ScrapeRuntimeLog.sequence))).all()
    entries = _unresolved_history_entries([
        {"payload": p, "createdAt": at.isoformat() if at else None} for p, at in logs
    ])
    failures = {url_key(e["url"]): e for e in entries}
    # A later success or exclusion must win over an earlier failure.
    for p, _ in logs:
        if isinstance(p, dict) and p.get("url") and p.get("kind") in {
            "sweep_recovered", "sweep_skipped", "course_staged", "course_skipped", "staged", "skipped",
        }:
            failures.pop(url_key(p["url"]), None)
        if isinstance(p, dict) and p.get("url") and p.get("kind") == "extract_error" and p.get("retryable") is False:
            failures.pop(url_key(p["url"]), None)
    successful = (await db.execute(select(ScrapedCourse.course_website).where(
        ScrapedCourse.university_id == source.university_id,
        ScrapedCourse.scrape_job_id.in_([root_id, source_id, *(c.runtime_job_id for c in children)]),
    ))).scalars().all()
    for url in successful:
        if url:
            failures.pop(url_key(url), None)
    spent = {}
    for child in children:
        policy = (child.request_payload or {}).get("selectiveRetry") or {}
        budgets = policy.get("budgets") or {}
        # An older continuation has no trustworthy attempt ledger. Conservatively
        # reserve the entire cap rather than silently offering unlimited retries.
        for url in ((child.request_payload or {}).get("courseUrls") or []):
            key = url_key(url)
            spent[key] = spent.get(key, 0) + int(budgets.get(key, MAX_ATTEMPTS))
    return source, failures, spent, children


async def proposal(db, source_id, urls=None, *, attempts=1, backoff=30):
    attempts, backoff = limits(attempts, backoff)
    source, failures, spent, children = await inventory(db, source_id)
    selected = list(dict.fromkeys(url_key(u) for u in urls)) if urls is not None else list(failures)
    if not selected:
        raise ValueError("No settled retryable failures remain for this scrape.")
    if len(selected) > 200 or any(u not in failures for u in selected):
        raise ValueError("Select at most 200 URLs still unresolved by this exact scrape.")
    budgets = {u: attempts - spent.get(u, 0) for u in selected}
    if any(v <= 0 for v in budgets.values()):
        raise ValueError("The selected attempt limit has already been reserved or exhausted. Successful work was not repeated.")
    uni = await db.get(University, source.university_id)
    if not uni:
        raise ValueError("Source provider no longer exists.")
    return {"id": uuid4().hex, "kind": "retry_failed",
        "label": "Retry selected settled failures", "courseCount": len(selected),
        "courseResults": [{"url": u, "status": "selected",
            "message": f"At most {budgets[u]} remaining extraction attempt(s); backoff at least {backoff}s."} for u in selected],
        "fields": [], "state": "proposed", "requiresConfirmation": len(selected) > 10,
        "_sourceId": source_id, "_urls": selected, "_attempts": attempts,
        "_backoff": backoff, "_budgets": budgets,
        "_owner": owner_proof(source), "_failures": digest([failures[u] for u in selected]),
        "_config": await effective_config(db, uni)}


async def scope_from_prompt(db, source_id, prompt, tool_urls=None):
    """Constrain model-selected URLs to the actual user's URL/name/reason scope."""
    explicit = [u.rstrip(".,);]") for u in re.findall(r"https?://[^\s<>]+", prompt)]
    if explicit:
        selected = explicit
    else:
        _, failures, _, _ = await inventory(db, source_id)
        text_without_urls = re.sub(r"https?://[^\s<>]+", "", prompt.lower())
        text_without_urls = re.sub(r"\b\d+\s*s?\b", "", text_without_urls)
        noise = set(("retry only the all any these those failed failure failures errors error course courses urls url "
                     "please can could would will you with for max maximum up to attempts attempt times time "
                     "backoff delay of second seconds s now again once do not don t stop restart stopping "
                     "successful successes completed resolved and without or keep preserving progress original "
                     "job settings already done work repeat timeout timeouts").split())
        qualifiers = set(re.findall(r"[a-z]+", text_without_urls)) - noise
        selected = []
        for url, entry in failures.items():
            if re.search(r"\btimeouts?\b", prompt, re.I) and not re.search(
                r"timeout|deadline", str(entry), re.I):
                continue
            owned_name = " ".join(str(entry.get(k) or "") for k in ("name", "title", "course_name"))
            words = set(re.findall(r"[a-z]+", (urlsplit(url).path + " " + owned_name).lower()))
            if qualifiers <= words:
                selected.append(url)
        if not selected:
            raise ValueError("No current failed URLs match that selection. Supply the exact failed course URL(s), or say 'retry all failed courses'.")
    if tool_urls and set(map(url_key, tool_urls)) != set(map(url_key, selected)):
        raise ValueError("Tool selection must match the user's exact failed-course scope.")
    return selected


async def queue(db, action, actor_id):
    attempts, backoff = limits(action["_attempts"], action["_backoff"])
    source = await db.get(ScrapeRuntimeJob, action["_sourceId"])
    if not source:
        raise ValueError("Source scrape no longer exists.")
    # Same provider admission lock as start_scrape; held through reservation commit.
    await db.execute(text("SELECT pg_advisory_xact_lock(:uid)"), {"uid": source.university_id})
    source = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.runtime_job_id == source.runtime_job_id
    ).with_for_update().execution_options(populate_existing=True))).scalar_one()
    existing = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.request_payload["selectiveRetry"]["actionId"].astext == action["id"],
        ScrapeRuntimeJob.university_id == source.university_id,
    ))).scalars().first()
    if existing:
        return existing
    from app.models import User
    from app.permissions import user_has_permission
    from app.routers.auth import _payload_for, _perm_keys
    actor = await db.get(User, actor_id)
    if not actor or not actor.is_active or not user_has_permission(
        _payload_for(actor, _perm_keys(actor)), "scraping.trigger"):
        raise ValueError("Permission denied.")
    uni = await db.get(University, source.university_id)
    from app.services.assistant_import_pause import is_paused, PAUSE_REASON
    if is_paused(uni):
        raise ValueError(PAUSE_REASON)
    current = await proposal(db, source.runtime_job_id, action["_urls"], attempts=attempts, backoff=backoff)
    for key in ("_owner", "_config", "_failures", "_budgets"):
        if current[key] != action[key]:
            raise ValueError("Failure evidence, ownership, configuration or reserved attempts changed. Prepare a fresh retry.")
    overlap = (await db.execute(select(ScrapeRuntimeJob.runtime_job_id).where(
        ScrapeRuntimeJob.university_id == source.university_id,
        ScrapeRuntimeJob.status.in_(ACTIVE),
    ))).first()
    if overlap:
        raise ValueError("Another job for this provider is active. No overlapping extraction was queued.")
    child_id = "job-" + uuid4().hex
    policy = {"actionId": action["id"], "actorId": actor_id, "sourceJobId": source.runtime_job_id,
        "rootJobId": ((source.request_payload or {}).get("selectiveRetry") or {}).get("rootJobId") or source.runtime_job_id,
        "configHash": current["_config"], "sourceOwner": current["_owner"],
        "failureProof": current["_failures"], "budgets": current["_budgets"],
        "maxAttempts": attempts, "backoffSeconds": backoff,
        "source": "staff-requested selective retry", "queuedAt": datetime.now(timezone.utc).isoformat(),
        "before": {"reservedAttempts": {u: attempts - n for u, n in current["_budgets"].items()}},
        "after": {"reservedAttempts": {u: attempts for u in current["_urls"]}}}
    child = ScrapeRuntimeJob(runtime_job_id=child_id, job_type="scrape",
        university_id=source.university_id, university_name=source.university_name,
        url=source.url, status="queued", fast_mode=False,
        request_payload={"url": source.url, "universityId": source.university_id,
            "courseUrls": current["_urls"], "course_urls": current["_urls"],
            "retrySourceJobId": source.runtime_job_id,
            "selectiveRetry": policy})
    db.add(child)
    await db.commit()
    # The existing queued-job recovery handles a broker failure. Never create a
    # replacement child or pretend enqueueing means the URLs were recovered.
    try:
        from app.tasks.scrape_tasks import scrape_university, set_initial_dispatch_lock
        scrape_university.delay(child_id)
        set_initial_dispatch_lock(child_id)
    except Exception:
        import logging
        logging.getLogger(__name__).exception("Selective retry persisted; broker dispatch awaits recovery: %s", child_id)
    return child


async def prepare_for_turn(db, source_id, urls, actor_id, turn_id, thread_id, *, attempts=1, backoff=30, prompt=None):
    """Stable turn identity also covers a crash between commit and chat receipt."""
    identity = digest([thread_id, turn_id, "selected-failure-retry"])
    existing = (await db.execute(select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.request_payload["selectiveRetry"]["actionId"].astext == identity,
    ))).scalars().first()
    if existing:
        policy = existing.request_payload["selectiveRetry"]
        if policy["actorId"] != actor_id or policy["sourceJobId"] != source_id:
            raise ValueError("This retry belongs to another actor or source job.")
        if urls is not None and set(map(url_key, urls)) != set(policy["budgets"]):
            raise ValueError("This turn already reserved a different selection. Start a new request to change it.")
        return {"id": identity, "kind": "retry_failed", "label": "Selected failure retry",
                "courseCount": len(policy["budgets"]), "fields": [], "state": "running",
                "jobId": existing.runtime_job_id, "requiresConfirmation": False,
                "_sourceId": source_id, "_turnId": turn_id}
    if prompt is not None:
        urls = await scope_from_prompt(db, source_id, prompt, urls)
    action = await proposal(db, source_id, urls, attempts=attempts, backoff=backoff)
    action.update(id=identity, _turnId=turn_id)
    if not action["requiresConfirmation"]:
        child = await queue(db, action, actor_id)
        action.update(state="running", jobId=child.runtime_job_id)
    return action


async def validate_before_run(db, job):
    policy = (job.request_payload or {}).get("selectiveRetry")
    if not policy:
        return
    from app.models import User
    from app.permissions import user_has_permission
    from app.routers.auth import _payload_for, _perm_keys
    actor = await db.get(User, policy["actorId"])
    if not actor or not actor.is_active or not user_has_permission(
        _payload_for(actor, _perm_keys(actor)), "scraping.trigger"):
        raise ValueError("Selective retry permission was revoked.")
    source, failures, _, _ = await inventory(db, policy["sourceJobId"])
    uni = await db.get(University, job.university_id)
    if source.university_id != job.university_id or owner_proof(source) != policy["sourceOwner"]:
        raise ValueError("Selective retry source ownership changed.")
    if await effective_config(db, uni) != policy["configHash"]:
        raise ValueError("Selective retry configuration changed after reservation.")
    selected = [u for u in policy["budgets"] if u in failures]
    skipped = [u for u in policy["budgets"] if u not in failures]
    updated = deepcopy(job.request_payload)
    updated["courseUrls"] = updated["course_urls"] = selected
    updated["selectiveRetry"]["skippedResolvedUrls"] = skipped
    job.request_payload = updated
    if not selected:
        raise ValueError("All selected failures were resolved before execution; no extraction performed.")
    await db.commit()


async def guard_source(db, job):
    policy = (job.request_payload or {}).get("selectiveRetry")
    if policy:
        source = (await db.execute(select(ScrapeRuntimeJob).where(
            ScrapeRuntimeJob.runtime_job_id == policy["sourceJobId"]
        ).with_for_update(read=True))).scalar_one_or_none()
        if not source or owner_proof(source) != policy["sourceOwner"]:
            raise ValueError("Selective retry source ownership changed; no course was staged.")


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    if not retry_requested(prompt):
        return False
    count = re.search(r"\b(\d+)\s*(?:attempts?|times)\b", prompt, re.I)
    delay = re.search(r"\b(?:backoff|delay)\s*(?:of\s*)?(\d+)\s*(?:s|seconds?)\b", prompt, re.I)
    action = None
    try:
        async with service.AsyncSessionLocal() as db:
            from app.models.scrape_assistant import ScrapeAssistantThread
            thread = (await db.execute(select(ScrapeAssistantThread).where(
                ScrapeAssistantThread.id == thread_id, ScrapeAssistantThread.turn_id == turn_id,
                ScrapeAssistantThread.run_id == run_id,
                ScrapeAssistantThread.status == "researching",
                ScrapeAssistantThread.job_id == ctx["job"]["id"],
            ).with_for_update())).scalar_one_or_none()
            if not thread:
                return True
            from app.models import User
            from app.permissions import user_has_permission
            from app.routers.auth import _payload_for, _perm_keys
            actor = await db.get(User, thread.actor_id)
            if not actor or not actor.is_active or not user_has_permission(
                _payload_for(actor, _perm_keys(actor)), "scraping.trigger"):
                raise ValueError("Permission denied.")
            action = await prepare_for_turn(db, ctx["job"]["id"], None,
                thread.actor_id, turn_id, thread_id,
                attempts=int(count.group(1)) if count else 1,
                backoff=int(delay.group(1)) if delay else 30, prompt=prompt)
            action["_turnId"] = turn_id
            if action["requiresConfirmation"]:
                content = f"Prepared {action['courseCount']} settled failures. Confirm this batch before extraction starts."
            else:
                content = f"Queued {action['courseCount']} settled failures only. No successful URLs were selected. Recovery is not yet verified; results will appear here when this job settles."
    except ValueError as exc:
        action = None
        content = str(exc)
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "sources": [], "actions": [action] if action else [],
    }, detail="Selected failure recovery checked without discovery or source-job restart.")
    return True


async def outcomes(db, child):
    policy = (child.request_payload or {}).get("selectiveRetry") or {}
    rows = (await db.execute(select(ScrapeRuntimeLog.payload).where(
        ScrapeRuntimeLog.runtime_job_id == child.runtime_job_id
    ).order_by(ScrapeRuntimeLog.sequence))).scalars().all()
    states = {u: {"url": u, "status": "unverified",
                  "message": "No persisted staging outcome verified."} for u in policy.get("budgets", {})}
    for payload in rows:
        if not isinstance(payload, dict) or not payload.get("url"):
            continue
        key = url_key(payload["url"])
        if key not in states:
            continue
        kind = payload.get("kind")
        if kind in {"staged", "sweep_recovered"}:
            row_id = payload.get("scraped_course_id")
            row = await db.get(ScrapedCourse, row_id) if row_id else None
            if row and row.university_id == child.university_id and row.status == "pending":
                states[key] = {"url": key, "status": "staged",
                               "message": f"Stored pending row {row.id}; not published."}
        elif kind in {"skipped", "sweep_skipped"}:
            states[key] = {"url": key, "status": "skipped", "message": str(payload.get("reason") or "Skipped by the normal course guards.")}
    for key in policy.get("skippedResolvedUrls", []):
        states[key] = {"url": key, "status": "skipped", "message": "Resolved before retry execution; not fetched again."}
    return list(states.values())
