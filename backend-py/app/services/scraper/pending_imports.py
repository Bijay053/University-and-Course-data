"""Transaction-owned pending import reuse; attempt evidence is append-only."""
from copy import deepcopy
from hashlib import sha256
import json

from sqlalchemy import select, text, or_

from app.models import ScrapedCourse
from app.models.page_snapshot import PageSnapshot
from app.services.scraper.url_identity import canonical_course_url_key

ATTEMPT = "pending_import_attempt"
AUTO_MUTATION_SQL = """ AND status IN ('pending', 'review_ready')
 AND NOT EXISTS (
 SELECT 1 FROM page_snapshots AS import_attempt
 WHERE import_attempt.snapshot_type = 'pending_import_attempt'
 AND import_attempt.university_id = scraped_courses.university_id
 AND import_attempt.original_extraction->'reused'
     @> jsonb_build_array(jsonb_build_object('courseId', scraped_courses.id))
 ) """


def automatic_mutation_clause():
    from sqlalchemy import literal_column
    return literal_column("(" + AUTO_MUTATION_SQL.removeprefix(" AND ").strip() + ")")


def norm(value):
    return " ".join(str(value or "").casefold().split())


def identity(row):
    methods = row.extraction_method or {}
    # Campus split names contain the campus suffix; keep the original award.
    scope = methods.get("campus_fee_scope") or {}
    variants = methods.get("fee_variants") or {}
    selected = variants.get("selected") or variants.get("options") or []
    evidence_keys = {"amount", "international_fee", "snippet", "source_url", "confidence", "method"}
    variant_identity = sorted({
        json.dumps({k: value for k, value in v.items()
                    if k not in evidence_keys and value is not None},
                   sort_keys=True, default=str)
        for v in selected if isinstance(v, dict)
    })
    return (
        row.university_id, canonical_course_url_key(row.course_website),
        norm(scope.get("original_name") or row.course_name), norm(row.degree_level),
        tuple(sorted(norm(c) for c in (row.course_location or "").split(",") if norm(c))),
        norm(row.study_mode), norm(row.study_load),
        row.fee_year, norm(row.fee_term), norm(row.currency), tuple(variant_identity),
        norm((methods.get("ulaw_qualification_scope") or {}).get("award")),
    )


async def lock_url(db, university_id, url):
    key = canonical_course_url_key(url)
    if not key:
        raise ValueError("Pending import reuse requires a canonical course URL")
    digest = sha256(f"pending-import:{university_id}:{key}".encode()).digest()
    await db.execute(text("SELECT pg_advisory_xact_lock(:key)"),
                     {"key": int.from_bytes(digest[:8], "big", signed=True)})


def candidates(row):
    from app.services.scraper.campus_fee_split import plan_campus_fees, _apply_group
    row = ScrapedCourse(**{
        c.key: deepcopy(getattr(row, c.key))
        for c in ScrapedCourse.__table__.columns if c.key not in {"id", "created_at"}
    })
    groups, _ = plan_campus_fees(row)
    variants = (row.extraction_method or {}).get("fee_variants") or {}
    if not groups or (len(groups) == 1 and variants.get("status") != "range"):
        return {"": row}
    result = {}
    for group in groups:
        clone = ScrapedCourse(**{
            c.key: deepcopy(getattr(row, c.key))
            for c in ScrapedCourse.__table__.columns if c.key not in {"id", "created_at"}
        })
        group = {**group, "key": pending_group_key(row, group)}
        _apply_group(clone, group, row.course_name, row.course_location)
        result[group["key"]] = clone
    return result


def pending_group_key(row, group):
    """Make the legacy URL+scope index distinguish every imported offering."""
    from app.services.scraper.campus_fee_split import _apply_group
    clone = ScrapedCourse(**{
        c.key: deepcopy(getattr(row, c.key))
        for c in ScrapedCourse.__table__.columns if c.key not in {"id", "created_at"}
    })
    _apply_group(clone, group, row.course_name, row.course_location)
    return sha256(repr(identity(clone)).encode()).hexdigest()[:20]


async def legacy_identities(db, university_id, originals):
    """Associate historical backups only through a row ID or owned URL evidence.

    Never guess by name: that is editable, and one URL may host several awards.
    The job and fee scope must also match, and ambiguous associations fail closed.
    """
    from app.models import ScrapedFieldEvidence
    rows = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.university_id == university_id,
        ScrapedCourse.status.in_(["pending", "review_ready"]),
        ScrapedCourse.id.not_in(originals),
    ))).scalars().all()
    if not rows:
        return
    evidence = (await db.execute(select(
        ScrapedFieldEvidence.scraped_course_id, ScrapedFieldEvidence.source_url
    ).where(ScrapedFieldEvidence.scraped_course_id.in_([r.id for r in rows])))).all()
    urls = {}
    for row_id, url in evidence:
        if url:
            urls.setdefault(row_id, set()).add(canonical_course_url_key(url))
    backups = (await db.execute(select(PageSnapshot).where(
        PageSnapshot.university_id == university_id,
        PageSnapshot.scrape_job_id.in_({r.scrape_job_id for r in rows}),
        PageSnapshot.snapshot_type == "staged_row",
    ).order_by(PageSnapshot.id))).scalars().all()
    columns = {c.key for c in ScrapedCourse.__table__.columns}
    for backup in backups:
        data = backup.original_extraction or {}
        if data.get("_snapshot_schema") != "staged_row_v1" or not data.get("course_name"):
            continue
        url = canonical_course_url_key(backup.course_url)
        matches = [
            row for row in rows
            if row.scrape_job_id == backup.scrape_job_id
            and (row.fee_scope_key or "") == (data.get("fee_scope_key") or "")
            and (
                data.get("_staged_course_id") == row.id
                or (not data.get("_staged_course_id") and (
                    url == canonical_course_url_key(row.course_website)
                    or url in urls.get(row.id, set())
                ))
            )
        ]
        if len(matches) == 1:
            original = ScrapedCourse(**{k: v for k, v in data.items()
                                       if k in columns and k != "university_id"})
            original.university_id = university_id
            original.course_website = original.course_website or backup.course_url
            originals.setdefault(matches[0].id, identity_token(original))
        elif len(matches) > 1:
            raise ValueError("Historical pending offering identity is ambiguous; review existing records before importing.")


async def match_pending(db, row):
    """Caller holds the URL transaction lock until row + evidence commit."""
    # First-import identities live outside the editable review row. Never
    # infer a new offering merely because staff corrected its identifying facts.
    history = (await db.execute(select(PageSnapshot.original_extraction["offeringIdentities"]).where(
        PageSnapshot.university_id == row.university_id,
        PageSnapshot.snapshot_type == ATTEMPT,
        PageSnapshot.original_extraction["canonicalUrl"].astext == canonical_course_url_key(row.course_website),
    ).order_by(PageSnapshot.id))).scalars().all()
    originals = {}
    for entries in history:
        for entry in entries or []:
            originals.setdefault(entry["courseId"], entry["identity"])
    await legacy_identities(db, row.university_id, originals)
    proposed = candidates(row)
    keys = {identity_token(candidate) for candidate in proposed.values()}
    historical_ids = [i for i, key in originals.items() if key in keys]
    existing = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.university_id == row.university_id,
        or_(ScrapedCourse.canonical_course_url == canonical_course_url_key(row.course_website),
            ScrapedCourse.id.in_(historical_ids)),
        ScrapedCourse.status.in_(["pending", "review_ready"]),
    ).order_by(ScrapedCourse.id).with_for_update()
        .execution_options(populate_existing=True))).scalars().all()
    matches = {}
    for key, candidate in proposed.items():
        match = next((old for old in existing
                      if originals.get(old.id, identity_token(old)) == identity_token(candidate)), None)
        if match:
            matches[key] = match
    # The legacy within-job index omits award and campus. Supply an offering
    # scope for unsplit imports so distinct awards can coexist on one URL.
    row.fee_scope_key = sha256(repr(identity(row)).encode()).hexdigest()[:20]
    return proposed, matches


def identity_token(row):
    return sha256(json.dumps(identity(row), sort_keys=True, default=str).encode()).hexdigest()


async def record_attempt(db, row, proposed, matches, ids, evidence):
    from app.services.scraper.snapshot_save import staged_row_backup_payload
    from app.services.scraper.payload_contract import PERSISTABLE_STAGING_PAYLOAD_FIELDS
    comparisons = []
    compared = (PERSISTABLE_STAGING_PAYLOAD_FIELDS | {"course_name"}) - {
        "extraction_method", "scrape_warnings", "course_website", "has_central_fee_page",
    }
    for key, old in matches.items():
        before = staged_row_backup_payload(old)
        incoming = staged_row_backup_payload(proposed[key])
        differences = {field: {"existing": before.get(field), "incoming": value}
                       for field, value in incoming.items()
                       if field in compared and before.get(field) != value}
        comparisons.append({"courseId": old.id, "reviewJobId": old.scrape_job_id,
                            "differences": differences})
    payload = {
        "schema": 1, "courseIds": list(dict.fromkeys(ids)),
        "canonicalUrl": canonical_course_url_key(row.course_website),
        "incoming": [staged_row_backup_payload(c) for c in proposed.values()],
        "evidence": deepcopy(evidence or []), "reused": comparisons,
    }
    new_ids = iter(i for i in ids if i not in {old.id for old in matches.values()})
    payload["offeringIdentities"] = [
        {"courseId": matches[key].id if key in matches else next(new_ids),
         "identity": identity_token(candidate)}
        for key, candidate in proposed.items()
    ]
    payload = json.loads(json.dumps(payload, default=str))
    db.add(PageSnapshot(
        university_id=row.university_id, scrape_job_id=row.scrape_job_id,
        course_url=row.course_website, url_hash=sha256(row.course_website.encode()).hexdigest()[:16],
        snapshot_type=ATTEMPT, original_extraction=payload,
        content_length=len(json.dumps(payload)),
    ))
    return comparisons


async def linked_course_ids(db, job_ids, university_id):
    payloads = (await db.execute(select(PageSnapshot.original_extraction).where(
        PageSnapshot.scrape_job_id.in_(job_ids),
        PageSnapshot.university_id == university_id,
        PageSnapshot.snapshot_type == ATTEMPT,
    ))).scalars().all()
    return {i for p in payloads for i in (p or {}).get("courseIds", [])
            if isinstance(i, int) and not isinstance(i, bool) and i > 0}


async def attempt_summary(db, job_id, university_id):
    attempts = (await db.execute(select(PageSnapshot).where(
        PageSnapshot.scrape_job_id == job_id,
        PageSnapshot.university_id == university_id,
        PageSnapshot.snapshot_type == ATTEMPT,
    ).order_by(PageSnapshot.id))).scalars().all()
    result = {}
    for attempt in attempts:
        payload = attempt.original_extraction or {}
        reused = payload.get("reused") or []
        result[canonical_course_url_key(attempt.course_url)] = {
            "courseIds": payload.get("courseIds", []),
            "existingReviewIds": sorted({r["reviewJobId"] for r in reused}),
            "conflictingFields": sorted({field for r in reused
                                        for field in r.get("differences", {})}),
            "comparisons": reused,
            "attemptSnapshotId": attempt.id,
        }
    return result
