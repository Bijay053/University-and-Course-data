"""Real loopback API processes; private socket-only PostgreSQL, never live DBs."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import signal
import socket
import subprocess
import sys
from types import SimpleNamespace
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.database import Base
from app.models import Course, ScrapedCourse, ScrapeRuntimeJob, University
from app.models.scrape_assistant import ScrapeAssistantThread
from tests.test_hsu_facts import hsu_postgres  # noqa: F401 -- owned private cluster

BACKEND = Path(__file__).resolve().parents[1]
ENDPOINT = "/api/scrape/jobs/source/assistant"
# Exercise the source-research lifecycle, not the immediate refusal path for
# negated field-edit instructions. The database write-denial triggers below
# still enforce read-only behavior independently of this wording.
PROMPT = {"prompt": "Only check the international fee at https://www.example.edu/course. "
                    "Read-only research; never publish any data."}


async def eventually(check, timeout=20):
    async with asyncio.timeout(timeout):
        while True:
            result = await check()
            if result:
                return result
            await asyncio.sleep(0.04)


class Processes:
    def __init__(self, store, directory):
        self.store, self.directory, self.items = store, directory, []

    async def start(self, label, block="none"):
        directory = self.directory / label
        directory.mkdir()
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        log = (directory / "api.log").open("wb")
        try:
            process = subprocess.Popen(
                [sys.executable, "-B", str(BACKEND / "tests/helpers/assistant_api_process.py"),
                 "--socket", self.store.socket, "--schema", self.store.schema,
                 "--fd", str(listener.fileno()), "--directory", str(directory),
                 "--label", label, "--block", block],
                cwd=BACKEND, pass_fds=(listener.fileno(),),
                stdout=log, stderr=subprocess.STDOUT,
            )
        finally:
            log.close()
            listener.close()
        client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False)
        item = SimpleNamespace(process=process, client=client, directory=directory, port=port)
        self.items.append(item)

        async def ready():
            assert process.poll() is None, (directory / "api.log").read_text()
            try:
                response = await client.get("/__ready")
            except httpx.TransportError:
                return False
            response.raise_for_status()
            assert response.json()["pid"] == process.pid
            assert response.json()["schema"] == self.store.schema
            return True

        await eventually(ready)
        return item

    async def stop(self, item, abrupt=False):
        item.process.send_signal(signal.SIGKILL if abrupt else signal.SIGTERM)
        code = await asyncio.to_thread(item.process.wait, timeout=15)
        # Uvicorn restores and re-raises SIGTERM after its lifespan finishes.
        # Exit status alone cannot establish whether cleanup happened.
        expected = {-signal.SIGKILL} if abrupt else {0, -signal.SIGTERM}
        assert code in expected, (item.directory / "api.log").read_text()
        if not abrupt:
            assert (item.directory / "cancelled").exists()
            assert (item.directory / "shutdown").read_text() == "runner drained"

    async def close(self):
        for item in self.items:
            if item.process.poll() is None:
                # Resume a deliberately suspended old owner before cleanup.
                item.process.send_signal(signal.SIGCONT)
                item.process.terminate()
                try:
                    await asyncio.to_thread(item.process.wait, timeout=12)
                except subprocess.TimeoutExpired:
                    item.process.kill()
                    await asyncio.to_thread(item.process.wait, timeout=5)
            await item.client.aclose()
            assert item.process.poll() is not None
            with socket.socket() as probe:
                assert probe.connect_ex(("127.0.0.1", item.port)) != 0, "Test listener leaked"


@pytest_asyncio.fixture
async def process_store(hsu_postgres, tmp_path):
    schema = "assistant_process_" + uuid.uuid4().hex
    admin = create_async_engine("postgresql+asyncpg://", connect_args=hsu_postgres, poolclass=NullPool)
    engine = create_async_engine(
        "postgresql+asyncpg://", poolclass=NullPool,
        connect_args={**hsu_postgres, "server_settings": {"search_path": schema}},
        execution_options={"schema_translate_map": {None: schema, "public": schema}},
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    processes = Processes(SimpleNamespace(socket=hsu_postgres["host"], schema=schema), tmp_path)
    try:
        async with admin.begin() as conn:
            await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            assert await conn.scalar(text("SELECT current_schema()")) == schema
            assert await conn.scalar(text(
                "SELECT count(*) FROM information_schema.tables WHERE table_schema=:s AND table_name='courses'"
            ), {"s": schema}) == 1
        async with factory() as db:
            db.add(University(id=1, name="Example University", country="UK", city="Example",
                              scrape_url="https://www.example.edu", scrape_config={}))
            await db.flush()
            db.add(ScrapeRuntimeJob(runtime_job_id="source", university_id=1, job_type="scrape",
                                   status="completed", total_found=1, imported=1))
            await db.flush()
            db.add(ScrapedCourse(id=1, university_id=1, scrape_job_id="source", status="pending",
                                course_name="BSc Music Technology", course_website="https://www.example.edu/course",
                                international_fee=9790))
            db.add(Course(id=1, university_id=1, name="Published sentinel", status="active",
                          course_website="https://www.example.edu/published"))
            await db.commit()
        async with engine.begin() as conn:
            # Sequence advances survive transaction rollback: even a swallowed,
            # rolled-back attempt to write outside chat will fail the test.
            await conn.execute(text(f'CREATE SEQUENCE "{schema}".forbidden_writes'))
            await conn.execute(text(f'''CREATE FUNCTION "{schema}".deny_course_write() RETURNS trigger
                LANGUAGE plpgsql AS $$ BEGIN
                PERFORM nextval('"{schema}".forbidden_writes');
                RAISE EXCEPTION 'Read-only research attempted a course-data write';
                END $$'''))
            for table in Base.metadata.sorted_tables:
                if table.name != "scrape_assistant_threads":
                    await conn.execute(text(f'''CREATE TRIGGER deny_research_write
                        BEFORE INSERT OR UPDATE OR DELETE ON "{schema}"."{table.name}"
                        FOR EACH STATEMENT EXECUTE FUNCTION "{schema}".deny_course_write()'''))
        yield SimpleNamespace(factory=factory, engine=engine, schema=schema, processes=processes)
    finally:
        await processes.close()
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            assert await conn.scalar(text("SELECT count(*) FROM pg_namespace WHERE nspname=:s"), {"s": schema}) == 0
        await admin.dispose()


async def thread_state(store):
    async with store.factory() as db:
        row = (await db.execute(select(ScrapeAssistantThread))).scalar_one()
        return SimpleNamespace(id=row.id, turn=row.turn_id, owner=row.run_id, status=row.status,
                               messages=row.messages, activity=row.activity)


async def age_owner(store, seconds):
    async with store.factory() as db:
        row = (await db.execute(select(ScrapeAssistantThread).with_for_update())).scalar_one()
        row.updated_at = datetime.now(timezone.utc) - timedelta(seconds=seconds)
        await db.commit()


async def assert_read_only(store):
    async with store.factory() as db:
        assert (await db.get(ScrapedCourse, 1)).international_fee == 9790
        assert (await db.get(ScrapedCourse, 1)).status == "pending"
        assert (await db.get(Course, 1)).name == "Published sentinel"
        assert (await db.get(Course, 1)).status == "active"
        assert len((await db.execute(select(ScrapeRuntimeJob))).scalars().all()) == 1
        assert not await db.scalar(text(f'SELECT is_called FROM "{store.schema}".forbidden_writes'))


async def await_answer(api):
    async def ready():
        response = await api.client.get(ENDPOINT)
        response.raise_for_status()
        value = response.json()
        assert value["status"] != "error", json.dumps(value)
        return value if value["status"] == "ready" else None
    return await eventually(ready)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["source", "answer"])
@pytest.mark.parametrize("abrupt", [False, True], ids=["graceful", "sigkill"])
async def test_reopen_in_separate_api_process_recovers_once(process_store, stage, abrupt):
    store = process_store
    old = await store.processes.start("old", block=stage)
    response = await old.client.post(ENDPOINT, json=PROMPT)
    assert response.status_code == 202, response.text

    async def entered():
        return (old.directory / "entered").exists()
    await eventually(entered)
    before = await thread_state(store)
    assert before.status == "researching" and before.owner
    await store.processes.stop(old, abrupt=abrupt)
    interrupted = await thread_state(store)
    if not abrupt:
        assert interrupted.status == "queued" and interrupted.owner is None
        assert not any(event["status"] == "running" for event in interrupted.activity)
    else:
        assert interrupted.status == "researching" and interrupted.owner == before.owner

    new = await store.processes.start("new")
    if abrupt:
        # A real second process must not steal a fresh claim merely because its
        # owner died. Advance only the isolated thread's durable timestamp.
        await age_owner(store, 90)
        fresh = await new.client.get(ENDPOINT)
        assert fresh.status_code == 200 and fresh.json()["status"] == "researching"
        assert (await thread_state(store)).owner == before.owner
        await age_owner(store, 101)
    result = await await_answer(new)
    assert result["threadId"] == before.id
    final = await thread_state(store)
    answers = [m for m in final.messages if m["role"] == "assistant"]
    assert len(answers) == 1 and "from new" in answers[0]["content"]
    assert final.turn == before.turn
    assert final.owner != before.owner
    assert not any(event["status"] == "running" for event in final.activity)
    rejected = await new.client.post("/__late_activity", json={
        "thread": before.id, "turn": before.turn, "owner": before.owner,
    })
    assert rejected.status_code == 409
    for _ in range(3):
        assert (await new.client.get(ENDPOINT)).json()["status"] == "ready"
    assert len([m for m in (await thread_state(store)).messages if m["role"] == "assistant"]) == 1
    assert all(a["state"] == "proposed" for a in answers[0].get("actions", []))
    await assert_read_only(store)


@pytest.mark.asyncio
async def test_suspended_old_api_cannot_append_events_or_answer_after_takeover(process_store):
    store = process_store
    old = await store.processes.start("old", block="answer")
    assert (await old.client.post(ENDPOINT, json=PROMPT)).status_code == 202

    async def entered():
        return (old.directory / "entered").exists()
    await eventually(entered)
    before = await thread_state(store)
    old.process.send_signal(signal.SIGSTOP)
    try:
        new = await store.processes.start("new", block="source")
        await age_owner(store, 101)
        assert (await new.client.get(ENDPOINT)).status_code == 200

        async def reclaimed():
            current = await thread_state(store)
            return current if current.owner not in (None, before.owner) else None
        current = await eventually(reclaimed)
        assert current.status == "researching"
        rejected = await new.client.post("/__late_activity", json={
            "thread": before.id, "turn": before.turn, "owner": before.owner,
        })
        assert rejected.status_code == 409
        # The real original coroutine continues after its final model call.
        # No monkeypatch bypasses its final answer's database ownership fence.
        (old.directory / "release").write_text("resume final model result")
        old.process.send_signal(signal.SIGCONT)

        async def drained():
            return (await old.client.get("/__ready")).json()["tasks"] == 0
        await eventually(drained)
        late = await thread_state(store)
        assert late.status == "researching" and late.owner == current.owner
        assert not any(m["role"] == "assistant" for m in late.messages)
        assert not any(e["label"] == "Late old-owner event" for e in late.activity)
        (new.directory / "release").write_text("complete surviving attempt")
        await await_answer(new)
        answers = [m for m in (await thread_state(store)).messages if m["role"] == "assistant"]
        assert len(answers) == 1 and "from new" in answers[0]["content"]
        await assert_read_only(store)
    finally:
        if old.process.poll() is None:
            old.process.send_signal(signal.SIGCONT)
