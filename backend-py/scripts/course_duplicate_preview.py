#!/usr/bin/env python3
"""Build an offline, read-only review manifest for legacy campus course duplicates.

The tool deliberately has no database connection or apply mode. It consumes a
bounded JSON export prepared by an operator and writes only a local review JSON
file. Never include secrets or personal data in the input export.

Snapshot contract:
  * top level: schema_version=1, reference_scan_complete,
    external_reference_scan_complete=false, evidence_rows[], courses[]
  * evidence rows: approved/published scraped_courses values including `id`,
    `course_id`, `university_id`, `scrape_job_id`, source/name/location/fee fields,
    `fee_scope_key`, and `extraction_method` with campus_fee_scope and
    fee_variants.selected[].study_variant
  * course rows: course identity/status and integer-only field_approval_counts,
    reference_counts, outside_reference_count, existing_offering_count, and
    alias_count. Counts must cover every known referencing FK and alias/offering
    table; certify reference_scan_complete only after that full scan.

The approved_law_legacy_mapping.json file controls logical IDs. Per-job split
families are connected only by overlapping course IDs. External portal
references are out of scope; old course IDs are retained and are never deleted.
The exporter is intentionally separate; no database query is run here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


SCHEMA_VERSION = 1
EXPECTED_RAW_FAMILIES = 119
EXPECTED_GROUPS = 102
EXPECTED_COURSE_IDS = 305
MAX_INPUT_BYTES = 32 * 1024 * 1024
MAX_EVIDENCE_ROWS = 20_000
MAX_COURSES = 10_000
SCOPE_KEY = "campus_fee_scope"
COUNT_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


class SnapshotError(ValueError):
    """The offline export is invalid or exceeds the inventory bounds."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise SnapshotError(f"{label} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"{label} must be an integer") from exc
    if result < 0:
        raise SnapshotError(f"{label} must not be negative")
    return result


def _fee_campus_binding(
    evidence: dict[str, Any],
) -> tuple[str | None, str | None, tuple[str, ...], str | None]:
    authority = (evidence.get("extraction_method") or {}).get("fee_variants") or {}
    selected = authority.get("selected")
    if not isinstance(selected, list) or not selected:
        return None, "missing selected study-variant evidence", (), None
    variants = [entry.get("study_variant") for entry in selected if isinstance(entry, dict)]
    if len(variants) != len(selected) or any(not isinstance(v, str) or not v.strip() for v in variants):
        return None, "ambiguous study-variant evidence", (), None
    scope = (evidence.get("extraction_method") or {}).get("campus_fee_scope") or {}
    locations = scope.get("locations")
    if (not isinstance(locations, list) or not locations
            or any(not isinstance(value, str) or not value.strip() for value in locations)):
        return None, "fee option cannot be bound to scoped campus evidence", (), None
    location_values = tuple(value.strip() for value in locations)
    location_keys = tuple(_norm(value) for value in location_values)
    if len(set(location_keys)) != len(location_keys):
        return None, "duplicate campus in scoped location evidence", (), None
    course_location = evidence.get("course_location")
    expected_location = ", ".join(location_values)
    if (not isinstance(course_location, str)
            or _norm(course_location) != _norm(expected_location)):
        return None, "staged campus is outside its authoritative scope locations", (), None
    campus_values = [entry.get("campus") for entry in selected if isinstance(entry, dict)]
    if len(campus_values) != len(selected) or any(
            not isinstance(campus, str) or not campus.strip() for campus in campus_values):
        return None, "selected fee option has invalid campus evidence", (), None
    campus_keys = {_norm(campus) for campus in campus_values}
    scope_keys = set(location_keys)
    row_route = evidence.get("course_website")
    fee = evidence.get("international_fee")
    exact_authority = (
        authority.get("status") == "uniform"
        and authority.get("validated_uniform_authority") is True
        and isinstance(row_route, str)
        and isinstance(fee, (int, float)) and not isinstance(fee, bool)
        and all(
            isinstance(entry, dict)
            and isinstance(entry.get("amount"), (int, float))
            and not isinstance(entry.get("amount"), bool)
            and entry.get("amount") == fee
            and entry.get("currency") == evidence.get("currency")
            and entry.get("year") == evidence.get("fee_year")
            and entry.get("period") == evidence.get("fee_term")
            and entry.get("source_url") == row_route
            for entry in selected
        )
    )
    regional_label = None
    if not campus_keys.issubset(scope_keys):
        # A regional label can bind to multiple concrete locations only when
        # the extraction scope explicitly lists those locations and the
        # selected authority is a validated uniform fee matching the stored
        # amount/cohort/route. The apply path revalidates raw options with
        # validated_fee_variants before any write.
        if len(campus_keys) != 1 or len(location_keys) < 2 or not exact_authority:
            return None, "selected fee option campus differs from scoped campus", (), None
        regional_label = next(iter(campus_keys))
    elif not scope_keys.issubset(campus_keys) or not exact_authority:
        return None, "selected fee option campus differs from scoped campus", (), None
    if len(set(variants)) != 1:
        return None, "multiple study variants in one source row", (), None
    return variants[0], None, location_values, regional_label


def _published_award_name_matches(name: Any, award: Any, locations: tuple[str, ...]) -> bool:
    if not isinstance(name, str) or not isinstance(award, str):
        return False
    accepted = {_norm(award), _norm(f"{award} — {', '.join(locations)}")}
    accepted.update(_norm(f"{award} — {location}") for location in locations)
    return _norm(name) in accepted


def _selected_study_variant(evidence: dict[str, Any]) -> tuple[str | None, str | None]:
    variant, error, _, _ = _fee_campus_binding(evidence)
    return variant, error


def _safe_fee(evidence: dict[str, Any]) -> dict[str, Any]:
    amount = evidence.get("international_fee")
    if (not isinstance(amount, (int, float)) or isinstance(amount, bool)
            or not math.isfinite(amount)):
        amount = None
    currency = evidence.get("currency")
    if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
        currency = None
    fee_year = evidence.get("fee_year")
    if not isinstance(fee_year, int) or isinstance(fee_year, bool) or not 1900 <= fee_year <= 2200:
        fee_year = None
    fee_term = evidence.get("fee_term")
    if (not isinstance(fee_term, str) or len(fee_term) > 40
            or not re.fullmatch(r"[A-Za-z0-9 .()/_-]*", fee_term)):
        fee_term = None
    return {
        "amount": amount,
        "currency": currency,
        "fee_year": fee_year,
        "fee_term": fee_term,
    }


def _legacy_fee_rows_match(course: dict[str, Any], staged: dict[str, Any]) -> bool:
    legacy_rows = course.get("legacy_fee_rows")
    if not isinstance(legacy_rows, list) or not legacy_rows:
        return False
    expected = _safe_fee(staged)
    if any(value is None for value in expected.values()):
        return False
    for legacy in legacy_rows:
        if not isinstance(legacy, dict):
            return False
        if ({
                "amount": legacy.get("amount"),
                "currency": legacy.get("currency"),
                "fee_year": legacy.get("fee_year"),
                "fee_term": legacy.get("fee_term"),
        } != expected):
            return False
    return True


def _row_hash(row: dict[str, Any]) -> str:
    # The source row itself is hashed, never copied to the review manifest.
    return _sha256(row)


def _source_route_hash(route: Any) -> str:
    """Accept either an offline raw route or the exporter’s redacted route digest."""
    if isinstance(route, str) and re.fullmatch(r"sha256:[0-9a-f]{64}", route):
        return route.removeprefix("sha256:")
    return _sha256(route)


def _safe_counts(value: Any) -> dict[str, int] | None:
    """Allow only table/field identifiers and integer counts into the manifest."""
    if not isinstance(value, dict):
        return None
    cleaned: dict[str, int] = {}
    for key, count in value.items():
        if (not isinstance(key, str) or not COUNT_KEY_RE.fullmatch(key)
                or not isinstance(count, int) or isinstance(count, bool) or count < 0):
            return None
        cleaned[key] = count
    return cleaned


def _scope_evidence(evidence: dict[str, Any]) -> dict[str, Any] | None:
    extraction = evidence.get("extraction_method")
    if not isinstance(extraction, dict):
        return None
    scope = extraction.get(SCOPE_KEY)
    return scope if isinstance(scope, dict) else None


def validate_approved_mapping(value: Any, *, expected_groups: int = EXPECTED_GROUPS,
                              expected_course_ids: int = EXPECTED_COURSE_IDS,
                              expected_aliases: int = 203) -> dict[str, Any]:
    """Validate the user-approved ID/source manifest, not a scrape-derived guess."""
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise SnapshotError("approved mapping must be schema_version 1 JSON")
    if value.get("university_id") != 92 or not isinstance(value.get("approval"), str):
        raise SnapshotError("approved mapping university/approval attestation is invalid")
    groups = value.get("groups")
    if not isinstance(groups, list) or len(groups) != expected_groups:
        raise SnapshotError(f"approved mapping must contain {expected_groups} logical groups")
    seen: set[int] = set()
    cleaned = []
    for raw in groups:
        if not isinstance(raw, dict):
            raise SnapshotError("approved mapping group must be an object")
        ids = raw.get("ids")
        if (not isinstance(ids, list) or len(ids) < 2
                or any(not isinstance(i, int) or isinstance(i, bool) or i < 1 for i in ids)
                or len(ids) != len(set(ids))):
            raise SnapshotError("approved IDs must be unique positive integers")
        if seen.intersection(ids):
            raise SnapshotError("approved groups overlap; each course ID must map exactly once")
        seen.update(ids)
        if raw.get("parent") != min(ids):
            raise SnapshotError("approved parent must be the lowest existing ID")
        if (not isinstance(raw.get("award"), str) or not raw["award"].strip()
                or not isinstance(raw.get("source"), str)
                or not re.fullmatch(r"https?://[^ ]+", raw["source"])):
            raise SnapshotError("approved group requires its exact award and source URL")
        cleaned.append({
            "parent": raw["parent"], "ids": sorted(ids),
            "award": raw["award"], "source": raw["source"],
        })
    if (len(seen) != expected_course_ids
            or sum(len(group["ids"]) - 1 for group in cleaned) != expected_aliases):
        raise SnapshotError(
            f"approved mapping must contain {expected_course_ids} IDs and {expected_aliases} aliases"
        )
    return {
        "schema_version": 1, "university_id": 92,
        "approval": value["approval"], "groups": cleaned,
    }


def _connected_components(rows: list[dict[str, Any]]) -> tuple[int, list[list[dict[str, Any]]]]:
    families: dict[tuple[int, str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scope = _scope_evidence(row) or {}
        split_id = scope.get("split_from_id")
        if (isinstance(split_id, int) and not isinstance(split_id, bool)
                and split_id > 0 and isinstance(row.get("university_id"), int)):
            job_id = row.get("scrape_job_id")
            families[(row["university_id"], job_id if isinstance(job_id, str) else "", split_id)].append(row)
    roots = {key: key for key in families}

    def find(key):
        while roots[key] != key:
            roots[key] = roots[roots[key]]
            key = roots[key]
        return key

    def union(left, right):
        left, right = find(left), find(right)
        if left != right:
            roots[max(left, right)] = min(left, right)

    owner: dict[tuple[int, int], tuple[int, str, int]] = {}
    for key, family_rows in families.items():
        for row in family_rows:
            cid = row.get("course_id")
            if isinstance(cid, int) and not isinstance(cid, bool):
                cid_key = (key[0], cid)
                if cid_key in owner:
                    union(key, owner[cid_key])
                else:
                    owner[cid_key] = key
    components: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for key, family_rows in families.items():
        components[find(key)].extend(family_rows)
    return len(families), list(components.values())


def _validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"snapshot schema_version must be {SCHEMA_VERSION}")
    evidence = snapshot.get("evidence_rows")
    courses = snapshot.get("courses")
    if not isinstance(evidence, list) or not isinstance(courses, list):
        raise SnapshotError("snapshot must contain evidence_rows and courses arrays")
    if snapshot.get("external_reference_scan_complete") is not False:
        raise SnapshotError("external application references are out of scope and must remain unscanned")
    if len(evidence) > MAX_EVIDENCE_ROWS or len(courses) > MAX_COURSES:
        raise SnapshotError("snapshot exceeds bounded row limits")
    if not all(isinstance(row, dict) for row in evidence + courses):
        raise SnapshotError("every evidence and course row must be an object")
    for key, rows in (("evidence_rows", evidence), ("courses", courses)):
        ids = [row.get("id") for row in rows]
        if any(not isinstance(i, int) or isinstance(i, bool) or i < 1 for i in ids):
            raise SnapshotError(f"{key} rows require positive integer IDs")
        if len(ids) != len(set(ids)):
            raise SnapshotError(f"{key} contains duplicate IDs")
    return snapshot


def _group_reason(rows: list[dict[str, Any]], courses_by_id: dict[int, dict[str, Any]],
                  snapshot: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Return safe member preview records and blocking reasons."""
    reasons: list[str] = []
    members: list[dict[str, Any]] = []
    identities: dict[str, set[Any]] = defaultdict(set)
    seen_locations: dict[str, int] = {}
    fee_by_location: dict[str, Any] = {}
    regional_scopes: dict[str, set[str]] = {}
    regional_fees: dict[str, set[str]] = defaultdict(set)
    course_ids: set[int] = set()

    for row in rows:
        scope = _scope_evidence(row) or {}
        variant, variant_error, scoped_locations, regional_label = _fee_campus_binding(row)
        if variant_error:
            reasons.append(variant_error)
        if not scoped_locations:
            reasons.append("ambiguous or non-unique location evidence")
            location = None
        else:
            location = str(row.get("course_location") or "").strip()
            location_key = _norm(location)
            if location_key in seen_locations:
                reasons.append(f"duplicate location evidence: {location}")
                if fee_by_location.get(location_key) != row.get("international_fee"):
                    reasons.append(f"conflicting fees for location: {location}")
            seen_locations[location_key] = row["id"]
            fee_by_location[location_key] = row.get("international_fee")
            if location_key not in {_norm(value) for value in scoped_locations}:
                reasons.append(f"location evidence does not match staged row {row['id']}")
            if regional_label:
                scope_locations = {_norm(value) for value in scoped_locations}
                previous_scope = regional_scopes.setdefault(regional_label, scope_locations)
                if previous_scope != scope_locations:
                    reasons.append(f"regional fee scope changed across rows for {regional_label}")
                regional_fees[regional_label].add(_canonical_json(_safe_fee(row)).decode())

        course_id = row.get("course_id")
        course = (courses_by_id.get(course_id)
                  if isinstance(course_id, int) and not isinstance(course_id, bool) else None)
        if course is None:
            reasons.append(f"published course row missing for staged row {row['id']}")
            continue
        course_ids.add(course_id)

        if row.get("status") not in {"approved", "published"}:
            reasons.append(f"staged row {row['id']} is not approved/published")
        if course.get("approval_status") != "approved":
            reasons.append(f"course {course_id} is not approved")
        if course.get("status") != "active":
            reasons.append(f"course {course_id} is not active/published")
        if row.get("university_id") != course.get("university_id"):
            reasons.append(f"university mismatch for course {course_id}")
        if _norm(course.get("course_location")) != _norm(row.get("course_location")):
            reasons.append(f"campus location mismatch for course {course_id}")

        source = row.get("course_website")
        if not source or scope.get("source_url") != source or course.get("course_website") != source:
            reasons.append(f"source route mismatch for course {course_id}")
        original_name = scope.get("original_name")
        if not isinstance(original_name, str) or not original_name.strip():
            reasons.append(f"missing original award name for staged row {row['id']}")
        elif row.get("course_name") != f"{original_name} — {row.get('course_location', '')}":
            reasons.append(f"ambiguous course name for staged row {row['id']}")

        fee_variants = (row.get("extraction_method") or {}).get("fee_variants") or {}
        identities["university_id"].add(_canonical_json(row.get("university_id")).decode())
        identities["source_route"].add(_canonical_json(source).decode())
        identities["original_name"].add(_canonical_json(original_name).decode())
        identities["degree_level"].add(_canonical_json(row.get("degree_level")).decode())
        identities["study_variant"].add(_canonical_json(variant).decode())
        identities["study_mode"].add(_canonical_json(row.get("study_mode")).decode())
        identities["fee_year"].add(_canonical_json(row.get("fee_year")).decode())
        identities["fee_term"].add(_canonical_json(row.get("fee_term")).decode())
        identities["currency"].add(_canonical_json(row.get("currency")).decode())
        for field_name, label in (("degree_level", "degree/award"),
                                  ("study_mode", "study mode")):
            if not isinstance(row.get(field_name), str) or not row[field_name].strip():
                reasons.append(f"missing {label} for staged row {row['id']}")
            elif _norm(course.get(field_name)) != _norm(row[field_name]):
                reasons.append(f"{label} mismatch for course {course_id}")
        if not isinstance(row.get("scrape_job_id"), str) or not row["scrape_job_id"].strip():
            reasons.append(f"missing scrape job for staged row {row['id']}")
        if not isinstance(row.get("study_mode"), str) or not row["study_mode"].strip():
            reasons.append(f"missing study-mode identity for staged row {row['id']}")
        if (isinstance(original_name, str) and _norm(course.get("name")) not in {
                _norm(original_name),
                _norm(f"{original_name} — {course.get('course_location', '')}"),
        }):
            reasons.append(f"ambiguous published award name for course {course_id}")

        # The source's validated cohort values must agree with the top-level
        # values used for the eventual offering. Missing values remain review
        # blockers rather than being inferred.
        if fee_variants.get("status") != "uniform":
            reasons.append(f"fee authority is not uniform for staged row {row['id']}")
        if row.get("international_fee") is None:
            reasons.append(f"missing source fee for staged row {row['id']}")
        if not row.get("fee_year") or not row.get("fee_term") or not row.get("currency"):
            reasons.append(f"incomplete fee cohort for staged row {row['id']}")
        if any(value is None for value in _safe_fee(row).values()):
            reasons.append(f"invalid source fee values for staged row {row['id']}")
        if row.get("fee_scope_key") != scope.get("key") or not scope.get("key"):
            reasons.append(f"campus scope key mismatch for staged row {row['id']}")

        ref_scan_complete = snapshot.get("reference_scan_complete") is True
        if not ref_scan_complete:
            reasons.append("foreign-key reference scan is not certified complete")
        reference_counts = _safe_counts(course.get("reference_counts"))
        if reference_counts is None:
            reasons.append(f"foreign-key counts missing for course {course_id}")
            reference_counts = {}
        field_approval_counts = _safe_counts(course.get("field_approval_counts"))
        if field_approval_counts is None:
            reasons.append(f"field approval counts missing for course {course_id}")
            field_approval_counts = {}
        outside_count = course.get("outside_reference_count")
        if not isinstance(outside_count, int) or isinstance(outside_count, bool):
            reasons.append(f"outside-reference count missing for course {course_id}")
            outside_count = None
        elif outside_count:
            reasons.append(f"course {course_id} has outside references")

        if course.get("offering_identity"):
            reasons.append(f"course {course_id} already has an offering identity")
        offering_count = course.get("existing_offering_count")
        if not isinstance(offering_count, int) or isinstance(offering_count, bool):
            reasons.append(f"existing-offering count missing for course {course_id}")
        elif offering_count:
            reasons.append(f"course {course_id} already has published offerings")
        alias_count = course.get("alias_count")
        if not isinstance(alias_count, int) or isinstance(alias_count, bool):
            reasons.append(f"alias count missing for course {course_id}")
        elif alias_count:
            reasons.append(f"course {course_id} already participates in an ID alias")
        members.append({
            "staged_row_id": row["id"],
            "course_id": course_id,
            "location": location,
            "source_fee": _safe_fee(row),
            "field_approval_counts": field_approval_counts,
            "foreign_key_reference_counts": reference_counts,
            "outside_reference_count": outside_count,
            "precondition_sha256": _row_hash(course),
            "evidence_precondition_sha256": _row_hash(row),
            "source_route_sha256": _source_route_hash(source),
        })

    for identity_name, values in identities.items():
        if len(values) != 1:
            reasons.append(f"ambiguous {identity_name} within split group")
    for regional_label, scoped in regional_scopes.items():
        if len(regional_fees[regional_label]) != 1:
            reasons.append(f"regional fee label {regional_label} has conflicting stored fee values")
    if len(course_ids) != len(rows):
        reasons.append("split group does not map one-to-one to distinct published course IDs")

    # Any existing course-level identity, alias, or offering collision can
    # invalidate grouping, even when it is present on a member not reached due
    # to incomplete evidence.
    unique_reasons = list(dict.fromkeys(reasons))
    return members, unique_reasons


def _component_reason(rows: list[dict[str, Any]], courses_by_id: dict[int, dict[str, Any]],
                      snapshot: dict[str, Any], approved: dict[str, Any]
                      ) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate repeated per-job evidence as one approved connected identity."""
    reasons: list[str] = []
    rows_by_course: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        cid = row.get("course_id")
        if isinstance(cid, int) and not isinstance(cid, bool):
            rows_by_course[cid].append(row)
    expected_ids = set(approved["ids"])
    if set(rows_by_course) != expected_ids:
        reasons.append("connected component IDs differ from approved JSON mapping")
    route_hash = "sha256:" + hashlib.sha256(approved["source"].encode("utf-8")).hexdigest()
    identities = set()
    seen_locations: dict[str, int] = {}
    regional_scopes: dict[str, set[str]] = {}
    regional_fees: dict[str, set[str]] = defaultdict(set)
    members = []

    for cid in approved["ids"]:
        course = courses_by_id.get(cid)
        evidence = sorted(rows_by_course.get(cid, []), key=lambda item: item["id"])
        if not evidence:
            reasons.append(f"approved course ID {cid} has no staged evidence")
            continue
        if course is None:
            reasons.append(f"approved course ID {cid} is missing")
            continue
        row_identities = set()
        row_locations = set()
        row_scope_sets = set()
        row_fees = set()
        evidence_rows = []
        for row in evidence:
            scope = _scope_evidence(row) or {}
            variant, variant_error, scoped_locations, regional_label = _fee_campus_binding(row)
            if variant_error:
                reasons.append(f"staged row {row['id']}: {variant_error}")
            if row.get("university_id") != approved.get("university_id"):
                reasons.append(f"university mismatch for staged row {row['id']}")
            if scope.get("original_name") != approved["award"]:
                reasons.append(f"award differs from approved JSON for staged row {row['id']}")
            if (row.get("course_website") != route_hash
                    or scope.get("source_url") != route_hash
                    or course.get("course_website") != route_hash):
                reasons.append(f"exact source URL differs from approved ID {cid}")
            if row.get("status") not in {"approved", "published"}:
                reasons.append(f"staged row {row['id']} is not approved/published")
            if not scoped_locations:
                reasons.append(f"staged row {row['id']} has ambiguous campus evidence")
                location = None
            else:
                location = str(row.get("course_location") or "").strip()
                scope_keys = tuple(_norm(value) for value in scoped_locations)
                row_scope_sets.add(scope_keys)
                row_locations.update(scope_keys)
                if regional_label:
                    scope_locations = set(scope_keys)
                    previous_scope = regional_scopes.setdefault(regional_label, scope_locations)
                    if previous_scope != scope_locations:
                        reasons.append(
                            f"regional fee scope changed across rows for {regional_label}"
                        )
                    regional_fees[regional_label].add(
                        _canonical_json(_safe_fee(row)).decode()
                    )
            accepted_staged_names = {
                _norm(f"{approved['award']} — {', '.join(scoped_locations)}"),
                *(_norm(f"{approved['award']} — {location}")
                  for location in scoped_locations),
            }
            if _norm(row.get("course_name")) not in accepted_staged_names:
                reasons.append(f"staged award name mismatch at row {row['id']}")
            if (course.get("status") != "active" or course.get("approval_status") != "approved"
                    or course.get("university_id") != approved.get("university_id")
                    or not _published_award_name_matches(
                        course.get("name"), approved["award"], scoped_locations)
                    or _norm(course.get("course_location")) != _norm(row.get("course_location"))
                    or _norm(course.get("degree_level")) != _norm(row.get("degree_level"))
                    or _norm(course.get("study_mode")) != _norm(row.get("study_mode"))):
                reasons.append(f"current Course properties mismatch for ID {cid}")
            if not all(isinstance(row.get(field), str) and row[field].strip()
                       for field in ("degree_level", "study_mode", "scrape_job_id")):
                reasons.append(f"staged row {row['id']} has incomplete identity fields")
            fee_authority = (row.get("extraction_method") or {}).get("fee_variants") or {}
            if fee_authority.get("status") != "uniform":
                reasons.append(f"staged row {row['id']} lacks uniform fee authority")
            fee = _safe_fee(row)
            if (any(value is None for value in fee.values())
                    or row.get("international_fee") is None
                    or not row.get("fee_year") or not row.get("fee_term") or not row.get("currency")):
                reasons.append(f"staged row {row['id']} has incomplete fee cohort")
            if (scope.get("key") != row.get("fee_scope_key") or not scope.get("key")
                    or not isinstance(scope.get("split_from_id"), int)):
                reasons.append(f"staged row {row['id']} has invalid campus_fee_scope")
            identity = (
                row.get("university_id"), row.get("course_website"), approved["award"],
                row.get("degree_level"), variant, row.get("study_mode"),
                row.get("fee_year"), row.get("fee_term"), row.get("currency"),
            )
            row_identities.add(identity)
            identities.add(identity)
            row_fees.add(_canonical_json(fee))
            evidence_rows.append({
                "staged_row_id": row["id"],
                "evidence_precondition_sha256": _row_hash(row),
                "scrape_job_id": row.get("scrape_job_id"),
                "split_from_id": scope.get("split_from_id"),
                "source_route_sha256": "sha256:" + hashlib.sha256(
                    approved["source"].encode()
                ).hexdigest(),
                "selected_for_offering": False,
            })
        if len(row_identities) != 1:
            reasons.append(f"cross-run identity/cohort conflict for course ID {cid}")
        if len(row_scope_sets) != 1 or len(row_fees) != 1:
            reasons.append(f"cross-run campus or fee conflict for course ID {cid}")
        if evidence and not _legacy_fee_rows_match(course, evidence[-1]):
            reasons.append(
                f"staged fee differs from, or lacks, the published legacy fee for course ID {cid}"
            )
        for location in row_locations:
            if location in seen_locations:
                reasons.append(f"campus is assigned to multiple approved IDs: {location}")
            seen_locations[location] = cid
        if course.get("offering_identity"):
            reasons.append(f"course {cid} already has offering_identity")
        if course.get("existing_offering_count") != 0:
            reasons.append(f"course {cid} already has published offerings")
        if course.get("alias_count") != 0:
            reasons.append(f"course {cid} already participates in an alias")
        if (course.get("outside_reference_count") != 0
                or _safe_counts(course.get("reference_counts")) is None
                or _safe_counts(course.get("field_approval_counts")) is None):
            reasons.append(f"course {cid} has incomplete/outside local FK safeguards")
        # Every listed observation is fingerprinted. The largest staged-row ID
        # is an explicit, deterministic representative only after all runs agree.
        representative = evidence[-1]
        for evidence_row in evidence_rows:
            evidence_row["selected_for_offering"] = (
                evidence_row["staged_row_id"] == representative["id"]
            )
        offering_locations = list(
            (_scope_evidence(representative) or {}).get("locations") or []
        )
        route_fingerprint = "sha256:" + hashlib.sha256(
            approved["source"].encode()
        ).hexdigest()
        source_fee = _safe_fee(representative)
        selected_evidence = next(
            evidence for evidence in evidence_rows
            if evidence["staged_row_id"] == representative["id"]
        )
        members.append({
            "course_id": cid,
            "staged_row_id": representative["id"],
            "location": representative.get("course_location"),
            "locations": offering_locations,
            "location_evidence": [
                {
                    "location": location,
                    "staged_row_id": representative["id"],
                    "evidence_precondition_sha256":
                        selected_evidence["evidence_precondition_sha256"],
                    "source_route_sha256": route_fingerprint,
                    "source_fee": source_fee,
                }
                for location in offering_locations
            ],
            "source_fee": source_fee,
            "precondition_sha256": _row_hash(course),
            "source_route_sha256": route_fingerprint,
            "evidence_rows": evidence_rows,
        })
    if len(identities) > 1:
        reasons.append("component rows differ in exact source/cohort/degree/variant/study mode")
    for regional_label, scoped in regional_scopes.items():
        if len(regional_fees[regional_label]) != 1:
            reasons.append(f"regional fee label {regional_label} has conflicting stored fee values")
    if snapshot.get("reference_scan_complete") is not True:
        reasons.append("local PostgreSQL FK census is incomplete")
    # external_reference_scan_complete remains false by design. This task
    # explicitly retains every old course ID; it does not claim portal coverage.
    return members, list(dict.fromkeys(reasons))


def _related_extra_ids(
    extra_rows: list[dict[str, Any]],
    approved_rows: list[dict[str, Any]],
    mapping_groups: list[dict[str, Any]],
) -> tuple[dict[int, set[int]], int]:
    """Find out-of-map IDs whose evidence can affect an approved component."""
    approved_by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in approved_rows:
        cid = row.get("course_id")
        if isinstance(cid, int) and not isinstance(cid, bool):
            approved_by_id[cid].append(row)

    conflicts: dict[int, set[int]] = defaultdict(set)
    unknown = 0
    for extra in extra_rows:
        extra_id = extra.get("course_id")
        if not isinstance(extra_id, int) or isinstance(extra_id, bool):
            unknown += 1
            continue
        extra_scope = _scope_evidence(extra) or {}
        extra_variant, _ = _selected_study_variant(extra)
        extra_route = extra.get("course_website")
        extra_award = _norm(extra_scope.get("original_name"))
        extra_degree = _norm(extra.get("degree_level"))
        extra_family = (
            extra.get("university_id"), extra.get("scrape_job_id"),
            extra_scope.get("split_from_id"),
        )
        extra_locations = {
            _norm(value) for value in (
                extra_scope.get("locations") if isinstance(extra_scope.get("locations"), list) else []
            ) if isinstance(value, str) and value.strip()
        }
        if isinstance(extra.get("course_location"), str):
            extra_locations.add(_norm(extra["course_location"]))
        for group in mapping_groups:
            members = [
                row for cid in group["ids"] for row in approved_by_id.get(cid, [])
            ]
            if not members:
                continue
            member_locations = {
                _norm(value) for row in members
                for value in (
                    (_scope_evidence(row) or {}).get("locations")
                    if isinstance((_scope_evidence(row) or {}).get("locations"), list)
                    else []
                )
                if isinstance(value, str) and value.strip()
            }
            member_locations.update(
                _norm(row["course_location"])
                for row in members if isinstance(row.get("course_location"), str)
            )
            related = False
            for row in members:
                scope = _scope_evidence(row) or {}
                variant, _ = _selected_study_variant(row)
                same_family = extra_family == (
                    row.get("university_id"), row.get("scrape_job_id"),
                    scope.get("split_from_id"),
                )
                same_route_award_degree = (
                    extra_route == row.get("course_website")
                    and extra_award == _norm(scope.get("original_name"))
                    and extra_degree == _norm(row.get("degree_level"))
                )
                identity_match = (
                    same_route_award_degree
                    and extra_variant is not None and variant is not None
                    and _norm(extra_variant) == _norm(variant)
                    and _norm(extra.get("study_mode")) == _norm(row.get("study_mode"))
                    and extra.get("fee_year") == row.get("fee_year")
                    and extra.get("fee_term") == row.get("fee_term")
                    and extra.get("currency") == row.get("currency")
                )
                route_award_campus_match = (
                    same_route_award_degree
                    and bool(extra_locations.intersection(member_locations))
                )
                if same_family or identity_match or route_award_campus_match:
                    related = True
                    break
            if related:
                conflicts[group["parent"]].add(extra_id)
    return conflicts, unknown


def build_review_manifest(snapshot: dict[str, Any], *,
                          approved_mapping: dict[str, Any],
                          approved_mapping_sha256: str,
                          expected_groups: int = EXPECTED_GROUPS,
                          expected_course_ids: int = EXPECTED_COURSE_IDS,
                          expected_families: int = EXPECTED_RAW_FAMILIES) -> dict[str, Any]:
    """Create a deterministic preview manifest; performs no I/O or database access."""
    snapshot = _validate_snapshot(snapshot)
    approved_mapping = validate_approved_mapping(
        approved_mapping, expected_groups=expected_groups,
        expected_course_ids=expected_course_ids,
        expected_aliases=expected_course_ids - expected_groups,
    )
    if not isinstance(approved_mapping_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", approved_mapping_sha256):
        raise SnapshotError("approved mapping requires its exact file SHA-256")
    courses_by_id = {row["id"]: row for row in snapshot["courses"]}
    ignored = 0
    all_candidate_rows = []
    for row in snapshot["evidence_rows"]:
        scope = _scope_evidence(row)
        if (row.get("status") not in {"approved", "published"} or not scope
                or not isinstance(scope.get("original_name"), str)
                or not isinstance(scope.get("split_from_id"), int)
                or isinstance(scope.get("split_from_id"), bool)
                or scope.get("split_from_id") < 1):
            ignored += 1
            continue
        if (not isinstance(row.get("university_id"), int)
                or isinstance(row.get("university_id"), bool)
                or row.get("university_id") != approved_mapping["university_id"]):
            ignored += 1
            continue
        candidate_row = dict(row)
        candidate_row["_split_from_id"] = scope["split_from_id"]
        all_candidate_rows.append(candidate_row)
    approved_ids = {cid for group in approved_mapping["groups"] for cid in group["ids"]}
    candidate_rows = [
        row for row in all_candidate_rows if row.get("course_id") in approved_ids
    ]
    extra_rows = [
        row for row in all_candidate_rows
        if not isinstance(row.get("course_id"), int)
        or isinstance(row.get("course_id"), bool)
        or row.get("course_id") not in approved_ids
    ]
    family_count, components = _connected_components(candidate_rows)
    exporter_family_count, exporter_components = _connected_components(all_candidate_rows)
    component_ids = [
        {row["course_id"] for row in component
         if isinstance(row.get("course_id"), int) and not isinstance(row.get("course_id"), bool)}
        for component in components
    ]
    observed_ids = set().union(*component_ids) if component_ids else set()
    extra_conflicts, unknown_extra_rows = _related_extra_ids(
        extra_rows, candidate_rows, approved_mapping["groups"],
    )
    coverage_ok = (
        family_count == expected_families
        and len(components) == expected_groups
        and len(observed_ids) == expected_course_ids
        and observed_ids == approved_ids
        and snapshot.get("raw_family_count", exporter_family_count) == exporter_family_count
        and snapshot.get("logical_component_count", len(exporter_components))
        == len(exporter_components)
    )
    groups = []
    for approved in approved_mapping["groups"]:
        target_ids = set(approved["ids"])
        matching = [
            component for component, ids in zip(components, component_ids)
            if ids.intersection(target_ids)
        ]
        rows = matching[0] if matching else []
        component_member_ids = {
            row["course_id"] for row in rows
            if isinstance(row.get("course_id"), int) and not isinstance(row.get("course_id"), bool)
        }
        members, reasons = _component_reason(rows, courses_by_id, snapshot, {
            **approved, "university_id": approved_mapping["university_id"],
        })
        if len(matching) != 1 or component_member_ids != target_ids:
            reasons.append("overlap-connected component differs from approved JSON ID group")
        if not coverage_ok:
            reasons.append(
                "approved-ID inventory must match 119 families / 102 components / 305 IDs"
            )
        if extra_conflicts.get(approved["parent"]):
            ids_text = ", ".join(str(value) for value in sorted(
                extra_conflicts[approved["parent"]]
            ))
            reasons.append(f"out-of-map course IDs may affect this approved group: {ids_text}")
        if unknown_extra_rows:
            reasons.append(
                "inventory has staged evidence with no course ID; cannot exclude it from approved groups"
            )
        member_ids = sorted(target_ids)
        canonical_id = approved["parent"]
        mapping = [
            {
                "old_course_id": m["course_id"],
                "canonical_course_id": approved["parent"],
                "offering_location": m["location"],
                "offering_locations": m["locations"],
                "source_fee": m["source_fee"],
            }
            for m in sorted(members, key=lambda item: item["course_id"])
        ]
        safe = not reasons and len(mapping) == len(target_ids)
        source_families = sorted({
            (row.get("scrape_job_id"), (scope := _scope_evidence(row) or {}).get("split_from_id"))
            for row in rows
            if isinstance((scope := _scope_evidence(row) or {}).get("split_from_id"), int)
        })
        source_families = [
            {"scrape_job_id": job_id, "split_from_id": split_id}
            for job_id, split_id in source_families
        ]
        groups.append({
            "university_id": approved_mapping["university_id"],
            "approved_award": approved["award"],
            "approved_source_sha256": hashlib.sha256(approved["source"].encode()).hexdigest(),
            "source_families": source_families,
            "member_count": len(member_ids),
            "evidence_row_count": sum(len(member.get("evidence_rows", [])) for member in members),
            "course_ids": member_ids,
            "proposed_canonical_course_id": canonical_id if safe else None,
            "proposed_mapping": mapping if safe else [],
            "members": members,
            "eligibility": "preview_candidate" if safe else "blocked",
            "blocking_reasons": reasons,
            "approval_required": True,
        })

    safe_count = sum(group["eligibility"] == "preview_candidate" for group in groups)
    safe_ids = sum(group["member_count"] for group in groups
                   if group["eligibility"] == "preview_candidate")
    manifest = {
        "manifest_version": 1,
        "mode": "read_only_preview",
        "generated_from_snapshot_sha256": _sha256(snapshot),
        "approval_required": True,
        "production_writes_performed": False,
        "apply_implemented": True,
        "manifest_digest_scope": "sha256 of canonical JSON object excluding manifest_sha256",
        "expected_scope": {
            "raw_families": expected_families,
            "groups": expected_groups,
            "course_ids": expected_course_ids,
        },
        "approved_mapping_sha256": approved_mapping_sha256,
        "external_reference_scope": "out_of_scope_preserve_original_course_ids",
        "observed_scope": {
            "raw_families": family_count,
            "groups": len(components),
            "exporter_raw_families": snapshot.get(
                "raw_family_count", exporter_family_count
            ),
            "exporter_logical_components": snapshot.get(
                "logical_component_count", len(exporter_components)
            ),
            "unique_course_ids": len(observed_ids),
            "extra_course_ids": len({
                row.get("course_id") for row in extra_rows
                if isinstance(row.get("course_id"), int) and not isinstance(row.get("course_id"), bool)
            }),
            "related_extra_course_ids": sorted({
                course_id for values in extra_conflicts.values() for course_id in values
            }),
            "unknown_extra_evidence_rows": unknown_extra_rows,
            "excluded_unrelated_course_ids": sorted({
                row.get("course_id") for row in extra_rows
                if isinstance(row.get("course_id"), int)
                and not isinstance(row.get("course_id"), bool)
                and all(row.get("course_id") not in values for values in extra_conflicts.values())
            }),
            "preview_candidate_groups": safe_count,
            "preview_candidate_course_ids": safe_ids,
            "blocked_groups": len(groups) - safe_count,
            "ignored_evidence_rows": ignored,
            "coverage_matches_approved_mapping": coverage_ok,
            "coverage_matches_expected": coverage_ok,
        },
        "reference_scan_complete": snapshot.get("reference_scan_complete") is True,
        "external_reference_scan_complete": snapshot.get("external_reference_scan_complete") is True,
        "groups": groups,
        "review_instructions": [
            "The approved JSON file is authoritative for the 102 logical groups, awards, URLs, and 305 IDs.",
            "The 119 per-job families are joined only by overlapping course IDs; award/source similarity never merges groups.",
            "Review every staged row fingerprint, source fee, campus, and cross-run cohort before applying.",
            "All original course IDs and course rows are retained; 203 aliases are compatibility mappings only.",
            "External application-portal references are explicitly out of scope and were not scanned or cleared.",
            "Final approval must bind this manifest digest, the approved mapping digest, reviewer, revision, scope, and preserve-ID out-of-scope policy.",
        ],
    }
    manifest["manifest_sha256"] = _sha256(manifest)
    return manifest


def load_snapshot(path: str | Path) -> dict[str, Any]:
    if str(path) == "-":
        payload = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(payload) > MAX_INPUT_BYTES:
            raise SnapshotError(f"input snapshot exceeds {MAX_INPUT_BYTES} byte limit")
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SnapshotError(f"input is not valid UTF-8 JSON: {exc}") from exc
        return _validate_snapshot(value)
    file_path = Path(path)
    size = file_path.stat().st_size
    if size > MAX_INPUT_BYTES:
        raise SnapshotError(f"input snapshot exceeds {MAX_INPUT_BYTES} byte limit")
    try:
        value = json.loads(file_path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"input is not valid UTF-8 JSON: {exc}") from exc
    return _validate_snapshot(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="bounded, operator-prepared JSON export")
    parser.add_argument(
        "--approved-mapping",
        default=str(Path(__file__).with_name("approved_law_legacy_mapping.json")),
        help="authoritative Task #629 mapping JSON (default: sibling approved mapping)",
    )
    parser.add_argument("--out", required=True, help="local path for the review manifest JSON")
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot)
        mapping_path = Path(args.approved_mapping)
        mapping_bytes = mapping_path.read_bytes()
        approved_mapping = json.loads(mapping_bytes.decode("utf-8"))
        manifest = build_review_manifest(
            snapshot, approved_mapping=approved_mapping,
            approved_mapping_sha256=hashlib.sha256(mapping_bytes).hexdigest(),
        )
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SnapshotError) as exc:
        print(f"course duplicate preview refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "manifest_sha256": manifest["manifest_sha256"],
        "groups": manifest["observed_scope"]["groups"],
        "course_ids": manifest["observed_scope"]["unique_course_ids"],
        "preview_candidate_groups": manifest["observed_scope"]["preview_candidate_groups"],
        "blocked_groups": manifest["observed_scope"]["blocked_groups"],
        "approval_required": True,
        "production_writes_performed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())