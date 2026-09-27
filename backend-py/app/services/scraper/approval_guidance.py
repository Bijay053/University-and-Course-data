"""Non-authoritative last-attempt guidance, fenced to the original evidence."""
from datetime import datetime, timezone
import hashlib
import json

from sqlalchemy import select

from app.models import ScrapedCourse
from app.services.scraper.ulaw_qualifications import APPROVAL_REASONS


def evidence_fingerprint(row):
    # Include every persisted input, including generic edits; exclude only this
    # diagnostic itself. Never persist input material or exception messages.
    payload = {column.key: getattr(row, column.key, None)
               for column in ScrapedCourse.__table__.columns
               if column.key != "last_qualification_approval"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str,
                                     separators=(",", ":")).encode()).hexdigest()


def attempt_identity(row):
    return {"rowId": row.id, "jobId": row.scrape_job_id,
            "universityId": row.university_id,
            "evidenceFingerprint": evidence_fingerprint(row)}


def public_approval_guidance(row):
    value = getattr(row, "last_qualification_approval", None)
    if not isinstance(value, dict) or row.status not in {"pending", "review_ready"}:
        return None
    if value.get("reasonCode") not in APPROVAL_REASONS:
        return None
    if any(value.get(key) != item for key, item in attempt_identity(row).items()):
        return None
    try:
        timestamp = datetime.fromisoformat(value["attemptedAt"])
        if timestamp.tzinfo is None:
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return {key: value[key] for key in
            ("rowId", "jobId", "universityId", "reasonCode", "attemptedAt")}


def redact_approval_diagnostics(value):
    """Remove only diagnostic containers, including legacy nested copies.

    Evidence/proof hashes outside these containers are legitimate data and
    must remain intact. Return a copy so serialization never mutates ORM JSON.
    """
    if isinstance(value, dict):
        return {key: redact_approval_diagnostics(item) for key, item in value.items()
                if key not in {"last_qualification_approval", "lastQualificationApproval"}}
    if isinstance(value, (list, tuple)):
        return [redact_approval_diagnostics(item) for item in value]
    return value


def sanitize_guidance_response(payload, row=None):
    """All public serializers must strip the internal identity fingerprint."""
    clean = redact_approval_diagnostics(payload)
    payload.clear()
    payload.update(clean)
    if row is not None:
        payload["lastQualificationApproval"] = public_approval_guidance(row)
    return payload


async def persist_failure(db, identity, reason_code):
    """Called only after approval rollback, inside the approve permission gate."""
    if not identity or reason_code not in APPROVAL_REASONS:
        return
    row = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.id == identity["rowId"],
        ScrapedCourse.scrape_job_id == identity["jobId"],
        ScrapedCourse.university_id == identity["universityId"],
    ).with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if row is not None and row.status in {"pending", "review_ready"} and attempt_identity(row) == identity:
        row.last_qualification_approval = {
            **identity, "reasonCode": reason_code,
            "attemptedAt": datetime.now(timezone.utc).isoformat(),
        }
    await db.commit()


async def synchronize_updated_guidance(db, result):
    """Consume explicit UPDATE RETURNING id; synchronize only its loaded rows.

    Callers opt in at actual DML sites. No SQL-string classification or global
    session hook; SELECTs, unrelated rows and pending assignments are untouched.
    Reading the post-trigger value preserves guidance on matched no-op updates.
    Returns IDs for callers which need them; CursorResult.rowcount stays usable.
    """
    from sqlalchemy import inspect
    from sqlalchemy.orm.attributes import set_committed_value
    ids = list(result.scalars())
    loaded = {}
    for row in db.identity_map.values():
        if isinstance(row, ScrapedCourse):
            identity = inspect(row).identity
            if identity and identity[0] in ids:
                loaded[identity[0]] = row
    if loaded:
        with db.no_autoflush:
            values = await db.execute(select(
                ScrapedCourse.id, ScrapedCourse.last_qualification_approval,
            ).where(ScrapedCourse.id.in_(loaded)))
        for row_id, guidance in values:
            row = loaded[row_id]
            if inspect(row).attrs.last_qualification_approval.history.has_changes():
                # Preserve a pending assignment on a no-op, but not one whose
                # evidence this very DML invalidated. Unrelated rows never reach
                # this branch, and no autoflush may write their pending values.
                from types import SimpleNamespace
                with db.no_autoflush:
                    fresh = (await db.execute(select(ScrapedCourse.__table__).where(
                        ScrapedCourse.id == row_id,
                    ))).mappings().one()
                candidate = SimpleNamespace(**{
                    **fresh, "last_qualification_approval": row.last_qualification_approval,
                })
                if row.last_qualification_approval is None or public_approval_guidance(candidate):
                    continue
            set_committed_value(row, "last_qualification_approval", guidance)
    return ids