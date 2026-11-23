"""Read-only, owner-scoped university history; never reconcile or dispatch actions."""
from __future__ import annotations

import base64
import json

from sqlalchemy import func, literal, select, true, tuple_

from app.models import ScrapeRuntimeJob
from app.models.scrape_assistant import ScrapeAssistantThread

LIVE_MESSAGE_LIMIT = 20
RETENTION_NOTICE = (
    "Earlier versions could remove messages beyond the latest 20. "
    "This view shows retained conversations; it does not restore removed messages."
)


def decode_cursor(cursor, university_id, actor_id):
    if not cursor:
        return None
    try:
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if (
            not isinstance(data, list) or len(data) != 5
            or data[:2] != [university_id, actor_id]
            or not isinstance(data[2], str) or len(data[2]) > 100
            or not isinstance(data[3], str) or len(data[3]) > 128
            or type(data[4]) is not int or data[4] < 1
        ):
            raise ValueError()
        return data[2:]
    except (ValueError, TypeError, UnicodeError) as exc:
        raise ValueError("Invalid history cursor. Refresh university history.") from exc


def public_message(message, *, thread_id, job_id, position, actor_id):
    """Explicit allowlist: no document tokens, executable action IDs or private facts."""
    result = {key: message[key] for key in ("role", "content", "createdAt", "turnId") if key in message}
    result.update(threadId=thread_id, jobId=job_id, position=position)
    result["sources"] = [
        {key: source[key] for key in ("url", "title", "excerpt", "page") if key in source}
        for source in message.get("sources", []) if isinstance(source, dict)
    ]
    result["actions"] = [
        {key: action[key] for key in ("label", "state", "resultSummary", "error") if key in action}
        for action in message.get("actions", []) if isinstance(action, dict)
    ]
    from app.services.assistant_documents import message_image, public_attachments
    attachments = public_attachments(message, actor_id, job_id)
    if attachments:
        result["attachments"] = attachments
    image = message_image(message, actor_id, job_id)
    if image:
        result["attachment"] = {key: image[key] for key in ("filename", "contentType")}
    return result


async def university_history(db, university_id, actor_id, *, cursor=None, limit=20):
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError("History page size must be between 1 and 50.")
    before = decode_cursor(cursor, university_id, actor_id)
    thread = ScrapeAssistantThread
    entries = func.jsonb_array_elements(thread.messages).table_valued(
        "message", with_ordinality="position",
    ).render_derived().lateral("entry")
    # Server-created UTC timestamps plus immutable thread/ordinal tie-breakers.
    # Do not order by thread.updated_at: a new turn must not shift older pages.
    created = func.coalesce(entries.c.message.op("->>")("createdAt"), literal("")).collate("C")
    stmt = select(
        thread.id.label("thread_id"), thread.job_id, entries.c.message, entries.c.position,
        created.label("created"),
    ).select_from(thread).join(
        ScrapeRuntimeJob, ScrapeRuntimeJob.runtime_job_id == thread.job_id,
    ).join(entries, true()).where(
        thread.actor_id == actor_id, ScrapeRuntimeJob.university_id == university_id,
        entries.c.message.op("->>")("role").in_(["user", "assistant"]),
    )
    if before:
        stmt = stmt.where(tuple_(created, thread.id, entries.c.position) < tuple_(*before))
    rows = (await db.execute(stmt.order_by(
        created.desc(), thread.id.desc(), entries.c.position.desc(),
    ).limit(limit + 1))).mappings().all()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit:
        last = page[-1]
        next_cursor = base64.urlsafe_b64encode(json.dumps([
            university_id, actor_id, last["created"], last["thread_id"], last["position"],
        ]).encode()).decode()
    return {
        "messages": [public_message(
            row["message"], thread_id=row["thread_id"], job_id=row["job_id"],
            position=row["position"], actor_id=actor_id,
        ) for row in page],
        "nextCursor": next_cursor, "retentionNotice": RETENTION_NOTICE,
    }
