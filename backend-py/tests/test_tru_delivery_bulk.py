import pytest
from sqlalchemy import select
from app.services import assistant_online_exclusion as online
from app.services import assistant_eligibility as eligibility
from app.services import scrape_assistant as service
from app.models import ScrapedCourse
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services.tru_delivery_source import materialize_delivery
from tests.test_assistant_online_exclusion_persistence import setup, store, assistant_postgres

URL = "https://www.tru.ca/programs/catalogue/bachelor-arts.html"

def page(value):
    return f"""<main class="program-transition-main"><h1>Bachelor of Arts</h1></main>
    <aside class="highlights transition-highlights"><div class="side-cell-fullwidth"><h4>Program Details</h4><dl>
    <dt>Credential</dt><dd>Bachelor of Arts</dd><dt>Delivery</dt><dd>{value}</dd>
    </dl></div></aside><nav>Online courses Vancouver</nav>"""

@pytest.mark.parametrize("value,state,location", [
    ("Kamloops", "on_campus", "Kamloops"),
    ("Williams Lake", "on_campus", "Williams Lake"),
    ("Online", "checked", None),
    ("Online and Kamloops", "mixed_mode", None),
    ("Contact us", "unverified", None),
])
def test_course_owned_sibling_delivery(value, state, location):
    proof = online.evidence(materialize_delivery(page(value), URL), URL)
    assert proof["state"] == state
    assert proof.get("courseLocation") == location

def test_duplicate_panels_not_promoted():
    assert online.evidence(page("Online") + page("Kamloops"), URL)["state"] == "unverified"

def test_different_award_not_promoted():
    html = page("Online").replace("<dd>Bachelor of Arts</dd>", "<dd>Bachelor of Science</dd>")
    assert online.evidence(materialize_delivery(html, URL), URL)["state"] == "unverified"

def test_other_provider_cannot_use_tru_sidebar_contract():
    url = URL.replace("www.tru.ca", "www.example.edu")
    assert online.evidence(materialize_delivery(page("Online"), url), url)["state"] == "unverified"

@pytest.mark.asyncio
async def test_combined_update_cross_review_preserves_fee_and_published_rows(store):
    ctx = await setup(store, count=3, prompt="update delivery and location and reject all online courses")
    ctx["_providerWide"] = True
    async with store() as db:
        row = await db.get(ScrapedCourse, 2)
        row.scrape_job_id = "another-review"
        published = await db.get(ScrapedCourse, 3)
        published.status = "approved"
        ctx["versions"]["3"] = service.row_version(published)
        ctx["versions"]["2"] = service.row_version(row)
        thread = await db.get(ScrapeAssistantThread, "eligibility")
        sources = {r["url"]: {"verified": True, "_onlineExclusionSchema": online.SCHEMA,
            "_fullText": "Delivery Kamloops", "_onlineExclusion": {
                "state": "on_campus", "studyMode": "On Campus",
                "courseLocation": "Kamloops", "deliveryQuote": "Delivery Kamloops"}}
            for r in ctx["samples"]}
        thread.messages = [dict(thread.messages[0], _sourceChecks=sources)]
        await db.commit()
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"],
                              {r["id"]: (None, "on_campus") for r in ctx["samples"]})
    async with store() as db:
        rows = (await db.execute(select(ScrapedCourse).order_by(ScrapedCourse.id))).scalars().all()
        assert rows[0].international_fee == 9790
        assert rows[0].study_mode == rows[1].study_mode == "On Campus"
        assert rows[0].course_location == rows[1].course_location == "Kamloops"
        assert rows[2].course_location != "Kamloops"
        reply = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]
        assert "updated on 2 records" in reply["content"]

@pytest.mark.asyncio
async def test_unverified_is_not_completed(store):
    ctx = await setup(store, count=1)
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"], {1: (None, "unverified")})
    async with store() as db:
        reply = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]
        assert reply["actions"][0]["state"] == "needs_review"

@pytest.mark.asyncio
async def test_opted_in_owner_runs_bulk_without_click(store, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "scrape_assistant_auto_pending_user_ids", "77")
    ctx = await setup(store, count=12)
    await eligibility.persist(service, "eligibility", "turn", "owner", ctx, ctx["samples"],
                              {i: ("Delivery Online", "checked") for i in range(1, 13)})
    async with store() as db:
        rows = (await db.execute(select(ScrapedCourse))).scalars().all()
        assert all(r.status == "rejected" for r in rows)
        action = (await db.get(ScrapeAssistantThread, "eligibility")).messages[-1]["actions"][0]
        assert action["state"] == "completed" and not action["requiresConfirmation"]

@pytest.mark.asyncio
async def test_all_courses_loads_provider_scope(store, monkeypatch):
    ctx = await setup(store, count=1)
    ctx["job"] = {"id": "source"}
    called = []
    async def load(job_id, *, provider_wide=False):
        called.append((job_id, provider_wide))
        return ctx
    async def check(*args):
        return None, "unverified"
    monkeypatch.setattr(service, "context", load)
    monkeypatch.setattr(online, "check", check)
    await eligibility.run(service, "eligibility", "turn", "owner", ctx,
                          "update delivery and reject all courses online")
    assert called == [("source", True)]
