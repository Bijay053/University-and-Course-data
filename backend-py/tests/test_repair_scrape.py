"""B18: regression tests for the repair-scrape pipeline.

Three layers:

1. **Endpoint validation** — POST ``/api/scrape/repair/start`` rejects
   missing/invalid ``universityId``, refuses to queue when there are no
   missing-field courses, and 404s on a non-existent uni.
2. **Endpoint happy-path** — when a course needs repair, the endpoint
   creates a ``ScrapeRuntimeJob`` row of ``job_type='repair'`` whose
   ``request_payload['repair_targets']`` is the (course_id, url) list
   the worker will iterate over. Celery enqueue is monkey-patched so the
   test does not need a live broker.
3. **Worker direct-merge** — drive ``run_repair`` against a stubbed
   extractor that returns a fixed payload. Verify (a) only previously
   blank ``courses`` columns are filled, (b) ``english_requirements``
   rows are inserted only when the course had none, and (c) the
   ``ScrapeRuntimeJob`` row lands ``status='completed'`` with correct
   counters.

The B18 incident: ``/repair/start`` was a stub that always raised
HTTPException(400, "No saved scraping config"). This test guards the
whole pipeline so a future refactor cannot silently revert that.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.database import AsyncSessionLocal, engine
from app.main import app


@pytest.fixture(autouse=True)
async def _reset_engine_pool():
    """pytest-asyncio gives every test its own event loop, but
    SQLAlchemy keeps the asyncpg connection pool warm between tests.
    Once the loop the connections were opened on closes, a reuse
    raises ``RuntimeError: Event loop is closed``. Disposing the
    engine before each test forces a fresh pool bound to the current
    loop. Same dispose dance the Celery tasks already do for the
    asyncio.run() boundary."""
    await engine.dispose()
    yield
    await engine.dispose()


def _client() -> TestClient:
    return TestClient(app)


# ─── Endpoint validation ──────────────────────────────────────────────────


def test_repair_start_missing_university_id_400() -> None:
    r = _client().post("/api/scrape/repair/start", json={})
    assert r.status_code == 400, r.text
    assert "University ID" in r.json()["detail"]


def test_repair_start_unknown_university_404() -> None:
    # 999_999_999 is well past any seeded test row.
    r = _client().post(
        "/api/scrape/repair/start", json={"universityId": 999_999_999}
    )
    assert r.status_code == 404, r.text


def test_repair_start_non_integer_id_400() -> None:
    r = _client().post(
        "/api/scrape/repair/start", json={"universityId": "abc"}
    )
    assert r.status_code == 400, r.text


# ─── Endpoint + worker happy path ─────────────────────────────────────────


async def _seed_uni_with_missing_course() -> dict[str, int]:
    """Insert a uni + 2 courses: one with a URL and missing fields
    (will be queued), one with no URL but missing fields (rejected)."""
    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                text(
                    "INSERT INTO universities "
                    "(name, country, city, scrape_url) "
                    "VALUES (:n, 'Australia', 'Sydney', "
                    "        'https://example.test/repair') "
                    "RETURNING id"
                ),
                {"n": "Repair Test University"},
            )
        ).scalar_one()
        # course #1 — has URL, missing duration + english reqs → queueable
        c1 = (
            await db.execute(
                text(
                    "INSERT INTO courses "
                    "(university_id, name, status, course_website) "
                    "VALUES (:u, :n, 'active', :url) RETURNING id"
                ),
                {
                    "u": uni_id,
                    "n": "Bachelor of Repair",
                    "url": "https://example.test/repair/course-1",
                },
            )
        ).scalar_one()
        # course #2 — missing fields AND no course_website → rejected
        c2 = (
            await db.execute(
                text(
                    "INSERT INTO courses "
                    "(university_id, name, status) "
                    "VALUES (:u, :n, 'active') RETURNING id"
                ),
                {"u": uni_id, "n": "Diploma of No URL"},
            )
        ).scalar_one()
        await db.commit()
        return {"uni_id": uni_id, "c1": c1, "c2": c2}


async def _teardown(ids: dict[str, int]) -> None:
    async with AsyncSessionLocal() as db:
        await db.execute(
            text(
                "DELETE FROM scrape_runtime_jobs "
                "WHERE university_id = :u AND job_type = 'repair'"
            ),
            {"u": ids["uni_id"]},
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :i"), {"i": ids["uni_id"]}
        )
        await db.commit()


@pytest.mark.asyncio
async def test_repair_start_queues_job_and_rejects_urlless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The endpoint must create a ``job_type='repair'`` runtime job
    populated with one (course_id, url) target, and report the URL-less
    course in ``rejectedForeignIds``. Celery enqueue is stubbed."""
    ids = await _seed_uni_with_missing_course()

    # Stub Celery enqueue so the test never touches a real broker.
    enqueued: list[str] = []

    def _fake_delay(job_id: str) -> None:
        enqueued.append(job_id)

    from app.tasks import scrape_tasks

    monkeypatch.setattr(scrape_tasks.repair_university, "delay", _fake_delay)

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            r = await ac.post(
                "/api/scrape/repair/start",
                json={"universityId": ids["uni_id"]},
            )
        assert r.status_code == 200, r.text
        body: dict[str, Any] = r.json()

        assert body["count"] == 1, body
        assert body["jobId"] and body["jobId"].startswith("repair_"), body
        assert ids["c2"] in body["rejectedForeignIds"], body
        assert ids["c1"] not in body["rejectedForeignIds"], body
        assert enqueued == [body["jobId"]], (
            "repair_university.delay was not called exactly once"
        )

        # Verify the runtime job row was actually persisted with the
        # right shape — the worker reads ``repair_targets`` straight
        # off this column so we want the contract pinned down.
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    text(
                        "SELECT job_type, status, request_payload "
                        "FROM scrape_runtime_jobs "
                        "WHERE runtime_job_id = :j"
                    ),
                    {"j": body["jobId"]},
                )
            ).mappings().one()
        assert row["job_type"] == "repair"
        assert row["status"] == "queued"
        targets = row["request_payload"].get("repair_targets") or []
        assert len(targets) == 1
        assert targets[0]["course_id"] == ids["c1"]
        assert targets[0]["url"].startswith("https://example.test/repair/")
    finally:
        await _teardown(ids)


@pytest.mark.asyncio
async def test_repair_start_no_targets_returns_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When no course on the uni needs repair, the endpoint must reply
    with ``count=0`` and *not* enqueue anything. Prevents wasting a
    Celery slot on a no-op."""
    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                text(
                    "INSERT INTO universities "
                    "(name, country, city) VALUES (:n, 'Australia', 'Sydney') RETURNING id"
                ),
                {"n": "Repair Empty University"},
            )
        ).scalar_one()
        # A complete course — has duration, location, AND an english
        # requirement row, so the missing-fields query returns 0 rows.
        c = (
            await db.execute(
                text(
                    "INSERT INTO courses "
                    "(university_id, name, status, duration, course_location) "
                    "VALUES (:u, :n, 'active', 2, 'Sydney') RETURNING id"
                ),
                {"u": uni_id, "n": "Complete Course"},
            )
        ).scalar_one()
        await db.execute(
            text(
                "INSERT INTO english_requirements "
                "(course_id, test_type, overall) VALUES (:c, 'IELTS', 6.5)"
            ),
            {"c": c},
        )
        await db.commit()

    enqueued: list[str] = []
    from app.tasks import scrape_tasks

    monkeypatch.setattr(
        scrape_tasks.repair_university,
        "delay",
        lambda j: enqueued.append(j),
    )

    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ) as ac:
            r = await ac.post(
                "/api/scrape/repair/start", json={"universityId": uni_id}
            )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["count"] == 0
        assert body["jobId"] is None
        assert enqueued == []
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(
                text("DELETE FROM universities WHERE id = :i"), {"i": uni_id}
            )
            await db.commit()


# ─── Worker direct-merge behaviour ────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_repair_back_fills_blanks_only_and_inserts_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the worker against a stubbed extractor and verify:
       * ``duration`` (was NULL) is filled with the extracted value
       * ``course_location`` (had a curated value) is **not** overwritten
       * a fresh ``english_requirements`` row is inserted
       * ``ScrapeRuntimeJob`` lands ``completed`` with imported=1
    """
    import uuid as _uuid

    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                text(
                    "INSERT INTO universities (name, country, city) "
                    "VALUES (:n, 'Australia', 'Sydney') RETURNING id"
                ),
                {"n": "Repair Merge University"},
            )
        ).scalar_one()
        # Curated location must survive; duration is NULL so it should
        # be filled in by the repair pass.
        c = (
            await db.execute(
                text(
                    "INSERT INTO courses "
                    "(university_id, name, status, course_location, course_website) "
                    "VALUES (:u, :n, 'active', 'Curated City', :url) "
                    "RETURNING id"
                ),
                {
                    "u": uni_id,
                    "n": "Bachelor of Merge",
                    "url": "https://example.test/merge/course",
                },
            )
        ).scalar_one()
        job_id = f"repair_{_uuid.uuid4().hex[:12]}"
        await db.execute(
            text(
                "INSERT INTO scrape_runtime_jobs "
                "(runtime_job_id, university_id, university_name, url, "
                " job_type, status, request_payload) "
                "VALUES (:j, :u, :n, :url, 'repair', 'queued', "
                "        CAST(:pl AS jsonb))"
            ),
            {
                "j": job_id,
                "u": uni_id,
                "n": "Repair Merge University",
                "url": "https://example.test/",
                # JSONB cast happens server-side — pass JSON text in.
                "pl": (
                    '{"universityId": ' + str(uni_id) + ', '
                    '"repair_targets": [{"course_id": ' + str(c) + ', '
                    '"url": "https://example.test/merge/course"}]}'
                ),
            },
        )
        await db.commit()

    # Stub _extract_only at the repair module's import site so we never
    # hit the network. Returns just enough payload to exercise the
    # back-fill branches: a duration, a location (which must be
    # ignored because the curated value wins) and an IELTS overall
    # so an english_requirements row gets inserted.
    async def _fake_extract(link: dict, country: str | None, uni_pdf_data, emit=None) -> dict:  # noqa: ANN001
        return {
            "name": link["name"],
            "url": link["url"],
            "payload": {
                "duration": 3,
                "course_location": "Discovered City",  # must NOT overwrite
                "ielts_overall": 6.5,
                "ielts_listening": 6.0,
            },
            "evidence": [],
        }

    from app.services.scraper import repair as repair_mod

    monkeypatch.setattr(repair_mod, "_extract_only", _fake_extract)

    async with AsyncSessionLocal() as db:
        result = await repair_mod.run_repair(db, job_id)

    assert result["ok"] is True, result
    assert result["staged"] == 1, result
    assert result["discovered"] == 1, result

    async with AsyncSessionLocal() as db:
        course_row = (
            await db.execute(
                text(
                    "SELECT duration, course_location FROM courses WHERE id = :i"
                ),
                {"i": c},
            )
        ).mappings().one()
        assert course_row["duration"] == 3.0
        # Curated value must survive — confirms "fill empty only" semantics.
        assert course_row["course_location"] == "Curated City"

        eng_rows = (
            await db.execute(
                text(
                    "SELECT test_type, overall, listening "
                    "FROM english_requirements WHERE course_id = :i"
                ),
                {"i": c},
            )
        ).mappings().all()
        assert len(eng_rows) == 1, eng_rows
        assert eng_rows[0]["test_type"] == "IELTS"
        assert eng_rows[0]["overall"] == 6.5
        assert eng_rows[0]["listening"] == 6.0

        job_row = (
            await db.execute(
                text(
                    "SELECT status, imported, total_found, errors "
                    "FROM scrape_runtime_jobs WHERE runtime_job_id = :j"
                ),
                {"j": job_id},
            )
        ).mappings().one()
        assert job_row["status"] == "completed"
        assert job_row["imported"] == 1
        assert job_row["total_found"] == 1
        assert job_row["errors"] == 0

        # Cleanup.
        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
            {"j": job_id},
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :i"), {"i": uni_id}
        )
        await db.commit()


@pytest.mark.asyncio
async def test_run_repair_skips_when_course_has_existing_english(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the course already has any ``english_requirements`` row we
    must not insert a second one — even if the extracted payload says
    a different overall. Prevents the repair pass from doubling up
    English entries."""
    import uuid as _uuid

    async with AsyncSessionLocal() as db:
        uni_id = (
            await db.execute(
                text(
                    "INSERT INTO universities (name, country, city) "
                    "VALUES (:n, 'Australia', 'Sydney') RETURNING id"
                ),
                {"n": "Repair Eng-Existing University"},
            )
        ).scalar_one()
        c = (
            await db.execute(
                text(
                    "INSERT INTO courses (university_id, name, status, "
                    "course_website) VALUES "
                    "(:u, :n, 'active', :url) RETURNING id"
                ),
                {
                    "u": uni_id,
                    "n": "Bachelor of Existing English",
                    "url": "https://example.test/eng/course",
                },
            )
        ).scalar_one()
        # Pre-existing english requirement — this is the row that must
        # remain the *only* one after the repair pass.
        await db.execute(
            text(
                "INSERT INTO english_requirements "
                "(course_id, test_type, overall) VALUES (:c, 'IELTS', 7.0)"
            ),
            {"c": c},
        )
        # NB: the repair-missing query catches this course because
        # duration + location are both NULL — but english is present.
        job_id = f"repair_{_uuid.uuid4().hex[:12]}"
        await db.execute(
            text(
                "INSERT INTO scrape_runtime_jobs "
                "(runtime_job_id, university_id, url, job_type, "
                " status, request_payload) "
                "VALUES (:j, :u, '', 'repair', 'queued', CAST(:pl AS jsonb))"
            ),
            {
                "j": job_id,
                "u": uni_id,
                "pl": (
                    '{"repair_targets": [{"course_id": ' + str(c) + ', '
                    '"url": "https://example.test/eng/course"}]}'
                ),
            },
        )
        await db.commit()

    async def _fake_extract(link: dict, country, uni_pdf_data, emit=None) -> dict:  # noqa: ANN001
        return {
            "name": link["name"],
            "url": link["url"],
            "payload": {"duration": 4, "ielts_overall": 5.5},  # 5.5 must be ignored
            "evidence": [],
        }

    from app.services.scraper import repair as repair_mod

    monkeypatch.setattr(repair_mod, "_extract_only", _fake_extract)

    async with AsyncSessionLocal() as db:
        await repair_mod.run_repair(db, job_id)
        eng_rows = (
            await db.execute(
                text(
                    "SELECT test_type, overall FROM english_requirements "
                    "WHERE course_id = :i ORDER BY id"
                ),
                {"i": c},
            )
        ).mappings().all()
        # Still exactly one row, still 7.0 — the repair pass didn't add
        # the extracted 5.5.
        assert len(eng_rows) == 1, eng_rows
        assert eng_rows[0]["overall"] == 7.0
        # Duration was NULL → got filled.
        d = (
            await db.execute(
                text("SELECT duration FROM courses WHERE id = :i"), {"i": c}
            )
        ).scalar_one()
        assert d == 4.0

        await db.execute(
            text("DELETE FROM scrape_runtime_jobs WHERE runtime_job_id = :j"),
            {"j": job_id},
        )
        await db.execute(
            text("DELETE FROM universities WHERE id = :i"), {"i": uni_id}
        )
        await db.commit()
