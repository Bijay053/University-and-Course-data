"""Opt-in end-to-end Task #629 test against an isolated PostgreSQL schema.

Set TASK629_CANARY_DATABASE_URL to a dedicated ``canary_*`` or ``*_test``
PostgreSQL database. The test creates and drops only a uniquely named schema;
it never accepts the configured application database implicitly.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine


SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))
MAPPING_PATH = SCRIPT_DIR / "approved_law_legacy_mapping.json"


def _load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPT_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


preview = _load_script("course_duplicate_preview")
exporter = _load_script("course_duplicate_snapshot_export")
apply_tool = _load_script("course_duplicate_apply")


DDL = (
    """
    CREATE TABLE alembic_version (version_num text PRIMARY KEY)
    """,
    """
    CREATE TABLE universities (
        id integer PRIMARY KEY, name text NOT NULL, city text, country text,
        website text, logo_url text, featured boolean, featured_priority integer
    )
    """,
    """
    CREATE TABLE courses (
        id integer PRIMARY KEY, university_id integer NOT NULL REFERENCES universities(id),
        name text NOT NULL, course_website text, degree_level text, study_mode text,
        course_location text, status text NOT NULL, approval_status text NOT NULL,
        offering_identity text, category text, sub_category text, duration double precision,
        duration_term text, last_edited_at timestamptz, last_edited_by text
    )
    """,
    """
    CREATE TABLE scraped_courses (
        id integer PRIMARY KEY, scrape_job_id text NOT NULL,
        university_id integer NOT NULL, course_id integer REFERENCES courses(id),
        status text NOT NULL, course_name text, course_location text,
        course_website text, degree_level text, study_mode text,
        international_fee double precision, fee_year integer, fee_term text,
        currency text, fee_scope_key text, extraction_method jsonb NOT NULL
    )
    """,
    """
    CREATE TABLE fees (
        id bigserial PRIMARY KEY, course_id integer NOT NULL REFERENCES courses(id),
        international_fee double precision, currency text, fee_term text, fee_year integer
    )
    """,
    """
    CREATE TABLE course_offerings (
        id bigserial PRIMARY KEY, course_id integer NOT NULL REFERENCES courses(id),
        location_key text NOT NULL, location text NOT NULL, fee_amount double precision,
        fee_currency text, fee_term text, fee_year integer, source_url text,
        UNIQUE (course_id, location_key)
    )
    """,
    """
    CREATE TABLE course_id_aliases (
        alias_course_id integer PRIMARY KEY REFERENCES courses(id) ON DELETE RESTRICT,
        canonical_course_id integer NOT NULL REFERENCES courses(id) ON DELETE RESTRICT,
        reason text NOT NULL, created_by text NOT NULL, audit_metadata jsonb NOT NULL DEFAULT '{}',
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK (alias_course_id <> canonical_course_id)
    )
    """,
    """
    CREATE TABLE course_field_approvals (
        id bigserial PRIMARY KEY, course_id integer NOT NULL REFERENCES courses(id),
        field_key text NOT NULL
    )
    """,
    """
    CREATE TABLE course_pathways (
        id bigserial PRIMARY KEY,
        source_course_id integer NOT NULL REFERENCES courses(id),
        target_course_id integer NOT NULL REFERENCES courses(id)
    )
    """,
    """
    CREATE TABLE intakes (
        id bigserial PRIMARY KEY, course_id integer NOT NULL REFERENCES courses(id),
        intake_month text
    )
    """,
    """
    CREATE TABLE english_requirements (
        id bigserial PRIMARY KEY, course_id integer NOT NULL REFERENCES courses(id),
        test_type text, overall double precision, listening double precision,
        reading double precision, writing double precision, speaking double precision
    )
    """,
    """
    CREATE TABLE academic_level_options (id integer PRIMARY KEY, name text NOT NULL)
    """,
    """
    CREATE TABLE academic_requirements (
        id bigserial PRIMARY KEY, course_id integer NOT NULL REFERENCES courses(id),
        academic_level_option_id integer, academic_level text, academic_score double precision,
        score_type text, academic_country text, created_at timestamptz
    )
    """,
    """
    CREATE TABLE course_identity_reconciliation_audit (
        id bigserial PRIMARY KEY, audit_id uuid NOT NULL, manifest_sha256 text NOT NULL,
        approval_revision text NOT NULL, actor text NOT NULL, event_type text NOT NULL,
        entity_table text NOT NULL, entity_key jsonb NOT NULL, row_payload jsonb NOT NULL,
        created_at timestamptz NOT NULL DEFAULT now(),
        CHECK (event_type IN ('before', 'after', 'run')),
        CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$')
    )
    """,
)


def _fixture(mapping: dict):
    course_rows = []
    evidence_rows = []
    staged_id = 1
    all_ids = []
    group_first_pair = []
    for group_index, group in enumerate(mapping["groups"]):
        degree = "Master"
        variant = f"Isolated integration variant {group_index + 1}"
        member_ids = list(group["ids"])
        if len(member_ids) >= 2 and not group_first_pair:
            group_first_pair = member_ids[:2]
        for course_id in member_ids:
            all_ids.append(course_id)
            location = f"Canary campus {course_id}"
            fee = float(10_000 + (course_id % 1_000))
            course_rows.append({
                "id": course_id, "university_id": 92, "name": group["award"],
                "course_website": group["source"], "degree_level": degree,
                "study_mode": "Full-time", "course_location": location,
                "status": "active", "approval_status": "approved",
                "offering_identity": None, "field_approval_counts": {"name": 1},
                "reference_counts": {}, "outside_reference_count": 0,
                "existing_offering_count": 0, "alias_count": 0,
            })
            family_records = [("job-base", group["parent"])]
            # Seventeen duplicate per-job families deliberately overlap the
            # same component ID, making 119 raw families but 102 components.
            if group_index < 17 and course_id == member_ids[0]:
                family_records.append((f"job-overlap-{group_index}", group["parent"]))
            for job_id, split_id in family_records:
                option = {
                    "amount": fee, "currency": "GBP", "campus": location,
                    "study_variant": variant, "year": 2026, "period": "Annual",
                    "source_url": group["source"],
                    "snippet": f"International Students | 2026 | {variant} | {location}: £{fee}",
                }
                scope = {
                    "original_name": group["award"], "locations": [location],
                    "key": f"scope-{course_id}", "source_url": group["source"],
                    "split_from_id": split_id,
                }
                evidence_rows.append({
                    "id": staged_id, "scrape_job_id": job_id,
                    "university_id": 92, "course_id": course_id,
                    "status": "published",
                    "course_name": f"{group['award']} — {location}",
                    "course_location": location, "course_website": group["source"],
                    "degree_level": degree, "study_mode": "Full-time",
                    "international_fee": fee, "fee_year": 2026,
                    "fee_term": "Annual", "currency": "GBP",
                    "fee_scope_key": scope["key"],
                    "extraction_method": {
                        "campus_fee_scope": scope,
                        "international_fee": "fee.ulaw_course_authority",
                        "fee_variants": {
                            "status": "uniform", "selected": [option], "options": [option],
                            "fee_year": 2026, "fee_term": "Annual", "currency": "GBP",
                            "international_fee": fee,
                        },
                    },
                })
                staged_id += 1

    assert len(all_ids) == 305 and len(set(all_ids)) == 305
    assert len(evidence_rows) == 322
    return {
        "schema_version": 1, "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "raw_family_count": 119, "logical_component_count": 102,
        "evidence_rows": evidence_rows, "courses": course_rows,
    }, course_rows, evidence_rows, group_first_pair


async def _seed(engine, course_rows: list[dict], evidence_rows: list[dict],
                internal_pathway: list[int]) -> None:
    async with engine.begin() as conn:
        for statement in DDL:
            await conn.execute(text(statement))
        await conn.execute(text(
            "INSERT INTO alembic_version(version_num) VALUES "
            "('389_course_identity_audit')"
        ))
        await conn.execute(text("""
            INSERT INTO universities(id, name, city, country, website, featured, featured_priority)
            VALUES (92, 'Integration University', 'London', 'United Kingdom',
                    'https://www.law.ac.uk', false, 0)
        """))
        await conn.execute(text("""
            INSERT INTO courses
                (id, university_id, name, course_website, degree_level, study_mode,
                 course_location, status, approval_status, offering_identity,
                 category, sub_category, duration, duration_term)
            VALUES
                (:id, :university_id, :name, :course_website, :degree_level, :study_mode,
                 :course_location, :status, :approval_status, NULL,
                 'Law', 'Legal Studies', 3, 'Years')
        """), [{
            **{key: row[key] for key in (
                "id", "university_id", "name", "course_website", "degree_level",
                "study_mode", "course_location", "status", "approval_status",
            )}
        } for row in course_rows])
        stage_payloads = []
        for row in evidence_rows:
            row = dict(row)
            row["extraction_method"] = json.dumps(row["extraction_method"])
            stage_payloads.append(row)
        await conn.execute(text("""
            INSERT INTO scraped_courses
                (id, scrape_job_id, university_id, course_id, status, course_name,
                 course_location, course_website, degree_level, study_mode,
                 international_fee, fee_year, fee_term, currency, fee_scope_key,
                 extraction_method)
            VALUES
                (:id, :scrape_job_id, :university_id, :course_id, :status, :course_name,
                 :course_location, :course_website, :degree_level, :study_mode,
                 :international_fee, :fee_year, :fee_term, :currency, :fee_scope_key,
                 CAST(:extraction_method AS jsonb))
        """), stage_payloads)
        fee_rows = []
        for row in course_rows:
            fee_rows.append({
                "course_id": row["id"], "international_fee": float(10_000 + row["id"] % 1_000),
                "currency": "GBP", "fee_term": "Annual", "fee_year": 2026,
            })
        await conn.execute(text("""
            INSERT INTO fees(course_id, international_fee, currency, fee_term, fee_year)
            VALUES (:course_id, :international_fee, :currency, :fee_term, :fee_year)
        """), fee_rows)
        await conn.execute(text("""
            INSERT INTO course_field_approvals(course_id, field_key)
            SELECT id, 'name' FROM courses
        """))
        if internal_pathway:
            await conn.execute(text("""
                INSERT INTO course_pathways(source_course_id, target_course_id)
                VALUES (:source, :target)
            """), {"source": internal_pathway[0], "target": internal_pathway[1]})


def _reviewed_manifest(snapshot: dict, mapping: dict, mapping_sha: str, path: Path):
    manifest = preview.build_review_manifest(
        snapshot, approved_mapping=mapping, approved_mapping_sha256=mapping_sha,
    )
    assert manifest["observed_scope"]["raw_families"] == 119
    assert manifest["observed_scope"]["groups"] == 102
    assert manifest["observed_scope"]["unique_course_ids"] == 305
    assert manifest["observed_scope"]["preview_candidate_groups"] == 102
    assert all(group["eligibility"] == "preview_candidate" for group in manifest["groups"])
    manifest["approval"] = {
        "status": "approved", "revision": "629-isolated-integration-r1",
        "approved_by": "isolated PostgreSQL integration test",
        "approved_scope": {"groups": 102, "course_ids": 305, "aliases": 203},
        "approved_mapping_sha256": mapping_sha,
        "external_reference_scope": "out_of_scope_preserve_original_course_ids",
    }
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":"))
    path.write_text(payload, encoding="utf-8")
    return apply_tool.load_approved_manifest(
        path, hashlib.sha256(payload.encode()).hexdigest(),
        "629-isolated-integration-r1", MAPPING_PATH, mapping_sha,
    )


async def _insert_unreviewed_same_award_course(engine, group: dict, *,
                                                course_id: int, staged_id: int,
                                                variant: str) -> None:
    location = f"Unreviewed campus {course_id}"
    fee = float(14_000 + course_id % 1_000)
    option = {
        "amount": fee, "currency": "GBP", "campus": location,
        "study_variant": variant, "year": 2026, "period": "Annual",
        "source_url": group["source"],
        "snippet": f"International Students | 2026 | {variant} | {location}: £{fee}",
    }
    scope = {
        "original_name": group["award"], "locations": [location],
        "key": f"unreviewed-{course_id}", "source_url": group["source"],
        "split_from_id": course_id,
    }
    metadata = {
        "campus_fee_scope": scope,
        "international_fee": "fee.ulaw_course_authority",
        "fee_variants": {
            "status": "uniform", "selected": [option], "options": [option],
            "fee_year": 2026, "fee_term": "Annual", "currency": "GBP",
            "international_fee": fee,
        },
    }
    async with engine.begin() as conn:
        await conn.execute(text("""
            INSERT INTO courses
                (id, university_id, name, course_website, degree_level, study_mode,
                 course_location, status, approval_status, category)
            VALUES (:id, 92, :name, :source, 'Master', 'Full-time',
                    :location, 'active', 'approved', 'Law')
        """), {
            "id": course_id, "name": group["award"], "source": group["source"],
            "location": location,
        })
        await conn.execute(text("""
            INSERT INTO scraped_courses
                (id, scrape_job_id, university_id, course_id, status, course_name,
                 course_location, course_website, degree_level, study_mode,
                 international_fee, fee_year, fee_term, currency, fee_scope_key,
                 extraction_method)
            VALUES (:id, 'unreviewed-job', 92, :course_id, 'published', :course_name,
                    :location, :source, 'Master', 'Full-time', :fee, 2026, 'Annual',
                    'GBP', :scope_key, CAST(:metadata AS jsonb))
        """), {
            "id": staged_id, "course_id": course_id,
            "course_name": f"{group['award']} — {location}", "location": location,
            "source": group["source"], "fee": fee, "scope_key": scope["key"],
            "metadata": json.dumps(metadata),
        })
        await conn.execute(text("""
            INSERT INTO fees(course_id, international_fee, currency, fee_term, fee_year)
            VALUES (:id, :fee, 'GBP', 'Annual', 2026)
        """), {"id": course_id, "fee": fee})


async def _remove_unreviewed_course(engine, course_id: int) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM fees WHERE course_id = :id"), {"id": course_id})
        await conn.execute(text("DELETE FROM scraped_courses WHERE course_id = :id"), {
            "id": course_id,
        })
        await conn.execute(text("DELETE FROM courses WHERE id = :id"), {"id": course_id})


def test_integration_fixture_models_119_overlapping_families_and_variant_identity():
    mapping_bytes = MAPPING_PATH.read_bytes()
    mapping = json.loads(mapping_bytes)
    snapshot, _, _, _ = _fixture(mapping)
    family_count, components = preview._connected_components(snapshot["evidence_rows"])
    assert family_count == 119
    assert len(components) == 102
    award_sources = {}
    for index, group in enumerate(mapping["groups"]):
        key = (group["award"], group["source"])
        award_sources.setdefault(key, set()).add(f"Isolated integration variant {index + 1}")
    assert sum(len(variants) > 1 for variants in award_sources.values()) == 35


@pytest.mark.integration
def test_task629_end_to_end_canary_schema():
    database_url = os.environ.get("TASK629_CANARY_DATABASE_URL")
    if not database_url:
        pytest.skip("requires an explicitly provisioned isolated TASK629_CANARY_DATABASE_URL")

    async def scenario():
        if not database_url.startswith("postgresql+asyncpg://"):
            pytest.fail("TASK629_CANARY_DATABASE_URL must use PostgreSQL/asyncpg")
        admin_engine = create_async_engine(database_url, echo=False, pool_size=1, max_overflow=0)
        schema = f"task629_test_{uuid.uuid4().hex[:12]}"
        test_engine = None
        try:
            async with admin_engine.begin() as conn:
                db_name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
                if not (db_name.startswith("canary_") or db_name.endswith("_test")):
                    pytest.fail("integration target database must be canary_* or *_test")
                await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
            test_engine = create_async_engine(
                database_url,
                echo=False, pool_size=1, max_overflow=0,
                connect_args={"server_settings": {"search_path": schema}},
            )
            mapping_bytes = MAPPING_PATH.read_bytes()
            mapping = json.loads(mapping_bytes)
            mapping_sha = hashlib.sha256(mapping_bytes).hexdigest()
            snapshot, course_rows, evidence_rows, internal_pathway = _fixture(mapping)
            await _seed(
                test_engine, course_rows, evidence_rows,
                internal_pathway,
            )
            async with test_engine.connect() as conn:
                async with conn.begin():
                    await conn.execute(text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    ))
                    exported = await exporter.build_snapshot(conn)
            assert exported["raw_family_count"] == 119
            assert exported["logical_component_count"] == 102
            manifest_path = SCRIPT_DIR / f".task629-integration-{schema}.json"
            try:
                approved = _reviewed_manifest(
                    exported, mapping, mapping_sha, manifest_path,
                )
                duplicate_group = mapping["groups"][0]
                mapped_variant = f"Isolated integration variant 1"
                await _insert_unreviewed_same_award_course(
                    test_engine, duplicate_group, course_id=900_001,
                    staged_id=900_101, variant=mapped_variant,
                )
                with pytest.raises(apply_tool.ApplyRefused, match="duplicates this exact"):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                await _remove_unreviewed_course(test_engine, 900_001)

                alternate_variant_course = 900_002
                await _insert_unreviewed_same_award_course(
                    test_engine, duplicate_group, course_id=alternate_variant_course,
                    staged_id=900_102, variant="Different full-time variant",
                )
                dry_run = await apply_tool._run(
                    approved, apply=False, actor="", expected_database=db_name,
                    confirm_write=False, _test_database_url=database_url,
                    _test_schema=schema,
                )
                assert dry_run["mode"] == "dry-run" and dry_run["writes"] == 0

                rolled_back = await apply_tool._run(
                    approved, apply=True, actor="integration-canary",
                    expected_database=db_name, confirm_write=True,
                    rollback_after_apply=True, _test_database_url=database_url,
                    _test_schema=schema,
                )
                assert rolled_back["rollback_verified"] is True

                applied = await apply_tool._run(
                    approved, apply=True, actor="integration-canary",
                    expected_database=db_name, confirm_write=True,
                    _test_database_url=database_url, _test_schema=schema,
                )
                assert applied["mode"] == "apply"
                async with test_engine.connect() as conn:
                    course_count = (await conn.execute(text("""
                        SELECT count(*) FROM courses WHERE id = ANY(:ids)
                    """), {"ids": [course_id for group in mapping["groups"]
                                   for course_id in group["ids"]]})).scalar_one()
                    alias_count = (await conn.execute(text(
                        "SELECT count(*) FROM course_id_aliases"
                    ))).scalar_one()
                    pathway_count = (await conn.execute(text(
                        "SELECT count(*) FROM course_pathways"
                    ))).scalar_one()
                    fee_rows = (await conn.execute(text("""
                        SELECT count(*) FROM fees
                         WHERE course_id = ANY(:ids)
                    """), {"ids": [group["parent"] for group in mapping["groups"]]})).scalar_one()
                    offer_rows = (await conn.execute(text(
                        "SELECT count(*) FROM course_offerings"
                    ))).scalar_one()
                assert course_count == 305
                assert alias_count == 203
                assert pathway_count == (1 if internal_pathway else 0)
                assert fee_rows == 0
                assert offer_rows == 305

                from app.routers.search import search_courses

                async with AsyncSession(test_engine) as session:
                    found = await search_courses(
                        session, university_id=92, limit=100, page=1,
                    )
                    payload = json.loads(found.body)
                    canonical_ids = {group["parent"] for group in mapping["groups"]}
                    target = next(
                        row for row in payload["results"]
                        if row["course_id"] in canonical_ids and row["offerings"]
                    )
                    assert target["international_fee"] is not None
                    assert target["offerings"]
                    assert all(item["feeAmount"] is not None for item in target["offerings"])
                    too_cheap = await search_courses(
                        session, university_id=92, max_fee=0, limit=100, page=1,
                    )
                    assert json.loads(too_cheap.body)["total"] == 0
            finally:
                manifest_path.unlink(missing_ok=True)
        finally:
            if test_engine is not None:
                await test_engine.dispose()
            async with admin_engine.begin() as conn:
                await conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            await admin_engine.dispose()

    asyncio.run(scenario())