from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.scrape_reports import CourseReport, report_payload, report_result, validate_official_urls
from app.services.scraper.autonomous_verification import validate_verification


def test_missing_requires_official_targets():
    with pytest.raises(ValidationError):
        CourseReport(kind="missing")
    with pytest.raises(ValidationError):
        CourseReport(kind="missing", course_urls=["https://uni.edu/course"], catalogue_url="https://uni.edu/list")
    with pytest.raises(ValidationError):
        CourseReport(kind="missing", course_urls=["https://uni.edu/course"], expected_count=0)


def test_incorrect_requires_fields_description_and_courses():
    for values in (
        {"fields": ["fee"], "description": "Wrong amount"},
        {"course_urls": ["https://uni.edu/course"], "description": "Wrong amount"},
        {"course_urls": ["https://uni.edu/course"], "fields": ["fee"]},
    ):
        with pytest.raises(ValidationError):
            CourseReport(kind="incorrect", **values)


@pytest.mark.asyncio
async def test_internal_policy_is_review_only_and_does_not_override_filters():
    parent = SimpleNamespace(runtime_job_id="parent", university_id=7, url="https://uni.edu/catalogue")
    report = CourseReport(kind="incorrect", course_urls=["https://uni.edu/course"],
                          fields=["fee"], description="Wrong fee", source_url="https://uni.edu/fees")
    payload = report_payload(parent, report, "report_1", 12)
    parent.request_payload = {"aiRepairWorkflow": {
        "job_id": "parent", "university_id": 7, "session_id": "report_1", "status": "running",
        "autonomous": {"verification_job_id": "child", "phase": "verification_queued"},
    }}
    job = SimpleNamespace(runtime_job_id="child", university_id=7, job_type="scrape", request_payload=payload)
    policy = await validate_verification(SimpleNamespace(get=AsyncMock(return_value=parent)), job)
    assert policy.round_index == 1
    assert policy.metadata()["review_only"] is True
    assert policy.metadata()["full_catalogue_verified"] is False
    assert payload["retrySourceJobId"] == "parent"
    assert payload["feePage"] == "https://uni.edu/fees"
    assert payload["courseReport"]["requested_by"] == "12"
    assert "admin_config" not in payload
    assert payload["courseReport"]["description"] == "Wrong fee"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://169.254.169.254/latest", "https://uni.edu.evil.test/course",
    "https://user:password@uni.edu/course", "https://uni.edu:8080/course", "file:///etc/passwd",
])
async def test_rejects_foreign_private_and_credential_urls(url):
    with pytest.raises(HTTPException) as error:
        await validate_official_urls(CourseReport(kind="missing", course_urls=[url]),
                                    SimpleNamespace(website="https://uni.edu", scrape_url=None))
    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_rejects_private_dns_on_official_host(monkeypatch):
    monkeypatch.setattr("app.services.scraper_config_ai._is_safe_public_url",
                        lambda url: (False, "resolves to non-public address"))
    with pytest.raises(HTTPException):
        await validate_official_urls(CourseReport(kind="missing", course_urls=["https://uni.edu/course"]),
                                    SimpleNamespace(website="https://uni.edu", scrape_url=None))


@pytest.mark.asyncio
async def test_redirects_are_not_followed(monkeypatch):
    monkeypatch.setattr("app.services.scraper_config_ai._is_safe_public_url", lambda url: (True, ""))
    real_client = httpx.AsyncClient
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(HTTPException) as error:
        await validate_official_urls(CourseReport(kind="missing", course_urls=["https://uni.edu/course"]),
                                    SimpleNamespace(website="https://uni.edu", scrape_url=None))
    assert error.value.status_code == 422
    assert requests == ["https://uni.edu/course"]


@pytest.mark.asyncio
async def test_accepts_public_connected_server_addr_when_peername_is_unavailable(monkeypatch):
    monkeypatch.setattr("app.services.scraper_config_ai._is_safe_public_url", lambda url: (True, ""))
    real_client = httpx.AsyncClient
    stream = SimpleNamespace(get_extra_info=lambda key: {
        "peername": None,
        "server_addr": ("52.76.147.18", 443),
    }.get(key))

    def handler(request):
        return httpx.Response(200, text="<html>Official course</html>",
                              extensions={"network_stream": stream})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    result = await validate_official_urls(
        CourseReport(kind="missing", course_urls=["https://uni.edu/course"]),
        SimpleNamespace(website="https://uni.edu", scrape_url=None),
    )
    assert result == {"verified_programmes": {}}


@pytest.mark.asyncio
async def test_rejects_private_connected_server_addr_when_peername_is_unavailable(monkeypatch):
    monkeypatch.setattr("app.services.scraper_config_ai._is_safe_public_url", lambda url: (True, ""))
    real_client = httpx.AsyncClient
    stream = SimpleNamespace(get_extra_info=lambda key: {
        "peername": None,
        "server_addr": ("127.0.0.1", 443),
    }.get(key))

    def handler(request):
        return httpx.Response(200, text="<html>Official course</html>",
                              extensions={"network_stream": stream})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    with pytest.raises(HTTPException) as error:
        await validate_official_urls(
            CourseReport(kind="missing", course_urls=["https://uni.edu/course"]),
            SimpleNamespace(website="https://uni.edu", scrape_url=None),
        )
    assert error.value.status_code == 422
    assert "non-public network peer" in str(error.value.detail)


@pytest.mark.asyncio
async def test_verifies_elementor_foundation_page_with_sibling_admissions_copy(monkeypatch):
    monkeypatch.setattr("app.services.scraper_config_ai._is_safe_public_url", lambda url: (True, ""))
    real_client = httpx.AsyncClient
    stream = SimpleNamespace(get_extra_info=lambda key: {
        "peername": None,
        "server_addr": ("52.76.147.18", 443),
    }.get(key))
    html = """
      <html><head><title>Foundation in Liberal Arts - Raffles University</title></head>
      <body>
        <main>
          <h3>FOUNDATION IN LIBERAL ARTS</h3>
          <section>
            <div><div><h2>Entry Requirements</h2></div></div>
            <div>Applicants require five credits or an equivalent qualification.</div>
            <div>International Student English Requirement</div>
          </section>
        </main>
      </body></html>
    """

    def handler(request):
        return httpx.Response(200, text=html, extensions={"network_stream": stream})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(handler), **kwargs))
    url = "https://uni.edu/programme/foundation-in-liberal-arts/"
    result = await validate_official_urls(
        CourseReport(kind="missing", course_urls=[url], eligibility_review=True),
        SimpleNamespace(website="https://uni.edu", scrape_url=None),
    )
    assert result["verified_programmes"][url]["kind"] == "foundation"


def test_result_does_not_equate_staging_with_catalogue_coverage():
    job = SimpleNamespace(runtime_job_id="child", status="completed", request_payload={"courseReport": {}},
                          discovered_config={}, total_found=2, imported=2, skipped=0, errors=0,
                          error_message=None, gate_skip_counts={"domestic": 3})
    result = report_result(job)
    assert result["catalogue_coverage"] == "not_verified"
    assert result["exclusions"] == {"domestic": 3}
    blocked = report_result(job, {"autonomous": {"phase": "blocked", "recovery_exhausted": True}})
    assert blocked["status"] == "blocked"
    assert blocked["recovery"]["exhausted"] is True


@pytest.mark.asyncio
async def test_submit_binds_durable_workflow_before_dispatch(monkeypatch):
    from app.routers import scrape_reports as routes
    from app.services import ai_repair_workflow as workflow
    from app.tasks.auto_repair_task import monitor_ai_scrape_repair

    parent = SimpleNamespace(runtime_job_id="parent", university_id=7, status="stopped", url="https://uni.edu")
    university = SimpleNamespace(id=7, name="University", website="https://uni.edu", scrape_url="https://uni.edu")
    db = SimpleNamespace(get=AsyncMock(side_effect=[parent, university]), flush=AsyncMock(), rollback=AsyncMock(), refresh=AsyncMock())
    children = []
    db.add = children.append
    monkeypatch.setattr(routes, "validate_official_urls", AsyncMock())
    monkeypatch.setattr("app.routers.scrape._lock_and_find_active_job", AsyncMock(return_value=None))
    monkeypatch.setattr(workflow, "active_audit", AsyncMock(return_value={}))
    monkeypatch.setattr(workflow.agent, "acquire_repair_lease", lambda *args: True)
    saved = []
    async def save(session, _db):
        saved.append(session)
        parent.request_payload = {"aiRepairWorkflow": session}
    async def dispatch(session, _db):
        assert saved
        child = children[0]
        policy = await validate_verification(SimpleNamespace(get=AsyncMock(return_value=parent)), child)
        assert policy.round_index == 1
        return session
    monkeypatch.setattr(workflow, "save", save)
    monkeypatch.setattr(workflow, "dispatch_verification", dispatch)
    monkeypatch.setattr(monitor_ai_scrape_repair, "apply_async", lambda **kwargs: None)
    result = await routes.submit_course_report("parent", CourseReport(
        kind="missing", course_urls=["https://uni.edu/course"], expected_count=30,
    ), db, {"id": 12})
    assert result["job_id"] == children[0].runtime_job_id
    assert saved[0]["course_report"]["expected_count"] == 30
    assert saved[0]["autonomous"]["config_changes_applied"] is False
    assert children[0].request_payload["retrySourceJobId"] == "parent"


@pytest.mark.asyncio
async def test_active_scrape_rejects_before_creating_report(monkeypatch):
    from app.routers import scrape_reports as routes
    from app.services import ai_repair_workflow as workflow
    parent = SimpleNamespace(runtime_job_id="parent", university_id=7, status="completed")
    db = SimpleNamespace(get=AsyncMock(side_effect=[parent, SimpleNamespace(id=7)]), rollback=AsyncMock(), refresh=AsyncMock())
    monkeypatch.setattr(routes, "validate_official_urls", AsyncMock())
    monkeypatch.setattr("app.routers.scrape._lock_and_find_active_job", AsyncMock(return_value=parent))
    monkeypatch.setattr(workflow, "active_audit", AsyncMock(return_value={}))
    with pytest.raises(HTTPException) as error:
        await routes.submit_course_report("parent", CourseReport(kind="missing", catalogue_url="https://uni.edu"), db, {"id": 12})
    assert error.value.status_code == 409
    db.rollback.assert_awaited_once()


@pytest.mark.asyncio
async def test_report_routes_require_existing_scrape_permissions():
    from fastapi import FastAPI
    from app.dependencies import get_current_user, get_db
    from app.routers.scrape_reports import router
    app = FastAPI()
    app.include_router(router, prefix="/api/scrape")
    async def database():
        yield SimpleNamespace()
    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        unauthenticated = await client.get("/api/scrape/jobs/parent/course-reports")
        assert unauthenticated.status_code == 401
        app.dependency_overrides[get_current_user] = lambda: {"id": 1, "permissions": []}
        forbidden = await client.post("/api/scrape/jobs/parent/course-reports", json={
            "kind": "missing", "course_urls": ["https://uni.edu/course"],
        })
        assert forbidden.status_code == 403