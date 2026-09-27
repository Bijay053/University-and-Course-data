"""Explicit, optimistic-concurrency-fenced refresh of an existing award cohort.

Only staging is changed. The caller owns the transaction and publication remains
behind the ordinary approval boundary. Preview tokens contain no source content.
"""
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import base64
import hmac
import json
import time

import httpx
from sqlalchemy import select, text

from app.config import settings
from app.models import ScrapedCourse, ScrapedFieldEvidence, FieldConflict
from app.services.scraper.ulaw_qualifications import (
    URL, AWARDS, QUALIFICATION_SCOPE, _verified_page, _contract_hash,
    _proven_intake_months, validate_qualification_scope,
)
from app.services.scraper.campus_fee_split import SCOPE, plan_campus_fees, _apply_group

RECEIPT = "ulaw_scope_refresh"


class RefreshConflict(ValueError):
    pass


def _hash(value):
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def _values(row):
    return {c.name: deepcopy(getattr(row, c.name)) for c in row.__table__.columns}


def _token(data):
    payload = base64.urlsafe_b64encode(json.dumps(data, sort_keys=True).encode()).decode()
    signature = hmac.new(settings.session_secret.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}.{signature}"


def _decode(token):
    try:
        payload, signature = token.split(".")
        expected = hmac.new(settings.session_secret.encode(), payload.encode(), "sha256").hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(payload))
        if not isinstance(data, dict) or type(data.get("expires")) is not int:
            raise ValueError()
        return data
    except (ValueError, TypeError):
        raise RefreshConflict("Invalid preview. Preview the current cohort again.") from None


async def _source():
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", URL) as response:
                response.raise_for_status()
                if str(response.url).rstrip("/") != URL.rstrip("/"):
                    raise RefreshConflict("Official source redirected; manual source review is required.")
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        raise RefreshConflict("Official source is too large to verify.")
                    chunks.append(chunk)
        raw = b"".join(chunks)
        verified = _verified_page(raw.decode("utf-8"), URL)
    except (httpx.HTTPError, UnicodeError):
        raise RefreshConflict("Official source is unavailable. Retry preview when it is reachable.") from None
    if not verified:
        raise RefreshConflict("Current official award fees, dates and physical campuses could not be verified. No changes made; review the official page (including current Online delivery).")
    authority, proofs = verified
    return {"source_url": URL, "captured_at": datetime.now(timezone.utc).isoformat(),
            "response_sha256": sha256(raw).hexdigest(), "authority": authority,
            "proofs": proofs, "contract_sha256": _contract_hash(authority, proofs)}


async def _cohort(db, row_id, lock=False):
    university = (await db.execute(select(ScrapedCourse.university_id).where(
        ScrapedCourse.id == row_id))).scalar_one_or_none()
    if university is None:
        raise RefreshConflict("Staged course not found.")
    if lock:
        from app.services.scraper.replay_extraction import review_restore_lock_scope
        await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                         {"scope": review_restore_lock_scope(university)})
    query = select(ScrapedCourse).where(ScrapedCourse.university_id == university).order_by(ScrapedCourse.id)
    if lock:
        query = query.with_for_update()
    candidates = (await db.execute(query.execution_options(populate_existing=True))).scalars().all()
    row = next((r for r in candidates if r.id == row_id), None)
    q = (row.extraction_method or {}).get(QUALIFICATION_SCOPE) if row else None
    if not isinstance(q, dict) or q.get("award") not in AWARDS or type(q.get("split_from_id")) is not int:
        raise RefreshConflict("Select an existing scoped Legal Technology award row.")
    rows = [r for r in candidates if r.scrape_job_id == row.scrape_job_id
            and (r.extraction_method or {}).get(QUALIFICATION_SCOPE, {}).get("split_from_id") == q["split_from_id"]]
    for r in rows:
        scope = (r.extraction_method or {}).get(QUALIFICATION_SCOPE, {})
        if (r.status not in {"pending", "review_ready", "approved"}
                or (r.course_website or "").rstrip("/") != URL.rstrip("/")
                or scope.get("source_url") != URL or scope.get("award") not in AWARDS):
            raise RefreshConflict("Cohort contains an ineligible or changed row. Reconcile it manually first.")
    if {r.extraction_method[QUALIFICATION_SCOPE]["award"] for r in rows} != set(AWARDS):
        raise RefreshConflict("Both original award scopes are required. Restore missing cohort rows before refreshing.")
    return rows


async def _snapshot(db, rows):
    ids = [r.id for r in rows]
    result = {"rows": [_values(r) for r in rows]}
    for model in (ScrapedFieldEvidence, FieldConflict):
        records = (await db.execute(select(model).where(model.scraped_course_id.in_(ids)).order_by(model.id))).scalars().all()
        result[model.__name__] = [_values(r) for r in records]
    return _hash(result)


def _plan(rows, source):
    changes, assignments = [], []
    for award, (name, _, _) in AWARDS.items():
        members = [r for r in rows if r.extraction_method[QUALIFICATION_SCOPE]["award"] == award]
        proof = source["proofs"][award]
        template = members[0]
        fresh = _values(template)
        authority = deepcopy(source["authority"])
        authority.update(selected=[o for o in authority["selected"] if o["study_variant"] == award],
                         status="range", international_fee=None)
        fresh.update(course_name=name, course_location=", ".join(proof["locations"]),
                     international_fee=None, currency="GBP", fee_year=authority["fee_year"],
                     fee_term=authority["fee_term"])
        fresh["extraction_method"] = {**fresh["extraction_method"], "fee_variants": authority,
                                     "campus_authority": proof}
        groups, reason = plan_campus_fees(fresh)
        if reason:
            raise RefreshConflict(reason)
        owned = {}
        for member in members:
            scope = (member.extraction_method or {}).get(SCOPE)
            locations = scope.get("locations") if scope else [p.strip() for p in member.course_location.split(",")]
            if scope and (len(locations) != 1 or locations[0] != member.course_location
                          or scope.get("key") != member.fee_scope_key):
                raise RefreshConflict("Stored campus identity changed; reconcile the row before refreshing.")
            if not set(locations) <= set(proof["locations"]):
                raise RefreshConflict("Current source removes an existing campus. No automatic removal is allowed; reconcile manually.")
            if not scope and len(members) != 1:
                raise RefreshConflict("Mixed campus and award scopes require manual reconciliation.")
            for location in locations:
                if location in owned:
                    raise RefreshConflict("Duplicate campus scopes require manual reconciliation.")
                owned[location] = member
        used = set()
        for group in groups:
            campus = group["locations"][0]
            existing = owned.get(campus)
            if existing and existing.id in used:
                existing = None
            if existing:
                used.add(existing.id)
            changes.append({"award": award, "campus": campus, "kind": "retained" if campus in owned else "new",
                            "stagedId": existing.id if existing else None,
                            "old": {"amount": owned[campus].international_fee, "year": owned[campus].fee_year,
                                    "term": owned[campus].fee_term} if campus in owned else None,
                            "new": {"amount": group["amount"], "year": authority["fee_year"], "term": authority["fee_term"]},
                            "intakes": proof["start_dates"]})
            assignments.append((existing, template, award, group))
        if used != {r.id for r in members}:
            raise RefreshConflict("Refresh cannot discard staged rows. Reconcile the cohort manually.")
    return changes, assignments


async def preview_refresh(db, row_id, actor):
    rows = await _cohort(db, row_id)
    before = await _snapshot(db, rows)
    source = await _source()
    changes, _ = _plan(rows, source)
    token = _token({"row": row_id, "actor": actor, "snapshot": before,
                    "source": source["contract_sha256"], "expires": int(time.time()) + 900})
    return {"token": token, "changes": changes, "courseIds": [r.id for r in rows],
            "sourceUrl": URL, "message": "Preview only. Apply updates staging; normal approval is still required."}


async def apply_refresh(db, row_id, token, actor):
    data = _decode(token)
    if data.get("row") != row_id or data.get("actor") != actor:
        raise RefreshConflict("Preview belongs to another row or reviewer. Preview again.")
    rows = await _cohort(db, row_id, lock=True)
    receipt = (rows[0].extraction_method or {}).get(RECEIPT)
    if receipt and receipt.get("token") == _hash(token):
        # Do not re-promote, re-fetch or create siblings on a committed retry.
        if (sorted(r.id for r in rows) != receipt.get("courseIds")
                or any((r.extraction_method or {}).get(RECEIPT, {}).get("token") != receipt["token"]
                       for r in rows)):
            raise RefreshConflict("Applied cohort membership changed. Preview again; no rows were recreated.")
        return {"status": "already_applied", "courseIds": receipt["courseIds"]}
    if data.get("expires", 0) < time.time():
        raise RefreshConflict("Preview expired. Preview the current source again.")
    if data.get("snapshot") != await _snapshot(db, rows):
        raise RefreshConflict("Staged cohort or source evidence changed since preview. Preview again.")
    source = await _source()
    if source["contract_sha256"] != data.get("source"):
        raise RefreshConflict("Official source changed since preview. Preview again before applying.")
    _, assignments = _plan(rows, source)
    from app.services.scraper.snapshot_save import persist_staged_row_backup
    originals = {r.id: _values(r) for r in rows}
    for row in rows:
        await persist_staged_row_backup(db, row)
    children = []
    for existing, template, award, group in assignments:
        old = deepcopy(originals[template.id if existing is None else existing.id])
        row = existing
        if row is None:
            row = ScrapedCourse(**{k: v for k, v in old.items() if k not in {"id", "created_at", "canonical_course_url"}})
            row.course_id = None
            db.add(row)
        linked_course = row.course_id
        from app.services.scraper.approval_guidance import redact_approval_diagnostics
        metadata = redact_approval_diagnostics(deepcopy(old["extraction_method"]))
        qualification = metadata[QUALIFICATION_SCOPE]
        history = qualification.setdefault("refresh_history", [])
        history.append({"actor": actor, "at": source["captured_at"],
                        "previous_source": deepcopy(qualification.get("verified_source")),
                        "previous_row": json.loads(json.dumps(
                            redact_approval_diagnostics(
                                {k: v for k, v in old.items() if k != "extraction_method"}), default=str))})
        qualification["verified_source"] = deepcopy(source)
        metadata["campus_authority"] = deepcopy(source["proofs"][award])
        row.extraction_method = metadata
        row.intake_months = _proven_intake_months(source["proofs"][award], award)
        row.fee_year = source["authority"]["fee_year"]
        row.fee_term = source["authority"]["fee_term"]
        row.currency = "GBP"
        _apply_group(row, group, AWARDS[award][0], ", ".join(source["proofs"][award]["locations"]))
        row.extraction_method = {
            **row.extraction_method,
            SCOPE: {
                **deepcopy(old["extraction_method"].get(SCOPE) or {
                    "split_from_id": template.id, "split_actor": actor, "split_at": source["captured_at"],
                }),
                **row.extraction_method[SCOPE],
            },
        }
        row.course_id = linked_course
        if not validate_qualification_scope(row):
            raise RefreshConflict("Refreshed scope failed validation; no changes were saved.")
        await db.flush()
        if existing is None:
            copied = {}
            evidence = (await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.scraped_course_id == template.id))).scalars().all()
            for item in evidence:
                ev = ScrapedFieldEvidence(**{k: v for k, v in _values(item).items()
                                            if k not in {"id", "created_at", "scraped_course_id"}},
                                          scraped_course_id=row.id)
                db.add(ev)
                await db.flush()
                copied[item.id] = ev.id
            conflicts = (await db.execute(select(FieldConflict).where(
                FieldConflict.scraped_course_id == template.id))).scalars().all()
            for item in conflicts:
                values = {k: v for k, v in _values(item).items() if k not in {
                    "id", "created_at", "scraped_course_id", "course_id", "evidence_a_id", "evidence_b_id"}}
                db.add(FieldConflict(**values, scraped_course_id=row.id,
                                     evidence_a_id=copied.get(item.evidence_a_id),
                                     evidence_b_id=copied.get(item.evidence_b_id)))
        children.append(row)
    ids = sorted(r.id for r in children)
    for row in children:
        row.extraction_method = {**row.extraction_method, RECEIPT: {
            "token": _hash(token), "courseIds": ids, "actor": actor, "at": source["captured_at"]}}
        await persist_staged_row_backup(db, row)
    await db.flush()
    return {"status": "applied", "courseIds": ids}