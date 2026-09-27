"""Real PostgreSQL savepoint tests; every fixture rolls back its outer transaction."""
from copy import deepcopy

import pytest
import httpx
from fastapi import FastAPI, HTTPException
from sqlalchemy import select

from app.models import ScrapedCourse, ScrapedFieldEvidence
from app.routers import staged_campus_preparation as api
from app.routers import staged_selected_approval as approval
from app.services.scraper import ulaw_scope_refresh as service
from app.services.scraper.ulaw_qualifications import QUALIFICATION_SCOPE
from tests.test_ulaw_qualifications import (
    db, seed, freeze_cohort, freeze_rollover, live_capture, mock_official_fetch, published,
)


async def prepared(db, monkeypatch):
    freeze_cohort(monkeypatch)
    mock_official_fetch(monkeypatch, live_capture())
    uni, job, row_id = await seed(db)
    result = await approval.approve_selected(
        approval.ApproveSelectedBody(courseIds=[row_id]), db, {"email": "reviewer"})
    assert not result["failed"], result
    rows = (await db.execute(select(ScrapedCourse).where(
        ScrapedCourse.scrape_job_id == job).order_by(ScrapedCourse.id))).scalars().all()
    assert len(rows) == 3
    freeze_rollover(monkeypatch)
    return uni, row_id, rows


@pytest.mark.asyncio
async def test_preview_apply_retains_ids_evidence_and_publication_then_replay(db, monkeypatch):
    uni, row_id, rows = await prepared(db, monkeypatch)
    ids = {r.id for r in rows}
    before = await service._snapshot(db, rows)
    links = {r.id: r.course_id for r in rows}
    pre_split = {r.id: deepcopy(r.extraction_method[QUALIFICATION_SCOPE]["pre_split"]) for r in rows}
    courses, offerings = await published(db, uni)
    course_ids = {c.id for c in courses}
    offering_values = {o.id: (o.course_id, o.location, o.fee_amount, o.fee_year) for o in offerings}
    preview = await api.preview_qualification_refresh(row_id, db, {"email": "reviewer"})
    assert await service._snapshot(db, rows) == before
    assert len(preview["changes"]) == 4
    new = [c for c in preview["changes"] if c["kind"] == "new"]
    assert [(c["award"], c["campus"], c["new"]["amount"]) for c in new] == [("PG Cert", "Bristol", 6300)]
    assert all(c["old"]["year"] == 2026 and c["new"]["year"] == 2027
               for c in preview["changes"] if c["old"])
    result = await api.apply_qualification_refresh(
        row_id, api.ScopeRefreshInput(token=preview["token"]), db, {"email": "reviewer"})
    assert result["status"] == "applied" and ids < set(result["courseIds"])
    for r in rows:
        assert r.course_id == links[r.id] and r.status == "pending"
        assert r.extraction_method[QUALIFICATION_SCOPE]["pre_split"] == pre_split[r.id]
        assert r.fee_year == 2027
    assert {c.id for c in (await published(db, uni))[0]} == course_ids
    assert {o.id: (o.course_id, o.location, o.fee_amount, o.fee_year)
            for o in (await published(db, uni))[1]} == offering_values
    new_id = next(iter(set(result["courseIds"]) - ids))
    new_row = await db.get(ScrapedCourse, new_id)
    assert new_row.course_location == "Bristol" and new_row.course_id is None
    evidence = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == new_id))).scalars().all()
    assert evidence and evidence[0].snippet == "Published qualification-specific non-domestic fees"
    retry = await api.apply_qualification_refresh(
        row_id, api.ScopeRefreshInput(token=preview["token"]), db, {"email": "reviewer"})
    assert retry == {"status": "already_applied", "courseIds": result["courseIds"]}
    approved = await approval.approve_selected(
        approval.ApproveSelectedBody(courseIds=result["courseIds"]), db, {"email": "reviewer"})
    assert not approved["failed"], approved
    assert {c.id for c in (await published(db, uni))[0]} == course_ids
    current = (await published(db, uni))[1]
    assert len(current) == 4
    assert set(offering_values) <= {o.id for o in current}
    assert all(o.fee_year == 2027 for o in current)


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["row", "source", "evidence", "actor", "token"])
async def test_stale_preview_fails_closed(db, monkeypatch, damage):
    uni, row_id, rows = await prepared(db, monkeypatch)
    preview = await service.preview_refresh(db, row_id, "reviewer")
    token, actor = preview["token"], "reviewer"
    if damage == "row":
        rows[-1].notes = "Concurrent reviewer edit"
        await db.commit()
    elif damage == "evidence":
        db.add(ScrapedFieldEvidence(scraped_course_id=rows[-1].id, field_key="fee_year",
                                   source_url=service.URL, snippet="Concurrent evidence"))
        await db.commit()
    elif damage == "source":
        async def changed():
            html = live_capture().replace("&pound;6,750", "&pound;6,760")
            authority, proofs = service._verified_page(html, service.URL)
            return {"authority": authority, "proofs": proofs,
                    "contract_sha256": service._contract_hash(authority, proofs)}
        monkeypatch.setattr(service, "_source", changed)
    elif damage == "actor":
        actor = "another reviewer"
    else:
        token += "tamper"
    before = await service._snapshot(db, rows)
    with pytest.raises(service.RefreshConflict):
        await service.apply_refresh(db, row_id, token, actor)
    assert await service._snapshot(db, rows) == before
    assert len((await published(db, uni))[1]) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("damage", ["online", "missing_award_starts", "removal"])
async def test_preview_never_infers_delivery_or_removes_campuses(db, monkeypatch, damage):
    _, row_id, rows = await prepared(db, monkeypatch)
    before = await service._snapshot(db, rows)
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(live_capture(), "html.parser")
    if damage == "online":
        soup.select_one(".key-facts__locations").clear()
        anchor = soup.new_tag("a", href="/locations/online/")
        anchor.string = "Online"
        soup.select_one(".key-facts__locations").append(anchor)
    elif damage == "missing_award_starts":
        soup.find(id="accordion-bcz").decompose()
    else:
        for li in soup.select("#accordion-bcz li li"):
            li.string = li.get_text().replace("Bristol and London Moorgate", "London Moorgate")
    # Real HTTP streaming boundary with an official-page response fixture.
    # The preparation helper patched AsyncClient; restore the actual class.
    from httpx._client import AsyncClient
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text=str(soup), request=request))
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=transport, **kwargs))
    with pytest.raises(service.RefreshConflict):
        await service.preview_refresh(db, row_id, "reviewer")
    assert await service._snapshot(db, rows) == before


@pytest.mark.asyncio
async def test_routes_enforce_permission_and_real_http_contract(db, monkeypatch):
    uni, row_id, _ = await prepared(db, monkeypatch)
    from app.dependencies import get_current_user, get_db
    app = FastAPI()
    app.include_router(api.router, prefix="/api/scrape")
    app.include_router(approval.router, prefix="/api/scrape")
    user = {"email": "reviewer", "permissions": []}
    app.dependency_overrides[get_current_user] = lambda: user
    async def session():
        yield db
    app.dependency_overrides[get_db] = session
    from httpx._client import AsyncClient
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        url = f"/api/scrape/staged/{row_id}/qualification-refresh"
        lookup = f"/api/scrape/universities/{uni}/approved-qualification-cohorts"
        assert (await client.get(lookup)).status_code == 403
        for suffix in ("preview", "apply"):
            response = await client.post(f"{url}/{suffix}", json={"token": "not-a-token"})
            assert response.status_code == 403
        user["permissions"] = ["staged.approve"]
        found = await client.get(lookup)
        assert found.status_code == 200
        cohorts = found.json()["cohorts"]
        assert len(cohorts) == 1 and cohorts[0]["rowId"] == row_id and cohorts[0]["latest"]
        assert all(r.status == "approved" for r in await service._cohort(db, row_id))
        preview = await client.post(f"{url}/preview")
        assert preview.status_code == 200
        token = preview.json()["token"]
        applied = await client.post(f"{url}/apply", json={"token": token})
        assert applied.status_code == 200 and applied.json()["status"] == "applied"
        replay = await client.post(f"{url}/apply", json={"token": token})
        assert replay.json()["status"] == "already_applied"
        assert (await client.get(lookup)).json()["cohorts"] == []
        assert all(r.status == "pending" for r in await service._cohort(db, row_id))
        assert len((await published(db, uni))[1]) == 3
        approved = await client.post("/api/scrape/staged/approve-selected",
                                     json={"courseIds": applied.json()["courseIds"]})
        assert approved.status_code == 200 and not approved.json()["failed"]
    assert len((await published(db, uni))[1]) == 4


@pytest.mark.asyncio
async def test_approved_lookup_never_merges_jobs_and_exposes_explicit_latest_identity(db, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from uuid import uuid4
    from app.models.scrape_runtime import ScrapeRuntimeJob
    uni, row_id, rows = await prepared(db, monkeypatch)
    new_job = str(uuid4())
    db.add(ScrapeRuntimeJob(runtime_job_id=new_job, university_id=uni,
                           job_type="scrape", status="completed",
                           created_at=datetime.now(timezone.utc) + timedelta(seconds=1)))
    await db.flush()
    # Same source/root ID in a different job must not borrow missing award rows.
    cert = next(r for r in rows if r.extraction_method[QUALIFICATION_SCOPE]["award"] == "PG Cert")
    async def clone(row):
        copy = ScrapedCourse(**{k: v for k, v in service._values(row).items()
                               if k not in {"id", "created_at", "canonical_course_url", "scrape_job_id"}},
                             scrape_job_id=new_job)
        db.add(copy)
        await db.flush()
        return copy
    new_rows = [await clone(cert)]
    found = await api.approved_qualification_cohorts(uni, db, {"email": "reviewer"})
    assert len(found["cohorts"]) == 1 and found["cohorts"][0]["jobId"] == rows[0].scrape_job_id
    for row in rows:
        if row.id != cert.id:
            new_rows.append(await clone(row))
    found = await api.approved_qualification_cohorts(uni, db, {"email": "reviewer"})
    assert len(found["cohorts"]) == 2
    assert found["cohorts"][0]["jobId"] == new_job and found["cohorts"][0]["latest"]
    assert not found["cohorts"][1]["latest"]
    assert set(found["cohorts"][0]["courseIds"]) == {r.id for r in new_rows}
    await db.commit()
    preview = await service.preview_refresh(db, row_id, "reviewer")
    result = await service.apply_refresh(db, row_id, preview["token"], "reviewer")
    assert set(result["courseIds"]).isdisjoint({r.id for r in new_rows})
    assert all(r.status == "approved" and r.fee_year == 2026 for r in new_rows)


@pytest.mark.asyncio
async def test_api_failure_rolls_back_all_rows_and_new_campus(db, monkeypatch):
    uni, row_id, rows = await prepared(db, monkeypatch)
    preview = await service.preview_refresh(db, row_id, "reviewer")
    before = await service._snapshot(db, rows)
    original = service._apply_group
    calls = 0
    def fail_late(*args, **kwargs):
        nonlocal calls
        calls += 1
        original(*args, **kwargs)
        if calls == 4:
            raise RuntimeError("Injected late failure")
    monkeypatch.setattr(service, "_apply_group", fail_late)
    with pytest.raises(HTTPException) as exc:
        await api.apply_qualification_refresh(
            row_id, api.ScopeRefreshInput(token=preview["token"]), db, {"email": "reviewer"})
    assert exc.value.status_code == 500
    rows = await service._cohort(db, row_id)
    assert len(rows) == 3 and await service._snapshot(db, rows) == before
    assert len((await published(db, uni))[1]) == 3
    monkeypatch.setattr(service, "_apply_group", original)
    retry = await api.apply_qualification_refresh(
        row_id, api.ScopeRefreshInput(token=preview["token"]), db, {"email": "reviewer"})
    assert retry["status"] == "applied" and len(retry["courseIds"]) == 4
    assert len((await published(db, uni))[1]) == 3