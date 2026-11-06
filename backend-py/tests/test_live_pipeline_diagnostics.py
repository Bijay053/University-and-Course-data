"""Surrey's zero-progress report must stay in infrastructure diagnostics."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from app.services import live_scrape_diagnostics as live
from app.services.scraper.extractors.uk_course_facts import expand_links


@pytest.mark.parametrize("prompt",[
    "why taking time fix it",
    "already been 3 minutes still 0 extraction",
    "why is this taking so long? Fix it",
    "scrape stuck at 0/256",
    "zero progress with a healthy heartbeat",
])
def test_actual_infrastructure_phrases_are_recognised(prompt):
    assert live.requested(prompt)


def test_stall_threshold_is_configurable_with_safe_bounds():
    from app.services.scraper.config.schema import ExtractionConfig
    from pydantic import ValidationError
    assert ExtractionConfig(live_stall_seconds=90).live_stall_seconds==90
    for value in (0,29,1801):
        with pytest.raises(ValidationError):
            ExtractionConfig(live_stall_seconds=value)


def test_existing_old_chat_followups_keep_pipeline_context_but_new_fields_reset_it():
    messages=[
        {"role":"user","content":"already been 3 minutes still 0 extraction"},
        {"role":"assistant","content":"Old reply without a diagnostics marker"},
        {"role":"user","content":"is not this bug?"},
        {"role":"assistant","content":"Old reply"},
        {"role":"user","content":"do it and fix this"},
    ]
    assert live.followup_requested("do it and fix this",messages,"surrey")
    marked=[{"role":"assistant","liveDiagnostics":{"jobId":"surrey"}},{"role":"user","content":"fix IELTS"}]
    assert not live.followup_requested("fix IELTS",marked,"surrey")
    assert not live.followup_requested("do it and fix this",marked+[{"role":"user","content":"fix this"}],"surrey")

def test_healthy_heartbeat_cannot_hide_zero_course_progress():
    now=datetime.now(timezone.utc)
    job=SimpleNamespace(runtime_job_id="surrey",status="running",current=0,total_found=256,
        started_at=now-timedelta(minutes=5),heartbeat_at=now-timedelta(seconds=11))
    result=live.summarize(job,[],now,stall_seconds=120)
    issue=next(i for i in result["issues"] if i["code"]=="no_extraction_progress")
    assert "healthy heartbeat does not prove" in issue["detail"]
    assert result["timeouts"]==0 and result["processed"]==0


def test_last_durable_completion_detects_stalls_beyond_the_log_window():
    now=datetime.now(timezone.utc)
    job=SimpleNamespace(runtime_job_id="surrey",status="running",current=1,total_found=256,
        started_at=now-timedelta(minutes=20),heartbeat_at=now)
    result=live.summarize(job,[],now,last_completion_at=now-timedelta(minutes=4))
    assert result["noProgressSeconds"]==240
    assert any(i["code"]=="no_extraction_progress" for i in result["issues"])
    job.current=256
    assert not any(i["code"]=="no_extraction_progress" for i in live.summarize(
        job,[],now,last_completion_at=now-timedelta(minutes=4))["issues"])


def test_preparation_is_visible_without_faking_completed_extractions():
    now=datetime.now(timezone.utc)
    job=SimpleNamespace(runtime_job_id="surrey",status="running",current=0,total_found=256,
        started_at=now-timedelta(minutes=5),heartbeat_at=now)
    result=live.summarize(job,[
        ("status",{"kind":"course_prepare_start","url":"https://surrey.ac.uk/a","total":256},now-timedelta(seconds=10)),
        ("status",{"kind":"course_prepare_done","url":"https://surrey.ac.uk/a","total":256},now-timedelta(seconds=5)),
        ("status",{"kind":"course_prepare_start","url":"https://surrey.ac.uk/b","total":256},now-timedelta(seconds=4)),
    ],now)
    assert result["processed"]==0 and result["preparation"]["completed"]==1
    assert result["preparation"]["active"]==[{"url":"https://surrey.ac.uk/b","elapsedSeconds":4}]
    assert {i["code"] for i in result["issues"]}=={"course_preparation"}


@pytest.mark.asyncio
async def test_surrey_preparation_is_bounded_deduplicated_and_observable():
    active=peak=0
    calls=[];events=[]
    async def fetch(url):
        nonlocal active,peak
        calls.append(url);active+=1;peak=max(peak,active)
        await asyncio.sleep(.01)
        active-=1
        return "<h1>Course</h1>"
    async def observe(**event):
        events.append(event)
    links=[{"url":f"https://www.surrey.ac.uk/undergraduate/{i}"} for i in range(4)]
    result=await expand_links([*links,links[0]],10,fetch,
        parallel=2,on_progress=observe)
    assert len(calls)==4 and peak==2
    assert len(result)==5 and all(r["_surrey_html"] for r in result)
    assert len([e for e in events if e["kind"]=="course_prepare_done"])==4
    assert events[-1]["completed"]==4


@pytest.mark.asyncio
async def test_option_parsing_yields_to_worker_heartbeat_and_reuses_duplicate_sources(monkeypatch):
    import threading
    from app.services.scraper.extractors import uk_course_facts as facts
    loop=asyncio.get_running_loop()
    started=asyncio.Event();release=threading.Event();calls=[]
    def parse(html,url):
        calls.append(url)
        loop.call_soon_threadsafe(started.set)
        assert release.wait(2),"Parsing blocked the event loop instead of yielding to heartbeat/control tasks"
        return []
    monkeypatch.setattr(facts,"surrey_options",parse)
    fetch=AsyncMock(return_value="<h1>Course</h1>")
    link={"url":"https://www.surrey.ac.uk/undergraduate/accounting"}
    task=asyncio.create_task(facts.expand_links([link,link],10,fetch))
    try:
        await asyncio.wait_for(started.wait(),1)
        release.set()
        result=await task
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task,return_exceptions=True)
    assert len(result)==2 and len(calls)==1
    fetch.assert_awaited_once()


def test_fetched_pages_wait_for_parsing_without_inflating_completed_counts():
    now=datetime.now(timezone.utc)
    job=SimpleNamespace(runtime_job_id="surrey",status="running",current=0,total_found=256,
        started_at=now-timedelta(minutes=5),heartbeat_at=now)
    result=live.summarize(job,[
        ("status",{"kind":"course_prepare_start","url":"https://surrey.ac.uk/a","total":256},now-timedelta(seconds=10)),
        ("status",{"kind":"course_source_fetched","url":"https://surrey.ac.uk/a"},now-timedelta(seconds=5)),
    ],now)
    assert result["preparation"]["completed"]==0
    assert result["preparation"]["waitingForParse"]==1
    assert result["processed"]==0
    assert not any(i["code"]=="no_extraction_progress" for i in result["issues"])


@pytest.mark.asyncio
async def test_preparation_reports_failure_and_cancels_siblings():
    events=[];cancelled=asyncio.Event()
    async def fetch(url):
        if url.endswith("/a"):
            await asyncio.sleep(.01)
            raise TimeoutError("source timed out")
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.set()
    async def observe(**event):
        events.append(event)
    with pytest.raises(TimeoutError):
        await expand_links([{"url":"https://www.surrey.ac.uk/a"},{"url":"https://www.surrey.ac.uk/b"}],
            10,fetch,parallel=2,on_progress=observe)
    assert cancelled.is_set()
    assert any(e["kind"]=="course_prepare_error" and e["error"]=="TimeoutError" for e in events)


@pytest.mark.asyncio
@pytest.mark.parametrize("prompt",["is not this bug?","do it and fix this"])
async def test_followup_stays_in_live_diagnostics_not_course_research(monkeypatch,prompt):
    now=datetime.now(timezone.utc)
    job=SimpleNamespace(runtime_job_id="surrey",status="running",current=0,total_found=256,
        started_at=now-timedelta(minutes=5),heartbeat_at=now)
    thread=SimpleNamespace(job_id="surrey",messages=[
        {"role":"user","content":"why taking time fix it"},
        {"role":"assistant","content":"Live status","liveDiagnostics":{"jobId":"surrey"}},
        {"role":"user","content":prompt},
    ])
    db=SimpleNamespace(get=AsyncMock(return_value=thread))
    @asynccontextmanager
    async def session():
        yield db
    service=SimpleNamespace(AsyncSessionLocal=session,save_research_reply=AsyncMock(),now=lambda:now)
    inspect=AsyncMock(return_value=live.summarize(job,[],now))
    monkeypatch.setattr(live,"inspect",inspect)
    assert await live.handle(service,"thread","turn","owner",{"job":{"id":"surrey"}},prompt)
    inspect.assert_awaited_once_with(db,"surrey")
    reply=service.save_research_reply.await_args.args[3]
    assert reply["sources"]==[] and reply["actions"]==[]
    assert "No recent completed extraction" in reply["content"]


@pytest.mark.asyncio
async def test_failed_diagnostics_does_not_request_an_official_course_url(monkeypatch):
    @asynccontextmanager
    async def session():
        yield object()
    service=SimpleNamespace(AsyncSessionLocal=session,save_research_reply=AsyncMock(),
        now=lambda:datetime.now(timezone.utc))
    monkeypatch.setattr(live,"inspect",AsyncMock(side_effect=ValueError("unavailable")))
    assert await live.handle(service,"thread","turn","owner",{"job":{"id":"surrey"}},"why taking time fix it")
    reply=service.save_research_reply.await_args.args[3]
    assert "an official course URL is not needed" in reply["content"]
    assert reply["liveDiagnostics"]["unavailable"]


@pytest.mark.asyncio
async def test_inspector_uses_provider_threshold_without_creating_configuration(monkeypatch):
    from unittest.mock import Mock,MagicMock
    from app.services.scraper.config import loader
    now=datetime.now(timezone.utc)
    job=SimpleNamespace(runtime_job_id="surrey",university_id=102,status="running",current=1,total_found=256,
        started_at=now-timedelta(minutes=20),heartbeat_at=now)
    university=SimpleNamespace(id=102,name="University of Surrey",slug="surrey",
        scrape_url="https://www.surrey.ac.uk",scrape_config={})
    rows=MagicMock();rows.all.return_value=[]
    db=SimpleNamespace(get=AsyncMock(side_effect=[job,university,None]),
        execute=AsyncMock(return_value=rows),scalar=AsyncMock(side_effect=[0,now-timedelta(seconds=100)]))
    load=Mock(return_value=SimpleNamespace(extraction=SimpleNamespace(live_stall_seconds=90)))
    monkeypatch.setattr(loader,"load_uni_config",load)
    monkeypatch.setattr(live,"worker_observation",AsyncMock(return_value={
        "workerStatus":"unavailable","queueStatus":"unavailable"}))
    result=await live.inspect(db,"surrey")
    assert result["stallThresholdSeconds"]==90
    assert {"no_extraction_progress","worker_visibility"}<={i["code"] for i in result["issues"]}
    assert load.call_args.kwargs["create_missing_stub"] is False
