"""Unit and opt-in isolated PostgreSQL commit/rollback adoption canaries."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import adopt_law_reviewed23 as tool


def fixture():
    snapshot = {t: [] for t in (
        "courses", "scraped_courses", "fees", "course_offerings", "course_id_aliases",
    )}
    for sid, cid in tool.PAIRS:
        url = f"https://www.law.ac.uk/study/postgraduate/law/canary-{cid}/"
        award, campus = f"Canary award {cid}", "London"
        option = dict(amount=20000, currency="GBP", year=2026, period="Annual",
                      campus=campus, study_variant="Standard", source_url=url,
                      snippet="International Students | Canary isolated test")
        fee = dict(international_fee=20000, currency="GBP", fee_year=2026, fee_term="Annual")
        scope = dict(key=hashlib.sha256(campus.casefold().encode()).hexdigest()[:20],
                     original_name=award, source_url=url, locations=[campus])
        metadata = {
            "international_fee": "fee.ulaw_course_authority",
            "fee_variants": dict(status="uniform", selected=[option], options=[option], **fee),
            "campus_fee_scope": scope,
        }
        row = dict(id=sid, university_id=92, scrape_job_id="job_435ad2d18e35",
                   course_id=None, status="pending", course_name=f"{award} — {campus}",
                   course_location=campus, course_website=url, degree_level="Master",
                   study_mode="On Campus", fee_scope_key=scope["key"],
                   extraction_method=metadata, **fee)
        snapshot["scraped_courses"].extend([
            row, {**deepcopy(row), "id": sid + 100000, "scrape_job_id": "historical",
                  "course_id": cid, "status": "approved"},
        ])
        snapshot["courses"].append(dict(
            id=cid, university_id=92, name=row["course_name"], offering_identity=None,
            course_website=url, course_location=campus, study_mode="On Campus",
            degree_level="Master's", status="active", approval_status="approved",
        ))
        snapshot["fees"].append(dict(id=cid, course_id=cid, **fee))
    return snapshot


def test_exact_mapping_and_plan():
    assert tool.JOB == "job_435ad2d18e35"
    assert len(tool.PAIRS) == len(set(tool.PAIRS)) == 23
    assert tool.PAIRS[0] == (42727, 9723)
    assert tool.PAIRS[-1] == (42961, 9660)
    data = fixture()
    before = deepcopy(data)
    assert len(tool.plan(data)) == 23
    assert data == before
    assert len(tool.file_hash()) == 64


@pytest.mark.parametrize("mutation", [
    lambda s: s["courses"][0].update(name="Different award"),
    lambda s: s["courses"][0].update(course_location="Birmingham"),
    lambda s: s["courses"][0].update(study_mode="Online"),
    lambda s: s["courses"][0].update(degree_level="Bachelor"),
    lambda s: s["fees"][0].update(international_fee=1),
    lambda s: s["scraped_courses"][0].update(scrape_job_id="different"),
    lambda s: s["scraped_courses"][0].update(status="approved"),
    lambda s: s["scraped_courses"][1].update(extraction_method={}),
    lambda s: s["scraped_courses"][1].update(course_id=None),
    lambda s: s["course_id_aliases"].append(dict(alias_course_id=9723, canonical_course_id=1)),
    lambda s: s["course_id_aliases"].append(dict(alias_course_id=1, canonical_course_id=9723)),
    lambda s: s["course_offerings"].append(dict(course_id=9723)),
    lambda s: s["courses"].append({**s["courses"][0], "id": 999999}),
    lambda s: s["scraped_courses"].append({**s["scraped_courses"][0], "id": 999999}),
])
def test_refuses_changed_or_ambiguous_state(mutation):
    data = fixture()
    mutation(data)
    with pytest.raises(ValueError):
        tool.plan(data)


@pytest.mark.parametrize("index", [0, 1])
@pytest.mark.parametrize("field,value", [
    ("campus", "Birmingham"),
    ("campus", "Outside London"),
    ("source_url", "https://www.law.ac.uk/study/postgraduate/law/canary-9723"),
    ("study_variant", "Professional Practice"),
])
def test_incoming_and_historical_options_cannot_lie_about_scope(index, field, value):
    data = fixture()
    authority = data["scraped_courses"][index]["extraction_method"]["fee_variants"]
    for key in ("selected", "options"):
        authority[key][0][field] = value
    with pytest.raises(tool.Refused):
        tool.plan(data)


def test_vetted_regional_labels_only_and_unscoped_history():
    data = fixture()
    for row in data["scraped_courses"]:
        for option in row["extraction_method"]["fee_variants"]["selected"]:
            option["campus"] = "All campuses"
    # Legacy historical witnesses may be unscoped but must still independently
    # attest the exact campus tuple with validated option labels/source URLs.
    data["scraped_courses"][1]["extraction_method"].pop("campus_fee_scope")
    assert len(tool.plan(data)) == 23
    data["scraped_courses"][1]["extraction_method"]["fee_variants"]["selected"][0]["campus"] = "Birmingham"
    with pytest.raises(tool.Refused, match="Selected fee campus"):
        tool.plan(data)


def test_postgres_commit_rollback_and_idempotency():
    url = os.environ.get("REVIEWED23_CANARY_DATABASE_URL")
    if not url:
        pytest.skip("Set REVIEWED23_CANARY_DATABASE_URL to an isolated canary database")
    parsed = make_url(url)
    assert parsed.database.startswith("canary_") or parsed.database.endswith("_test")
    assert parsed.host in ("localhost", "127.0.0.1", None), "Local isolated PostgreSQL only"

    async def run():
        # Reuse existing canary DDL, never application schema or configured URL.
        from test_course_duplicate_postgres_integration import DDL
        schema = "reviewed23_" + uuid.uuid4().hex
        root = create_async_engine(url)
        engine = create_async_engine(url, isolation_level="SERIALIZABLE",
                                     connect_args={"server_settings": {"search_path": schema}})
        try:
            async with root.begin() as conn:
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            async with engine.begin() as conn:
                for statement in DDL:
                    statement = statement.replace(
                        "UNIQUE (course_id, location_key)",
                        "CONSTRAINT uq_course_offering_location UNIQUE (course_id, location_key)",
                    )
                    await conn.execute(text(statement))
                await conn.execute(text("INSERT INTO universities (id,name) VALUES (92,'Canary')"))
                await conn.execute(text("""
                    INSERT INTO course_identity_reconciliation_audit
                    (audit_id, manifest_sha256, approval_revision, actor, event_type,
                     entity_table, entity_key, row_payload)
                    VALUES (:id, :sha, 'prior61-canary', 'prior-reviewer', 'run',
                            'prior61', '{}', '{"preserve":"exact prior audit"}')
                """), {"id": str(uuid.uuid4()), "sha": "0" * 64})
                data = fixture()
                for table in ("courses", "scraped_courses", "fees"):
                    for row in data[table]:
                        columns = ", ".join(row)
                        values = ", ".join(
                            f"CAST(:{k} AS jsonb)" if isinstance(v, dict) else f":{k}"
                            for k, v in row.items()
                        )
                        await conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({values})"),
                                           {k: json.dumps(v) if isinstance(v, dict) else v for k, v in row.items()})

            async def call(**kwargs):
                async with AsyncSession(engine) as db, db.begin():
                    return await tool.execute(db, **kwargs)

            preview = await call()
            assert preview["count"] == 23
            async with AsyncSession(engine) as db, db.begin():
                assert await tool.execute(db) == preview
                assert (await db.execute(text("SHOW transaction_read_only"))).scalar() == "on"
                assert not (await db.execute(text("""
                    SELECT 1 FROM pg_locks WHERE pid=pg_backend_pid()
                    AND (locktype='advisory' OR mode IN (
                        'RowShareLock','RowExclusiveLock','ShareRowExclusiveLock'))
                """))).all()
                with pytest.raises(DBAPIError) as readonly_error:
                    await db.execute(text("UPDATE courses SET name=name WHERE id=9723"))
                assert readonly_error.value.orig.sqlstate == "25006"
                await db.rollback()
            args = dict(apply=True, expected_file=tool.file_hash(),
                        expected_snapshot=preview["snapshot_sha256"],
                        confirm=tool.CONFIRM, actor="isolated-canary")
            with pytest.raises(tool.Refused):
                await call(**{**args, "expected_file": "0" * 64})
            with pytest.raises(tool.Refused):
                await call(**{**args, "expected_snapshot": "0" * 64})

            # A writer already in flight must finish BEFORE adoption establishes
            # its SERIALIZABLE snapshot. Its newly committed collision is seen.
            async with engine.connect() as writer:
                transaction = await writer.begin()
                await writer.execute(text("""
                    INSERT INTO courses (id, university_id, name, course_website,
                        status, approval_status)
                    SELECT 999999, university_id, name, course_website, status,
                        approval_status FROM courses WHERE id=9723
                """))
                adoption = asyncio.create_task(call(**args))
                try:
                    async def barrier_is_waiting():
                        for _ in range(200):
                            async with engine.connect() as observer:
                                waiting = (await observer.execute(text("""
                                    SELECT 1 FROM pg_locks
                                    WHERE relation = 'courses'::regclass
                                      AND mode='ShareRowExclusiveLock' AND NOT granted
                                """))).first()
                            if waiting:
                                return
                            await asyncio.sleep(.01)
                        raise AssertionError("Adoption did not wait for writer barrier")
                    await asyncio.wait_for(barrier_is_waiting(), timeout=4)
                    await transaction.commit()
                    with pytest.raises(tool.Refused, match="Published collision"):
                        await adoption
                finally:
                    if transaction.is_active:
                        await transaction.rollback()
                    if not adoption.done():
                        adoption.cancel()
                        await asyncio.gather(adoption, return_exceptions=True)
            async with engine.begin() as conn:
                await conn.execute(text("DELETE FROM courses WHERE id=999999"))

            with pytest.raises(RuntimeError, match="force rollback"):
                async with AsyncSession(engine) as db, db.begin():
                    assert (await tool.execute(db, **args))["status"] == "applied"
                    assert (await db.execute(text("SHOW lock_timeout"))).scalar() == "5s"
                    # Writers ignoring the advisory lock cannot insert phantom
                    # courses, staged evidence or aliases during adoption.
                    insertions = [
                        """INSERT INTO courses (id,university_id,name,status,approval_status)
                           VALUES (999999,92,'Phantom','active','approved')""",
                        """INSERT INTO scraped_courses (id,university_id,scrape_job_id,
                           status,extraction_method) VALUES
                           (999999,92,'job_phantom','pending','{}')""",
                        """INSERT INTO course_id_aliases (alias_course_id,
                           canonical_course_id,reason,created_by)
                           VALUES (9723,9722,'phantom','canary')""",
                    ]
                    for statement in insertions:
                        with pytest.raises(DBAPIError) as blocked:
                            async with engine.begin() as other:
                                await other.execute(text("SET LOCAL lock_timeout = '100ms'"))
                                await other.execute(text(statement))
                        assert blocked.value.orig.sqlstate == "55P03"
                    raise RuntimeError("force rollback")
            assert await call() == preview
            assert (await call(**args))["status"] == "applied"
            assert (await call(**args))["status"] == "already_applied"
            async with engine.begin() as conn:
                assert (await conn.execute(text("SELECT count(*) FROM course_offerings"))).scalar() == 23
                assert (await conn.execute(text("SELECT count(*) FROM course_id_aliases"))).scalar() == 0
                assert (await conn.execute(text("SELECT count(*) FROM courses"))).scalar() == 23
                assert (await conn.execute(text("SELECT count(*) FROM course_identity_reconciliation_audit"))).scalar() == 4
                assert (await conn.execute(text("""
                    SELECT row_payload FROM course_identity_reconciliation_audit
                    WHERE approval_revision='prior61-canary'
                """))).scalar() == {"preserve": "exact prior audit"}
                await conn.execute(text("UPDATE fees SET international_fee=1 WHERE course_id=9723"))
            with pytest.raises(tool.Refused, match="state changed"):
                await call(**args)
        finally:
            await engine.dispose()
            async with root.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await root.dispose()

    asyncio.run(run())