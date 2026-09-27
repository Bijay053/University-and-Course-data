"""Sanitized source failures and transaction-local retry contracts."""
import httpx
import pytest

from app.models import ScrapedCourse
from app.services.scraper.ulaw_qualifications import (
    APPROVAL_REASONS, SelectedQualificationProof, qualification_source_reason,
)
from tests.test_ulaw_qualifications import (
    HTML, URL, db, freeze_cohort, seed, prepare_qualification_children, published,
    live_capture, freeze_rollover, captured_values, counted_official_fetch,
)
from app.routers import staged_selected_approval as route


@pytest.mark.parametrize("code", list(APPROVAL_REASONS))
def test_http_failure_contract_allowlists_reason_and_message(code):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import Session
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.dependencies import get_db, get_current_user
    app = FastAPI()
    app.include_router(route.router, prefix="/api/scrape")
    with Session() as session:
        session.begin()
        fake = SimpleNamespace(
            sync_session=session,
            execute=AsyncMock(side_effect=route.ApprovalValidationError(
                "PRIVATE https://example.test/?token=secret", reason_code=code)),
            rollback=AsyncMock(side_effect=session.rollback),
        )
        app.dependency_overrides[get_db] = lambda: fake
        app.dependency_overrides[get_current_user] = lambda: {
            "email": "reviewer", "permissions": ["staged.approve"],
        }
        with TestClient(app) as client:
            response = client.post("/api/scrape/staged/approve-selected", json={"courseIds": [1], "force": True})
        assert response.status_code == 200
        assert response.json()["failed"] == [{"id": 1, "error": APPROVAL_REASONS[code], "reasonCode": code}]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "network", "503"])
async def test_unavailable_failure_reused_but_next_approval_refetches(db, monkeypatch, failure):
    freeze_cohort(monkeypatch)
    real_client = httpx.AsyncClient
    state = {"failure": None, "calls": 0}

    def respond(request):
        state["calls"] += 1
        if state["failure"] == "timeout":
            raise httpx.ReadTimeout("PRIVATE token=secret", request=request)
        if state["failure"] == "network":
            raise httpx.ConnectError("PRIVATE provider diagnostics", request=request)
        return httpx.Response(503 if state["failure"] else 200,
                              text="PRIVATE secret" if state["failure"] else HTML, request=request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(respond), **kwargs))
    uni, _, parent = await seed(db)
    ids = await prepare_qualification_children(db, parent)
    state.update(failure=failure, calls=0)
    child = await db.get(ScrapedCourse, ids[0])
    context = SelectedQualificationProof(db)
    try:
        for _ in range(2):
            assert await qualification_source_reason(child, proof_context=context, db=db) == "official_source_unavailable"
        assert state["calls"] == 1
    finally:
        context.close()
        await db.rollback()
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[ids[0]], force=True),
                                          db, {"email": "reviewer"})
    assert result["failed"] == [{"id": ids[0], "error": APPROVAL_REASONS["official_source_unavailable"],
                                "reasonCode": "official_source_unavailable"}]
    assert state["calls"] == 2
    assert not (await published(db, uni))[0]
    state["failure"] = None
    retry = await route.approve_selected(route.ApproveSelectedBody(courseIds=[ids[0]], force=True),
                                         db, {"email": "reviewer"})
    assert not retry["failed"]
    assert len(retry["approvedIds"]) == 3
    assert state["calls"] == 3


@pytest.mark.asyncio
async def test_ordinary_selected_approval_refetches_on_each_unavailable_retry(db, monkeypatch):
    freeze_cohort(monkeypatch)
    upstream = counted_official_fetch(monkeypatch)
    uni, _, parent = await seed(db)
    ids = await prepare_qualification_children(db, parent)
    upstream["calls"].clear()
    upstream["status"] = 503

    for attempt in (1, 2):
        result = await route.approve_selected(
            route.ApproveSelectedBody(courseIds=[ids[0]]), db, {"email": "reviewer"},
        )
        assert result["approvedIds"] == []
        assert result["failed"] == [{
            "id": ids[0], "error": APPROVAL_REASONS["official_source_unavailable"],
            "reasonCode": "official_source_unavailable",
        }]
        assert upstream["calls"] == [("GET", URL)] * attempt
        assert (await db.get(ScrapedCourse, ids[0])).status == "pending"
        assert await published(db, uni) == ([], [])

    upstream["status"] = 200
    recovered = await route.approve_selected(
        route.ApproveSelectedBody(courseIds=[ids[0]]), db, {"email": "reviewer"},
    )
    assert not recovered["failed"] and len(recovered["approvedIds"]) == 3
    assert upstream["calls"] == [("GET", URL)] * 3


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["fees", "intakes", "unverified"])
async def test_current_source_change_is_distinct_from_unverified_page(db, monkeypatch, change):
    from app.services.scraper.extractors.ulaw_fees import parse_course_fees
    freeze_rollover(monkeypatch)
    html = live_capture()
    upstream = counted_official_fetch(monkeypatch, html)
    values = captured_values()
    values["fee_year"] = 2027
    values["extraction_method"]["fee_variants"] = parse_course_fees(html, URL)
    uni, _, parent = await seed(db, values)
    ids = await prepare_qualification_children(db, parent)
    upstream["calls"].clear()
    upstream["html"] = (html.replace("6,750", "6,850") if change == "fees"
                        else html.replace("February 2028", "March 2028") if change == "intakes"
                        else "<h1>PRIVATE unknown source token=secret</h1>")
    result = await route.approve_selected(route.ApproveSelectedBody(courseIds=[ids[0]], force=True),
                                          db, {"email": "reviewer"})
    code = "unverified_page" if change == "unverified" else "changed_cohort"
    assert result["failed"] == [{"id": ids[0], "error": APPROVAL_REASONS[code], "reasonCode": code}]
    assert len(upstream["calls"]) == 1
    assert not (await published(db, uni))[0]
    upstream["html"] = html
    retry = await route.approve_selected(route.ApproveSelectedBody(courseIds=[ids[0]]),
                                         db, {"email": "reviewer"})
    assert not retry["failed"]
    assert len(upstream["calls"]) == 2