#!/usr/bin/env python3
"""Export a bounded, read-only, secret-redacted course duplicate snapshot.

Uses the configured DATABASE_URL without printing it or accepting it in argv.
The transaction is PostgreSQL REPEATABLE READ + READ ONLY. This script never
creates, updates, or deletes database rows; output is JSON on stdout only.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
import re
import ssl
import sys
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine


SCHEMA_VERSION = 1
MAX_EVIDENCE_ROWS = 2_000
MAX_COURSE_ROWS = 1_000
MAX_OUTPUT_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOT_GROUPS = 500
TARGET_UNIVERSITY_ID = 92
MAX_REFERENCE_CONSTRAINTS = 128

# New FK sources must be reviewed before they can be classified as known
# historical/internal references. Unknown refs abort export rather than being
# silently omitted from the precondition inventory.
KNOWN_COURSE_FKS = {
    ("academic_requirements", "course_id"),
    ("english_requirements", "course_id"),
    ("intakes", "course_id"),
    ("fees", "course_id"),
    ("course_accreditations", "course_id"),
    ("course_pathways", "source_course_id"),
    ("course_pathways", "target_course_id"),
    ("course_change_events", "course_id"),
    ("course_snapshots", "course_id"),
    ("scraping_changes", "course_id"),
    ("scraped_courses", "course_id"),
    ("field_conflicts", "course_id"),
    ("course_audit_log", "course_id"),
    ("scholarships", "course_id"),
    ("course_offerings", "course_id"),
    ("course_id_aliases", "alias_course_id"),
    ("course_id_aliases", "canonical_course_id"),
    ("course_field_approvals", "course_id"),
}
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
URL_RE = re.compile(r"\bhttps?://", re.IGNORECASE)
SECRET_ASSIGNMENT_RE = re.compile(
    r"\b(?:password|secret|api[_ -]?key|access[_ -]?token|authorization)\b\s*[:=]",
    re.IGNORECASE,
)


class ExportRefused(RuntimeError):
    """The snapshot cannot be certified complete and safe for review."""


def _digest_route(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ExportRefused("course source route is not text")
    # Only the digest leaves this process. Credentials, signed query values,
    # fragments, and URL paths are not exported.
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_label(value: Any, label: str, *, max_length: int = 500) -> str | None:
    if value is None:
        return None
    if (not isinstance(value, str) or len(value) > max_length
            or EMAIL_RE.search(value) or URL_RE.search(value)
            or SECRET_ASSIGNMENT_RE.search(value)):
        raise ExportRefused(f"sensitive or malformed {label} cannot be exported")
    return value


def _json_safe_fee_variant(raw: Any, *, authority_verified: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "status": None, "selected": [],
            "validated_uniform_authority": False,
        }
    selected = raw.get("selected")
    safe_selected = []
    if isinstance(selected, list):
        for item in selected:
            if isinstance(item, dict):
                variant = item.get("study_variant")
                amount = item.get("amount")
                if (isinstance(amount, bool) or not isinstance(amount, (int, float))
                        or not math.isfinite(amount)):
                    amount = None
                year = item.get("year")
                if not isinstance(year, int) or isinstance(year, bool):
                    year = None
                safe_selected.append({
                    "study_variant": _safe_label(variant, "study variant"),
                    "campus": _safe_label(item.get("campus"), "fee option campus",
                                          max_length=200),
                    "amount": amount,
                    "currency": _safe_label(item.get("currency"), "fee option currency",
                                            max_length=3),
                    "year": year,
                    "period": _safe_label(item.get("period"), "fee option period",
                                          max_length=40),
                    "source_url": _digest_route(item.get("source_url")),
                })
    status = raw.get("status")
    return {
        "status": _safe_label(status, "fee evidence status", max_length=40),
        "selected": safe_selected,
        "validated_uniform_authority": authority_verified,
    }


def _json_safe_scope(raw: Any, route_hash: str | None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    locations = raw.get("locations")
    if isinstance(locations, list):
        locations = [_safe_label(value, "course location", max_length=200)
                     for value in locations]
    else:
        locations = None
    return {
        "original_name": _safe_label(raw.get("original_name"), "award name"),
        "locations": locations,
        "key": _safe_label(raw.get("key"), "campus scope key", max_length=128),
        "source_url": route_hash,
        "split_from_id": raw.get("split_from_id"),
    }


def _safe_count_key(value: Any) -> str:
    key = str(value or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", key):
        raise ExportRefused("field approval field key is outside the safe count-key format")
    return key


async def _candidate_evidence(conn: AsyncConnection) -> list[dict[str, Any]]:
    statement = text("""
        SELECT id, scrape_job_id, university_id, course_id, status, course_name,
               course_location, course_website, degree_level, study_mode,
               international_fee, fee_year, fee_term, currency, fee_scope_key,
               extraction_method->'campus_fee_scope' AS campus_fee_scope,
                extraction_method->'fee_variants' AS fee_variants,
                extraction_method->>'international_fee' AS fee_authority_method
          FROM scraped_courses
         WHERE status IN (:approved, :published)
           AND university_id = :university_id
           AND jsonb_typeof(extraction_method->'campus_fee_scope') = 'object'
           AND (extraction_method->'campus_fee_scope') ? 'original_name'
           AND (extraction_method->'campus_fee_scope') ? 'split_from_id'
         ORDER BY university_id, (extraction_method->'campus_fee_scope'->>'split_from_id'),
                  id
         LIMIT :row_limit
    """)
    result = await conn.execute(statement, {
        "approved": "approved",
        "published": "published",
        "university_id": TARGET_UNIVERSITY_ID,
        "row_limit": MAX_EVIDENCE_ROWS + 1,
    })
    rows = [dict(row) for row in result.mappings().all()]
    if len(rows) > MAX_EVIDENCE_ROWS:
        raise ExportRefused(f"matching staged evidence exceeds {MAX_EVIDENCE_ROWS} rows")

    output = []
    for row in rows:
        route_hash = _digest_route(row.get("course_website"))
        scope = _json_safe_scope(row.get("campus_fee_scope"), route_hash)
        from app.services.scraper.extractors.ulaw_fees import validated_fee_variants

        validation_row = dict(row)
        validation_row["extraction_method"] = {
            "international_fee": row.get("fee_authority_method"),
            "fee_variants": row.get("fee_variants"),
        }
        validated_authority = validated_fee_variants(validation_row)
        variants = _json_safe_fee_variant(
            row.get("fee_variants"),
            authority_verified=bool(
                validated_authority and validated_authority.get("status") == "uniform"
            ),
        )
        currency = _safe_label(row.get("currency"), "fee currency", max_length=3)
        if currency is not None and not re.fullmatch(r"[A-Z]{3}", currency):
            raise ExportRefused("fee currency is outside the safe three-letter format")
        output.append({
            "id": row["id"],
            "scrape_job_id": _safe_label(row["scrape_job_id"], "scrape job ID", max_length=128),
            "university_id": row["university_id"],
            "course_id": row["course_id"],
            "status": row["status"],
            "course_name": _safe_label(row["course_name"], "staged course name"),
            "course_location": _safe_label(row["course_location"], "staged course location"),
            "course_website": route_hash,
            "degree_level": _safe_label(row["degree_level"], "degree"),
            "study_mode": _safe_label(row["study_mode"], "study mode"),
            "international_fee": row["international_fee"],
            "fee_year": row["fee_year"],
            "fee_term": _safe_label(row["fee_term"], "fee term", max_length=40),
            "currency": currency,
            "fee_scope_key": row["fee_scope_key"],
            "extraction_method": {
                "campus_fee_scope": scope,
                "fee_variants": variants,
            },
        })
    return output


async def _courses(conn: AsyncConnection, course_ids: list[int]) -> dict[int, dict[str, Any]]:
    if not course_ids:
        return {}
    stmt = text("""
        SELECT id, university_id, name, course_website, degree_level, study_mode,
               course_location, status, approval_status,
               (offering_identity IS NOT NULL) AS has_offering_identity
          FROM courses
         WHERE id IN :course_ids
    """).bindparams(bindparam("course_ids", expanding=True))
    result = await conn.execute(stmt, {"course_ids": course_ids})
    rows = [dict(row) for row in result.mappings().all()]
    if len(rows) > MAX_COURSE_ROWS:
        raise ExportRefused(f"course row count exceeds {MAX_COURSE_ROWS}")
    output = {}
    for row in rows:
        row["name"] = _safe_label(row["name"], "published award name")
        row["degree_level"] = _safe_label(row["degree_level"], "published degree")
        row["study_mode"] = _safe_label(row["study_mode"], "published study mode")
        row["course_location"] = _safe_label(row["course_location"], "published location")
        row["course_website"] = _digest_route(row.get("course_website"))
        row["offering_identity"] = "present" if row.pop("has_offering_identity") else None
        output[row["id"]] = row
    return output


async def _field_approval_counts(conn: AsyncConnection, course_ids: list[int]) -> dict[int, dict[str, int]]:
    counts: dict[int, dict[str, int]] = defaultdict(dict)
    if not course_ids:
        return counts
    stmt = text("""
        SELECT course_id, field_key, COUNT(*) AS row_count
          FROM course_field_approvals
         WHERE course_id IN :course_ids
         GROUP BY course_id, field_key
    """).bindparams(bindparam("course_ids", expanding=True))
    result = await conn.execute(stmt, {"course_ids": course_ids})
    for row in result.mappings():
        key = _safe_count_key(row["field_key"])
        counts[row["course_id"]][key] = int(row["row_count"])
    return counts


async def _simple_counts(conn: AsyncConnection, table: str, column: str,
                        course_ids: list[int]) -> dict[int, int]:
    if not IDENTIFIER_RE.fullmatch(table) or not IDENTIFIER_RE.fullmatch(column):
        raise ExportRefused("unsafe SQL identifier encountered")
    if not course_ids:
        return {}
    # Identifiers are accepted only from fixed reviewed constants; all values
    # remain bound parameters via expanding IN.
    stmt = text(
        f"SELECT {column} AS course_id, COUNT(*) AS row_count "
        f"FROM {table} WHERE {column} IN :course_ids GROUP BY {column}"
    ).bindparams(bindparam("course_ids", expanding=True))
    result = await conn.execute(stmt, {"course_ids": course_ids})
    return {row["course_id"]: int(row["row_count"]) for row in result.mappings()}


def _safe_legacy_fee_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe_rows = []
    for row in sorted(rows, key=lambda item: item["id"]):
        amount = row.get("international_fee")
        try:
            amount = float(amount) if amount is not None else None
        except (TypeError, ValueError, OverflowError):
            amount = None
        if amount is not None and not math.isfinite(amount):
            amount = None
        safe_rows.append({
            "id": row["id"],
            "amount": amount,
            "currency": _safe_label(row.get("currency"), "legacy fee currency", max_length=3),
            "fee_term": _safe_label(row.get("fee_term"), "legacy fee term", max_length=40),
            "fee_year": row.get("fee_year"),
        })
    return safe_rows


async def _legacy_fee_rows(
    conn: AsyncConnection, course_ids: list[int],
) -> dict[int, list[dict[str, Any]]]:
    if not course_ids:
        return {}
    stmt = text("""
        SELECT id, course_id, international_fee, currency, fee_term, fee_year
          FROM fees
         WHERE course_id IN :course_ids
         ORDER BY course_id, id
    """).bindparams(bindparam("course_ids", expanding=True))
    rows = (await conn.execute(stmt, {"course_ids": course_ids})).mappings().all()
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[row["course_id"]].append(dict(row))
    return {
        course_id: _safe_legacy_fee_rows(fee_rows)
        for course_id, fee_rows in grouped.items()
    }


async def _alias_counts(conn: AsyncConnection, course_ids: list[int]) -> dict[int, int]:
    if not course_ids:
        return {}
    stmt = text("""
        SELECT related.course_id, COUNT(*) AS row_count
          FROM (
                SELECT alias_course_id AS course_id
                  FROM course_id_aliases
                 WHERE alias_course_id IN :course_ids
                UNION ALL
                SELECT canonical_course_id AS course_id
                  FROM course_id_aliases
                 WHERE canonical_course_id IN :course_ids
          ) AS related
         GROUP BY related.course_id
    """).bindparams(bindparam("course_ids", expanding=True))
    result = await conn.execute(stmt, {"course_ids": course_ids})
    return {row["course_id"]: int(row["row_count"]) for row in result.mappings()}


async def _course_fk_census(conn: AsyncConnection) -> list[tuple[str, str, str]]:
    stmt = text("""
        SELECT current_schema() AS target_schema,
               source_ns.nspname AS table_schema,
               source_table.relname AS table_name,
               source_att.attname AS column_name,
               constraint_row.conname AS constraint_name,
               cardinality(constraint_row.conkey) AS constraint_column_count
          FROM pg_catalog.pg_constraint AS constraint_row
          JOIN pg_catalog.pg_class AS target_table
            ON target_table.oid = constraint_row.confrelid
          JOIN pg_catalog.pg_namespace AS target_ns
            ON target_ns.oid = target_table.relnamespace
          JOIN pg_catalog.pg_class AS source_table
            ON source_table.oid = constraint_row.conrelid
          JOIN pg_catalog.pg_namespace AS source_ns
            ON source_ns.oid = source_table.relnamespace
          JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
               AS source_key(attnum, ordinality) ON TRUE
          JOIN LATERAL unnest(constraint_row.confkey) WITH ORDINALITY
               AS target_key(attnum, ordinality)
            ON target_key.ordinality = source_key.ordinality
          JOIN pg_catalog.pg_attribute AS source_att
            ON source_att.attrelid = source_table.oid
           AND source_att.attnum = source_key.attnum
          JOIN pg_catalog.pg_attribute AS target_att
            ON target_att.attrelid = target_table.oid
           AND target_att.attnum = target_key.attnum
         WHERE constraint_row.contype = 'f'
           AND target_ns.nspname = current_schema()
           AND target_table.relname = :target_table
           AND target_att.attname = :target_column
           AND source_ns.nspname NOT IN ('pg_catalog', 'information_schema')
         ORDER BY source_ns.nspname, source_table.relname,
                  constraint_row.conname, source_key.ordinality
    """)
    rows = [dict(row) for row in (await conn.execute(stmt, {
        "target_table": "courses", "target_column": "id",
    })).mappings()]
    if len(rows) > MAX_REFERENCE_CONSTRAINTS:
        raise ExportRefused("too many course foreign-key references to census safely")
    refs = []
    seen = set()
    for row in rows:
        table = row["table_name"]
        column = row["column_name"]
        if row["table_schema"] != row["target_schema"]:
            raise ExportRefused(
                f"external-schema FK reference requires compatibility review ({table})"
            )
        if row["constraint_column_count"] != 1:
            raise ExportRefused(
                f"composite FK referencing courses is not safely classified ({table})"
            )
        if (table, column) not in KNOWN_COURSE_FKS:
            raise ExportRefused(
                f"unknown FK reference to courses requires compatibility review ({table}.{column})"
            )
        ref = (row["table_schema"], table, column)
        if ref not in seen:
            seen.add(ref)
            refs.append(ref)
    return refs


async def _foreign_key_counts(
    conn: AsyncConnection, refs: list[tuple[str, str, str]], course_ids: list[int],
) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = defaultdict(dict)
    if not course_ids:
        return result
    preparer = conn.dialect.identifier_preparer
    for schema, table, column in refs:
        if not all(IDENTIFIER_RE.fullmatch(part) for part in (schema, table, column)):
            raise ExportRefused("unsafe metadata identifier in FK census")
        qualified_table = f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"
        qualified_column = preparer.quote(column)
        stmt = text(
            f"SELECT {qualified_column} AS course_id, COUNT(*) AS row_count "
            f"FROM {qualified_table} WHERE {qualified_column} IN :course_ids "
            f"GROUP BY {qualified_column}"
        ).bindparams(bindparam("course_ids", expanding=True))
        rows = (await conn.execute(stmt, {"course_ids": course_ids})).mappings()
        for row in rows:
            key = table
            result[row["course_id"]][key] = (
                result[row["course_id"]].get(key, 0) + int(row["row_count"])
            )
    return result


async def _outside_pathway_counts(
    conn: AsyncConnection,
    evidence_rows: list[dict[str, Any]],
    course_ids: list[int],
) -> dict[int, int]:
    """Count pathway relationships crossing a campus-split cohort boundary."""
    result: dict[int, int] = defaultdict(int)
    if not course_ids:
        return result
    memberships: dict[tuple[int, str, int], set[int]] = defaultdict(set)
    for row in evidence_rows:
        scope = row["extraction_method"]["campus_fee_scope"] or {}
        split_from = scope.get("split_from_id")
        cid = row.get("course_id")
        if isinstance(split_from, int) and not isinstance(split_from, bool) and isinstance(cid, int):
            memberships[(row["university_id"], row["scrape_job_id"], split_from)].add(cid)
    parent = {family: family for family in memberships}

    def find(family):
        while parent[family] != family:
            parent[family] = parent[parent[family]]
            family = parent[family]
        return family

    owner = {}
    for family, members in memberships.items():
        for course_id in members:
            key = (family[0], course_id)
            if key in owner:
                left, right = find(family), find(owner[key])
                if left != right:
                    parent[max(left, right)] = min(left, right)
            else:
                owner[key] = family
    course_group = {}
    for family, members in memberships.items():
        for course_id in members:
            course_group[course_id] = find(family)

    stmt = text("""
        SELECT id, source_course_id, target_course_id
          FROM course_pathways
         WHERE source_course_id IN :course_ids
            OR target_course_id IN :course_ids
    """).bindparams(bindparam("course_ids", expanding=True))
    rows = (await conn.execute(stmt, {
        "course_ids": course_ids,
    })).mappings()
    for row in rows:
        source, target = row["source_course_id"], row["target_course_id"]
        for member, counterpart in ((source, target), (target, source)):
            if member not in course_group:
                continue
            if course_group.get(counterpart) != course_group[member]:
                result[member] += 1
    return result


async def build_snapshot(conn: AsyncConnection) -> dict[str, Any]:
    evidence_rows = await _candidate_evidence(conn)
    if not evidence_rows:
        raise ExportRefused("no approved/published campus-scope evidence matched the query")
    group_keys = set()
    for row in evidence_rows:
        scope = row["extraction_method"]["campus_fee_scope"] or {}
        split_from = scope.get("split_from_id")
        if not isinstance(split_from, int) or isinstance(split_from, bool):
            raise ExportRefused("campus split lineage is malformed")
        group_keys.add((row["university_id"], row["scrape_job_id"], split_from))
    if len(group_keys) > MAX_SNAPSHOT_GROUPS:
        raise ExportRefused(f"group count exceeds {MAX_SNAPSHOT_GROUPS}")
    # Count overlap-connected logical groups independently from the 119
    # per-job families; split_from_id is not a logical-group identifier.
    family_ids = {family: set() for family in group_keys}
    for row in evidence_rows:
        scope = row["extraction_method"]["campus_fee_scope"] or {}
        family_ids[(row["university_id"], row["scrape_job_id"],
                    scope["split_from_id"])].add(row["course_id"])
    family_parent = {family: family for family in group_keys}

    def family_find(family):
        while family_parent[family] != family:
            family_parent[family] = family_parent[family_parent[family]]
            family = family_parent[family]
        return family

    course_owner = {}
    for family, members in family_ids.items():
        for course_id in members:
            key = (family[0], course_id)
            if key in course_owner:
                left, right = family_find(family), family_find(course_owner[key])
                if left != right:
                    family_parent[max(left, right)] = min(left, right)
            else:
                course_owner[key] = family
    logical_component_count = len({family_find(family) for family in group_keys})
    if len(evidence_rows) > MAX_EVIDENCE_ROWS:
        raise ExportRefused("evidence row bound exceeded")

    course_ids = sorted({
        row["course_id"] for row in evidence_rows
        if isinstance(row.get("course_id"), int) and not isinstance(row.get("course_id"), bool)
    })
    if len(course_ids) > MAX_COURSE_ROWS:
        raise ExportRefused(f"published course count exceeds {MAX_COURSE_ROWS}")

    courses = await _courses(conn, course_ids)
    legacy_fees = await _legacy_fee_rows(conn, course_ids)
    approvals = await _field_approval_counts(conn, course_ids)
    offerings = await _simple_counts(conn, "course_offerings", "course_id", course_ids)
    aliases = await _alias_counts(conn, course_ids)
    refs = await _course_fk_census(conn)
    fk_counts = await _foreign_key_counts(conn, refs, course_ids)
    outside = await _outside_pathway_counts(conn, evidence_rows, course_ids)

    missing = sorted(set(course_ids) - set(courses))
    if missing:
        raise ExportRefused("one or more published course rows are missing")

    for course_id, course in courses.items():
        course["field_approval_counts"] = dict(approvals.get(course_id, {}))
        course["reference_counts"] = dict(fk_counts.get(course_id, {}))
        course["outside_reference_count"] = int(outside.get(course_id, 0))
        course["existing_offering_count"] = int(offerings.get(course_id, 0))
        course["alias_count"] = int(aliases.get(course_id, 0))
        course["legacy_fee_rows"] = legacy_fees.get(course_id, [])

    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "reference_scan_complete": True,
        # External application-portal references are explicitly out of scope;
        # this flag is factual, not a blocker or a claim of a completed scan.
        "external_reference_scan_complete": False,
        "raw_family_count": len(group_keys),
        "logical_component_count": logical_component_count,
        "evidence_rows": evidence_rows,
        "courses": [courses[course_id] for course_id in sorted(courses)],
    }


async def read_snapshot(*, require_tls: bool = True) -> dict[str, Any]:
    # Import only configuration; the DSN is never printed, serialized, or
    # accepted on the command line. Prod must use certificate-validated TLS.
    from app.config import settings

    if not settings.database_url.startswith("postgresql+asyncpg://"):
        raise ExportRefused("configured database is not PostgreSQL/asyncpg")
    if require_tls and not settings.database_require_tls:
        raise ExportRefused("DATABASE_REQUIRE_TLS must be enabled for production export")
    connect_args = {"ssl": ssl.create_default_context()} if settings.database_require_tls else {}
    engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_size=1,
        max_overflow=0,
        connect_args=connect_args,
    )
    try:
        async with engine.connect() as conn:
            async with conn.begin():
                await conn.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                ))
                await conn.execute(text("SET LOCAL statement_timeout = '45000ms'"))
                return await build_snapshot(conn)
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-read-only-production",
        action="store_true",
        help="required acknowledgement; uses only DATABASE_URL from the environment",
    )
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.confirm_read_only_production:
        print("refused: pass --confirm-read-only-production after checking the configured target",
              file=sys.stderr)
        return 2
    try:
        snapshot = await read_snapshot()
        payload = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        if len(payload.encode("utf-8")) > MAX_OUTPUT_BYTES:
            raise ExportRefused(f"serialized snapshot exceeds {MAX_OUTPUT_BYTES} byte limit")
    except ExportRefused as exc:
        print(f"snapshot export refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # Driver/SQL errors can include connection details; do not leak them to
        # logs or terminal output.
        print("snapshot export failed; details suppressed to protect credentials and row data",
              file=sys.stderr)
        return 2
    sys.stdout.write(payload + "\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())