"""Real PostgreSQL, HTTP boundary and stale-write fencing for diagnostic metadata."""
import json
from copy import deepcopy
import httpx
import pytest
from fastapi import FastAPI

from app.dependencies import get_db, get_current_user
from app.models import ScrapedCourse
from app.routers import scrape, staged_selected_approval
from app.services.scraper.approval_guidance import (
    attempt_identity, persist_failure, public_approval_guidance,
)
from tests.test_ulaw_qualifications import db, seed, freeze_cohort, counted_official_fetch


@pytest.mark.asyncio
async def test_historical_json_download_links_are_authenticated_and_redacted(db, monkeypatch):
    import gzip
    from unittest.mock import AsyncMock
    from app.models.page_snapshot import PageSnapshot
    from app.routers import snapshots
    from app.services.snapshot_store import url_hash

    uni, job, _ = await seed(db)
    course_url = "https://snapshot.test/course"
    records = []
    for kind, key in (("json", "legacy/source.pdf"),
                      ("html", "legacy/source.json"), ("pdf", "legacy/document.bin")):
        record = PageSnapshot(university_id=uni, scrape_job_id=job,
                              course_url=course_url, url_hash=url_hash(course_url),
                              snapshot_type=kind, storage_path=key)
        db.add(record)
        records.append(record)
    await db.flush()
    stored = gzip.compress(json.dumps({
        "nested": [{"last_qualification_approval": {"evidenceFingerprint": "PRIVATE-HASH"},
                    "lastQualificationApproval": {"evidenceFingerprint": "PRIVATE-HASH"},
                    "evidenceFingerprint": "keep-evidence", "proofcontract_sha256": "keep-proof"}],
    }).encode())
    monkeypatch.setattr(snapshots, "get_snapshot_bytes", AsyncMock(return_value=stored))
    monkeypatch.setattr(snapshots, "snapshot_availability", AsyncMock(return_value={
        "available": True, "availability": "available", "expires_at": None,
    }))
    signer = AsyncMock(side_effect=lambda key, **kwargs: f"https://storage.test/{key}")
    monkeypatch.setattr(snapshots, "presign_url", signer)
    monkeypatch.setattr(snapshots, "is_enabled", lambda: True)
    app = FastAPI()
    app.include_router(snapshots.router, prefix="/api/scrape")
    permissions = ["staged.view"]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {"permissions": permissions}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        direct = await client.get(f"/api/scrape/snapshot/download/{job}",
                                  params={"key": records[0].storage_path})
        assert direct.status_code == 200, direct.text
        listed = await client.get("/api/scrape/snapshot/for-course",
                                  params={"job_id": job, "course_url": course_url})
        assert listed.status_code == 200, listed.text
        links = {item["snapshot_type"]: item["download_url"] for item in listed.json()["snapshots"]}
        for link in (direct.json()["url"], links["json"]):
            assert link.startswith("/api/scrape/snapshot/content/")
            downloaded = await client.get(link)
            assert downloaded.status_code == 200
            assert downloaded.headers["content-type"] == "application/json"
            assert downloaded.headers["content-disposition"].startswith("attachment;")
            assert "PRIVATE-HASH" not in downloaded.text
            assert "last_qualification_approval" not in downloaded.text
            assert "lastQualificationApproval" not in downloaded.text
            assert "keep-evidence" in downloaded.text and "keep-proof" in downloaded.text
            permissions.clear()
            assert (await client.get(link)).status_code == 403
            permissions.append("staged.view")
        assert links["html"] == "https://storage.test/legacy/source.json"
        assert links["pdf"] == "https://storage.test/legacy/document.bin"
        for kind, record in zip(("json", "html", "pdf"), records):
            response = await client.get(f"/api/scrape/snapshot/download/{job}",
                                        params={"key": record.storage_path})
            assert response.status_code == 200
            assert response.json()["url"] == links[kind]
        for wrong_job, key in (("other-job", records[0].storage_path),
                               (job, "unrecorded/object.html")):
            assert (await client.get(f"/api/scrape/snapshot/download/{wrong_job}",
                                     params={"key": key})).status_code == 404
        assert all(call.args[0] != records[0].storage_path for call in signer.call_args_list)


@pytest.mark.asyncio
async def test_failed_approval_refresh_backup_and_legacy_nested_http_redaction(db, monkeypatch):
    from sqlalchemy import select
    from app.models.page_snapshot import PageSnapshot
    from app.routers import snapshots, staged_campus_preparation, reviews
    from app.services.scraper.snapshot_save import persist_staged_row_backup
    from tests.test_ulaw_scope_refresh import prepared
    from httpx._client import AsyncClient

    uni, row_id, rows = await prepared(db, monkeypatch)
    job = rows[0].scrape_job_id
    for row in rows:
        row.status = "pending"
    await db.commit()
    app = FastAPI()
    for router in (scrape.router, staged_selected_approval.router,
                   staged_campus_preparation.router, snapshots.router):
        app.include_router(router, prefix="/api/scrape")
    app.include_router(reviews.router, prefix="/api")
    permissions = ["staged.approve", "staged.view", "staged.edit"]
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: {
        "email": "reviewer", "permissions": permissions,
    }
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        failure = await client.post("/api/scrape/staged/approve-selected",
                                    json={"courseIds": [row_id]})
        assert failure.status_code == 200
        assert failure.json()["failed"][0]["reasonCode"] == "invalid_stored_scope"
        db.expire_all()
        row = await db.get(ScrapedCourse, row_id)
        private = deepcopy(row.last_qualification_approval)
        assert private and private["evidenceFingerprint"]
        await persist_staged_row_backup(db, row)
        await db.flush()
        snap = (await db.execute(select(PageSnapshot).where(
            PageSnapshot.scrape_job_id == job,
            PageSnapshot.snapshot_type == "staged_row"))).scalars().first()
        assert "last_qualification_approval" not in json.dumps(snap.original_extraction)

        url = f"/api/scrape/staged/{row_id}/qualification-refresh"
        preview = await client.post(url + "/preview")
        assert preview.status_code == 200, preview.text
        applied = await client.post(url + "/apply", json={"token": preview.json()["token"]})
        assert applied.status_code == 200, applied.text
        db.expire_all()
        row = await db.get(ScrapedCourse, row_id)
        assert "last_qualification_approval" not in json.dumps(row.extraction_method)
        assert private["evidenceFingerprint"] not in json.dumps(row.extraction_method)

        # Simulate already-stored legacy copies, including both key spellings.
        legacy = {"items": [{"last_qualification_approval": private,
                             "lastQualificationApproval": private,
                             "evidenceFingerprint": "unrelated-evidence-hash",
                             "proofcontract_sha256": "unrelated-proof-hash"}]}
        row.extraction_method = {**row.extraction_method, "legacy": legacy}
        await persist_staged_row_backup(db, row)
        await db.flush()
        snap = (await db.execute(select(PageSnapshot).where(
            PageSnapshot.scrape_job_id == job,
            PageSnapshot.snapshot_type == "staged_row"))).scalars().first()
        assert "last_qualification_approval" not in json.dumps(snap.original_extraction)
        snap.original_extraction = {**snap.original_extraction, "legacy": legacy}
        await db.commit()
        snapshot_url = "/api/scrape/snapshot/for-course"
        snapshot_params = {"job_id": job, "course_url": row.course_website}
        urls = [f"/api/scrape/staged/{row_id}", f"/api/scrape/staged/{row_id}/review",
                f"/api/scrape/staged/{job}", f"/api/scrape/staged?jobId={job}",
                f"/api/scraped-courses?university_id={uni}",
                f"/api/scrape/export?universityId={uni}&format=json",
                f"/api/scrape/export?universityId={uni}&format=csv"]
        for path in urls:
            result = await client.get(path)
            assert result.status_code == 200, result.text
            assert private["evidenceFingerprint"] not in result.text
            assert "last_qualification_approval" not in result.text
            assert "unrelated-evidence-hash" in result.text
            assert "unrelated-proof-hash" in result.text
        result = await client.get(snapshot_url, params=snapshot_params)
        assert result.status_code == 200, result.text
        assert private["evidenceFingerprint"] not in result.text
        assert "last_qualification_approval" not in result.text
        assert "unrelated-evidence-hash" in result.text
        permissions[:] = ["staged.approve"]
        for path in urls:
            assert (await client.get(path)).status_code == 403
        assert (await client.get(snapshot_url, params=snapshot_params)).status_code == 403
        for path in (f"/api/scrape/snapshots/{job}",
                     f"/api/scrape/snapshots/{job}/summary",
                     "/api/scrape/snapshots/storage-stats",
                     f"/api/scrape/snapshot/text/{snap.id}",
                     f"/api/scrape/snapshot/download/{job}?key=unavailable"):
            assert (await client.get(path)).status_code == 403


@pytest.mark.asyncio
async def test_http_failure_reload_fresh_success_and_permissions(db, monkeypatch):
    freeze_cohort(monkeypatch)
    uni, job, row_id = await seed(db)
    app = FastAPI()
    app.include_router(staged_selected_approval.router, prefix="/api/scrape")
    app.include_router(scrape.router, prefix="/api/scrape")
    app.dependency_overrides[get_db] = lambda: db
    permissions = ["staged.approve", "staged.view"]
    app.dependency_overrides[get_current_user] = lambda: {"email": "reviewer", "permissions": permissions}
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
    upstream = counted_official_fetch(monkeypatch)
    upstream.update(status=503, html="PRIVATE provider token=secret")
    async with client:
        denied_body = {"courseIds": [row_id], "force": True}
        permissions.clear()
        assert (await client.post("/api/scrape/staged/approve-selected", json=denied_body)).status_code == 403
        assert (await client.get(f"/api/scrape/staged/{row_id}")).status_code == 403
        assert (await db.get(ScrapedCourse, row_id)).last_qualification_approval is None
        permissions.extend(["staged.approve", "staged.view"])
        failed = await client.post("/api/scrape/staged/approve-selected", json=denied_body)
        assert failed.json()["failed"][0]["reasonCode"] == "official_source_unavailable"
        db.expire_all()
        row = await db.get(ScrapedCourse, row_id)
        saved = row.last_qualification_approval
        assert set(saved) == {"rowId", "jobId", "universityId", "evidenceFingerprint", "reasonCode", "attemptedAt"}
        assert "PRIVATE" not in json.dumps(saved) and "secret" not in json.dumps(saved)
        loaded = await client.get(f"/api/scrape/staged/{row_id}")
        assert loaded.status_code == 200
        assert loaded.json()["lastQualificationApproval"]["jobId"] == job
        assert "evidenceFingerprint" not in loaded.text
        from tests.test_ulaw_qualifications import HTML
        upstream.update(status=200, html=HTML)
        calls = len(upstream["calls"])
        result = await client.post("/api/scrape/staged/approve-selected", json=denied_body)
        assert result.json()["approvedCount"] > 0
        assert len(upstream["calls"]) > calls
        db.expire_all()
        assert (await db.get(ScrapedCourse, row_id)).last_qualification_approval is None


@pytest.mark.asyncio
async def test_same_id_evidence_change_and_wrong_identity_fences(db):
    uni, job, row_id = await seed(db)
    row = await db.get(ScrapedCourse, row_id)
    original = attempt_identity(row)
    await persist_failure(db, original, "changed_cohort")
    assert public_approval_guidance(row)
    row.notes = "Reviewer changed evidence"
    await db.commit()
    assert public_approval_guidance(row) is None
    assert row.last_qualification_approval is None
    await persist_failure(db, original, "official_source_unavailable")
    assert row.last_qualification_approval is None
    fresh = attempt_identity(row)
    for key, wrong in [("jobId", "wrong-job"), ("rowId", row_id + 999999), ("universityId", uni + 999999)]:
        await persist_failure(db, {**fresh, key: wrong}, "changed_cohort")
        assert row.last_qualification_approval is None
    await persist_failure(db, fresh, "PRIVATE token")
    assert row.last_qualification_approval is None
    await persist_failure(db, fresh, "unverified_page")
    assert public_approval_guidance(row)["reasonCode"] == "unverified_page"


@pytest.mark.asyncio
async def test_split_child_rollback_records_original_stable_row_only(db, monkeypatch):
    from sqlalchemy import select
    from app.services.scraper.approve_course import ApprovalValidationError
    freeze_cohort(monkeypatch)
    counted_official_fetch(monkeypatch)
    uni, job, row_id = await seed(db)
    children = []

    async def fail_child(_db, row, **kwargs):
        children.append(row.id)
        if len(children) > 1:
            raise ApprovalValidationError("PRIVATE token=secret", reason_code="invalid_stored_scope")

    monkeypatch.setattr(staged_selected_approval, "approve_scraped_course", fail_child)
    result = await staged_selected_approval.approve_selected(
        staged_selected_approval.ApproveSelectedBody(courseIds=[row_id], force=True),
        db, {"email": "reviewer"},
    )
    assert result["failed"][0]["id"] == row_id
    assert len(children) > 1
    rows = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.university_id == uni))).scalars().all()
    assert [row.id for row in rows] == [row_id]
    assert public_approval_guidance(rows[0])["rowId"] == row_id
    assert public_approval_guidance(rows[0])["reasonCode"] == "invalid_stored_scope"


@pytest.mark.asyncio
async def test_delayed_failure_write_loses_to_concurrent_evidence_edit(db):
    import asyncio
    from sqlalchemy import text
    _, _, row_id = await seed(db)
    captured = attempt_identity(await db.get(ScrapedCourse, row_id))
    rolled_back = asyncio.Event()
    edit_finished = asyncio.Event()

    async def delayed_failure():
        await db.rollback()
        rolled_back.set()
        await edit_finished.wait()
        await persist_failure(db, captured, "official_source_unavailable")

    async def intervening_writer():
        await rolled_back.wait()
        # A bulk SQL path bypasses ORM cleanup; the durable writer must still
        # reload and compare the exact persisted evidence under its row lock.
        await db.execute(text("UPDATE scraped_courses SET notes = 'concurrent evidence edit' WHERE id = :id"),
                         {"id": row_id})
        await db.commit()
        edit_finished.set()

    await asyncio.gather(delayed_failure(), intervening_writer())
    db.expire_all()
    row = await db.get(ScrapedCourse, row_id)
    assert row.last_qualification_approval is None
    assert row.notes == "concurrent evidence edit"


@pytest.mark.asyncio
async def test_all_row_serializers_strip_private_fingerprint_and_gate_reads(db, monkeypatch):
    from app.routers import reviews
    uni, job, row_id = await seed(db)
    row = await db.get(ScrapedCourse, row_id)
    await persist_failure(db, attempt_identity(row), "changed_cohort")
    app = FastAPI()
    app.include_router(scrape.router, prefix="/api/scrape")
    app.include_router(reviews.router, prefix="/api")
    app.dependency_overrides[get_db] = lambda: db
    permissions = []
    app.dependency_overrides[get_current_user] = lambda: {"permissions": permissions}
    from unittest.mock import AsyncMock
    monkeypatch.setattr(scrape, "_apply_backup_one", AsyncMock(return_value={"appliedFields": []}))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        urls = [f"/api/scraped-courses?university_id={uni}",
                f"/api/scrape/staged/{row_id}", f"/api/scrape/staged/{row_id}/review",
                f"/api/scrape/staged?jobId={job}", f"/api/scrape/staged/{job}"]
        for url in urls:
            assert (await client.get(url)).status_code == 403
        for output_format in ("json", "csv"):
            assert (await client.get(f"/api/scrape/export?universityId={uni}&format={output_format}")).status_code == 403
        # Approval alone grants neither staged reads nor edit/export access.
        permissions.append("staged.approve")
        assert (await client.post(f"/api/scrape/staged/{row_id}/fee-selection",
                                 json={"snapshotToken": "token", "optionId": "option"})).status_code == 403
        assert (await client.get(f"/api/scrape/export?universityId={uni}")).status_code == 403
        backup_url = f"/api/scrape/staged/{row_id}/apply-backup"
        assert (await client.post(backup_url, json={})).status_code == 403
        permissions.extend(["staged.view", "staged.edit"])
        for output_format in ("json", "csv"):
            exported = await client.get(f"/api/scrape/export?universityId={uni}&format={output_format}")
            assert exported.status_code == 200
            assert "last_qualification_approval" not in exported.text
            assert "evidenceFingerprint" not in exported.text
            assert row.last_qualification_approval["evidenceFingerprint"] not in exported.text
        for url in urls:
            response = await client.get(url)
            assert response.status_code == 200, response.text
            assert "evidenceFingerprint" not in response.text
            assert "last_qualification_approval" not in response.text
            assert "changed_cohort" in response.text
        response = await client.post(backup_url, json={})
        assert response.status_code == 200
        assert response.json()["course"]["lastQualificationApproval"]["rowId"] == row_id
        assert "evidenceFingerprint" not in response.text


@pytest.mark.asyncio
async def test_raw_and_bulk_mutations_clear_durably_without_resurrection(db):
    from sqlalchemy import text, update
    from app.services.scraper.approval_guidance import synchronize_updated_guidance
    _, _, row_id = await seed(db)
    row = await db.get(ScrapedCourse, row_id)
    original_notes = row.notes
    for statement in [
        text("UPDATE scraped_courses SET notes='changed' WHERE id=:id RETURNING id").bindparams(id=row_id),
        update(ScrapedCourse).where(ScrapedCourse.id == row_id).values(status="rejected").returning(ScrapedCourse.id),
    ]:
        await persist_failure(db, attempt_identity(row), "changed_cohort")
        assert public_approval_guidance(row)
        result = await db.execute(statement)
        await synchronize_updated_guidance(db, result)
        # No refresh needed to avoid showing stale guidance in this session.
        assert row.last_qualification_approval is None
        await db.commit()
        await db.execute(text("UPDATE scraped_courses SET notes=:notes, status='pending' WHERE id=:id"),
                         {"notes": original_notes, "id": row_id})
        await db.commit()
        await db.refresh(row)
        assert row.last_qualification_approval is None
        assert public_approval_guidance(row) is None


@pytest.mark.asyncio
async def test_explicit_dml_sync_preserves_other_rows_noops_selects_and_pending_assignments(db):
    from sqlalchemy import text, inspect
    from app.services.scraper.approval_guidance import synchronize_updated_guidance
    _, _, a_id = await seed(db)
    _, _, b_id = await seed(db)
    a = await db.get(ScrapedCourse, a_id)
    b = await db.get(ScrapedCourse, b_id)
    await persist_failure(db, attempt_identity(a), "changed_cohort")
    await persist_failure(db, attempt_identity(b), "unverified_page")
    saved_a = dict(a.last_qualification_approval)
    saved_b = dict(b.last_qualification_approval)
    b.last_qualification_approval = {**saved_b, "reasonCode": "official_source_unavailable"}
    pending_b = dict(b.last_qualification_approval)
    assert inspect(b).attrs.last_qualification_approval.history.has_changes()

    await db.execute(text("SELECT 'UPDATE scraped_courses SET notes=''not DML'''"))
    await db.execute(text("/* UPDATE scraped_courses SET notes='not DML' */ SELECT 1"))
    assert a.last_qualification_approval == saved_a
    assert b.last_qualification_approval == pending_b

    # An UPDATE matching zero rows must not synchronize unrelated identities.
    result = await db.execute(text("UPDATE scraped_courses SET notes='none' WHERE false RETURNING id"))
    assert await synchronize_updated_guidance(db, result) == []
    # A matched no-op returns A, but the trigger correctly keeps its guidance.
    result = await db.execute(text("UPDATE scraped_courses SET notes=notes WHERE id=:id RETURNING id"),
                              {"id": a_id})
    assert await synchronize_updated_guidance(db, result) == [a_id]
    assert a.last_qualification_approval == saved_a
    assert b.last_qualification_approval == pending_b
    assert inspect(b).attrs.last_qualification_approval.history.has_changes()

    result = await db.execute(text("UPDATE scraped_courses SET notes='changed A' WHERE id=:id RETURNING id"),
                              {"id": a_id})
    assert await synchronize_updated_guidance(db, result) == [a_id]
    assert a.last_qualification_approval is None
    assert b.last_qualification_approval == pending_b
    assert inspect(b).attrs.last_qualification_approval.history.has_changes()
    await db.commit()
    await db.refresh(b)
    assert b.last_qualification_approval == pending_b