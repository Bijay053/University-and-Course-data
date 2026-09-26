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
    unscoped_mode_by_parent = {
        next(group["parent"] for group in mapping["groups"]
             if alias["canonical_course_id"] in group["ids"]): alias["study_mode"]
        for alias in mapping["unscoped_aliases"]
    }
    all_ids = []
    group_first_pair = []
    for group_index, group in enumerate(mapping["groups"]):
        variant = f"Isolated integration variant {group_index + 1}"
        member_ids = list(group["ids"])
        if len(member_ids) >= 2 and not group_first_pair:
            group_first_pair = member_ids[:2]
        for course_id in member_ids:
            all_ids.append(course_id)
            if "diploma" in group["award"].casefold():
                degree, published_degree = "Graduate Diploma", "Graduate Certificate & Diploma"
            elif group["award"] == "LLB International Law":
                degree, published_degree = "Bachelor's", "Bachelor"
            elif course_id in {9389, 9547}:
                degree, published_degree = "Master's", "Master"
            else:
                degree = published_degree = "Master"
            if group["parent"] in preview.REVISED_UNIONS:
                # The revised groups have one exact reviewed study identity
                # across every member, not the legacy representative's
                # punctuation variant.
                degree = published_degree = "Master"
            fee_term = "Annual"
            if group["parent"] == 9389:
                campuses_by_id = {
                    9389: ["Birmingham", "Leeds", "Manchester"],
                    9408: ["London"],
                    9645: ["Manchester"],
                    9646: ["Birmingham"],
                    9647: ["Leeds"],
                    9648: ["London Moorgate"],
                }
                scope_locations = campuses_by_id[course_id]
                location = ", ".join(scope_locations)
                name_location = scope_locations[0]
                fee_campus = scope_locations
                fee = 17_500.0 if course_id in {9389, 9645, 9646, 9647} else 19_050.0
                fee_term = "Full Course"
                variant = "Standard"
            elif group["parent"] == 9396:
                campuses_by_id = {
                    9396: ["Birmingham", "Manchester"],
                    9421: ["London"],
                    9608: ["Manchester"],
                    9675: ["Birmingham"],
                    9676: ["London Bloomsbury"],
                }
                scope_locations = campuses_by_id[course_id]
                location = ", ".join(scope_locations)
                name_location = scope_locations[0]
                fee_campus = scope_locations
                fee = 18_250.0 if course_id in {9396, 9608, 9675} else 19_600.0
                fee_term = "Full Course"
                variant = "Standard"
            elif group["parent"] in unscoped_mode_by_parent:
                location = f"Canary campus {course_id}"
                scope_locations = [location]
                name_location = location
                fee_campus = location
                fee = 20_600.0
                fee_term = "Full Course"
                variant = "Standard"
            elif group_index == 1 and course_id == group["parent"]:
                location = "Hull"
                scope_locations = ["Hull"]
                name_location = location
                fee_campus = "Outside London"
                fee = float(10_000 + (course_id % 1_000))
            elif course_id == 9406:
                location = "Manchester"
                scope_locations = ["Manchester"]
                name_location = location
                fee_campus = "Outside London"
                fee = 18_250.0
            elif course_id == 9498:
                location = "London Bloomsbury"
                scope_locations = ["London Bloomsbury"]
                name_location = location
                fee_campus = "London"
                fee = 18_100.0
            elif group_index == 2 and course_id == group["parent"]:
                location = "Manchester"
                scope_locations = ["Manchester"]
                name_location = location
                fee_campus = "Birmingham/Manchester"
                fee = 16_700.0
            elif group_index == 2 and course_id == group["ids"][1]:
                location = "Birmingham"
                scope_locations = ["Birmingham"]
                name_location = location
                fee_campus = "Birmingham"
                fee = 16_700.0
            else:
                location = f"Canary campus {course_id}"
                scope_locations = [location]
                name_location = location
                fee_campus = location
                fee = float(10_000 + (course_id % 1_000))
            course_study_mode = unscoped_mode_by_parent.get(group["parent"], "Full-time")
            course_rows.append({
                "id": course_id, "university_id": 92,
                "name": f"{group['award']} — {name_location}",
                "course_website": group["source"], "degree_level": published_degree,
                "study_mode": course_study_mode, "course_location": location,
                "status": "active", "approval_status": "approved",
                "offering_identity": None, "field_approval_counts": {"name": 1},
                "reference_counts": {}, "outside_reference_count": 0,
                "existing_offering_count": 0, "alias_count": 0,
            })
            if group["parent"] in preview.REVISED_UNIONS:
                component_index = member_ids.index(course_id) // 2
                split_id = group["parent"] + 10_000 + component_index
            else:
                split_id = group["parent"] + 10_000
            family_records = [(f"job-base-{group_index}", split_id)]
            # Fifteen extra per-job families overlap an existing ID. The
            # approved inventory therefore has 119 families but 104 components.
            if group_index < 15 and course_id == member_ids[0]:
                family_records.append((f"job-overlap-{group_index}", split_id))
            for job_id, split_id in family_records:
                fee_campuses = (
                    fee_campus if isinstance(fee_campus, list) else [fee_campus]
                )
                options = [{
                    "amount": fee, "currency": "GBP", "campus": campus,
                    "study_variant": variant, "year": 2026, "period": fee_term,
                    "source_url": group["source"],
                    "snippet": f"International Students | 2026 | {variant} | {campus}: £{fee}",
                } for campus in fee_campuses]
                scope = {
                    "original_name": group["award"], "locations": scope_locations,
                    "key": f"scope-{course_id}", "source_url": group["source"],
                    "split_from_id": split_id,
                }
                evidence_rows.append({
                    "id": staged_id, "scrape_job_id": job_id,
                    "university_id": 92, "course_id": course_id,
                    "status": "published",
                    "course_name": f"{group['award']} — {name_location}",
                    "course_location": location, "course_website": group["source"],
                    "degree_level": degree, "study_mode": course_study_mode,
                    "international_fee": fee, "fee_year": 2026,
                    "fee_term": fee_term, "currency": "GBP",
                    "fee_scope_key": scope["key"],
                    "extraction_method": {
                        "campus_fee_scope": scope,
                        "international_fee": "fee.ulaw_course_authority",
                        "fee_variants": {
                            "status": "uniform", "selected": options, "options": options,
                            "fee_year": 2026, "fee_term": fee_term, "currency": "GBP",
                            "international_fee": fee,
                        },
                    },
                })
                staged_id += 1

    assert len(all_ids) == 307 and len(set(all_ids)) == 307
    assert len(evidence_rows) == 322
    repeated_courses = set()
    repeat_sources = []
    for row in evidence_rows:
        if row["course_id"] not in repeated_courses:
            repeat_sources.append(row)
            repeated_courses.add(row["course_id"])
        if len(repeat_sources) == 27:
            break
    for repeat_index, source_row in enumerate(repeat_sources):
        repeated = dict(source_row)
        repeated["id"] = staged_id
        repeated["scrape_job_id"] = f"approved-repeat-{repeat_index}"
        evidence_rows.append(repeated)
        staged_id += 1
    for extra_index in range(25):
        course_id = 910_000 + extra_index
        location = f"Unrelated campus {extra_index + 1}"
        award = f"Unrelated course family {extra_index + 1}"
        source = f"https://unrelated.example.test/courses/{extra_index + 1}"
        fee = float(20_000 + extra_index)
        course_rows.append({
            "id": course_id, "university_id": 92, "name": f"{award} — {location}",
            "course_website": source, "degree_level": "Master",
            "study_mode": "Full-time", "course_location": location,
            "status": "active", "approval_status": "approved",
            "offering_identity": None, "field_approval_counts": {"name": 1},
            "reference_counts": {}, "outside_reference_count": 0,
            "existing_offering_count": 0, "alias_count": 0,
        })
        family_count = 1
        for family_index in range(family_count):
            job_id = f"unrelated-job-{extra_index}-{family_index}"
            scope = {
                "original_name": award, "locations": [location],
                "key": f"unrelated-scope-{course_id}",
                "source_url": source, "split_from_id": course_id,
            }
            option = {
                "amount": fee, "currency": "GBP", "campus": location,
                "study_variant": f"Unrelated variant {extra_index + 1}",
                "year": 2026, "period": "Annual", "source_url": source,
                "snippet": f"International Students | 2026 | {award} | {location}: £{fee}",
            }
            evidence_rows.append({
                "id": staged_id, "scrape_job_id": job_id, "university_id": 92,
                "course_id": course_id, "status": "published",
                "course_name": f"{award} — {location}", "course_location": location,
                "course_website": source, "degree_level": "Master",
                "study_mode": "Full-time", "international_fee": fee,
                "fee_year": 2026, "fee_term": "Annual", "currency": "GBP",
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
    for alias in mapping["unscoped_aliases"]:
        canonical = next(row for row in course_rows
                         if row["id"] == alias["canonical_course_id"])
        source = alias["source"]
        route_hash_source = "sha256:" + hashlib.sha256(source.encode()).hexdigest()
        study_mode = alias["study_mode"]
        fee = float(alias["amount"])
        course_rows.append({
            "id": alias["alias_course_id"], "university_id": 92,
            "name": alias["award"],
            "course_website": source,
            "degree_level": canonical["degree_level"],
            "study_mode": study_mode,
            "course_location": None, "status": "active",
            "approval_status": "approved", "offering_identity": None,
            "field_approval_counts": {"name": 1}, "reference_counts": {},
            "outside_reference_count": 0, "existing_offering_count": 0,
            "alias_count": 0,
        })
        options = [{
            "amount": fee, "currency": "GBP", "campus": "Unspecified",
            "study_variant": "Standard", "year": 2026,
            "period": "Full Course", "source_url": route_hash_source,
            "snippet": f"International Students | 2026 | Standard | Unspecified: £{fee}",
        }]
        evidence_rows.append({
            "id": staged_id, "scrape_job_id": f"unscoped-job-{alias['alias_course_id']}",
            "university_id": 92, "course_id": alias["alias_course_id"],
            "status": "published", "course_name": alias["award"],
            "course_location": None, "course_website": route_hash_source,
            "degree_level": "Master's",
            "study_mode": study_mode,
            "international_fee": fee, "fee_year": 2026,
            "fee_term": "Full Course", "currency": "GBP",
            "fee_scope_key": None,
            "extraction_method": {
                "international_fee": "fee.ulaw_course_authority",
                "fee_variants": {
                    "status": "uniform", "selected": options, "options": options,
                    "fee_year": 2026, "fee_term": "Full Course",
                    "currency": "GBP", "international_fee": fee,
                    "validated_uniform_authority": True,
                },
            },
        })
        staged_id += 1
    fee_by_course = {row["course_id"]: row for row in evidence_rows}
    published_inventory = []
    for course in course_rows:
        fee_row = fee_by_course.get(course["id"])
        fees = [] if fee_row is None else [{
                "id": 1, "amount": float(fee_row["international_fee"]),
            "currency": fee_row["currency"], "fee_term": fee_row["fee_term"],
            "fee_year": fee_row["fee_year"],
        }]
        published_inventory.append({
            key: (
                "sha256:" + hashlib.sha256(course["course_website"].encode()).hexdigest()
                if key == "course_website" and course.get("course_website") else course.get(key)
            ) for key in (
                "id", "university_id", "name", "course_website", "degree_level",
                "study_mode", "course_location", "status", "approval_status",
            )
        } | {
            "offering_identity": "present" if course.get("offering_identity") else None,
            "legacy_fee_rows": fees,
        })
    return {
        "schema_version": 1, "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "raw_family_count": 171, "logical_component_count": 129,
        "evidence_rows": evidence_rows, "courses": course_rows,
        "published_course_inventory": published_inventory,
        "published_course_inventory_complete": True,
        "published_course_inventory_count": len(published_inventory),
    }, course_rows, evidence_rows, group_first_pair


async def _seed(engine, course_rows: list[dict], evidence_rows: list[dict],
                internal_pathway: list[int]) -> None:
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    unscoped_by_id = {
        item["alias_course_id"]: item for item in mapping["unscoped_aliases"]
    }
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
            , **({"course_website": unscoped_by_id[row["id"]]["source"]}
                 if row["id"] in unscoped_by_id else {})
        } for row in course_rows])
        stage_payloads = []
        for row in evidence_rows:
            row = dict(row)
            alias = unscoped_by_id.get(row["course_id"])
            if alias:
                row["course_website"] = alias["source"]
                variants = row.get("extraction_method", {}).get("fee_variants", {})
                for option in variants.get("selected", []) + variants.get("options", []):
                    option["source_url"] = alias["source"]
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
        staged_fee_by_course = {
            row["course_id"]: row for row in evidence_rows
        }
        for row in course_rows:
            source_fee = staged_fee_by_course[row["id"]]
            fee_rows.append({
                "course_id": row["id"],
                "international_fee": source_fee["international_fee"],
                "currency": source_fee["currency"],
                "fee_term": source_fee["fee_term"],
                "fee_year": source_fee["fee_year"],
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
    assert manifest["observed_scope"]["raw_families"] == 146
    assert manifest["observed_scope"]["groups"] == 100
    assert manifest["observed_scope"]["unique_course_ids"] == 307
    assert manifest["observed_scope"]["unscoped_aliases"] == 4
    assert manifest["observed_scope"]["preview_candidate_groups"] == 100, [
        (group["course_ids"][0], group["blocking_reasons"][:8])
        for group in manifest["groups"] if group["eligibility"] != "preview_candidate"
    ]
    assert all(group["eligibility"] == "preview_candidate" for group in manifest["groups"])
    manifest["approval"] = {
        "status": "approved", "revision": "629-isolated-integration-r1",
        "approved_by": "isolated PostgreSQL integration test",
        "approved_scope": {"groups": 100, "course_ids": 307, "aliases": 207},
        "approved_unscoped_scope": {
            "aliases": 4, "total_course_ids": 311, "total_aliases": 211,
        },
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
                                                variant: str,
                                                study_mode: str = "Full-time") -> None:
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
            VALUES (:id, 92, :name, :source, 'Master', :study_mode,
                    :location, 'active', 'approved', 'Law')
        """), {
            "id": course_id, "name": group["award"], "source": group["source"],
            "location": location, "study_mode": study_mode,
        })
        await conn.execute(text("""
            INSERT INTO scraped_courses
                (id, scrape_job_id, university_id, course_id, status, course_name,
                 course_location, course_website, degree_level, study_mode,
                 international_fee, fee_year, fee_term, currency, fee_scope_key,
                 extraction_method)
            VALUES (:id, 'unreviewed-job', 92, :course_id, 'published', :course_name,
                    :location, :source, 'Master', :study_mode, :fee, 2026, 'Annual',
                    'GBP', :scope_key, CAST(:metadata AS jsonb))
        """), {
            "id": staged_id, "course_id": course_id,
            "course_name": f"{group['award']} — {location}", "location": location,
            "source": group["source"], "fee": fee, "scope_key": scope["key"],
            "study_mode": study_mode,
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
    approved_ids = {course_id for group in mapping["groups"] for course_id in group["ids"]}
    approved_rows = [
        row for row in snapshot["evidence_rows"] if row["course_id"] in approved_ids
    ]
    family_count, components = preview._connected_components(approved_rows)
    assert family_count == 146
    assert len(components) == 104
    component_ids = [
        {row["course_id"] for row in component}
        for component in components
    ]
    mapping_parent_by_id = {
        course_id: group["parent"]
        for group in mapping["groups"] for course_id in group["ids"]
    }
    assert all(len({mapping_parent_by_id[course_id] for course_id in ids}) == 1
               for ids in component_ids)
    assert sum(
        1 for ids in component_ids if mapping_parent_by_id[next(iter(ids))] == 9389
    ) == 3
    assert sum(
        1 for ids in component_ids if mapping_parent_by_id[next(iter(ids))] == 9396
    ) == 3
    for parent, expected_campuses, fee_by_id in (
        (9389, {"Birmingham", "Leeds", "Manchester"}, {
            9389: 17_500.0, 9408: 19_050.0, 9645: 17_500.0,
            9646: 17_500.0, 9647: 17_500.0, 9648: 19_050.0,
        }),
        (9396, {"Birmingham", "Manchester"}, {
            9396: 18_250.0, 9421: 19_600.0, 9608: 18_250.0,
            9675: 18_250.0, 9676: 19_600.0,
        }),
    ):
        approved_group = next(group for group in mapping["groups"]
                              if group["parent"] == parent)
        group_rows = {
            row["course_id"]: row for row in approved_rows
            if row["course_id"] in approved_group["ids"]
        }
        campus_counts = {}
        tuple_by_campus = {}
        for row in group_rows.values():
            selected = row["extraction_method"]["fee_variants"]["selected"]
            for campus in row["extraction_method"]["campus_fee_scope"]["locations"]:
                campus_counts[campus] = campus_counts.get(campus, 0) + 1
                variant = next(item["study_variant"] for item in selected
                               if item["campus"] == campus)
                tuple_by_campus.setdefault(campus, set()).add((
                    row["course_website"], row["international_fee"], row["currency"],
                    row["fee_year"], row["fee_term"], variant,
                ))
            assert row["international_fee"] == fee_by_id[row["course_id"]]
            assert row["currency"] == "GBP" and row["fee_year"] == 2026
            assert row["fee_term"] == "Full Course"
            assert all(item["study_variant"] == "Standard" for item in selected)
        assert {campus for campus, count in campus_counts.items() if count > 1} == expected_campuses
        assert all(len(values) == 1 for values in tuple_by_campus.values())
    assert preview._norm("London") != preview._norm("London Moorgate")
    assert preview._norm("London") != preview._norm("London Bloomsbury")
    total_families, total_components = preview._connected_components(snapshot["evidence_rows"])
    assert total_families == 171
    assert len(total_components) == 129
    award_sources = {}
    for index, group in enumerate(mapping["groups"]):
        key = (group["award"], group["source"])
        award_sources.setdefault(key, set()).add(f"Isolated integration variant {index + 1}")
    assert sum(len(variants) > 1 for variants in award_sources.values()) == 33


def test_approved_mapping_contains_only_the_two_reviewed_group_merges():
    mapping = json.loads(MAPPING_PATH.read_text(encoding="utf-8"))
    groups = mapping["groups"]
    by_parent = {group["parent"]: group for group in groups}

    assert len(groups) == 100
    assert by_parent[9389]["ids"] == [9389, 9408, 9645, 9646, 9647, 9648]
    assert by_parent[9396]["ids"] == [9396, 9421, 9608, 9675, 9676]
    assert not {9645, 9648, 9608, 9676} & set(by_parent)
    changed_ids = {9645, 9648, 9608, 9676}
    assert {
        group["parent"] for group in groups
        if changed_ids.intersection(group["ids"])
    } == {9389, 9396}
    assert len(groups) - 2 == 98
    assert [
        (item["alias_course_id"], item["canonical_course_id"])
        for item in json.loads(MAPPING_PATH.read_text())["unscoped_aliases"]
    ] == [(9395, 9611), (9392, 9636), (9391, 9639), (9390, 9642)]
    assert [item["study_mode"] for item in json.loads(
        MAPPING_PATH.read_text()
    )["unscoped_aliases"]] == ["Blended", "On Campus", "On Campus", "On Campus"]
    assert all("location" not in item for item in json.loads(
        MAPPING_PATH.read_text()
    )["unscoped_aliases"])
    assert not {
        item["alias_course_id"] for item in json.loads(MAPPING_PATH.read_text())["unscoped_aliases"]
    }.intersection({course_id for group in groups for course_id in group["ids"]})


def test_four_unscoped_aliases_require_exact_evidence_and_complete_collision_scan():
    mapping_bytes = MAPPING_PATH.read_bytes()
    mapping = json.loads(mapping_bytes)
    snapshot, _, _, _ = _fixture(mapping)
    manifest = preview.build_review_manifest(
        snapshot, approved_mapping=mapping,
        approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
    )
    assert len(manifest["unscoped_aliases"]) == 4
    assert manifest["observed_scope"]["original_course_ids_including_unscoped_aliases"] == 311
    assert manifest["observed_scope"]["total_aliases_including_unscoped_aliases"] == 211
    assert all(len(alias["evidence_rows"]) == 1 for alias in manifest["unscoped_aliases"])
    assert [alias["study_mode"] for alias in manifest["unscoped_aliases"]] == [
        "Blended", "On Campus", "On Campus", "On Campus",
    ]
    alias_evidence = next(row for row in snapshot["evidence_rows"]
                          if row["course_id"] == 9395)
    assert alias_evidence["degree_level"] == "Master's"
    assert next(row for row in snapshot["published_course_inventory"]
                if row["id"] == 9395)["degree_level"] == "Master"

    mismatched_course_mode = dict(snapshot)
    mismatched_course_mode["published_course_inventory"] = [
        dict(row) for row in snapshot["published_course_inventory"]
    ]
    next(row for row in mismatched_course_mode["published_course_inventory"]
         if row["id"] == 9395)["study_mode"] = "On Campus"
    with pytest.raises(preview.SnapshotError, match="canonical identity"):
        preview.build_review_manifest(
            mismatched_course_mode, approved_mapping=mapping,
            approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
        )

    mismatched_evidence_mode = dict(snapshot)
    mismatched_evidence_mode["evidence_rows"] = [
        dict(row) for row in snapshot["evidence_rows"]
    ]
    next(row for row in mismatched_evidence_mode["evidence_rows"]
         if row["course_id"] == 9395)["study_mode"] = "On Campus"
    with pytest.raises(preview.SnapshotError, match="staged evidence"):
        preview.build_review_manifest(
            mismatched_evidence_mode, approved_mapping=mapping,
            approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
        )

    mismatched_evidence_degree = dict(snapshot)
    mismatched_evidence_degree["evidence_rows"] = [
        dict(row) for row in snapshot["evidence_rows"]
    ]
    next(row for row in mismatched_evidence_degree["evidence_rows"]
         if row["course_id"] == 9395)["degree_level"] = "Bachelor's"
    with pytest.raises(preview.SnapshotError, match="staged evidence"):
        preview.build_review_manifest(
            mismatched_evidence_degree, approved_mapping=mapping,
            approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
        )

    collision = dict(snapshot)
    collision["published_course_inventory"] = list(snapshot["published_course_inventory"])
    duplicate = dict(next(
        row for row in collision["published_course_inventory"] if row["id"] == 9395
    ))
    duplicate["id"] = 999_999
    collision["published_course_inventory"].append(duplicate)
    collision["published_course_inventory_count"] += 1
    with pytest.raises(preview.SnapshotError, match="unreviewed same-identity"):
        preview.build_review_manifest(
            collision, approved_mapping=mapping,
            approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
        )
    scoped_collision = dict(snapshot)
    scoped_collision["published_course_inventory"] = list(
        snapshot["published_course_inventory"]
    )
    duplicate_scoped = dict(next(
        row for row in scoped_collision["published_course_inventory"]
        if row["id"] == 9389
    ))
    duplicate_scoped["id"] = 999_998
    scoped_collision["published_course_inventory"].append(duplicate_scoped)
    scoped_collision["published_course_inventory_count"] += 1
    blocked = preview.build_review_manifest(
        scoped_collision, approved_mapping=mapping,
        approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
    )
    group_9389 = next(group for group in blocked["groups"]
                      if group["course_ids"][0] == 9389)
    assert group_9389["eligibility"] == "blocked"
    assert any("published collision inventory" in reason
               for reason in group_9389["blocking_reasons"])


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
            assert exported["raw_family_count"] == 171
            assert exported["logical_component_count"] == 129
            manifest_path = SCRIPT_DIR / f".task629-integration-{schema}.json"
            try:
                approved = _reviewed_manifest(
                    exported, mapping, mapping_sha, manifest_path,
                )
                observed = approved["manifest"]["observed_scope"]
                assert observed["raw_families"] == 146
                assert observed["additional_approved_id_families"] == 27
                assert observed["groups"] == 100
                assert observed["unique_course_ids"] == 307
                assert observed["extra_course_ids"] == 25
                assert observed["excluded_unrelated_course_ids"] == list(range(910_000, 910_025))
                assert observed["related_extra_course_ids"] == []

                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        INSERT INTO courses
                            (id, university_id, name, course_website, degree_level,
                             study_mode, course_location, status, approval_status)
                        VALUES (900005, 92, 'Inventory drift', 'https://unrelated.example.test/drift',
                                'Master', 'Full-time', NULL, 'active', 'approved')
                    """))
                with pytest.raises(
                    apply_tool.ApplyRefused,
                    match="complete university-wide published-course inventory changed",
                ):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                async with test_engine.begin() as conn:
                    await conn.execute(text("DELETE FROM courses WHERE id = 900005"))

                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        INSERT INTO scraped_courses
                            (id, scrape_job_id, university_id, course_id, status,
                             course_name, course_location, course_website, degree_level,
                             study_mode, international_fee, fee_year, fee_term, currency,
                             fee_scope_key, extraction_method)
                        SELECT 900105, scrape_job_id || '-late', university_id, course_id,
                               status, course_name, course_location, course_website,
                               degree_level, study_mode, international_fee, fee_year,
                               fee_term, currency, fee_scope_key, extraction_method
                          FROM scraped_courses
                         WHERE course_id = 9395 AND status IN ('approved', 'published')
                         ORDER BY id LIMIT 1
                    """))
                with pytest.raises(
                    apply_tool.ApplyRefused,
                    match="staged evidence set changed after review",
                ):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                async with test_engine.begin() as conn:
                    await conn.execute(text("DELETE FROM scraped_courses WHERE id = 900105"))

                duplicate_group = mapping["groups"][0]
                mapped_variant = "Standard"
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

                await _insert_unreviewed_same_award_course(
                    test_engine, duplicate_group, course_id=900_003,
                    staged_id=900_103, variant="Different full-time variant",
                )
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE courses SET course_location = 'Leeds' WHERE id = 900003
                    """))
                    await conn.execute(text("""
                        UPDATE scraped_courses
                           SET course_location = 'Leeds',
                               course_name = :course_name,
                               extraction_method = jsonb_set(
                                   extraction_method,
                                   '{campus_fee_scope,locations}',
                                   '["Birmingham","Leeds","Manchester"]'::jsonb
                               )
                         WHERE id = 900103
                    """), {
                        "course_name": f"{duplicate_group['award']} — Leeds",
                    })
                with pytest.raises(apply_tool.ApplyRefused, match="award/source/campus scope"):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                await _remove_unreviewed_course(test_engine, 900_003)

                mba_group = next(
                    group for group in mapping["groups"] if 9611 in group["ids"]
                )
                await _insert_unreviewed_same_award_course(
                    test_engine, mba_group, course_id=900_004,
                    staged_id=900_104, variant="Standard",
                    study_mode="Blended",
                )
                with pytest.raises(apply_tool.ApplyRefused):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                await _remove_unreviewed_course(test_engine, 900_004)

                unscoped_evidence = next(
                    row for row in evidence_rows if row["course_id"] == 9395
                )
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE scraped_courses
                           SET international_fee = international_fee + 1
                         WHERE id = :id
                    """), {"id": unscoped_evidence["id"]})
                with pytest.raises(apply_tool.ApplyRefused, match="changed|fingerprint"):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE scraped_courses SET international_fee = :fee
                         WHERE id = :id
                    """), {
                        "id": unscoped_evidence["id"],
                        "fee": unscoped_evidence["international_fee"],
                    })

                stale_row = evidence_rows[0]
                original_metadata = json.dumps(stale_row["extraction_method"])
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE scraped_courses
                           SET extraction_method = jsonb_set(
                               extraction_method,
                               '{fee_variants,selected,0,amount}',
                               to_jsonb(international_fee + 1)
                           )
                         WHERE id = :id
                    """), {"id": stale_row["id"]})
                with pytest.raises(apply_tool.ApplyRefused, match="changed after review"):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE scraped_courses SET extraction_method = CAST(:metadata AS jsonb)
                         WHERE id = :id
                    """), {"id": stale_row["id"], "metadata": original_metadata})

                legacy_course_id = 9389
                original_legacy_fee = next(
                    row["international_fee"] for row in evidence_rows
                    if row["course_id"] == legacy_course_id
                )
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE fees SET international_fee = international_fee + 1
                         WHERE course_id = :course_id
                    """), {"course_id": legacy_course_id})
                with pytest.raises(apply_tool.ApplyRefused, match="published legacy fee differs"):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                async with test_engine.connect() as conn:
                    assert (await conn.execute(text(
                        "SELECT count(*) FROM course_offerings"
                    ))).scalar_one() == 0
                async with test_engine.begin() as conn:
                    await conn.execute(text("""
                        UPDATE fees SET international_fee = :amount
                         WHERE course_id = :course_id
                    """), {"course_id": legacy_course_id, "amount": original_legacy_fee})

                alternate_variant_course = 900_002
                await _insert_unreviewed_same_award_course(
                    test_engine, duplicate_group, course_id=alternate_variant_course,
                    staged_id=900_102, variant="Different full-time variant",
                )
                with pytest.raises(
                    apply_tool.ApplyRefused,
                    match="complete university-wide published-course inventory changed",
                ):
                    await apply_tool._run(
                        approved, apply=False, actor="", expected_database=db_name,
                        confirm_write=False, _test_database_url=database_url,
                        _test_schema=schema,
                    )
                await _remove_unreviewed_course(test_engine, alternate_variant_course)
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
                        """), {"ids": (
                            [course_id for group in mapping["groups"] for course_id in group["ids"]]
                            + [alias["alias_course_id"] for alias in mapping["unscoped_aliases"]
                            ]
                        )})).scalar_one()
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
                    duplicate_offering_campuses = (await conn.execute(text("""
                        SELECT count(*) FROM (
                            SELECT course_id, location_key
                              FROM course_offerings
                             GROUP BY course_id, location_key
                            HAVING count(*) > 1
                        ) duplicate_campuses
                    """))).scalar_one()
                    group_zero_id = duplicate_group["parent"]
                    group_zero_offerings = (await conn.execute(text("""
                        SELECT location, fee_amount FROM course_offerings
                         WHERE course_id = :course_id ORDER BY location
                    """), {"course_id": group_zero_id})).mappings().all()
                    group_one_revised_id = next(
                        group["parent"] for group in mapping["groups"]
                        if group["parent"] == 9396
                    )
                    group_one_revised_offerings = (await conn.execute(text("""
                        SELECT location, fee_amount FROM course_offerings
                         WHERE course_id = :course_id ORDER BY location
                    """), {"course_id": group_one_revised_id})).mappings().all()
                    hull_course_id = mapping["groups"][1]["parent"]
                    hull_offerings = (await conn.execute(text("""
                        SELECT location, fee_amount FROM course_offerings
                         WHERE course_id = :course_id ORDER BY location
                    """), {"course_id": hull_course_id})).mappings().all()
                    manchester_course_id = next(
                        group["parent"] for group in mapping["groups"]
                        if 9406 in group["ids"]
                    )
                    london_course_id = next(
                        group["parent"] for group in mapping["groups"]
                        if 9498 in group["ids"]
                    )
                    regional_offerings = (await conn.execute(text("""
                        SELECT course_id, location, fee_amount FROM course_offerings
                         WHERE course_id = ANY(:ids)
                    """), {"ids": [manchester_course_id, london_course_id]})).mappings().all()
                    slash_course_id = mapping["groups"][2]["parent"]
                    slash_offerings = (await conn.execute(text("""
                        SELECT location, fee_amount FROM course_offerings
                         WHERE course_id = :course_id
                    """), {"course_id": slash_course_id})).mappings().all()
                assert course_count == 311
                assert alias_count == 211
                assert pathway_count == (1 if internal_pathway else 0)
                assert fee_rows == 0
                expected_offering_count = sum(
                    len({
                        location
                        for course_id in group["ids"]
                        for row in evidence_rows if row["course_id"] == course_id
                        for location in row["extraction_method"]["campus_fee_scope"]["locations"]
                    })
                    for group in mapping["groups"]
                )
                assert offer_rows == expected_offering_count == 305
                assert duplicate_offering_campuses == 0
                expected_group_zero_prices = {
                    location: next(
                        row["international_fee"] for row in evidence_rows
                        if row["course_id"] == course_id
                    )
                    for course_id in duplicate_group["ids"]
                    for location in next(
                        row["extraction_method"]["campus_fee_scope"]["locations"]
                        for row in evidence_rows if row["course_id"] == course_id
                    )
                }
                assert {row["location"]: row["fee_amount"]
                        for row in group_zero_offerings} == expected_group_zero_prices
                assert len(group_zero_offerings) == len(expected_group_zero_prices) == 5
                assert expected_group_zero_prices == {
                    "Birmingham": 17_500.0,
                    "Leeds": 17_500.0,
                    "London": 19_050.0,
                    "London Moorgate": 19_050.0,
                    "Manchester": 17_500.0,
                }
                assert {
                    row["location"]: row["fee_amount"]
                    for row in group_one_revised_offerings
                } == {
                    "Birmingham": 18_250.0,
                    "London": 19_600.0,
                    "London Bloomsbury": 19_600.0,
                    "Manchester": 18_250.0,
                }
                expected_hull_fee = next(
                    row["international_fee"] for row in evidence_rows
                    if row["course_id"] == hull_course_id
                )
                hull_prices = {
                    row["location"]: row["fee_amount"] for row in hull_offerings
                }
                assert hull_prices["Hull"] == expected_hull_fee
                assert sum(row["location"] == "Hull" for row in hull_offerings) == 1
                regional_prices = {
                    (row["course_id"], row["location"]): row["fee_amount"]
                    for row in regional_offerings
                }
                assert regional_prices[(manchester_course_id, "Manchester")] == 18_250.0
                assert regional_prices[(london_course_id, "London Bloomsbury")] == 18_100.0
                slash_prices = {
                    row["location"]: row["fee_amount"] for row in slash_offerings
                }
                assert slash_prices["Manchester"] == slash_prices["Birmingham"] == 16_700.0

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