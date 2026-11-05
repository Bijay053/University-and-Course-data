import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.test_scrape_prompt_assistant import assistant_postgres, store, prompt_thread, research_fakes
from app.models import ScrapedCourse, ScrapeRuntimeJob, University
from app.models.course import Course
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services import assistant_catalogue as catalogue, scrape_assistant as service
from app.routers.scrape_assistant import ConfirmBody, confirm_action, get_assistant


def config(seeds):
    return SimpleNamespace(discovery=SimpleNamespace(seed_urls=seeds, block_url_patterns=[], generic_search_api=None))


def test_identity_preserves_programme_query_but_removes_tracking():
    assert catalogue.identity("https://www.example.edu/course/?utm_source=x&id=2#fees") == "https://example.edu/course?id=2"
    assert catalogue.identity("https://example.edu/course?id=1") != catalogue.identity("https://example.edu/course?id=2")


@pytest.mark.asyncio
async def test_comparison_distinguishes_all_inventory_states_and_university(store):
    home = "https://www.example.edu"
    async with store() as db:
        db.add(University(id=2, name="Other university", country="UK", city="London"))
        await db.flush()
        db.add_all([
            Course(id=17, university_id=1, name="Approved", course_website=home+"/approved/", status="active", approval_status="approved"),
            Course(id=18, university_id=2, name="Other owner", course_website=home+"/new", status="active", approval_status="approved"),
            ScrapedCourse(id=3, university_id=1, scrape_job_id="source", status="rejected", course_name="Previous", course_website=home+"/old"),
        ])
        await db.commit()
    rows = await catalogue.compare_candidates([{"url": home+path, "title": path} for path in
                                               ["/approved?utm_source=x", "/course", "/old", "/new"]], 1, store)
    assert [row["status"] for row in rows] == ["published", "pending", "recorded", "missing"]
    assert rows[1]["reviewJobIds"] == ["source"]


@pytest.mark.asyncio
async def test_html_scan_is_bounded_partial_and_never_accepts_external_course_links(store, monkeypatch):
    home = "https://www.example.edu"
    calls = []
    async def fetch(url, **kwargs):
        calls.append(url)
        if "page2" in url:
            raise ValueError("blocked")
        if "/courses/bachelor-new" in url:
            return b"<main><h1>Bachelor of New Studies</h1><p>Duration: 3 years</p></main>"
        return (f"<html><title>Course catalogue</title><main>{'Official programme descriptions. '*8}"
                f'<a href="/courses/bachelor-new">Bachelor of New Studies</a>'
                f'<a href="/news/bachelor-of-community-services-launched">Bachelor of Community Services launched</a>'
                f'<a href="/news-and-opinion/bachelor-launch">Bachelor of Community Services</a>'
                f'<a href="https://evil.test/courses/master-other">Master of Other</a>'
                f'<a href="/courses/bachelor-new?utm_source=nav">Bachelor of New Studies</a>'
                f'<a href="/page2" rel="next">Next</a></main></html>').encode()
    monkeypatch.setattr(catalogue, "public_request", fetch)
    result = await catalogue.inspect_comparison(config([home]), home, 1, store)
    assert not result["complete"] and result["observed"] == 1
    assert result["counts"]["missing"] == 1
    assert result["courses"][0]["url"] == home+"/courses/bachelor-new"
    assert any(item["status"] == "unverified" for item in result["sources"])
    assert len(calls) <= catalogue.MAX_PAGES


@pytest.mark.asyncio
async def test_catalogue_reply_offers_selection_without_model_or_automatic_writes(store, monkeypatch):
    research_fakes(monkeypatch)
    url = "https://www.example.edu/courses/bachelor-new"
    async def compare(*args):
        return {"available": True, "complete": False, "observed": 1, "note": "Partial",
                "sources": [], "counts": {"published": 0, "pending": 0, "recorded": 0, "missing": 1},
                "courses": [{"url": url, "title": "Bachelor New", "status": "missing", "courseIds": [], "reviewJobIds": []}]}
    async def no_model(**kwargs):
        raise AssertionError("Deterministic comparison should not call the model")
    monkeypatch.setattr(service, "inspect_comparison", compare)
    monkeypatch.setattr(service, "chat_json", no_model)
    tid, turn = await prompt_thread(store, "Compare all courses and find missing ones")
    await service.investigate(tid, turn)
    async with store() as db:
        result = await get_assistant("source", db, {"id": 1})
        assert result["status"] == "ready"
        actions = result["messages"][-1]["actions"]
        assert [a["kind"] for a in actions] == ["recover_catalogue", "scan_catalogue"]
        action = actions[0]
        for selected in ([], ["https://evil.test/inject"]):
            with pytest.raises(HTTPException) as err:
                await confirm_action("source", action["id"], ConfirmBody(confirm=True, courseUrls=selected), db, {"id": 1})
            assert err.value.status_code == 422
        assert len((await db.execute(select(ScrapeRuntimeJob))).scalars().all()) == 1
    from app.routers import scrape
    received = []
    async def start(body, db):
        received.append(body)
        db.add(ScrapeRuntimeJob(runtime_job_id="recovered", university_id=1, status="queued", job_type="targeted"))
        await db.flush()
        return SimpleNamespace(job_id="recovered")
    monkeypatch.setattr(scrape, "start_scrape", start)
    async with store() as db:
        await confirm_action("source", action["id"], ConfirmBody(confirm=True, courseUrls=[url]), db, {"id": 1})
        await confirm_action("source", action["id"], ConfirmBody(confirm=True, courseUrls=[url]), db, {"id": 1})
    assert len(received) == 1 and received[0].course_urls == [url]
    assert received[0].retry_source_job_id == "source"


@pytest.mark.asyncio
async def test_selection_is_rechecked_before_recovery_and_full_scan_is_review_only(store, monkeypatch):
    tid, _ = await prompt_thread(store)
    ctx = await service.context("source")
    url = "https://www.example.edu/course"
    async with store() as db:
        thread = await db.get(ScrapeAssistantThread, tid)
        thread.status = "ready"
        thread.messages = [{"role": "assistant", "createdAt": service.now().isoformat(), "content": "Compare",
                            "actions": [
                                {"id": "recover", "kind": "recover_catalogue", "state": "proposed", "fields": [],
                                 "courseCount": 1, "_requestedUrls": [url], "_evidenceUrls": [url],
                                 "_configVersion": ctx["config_version"]},
                                {"id": "scan", "kind": "scan_catalogue", "state": "proposed", "fields": [],
                                 "courseCount": 0, "_configVersion": ctx["config_version"]},
                            ]}]
        await db.commit()
        with pytest.raises(HTTPException) as err:
            await confirm_action("source", "recover", ConfirmBody(confirm=True, courseUrls=[url]), db, {"id": 1})
        assert err.value.status_code == 409
    from app.routers import scrape
    calls = []
    async def start(body, db):
        calls.append(body)
        db.add(ScrapeRuntimeJob(runtime_job_id="full-review", university_id=1, status="queued", job_type="single"))
        await db.flush()
        return SimpleNamespace(job_id="full-review")
    monkeypatch.setattr(scrape, "start_scrape", start)
    async with store() as db:
        await confirm_action("source", "scan", ConfirmBody(confirm=True), db, {"id": 1})
    assert calls[0].full_catalogue_review_only and calls[0].force_discovery
    assert not calls[0].course_urls and calls[0].retry_source_job_id is None
