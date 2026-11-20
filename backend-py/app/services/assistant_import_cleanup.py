"""Reversibly archive untouched duplicate copies from a review-only import.

Never merge values, infer a variant, touch originals or repair by deleting data.
The original extraction, citations and rejected copy remain available for audit.
"""
from copy import deepcopy
from collections import Counter
from hashlib import sha256
import json
import re
import uuid

from sqlalchemy import select

from app.models import ScrapedCourse, User
from app.models.audit import CourseAuditLog
from app.models.page_snapshot import PageSnapshot
from app.models.scrape_runtime import ScrapeRuntimeJob
from app.services.assistant_staff_edits import permissions
from app.services.scraper.pending_imports import ATTEMPT, RECONCILIATION, compatible_identity
from app.services.scraper.review_policy import full_catalogue_review


REVIEW = {"pending", "review_ready"}
METADATA = {
    "id", "scrape_job_id", "university_id", "created_at", "updated_at",
    "status", "reviewed_at", "rejection_reason", "extraction_method",
    "scrape_warnings", "fee_scope_key", "canonical_course_url",
    "course_id", "last_qualification_approval",
}
WRITTEN = ("status", "rejection_reason", "reviewed_at")


def requested(prompt):
    return bool(
        re.search(r"\b(?:discard|reject|archive|remove|clean\s+up)\b", prompt, re.I)
        and re.search(r"\bduplicate\s+(?:imports?|copies|records?|rows?)\b", prompt, re.I)
        and re.search(r"\b(?:latest|last|previous)\b.*\b(?:scan|import|verification)\b", prompt, re.I)
        and not re.search(
            r"\b(?:do not|don't|never)\s+(?:discard|reject|archive|remove|clean\s+up)\b"
            r"|\b(?:only investigate|read.only)\b", prompt, re.I,
        )
    )


def pristine(row, snapshots):
    """Only copies which still equal their own immutable import may be archived."""
    if row.status not in REVIEW or row.reviewed_at is not None or row.course_id is not None:
        return False
    for snapshot in snapshots:
        attempt = snapshot.original_extraction or {}
        identities, incoming = attempt.get("offeringIdentities", []), attempt.get("incoming", [])
        if (snapshot.scrape_job_id != row.scrape_job_id or snapshot.snapshot_type != ATTEMPT
                or attempt.get("schema") != 1 or len(identities) != len(incoming)
                or row.id in {r.get("courseId") for r in attempt.get("reused", [])}):
            continue
        for entry, data in zip(identities, incoming):
            if entry.get("courseId") != row.id or data.get("_snapshot_schema") != "staged_row_v1":
                continue
            identical = True
            for column in ScrapedCourse.__table__.columns:
                if column.key in METADATA:
                    continue
                expected = data.get(column.key)
                if expected is None and column.default is not None and column.default.is_scalar:
                    expected = column.default.arg
                if column.key not in data or expected != getattr(row, column.key):
                    identical = False
                    break
            if identical:
                return True
            # The importer adds readiness notes and quality metadata after the
            # transient proposal. Its persisted-row backup captures those
            # defaults. This fallback is used only with the no-audit fence in
            # pairs(), never as proof that an edited row is an untouched import.
            for saved in snapshots:
                persisted = saved.original_extraction or {}
                if (saved.scrape_job_id == row.scrape_job_id
                        and saved.snapshot_type == "staged_row"
                        and persisted.get("_snapshot_schema") == "staged_row_v1"
                        and persisted.get("course_website") == row.course_website
                        and persisted.get("fee_scope_key") == row.fee_scope_key
                        and all(c.key in persisted and persisted[c.key] == getattr(row, c.key)
                                for c in ScrapedCourse.__table__.columns if c.key not in METADATA)):
                    return True
    return False


def version(row):
    return sha256(json.dumps(
        {c.key: getattr(row, c.key) for c in ScrapedCourse.__table__.columns},
        sort_keys=True, default=str,
    ).encode()).hexdigest()


def pairs(rows, snapshots, job, audited_ids=()):
    if job.status not in {"completed", "failed", "failed_degraded"} or not full_catalogue_review(job.request_payload):
        raise ValueError("Only a finished review-only import can be reconciled")
    originals = [r for r in rows if r.status in REVIEW
                 and r.scrape_job_id != job.runtime_job_id and r.created_at < job.started_at]
    result = []
    for row in rows:
        if (row.id in audited_ids or row.scrape_job_id != job.runtime_job_id or row.created_at < job.started_at
                or not pristine(row, snapshots)):
            continue
        matches = [old for old in originals if compatible_identity(row, old)]
        if len(matches) == 1:
            result.append((row, matches[0]))
    counts = Counter(original.id for _, original in result)
    return [(copy, original) for copy, original in result if counts[original.id] == 1]


async def load(db, university_id, job_id=None, *, lock=False):
    query = select(ScrapeRuntimeJob).where(
        ScrapeRuntimeJob.university_id == university_id,
        ScrapeRuntimeJob.request_payload["fullCatalogueReviewOnly"].as_boolean().is_(True),
    )
    if job_id:
        query = query.where(ScrapeRuntimeJob.runtime_job_id == job_id)
    job = (await db.execute(query.order_by(ScrapeRuntimeJob.started_at.desc()).limit(1)
                            .with_for_update())).scalar_one_or_none()
    if not job:
        raise ValueError("No review-only import was found for this university")
    rows_query = select(ScrapedCourse).where(
        ScrapedCourse.university_id == university_id,
        ScrapedCourse.status.in_(REVIEW | {"rejected"}),
    ).order_by(ScrapedCourse.id).limit(5001)
    rows = list((await db.execute(rows_query.with_for_update() if lock else rows_query)).scalars())
    if len(rows) > 5000:
        raise ValueError("The duplicate review exceeds the bounded limit")
    snapshots = list((await db.execute(select(PageSnapshot).where(
        PageSnapshot.university_id == university_id,
        PageSnapshot.scrape_job_id == job.runtime_job_id,
        PageSnapshot.snapshot_type.in_([ATTEMPT, "staged_row"]),
    ))).scalars())
    audited = set((await db.execute(select(CourseAuditLog.scraped_course_id).where(
        CourseAuditLog.scraped_course_id.in_([r.id for r in rows if r.scrape_job_id == job.runtime_job_id]),
    ))).scalars())
    return job, rows, snapshots, audited


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    async with service.AsyncSessionLocal() as db:
        job, rows, snapshots, audited = await load(db, ctx["university_id"])
        targets = []
        for copy, original in pairs(rows, snapshots, job, audited):
            targets.append({
                "id": copy.id, "keepId": original.id, "name": copy.course_name,
                "version": version(copy), "keepVersion": version(original),
                "before": {k: getattr(copy, k) for k in WRITTEN},
            })
    action = {
        "id": uuid.uuid4().hex, "kind": "cleanup_import_duplicates", "state": "proposed",
        "label": "Archive duplicate copies from the last review-only import",
        "courseCount": len(targets), "fields": [], "requiresConfirmation": True,
        "_targets": targets, "_sourceJobId": job.runtime_job_id,
        "_universityId": ctx["university_id"], "_turnId": turn_id,
    }
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": (
            f"Found {len(targets)} untouched duplicate import copies. "
            "Only those copies will be archived reversibly. Original pending records, "
            "all imported values and source evidence are retained; nothing is merged or published. "
            "Changed copies, ambiguous matches and conflicting known variants are not archived."
        ),
        "actions": [action] if targets else [], "sources": [],
    }, detail="Checked immutable import snapshots and existing offering identities.")
    return True


async def apply(service, db, action, university_id, actor_id, *, undo=False):
    permissions(await db.get(User, actor_id))
    if action.get("_universityId") != university_id:
        raise ValueError("The duplicate cleanup belongs to another university")
    job, rows, snapshots, audited = await load(db, university_id, action["_sourceJobId"], lock=True)
    by_id = {r.id: r for r in rows}
    confirmed = {(copy.id, original.id) for copy, original in pairs(rows, snapshots, job, audited)} if not undo else set()
    targets = action.get("_targets") or []
    if not targets or len({t["id"] for t in targets}) != len(targets):
        raise ValueError("The duplicate cleanup scope is invalid")
    locked = []
    for target in targets:
        copy, original = by_id.get(target["id"]), by_id.get(target["keepId"])
        expected = target.get("afterVersion") if undo else target["version"]
        if (not copy or not original or original.status not in REVIEW
                or version(copy) != expected
                or version(original) != target["keepVersion"]
                or (not undo and (copy.id, original.id) not in confirmed)
                or (undo and (copy.status != "rejected"
                              or copy.rejection_reason != f"duplicate_import_of:{original.id}"))):
            raise ValueError("A duplicate or its retained original changed; refresh the preview")
        locked.append((copy, original, target))
    for copy, original, target in locked:
        before = {k: getattr(copy, k) for k in WRITTEN}
        after = target["before"] if undo else {
            "status": "rejected", "rejection_reason": f"duplicate_import_of:{original.id}",
            "reviewed_at": service.now(),
        }
        for key, value in after.items():
            setattr(copy, key, value)
        db.add(CourseAuditLog(
            scraped_course_id=copy.id, field_key="status",
            action="assistant_import_cleanup_undo" if undo else "assistant_import_cleanup",
            old_value=json.dumps(before, default=str), new_value=json.dumps(after, default=str),
            actor=str(actor_id), reason=f"Reconciled untouched import {job.runtime_job_id}; retained original {original.id}",
        ))
        if not undo:
            # Append a compensating membership record. Never rewrite the
            # original attempt or assign a new "first import" identity.
            payload = {
                "schema": 1, "courseIds": [original.id], "archivedCourseIds": [copy.id],
                "reused": [{"courseId": original.id, "reviewJobId": original.scrape_job_id,
                            "differences": {}}],
                "action": action["id"],
            }
            db.add(PageSnapshot(
                university_id=university_id, scrape_job_id=job.runtime_job_id,
                course_url=copy.course_website,
                url_hash=sha256(copy.course_website.encode()).hexdigest()[:16],
                snapshot_type=RECONCILIATION, original_extraction=payload,
                content_length=len(json.dumps(payload)),
            ))
    await db.flush()
    for copy, _, target in locked:
        await db.refresh(copy)
        target["afterVersion"] = version(copy)
    action.update(
        state="completed",
        resultSummary=f"{'Restored' if undo else 'Archived'} {len(locked)} duplicate import copies. "
                      "Original course records unchanged; source evidence retained. Nothing published.",
    )


def undo_proposal(action):
    return {
        **{key: deepcopy(action[key]) for key in (
            "_targets", "_sourceJobId", "_universityId", "_turnId", "courseCount")},
        "id": uuid.uuid4().hex, "kind": "undo_import_cleanup", "state": "proposed",
        "label": "Restore archived import copies", "fields": [], "requiresConfirmation": True,
    }
