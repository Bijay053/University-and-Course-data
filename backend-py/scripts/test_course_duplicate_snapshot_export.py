"""Unit tests for the read-only, redacted production snapshot exporter."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).with_name("course_duplicate_snapshot_export.py")
SPEC = importlib.util.spec_from_file_location("course_duplicate_snapshot_export", SCRIPT)
exporter = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(exporter)


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def __iter__(self):
        return iter(self._rows)


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.dialect = SimpleNamespace(identifier_preparer=None)

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))
        return FakeResult(self.rows)


def test_route_export_is_digest_only():
    url = "https://user:password@example.edu/course?access_token=secret#frag"
    digest = exporter._digest_route(url)
    assert digest == "sha256:" + hashlib.sha256(url.encode()).hexdigest()
    assert url not in digest


def test_scope_and_fee_variant_projection_omits_unapproved_json_keys():
    scope = exporter._json_safe_scope({
        "original_name": "MSc Data Science",
        "locations": ["London"],
        "key": "scope",
        "source_url": "must-not-be-used",
        "split_from_id": 42,
        "split_actor": "reviewer@example.edu",
        "original_locations": "sensitive/unneeded",
    }, "sha256:" + "a" * 64)
    variant = exporter._json_safe_fee_variant({
        "status": "uniform",
        "selected": [{
            "study_variant": "Standard", "campus": "London", "amount": 100, "private": "x"
        }],
        "internal_note": "private",
    })
    assert scope == {
        "original_name": "MSc Data Science", "locations": ["London"], "key": "scope",
        "source_url": "sha256:" + "a" * 64, "split_from_id": 42,
    }
    assert variant == {
        "status": "uniform",
        "selected": [{
            "study_variant": "Standard", "campus": "London", "amount": 100,
            "currency": None, "year": None, "period": None, "source_url": None,
        }],
        "validated_uniform_authority": False,
    }


def test_candidate_query_is_parameterized_and_projects_only_allowed_fields():
    route = "https://example.edu/c?token=secret"
    fake = FakeConnection([{
        "id": 10,
        "scrape_job_id": "job-1",
        "university_id": 3,
        "course_id": 90,
        "status": "published",
        "course_name": "MSc Data Science — London",
        "course_location": "London",
        "course_website": route,
        "degree_level": "Master",
        "study_mode": "Full-time",
        "international_fee": 10000,
        "fee_year": 2026,
        "fee_term": "Annual",
        "currency": "GBP",
        "fee_scope_key": "k",
        "campus_fee_scope": {
            "original_name": "MSc Data Science", "locations": ["London"],
            "key": "k", "source_url": route, "split_from_id": 10,
            "split_actor": "reviewer@example.edu",
        },
        "fee_variants": {
            "status": "uniform",
            "selected": [{"study_variant": "Standard", "campus": "London", "amount": 10000}],
            "private": "do not export",
        },
        "notes": "do not select",
    }])
    rows = asyncio.run(exporter._candidate_evidence(fake))
    statement, params = fake.calls[0]
    assert params == {
        "approved": "approved", "published": "published", "row_limit": 2001,
        "university_id": 92, "unscoped_ids": [9395, 9392, 9391, 9390],
    }
    assert "status IN (:approved, :published)" in statement
    assert "LIMIT :row_limit" in statement
    assert "notes" not in statement
    assert rows[0]["course_website"].startswith("sha256:")
    assert route not in str(rows)
    assert "split_actor" not in str(rows)
    selected = rows[0]["extraction_method"]["fee_variants"]["selected"][0]
    assert selected["campus"] == "London"
    assert selected["amount"] == 10000
    assert rows[0]["extraction_method"]["fee_variants"][
        "validated_uniform_authority"
    ] is False
    assert "private" not in str(rows[0]["extraction_method"]["fee_variants"])


def test_unknown_or_composite_course_foreign_keys_fail_closed():
    unknown = FakeConnection([{
        "target_schema": "public",
        "table_schema": "public",
        "table_name": "external_applications",
        "column_name": "course_id",
        "constraint_name": "fk_external_course",
        "constraint_column_count": 1,
    }])
    try:
        asyncio.run(exporter._course_fk_census(unknown))
    except exporter.ExportRefused as exc:
        assert "unknown FK" in str(exc)
    else:
        raise AssertionError("unknown FK references must refuse export")

    composite = FakeConnection([{
        "target_schema": "public",
        "table_schema": "public",
        "table_name": "course_pathways",
        "column_name": "source_course_id",
        "constraint_name": "fk_composite",
        "constraint_column_count": 2,
    }])
    try:
        asyncio.run(exporter._course_fk_census(composite))
    except exporter.ExportRefused as exc:
        assert "composite FK" in str(exc)
    else:
        raise AssertionError("composite references must refuse export")


def test_known_reference_census_emits_only_reviewed_tables():
    fake = FakeConnection([{
        "target_schema": "public",
        "table_schema": "public",
        "table_name": "course_pathways",
        "column_name": "source_course_id",
        "constraint_name": "fk_pathway_source",
        "constraint_column_count": 1,
    }, {
        "target_schema": "public",
        "table_schema": "public",
        "table_name": "course_pathways",
        "column_name": "target_course_id",
        "constraint_name": "fk_pathway_target",
        "constraint_column_count": 1,
    }])
    refs = asyncio.run(exporter._course_fk_census(fake))
    assert refs == [
        ("public", "course_pathways", "source_course_id"),
        ("public", "course_pathways", "target_course_id"),
    ]


def test_simple_count_uses_bound_course_ids():
    fake = FakeConnection([{"course_id": 12, "row_count": 3}])
    counts = asyncio.run(exporter._simple_counts(fake, "intakes", "course_id", [12, 13]))
    statement, params = fake.calls[0]
    assert "WHERE course_id IN (__[POSTCOMPILE_course_ids])" in statement
    assert params == {"course_ids": [12, 13]}
    assert counts == {12: 3}


def test_cli_requires_explicit_read_only_acknowledgement():
    args = exporter.parse_args([])
    assert args.confirm_read_only_production is False