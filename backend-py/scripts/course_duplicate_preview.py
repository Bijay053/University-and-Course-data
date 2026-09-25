#!/usr/bin/env python3
"""Build an offline, read-only review manifest for legacy campus course duplicates.

The tool deliberately has no database connection or apply mode. It consumes a
bounded JSON export prepared by an operator and writes only a local review JSON
file. Never include secrets or personal data in the input export.

Snapshot contract:
  * top level: schema_version=1, reference_scan_complete,
    external_reference_scan_complete, evidence_rows[], courses[]
  * evidence rows: approved/published scraped_courses values including `id`,
    `course_id`, `university_id`, `scrape_job_id`, source/name/location/fee fields,
    `fee_scope_key`, and `extraction_method` with campus_fee_scope and
    fee_variants.selected[].study_variant
  * course rows: course identity/status and integer-only field_approval_counts,
    reference_counts, outside_reference_count, existing_offering_count, and
    alias_count. Counts must cover every known referencing FK and alias/offering
    table; certify reference_scan_complete only after that full scan.

The exporter is intentionally separate from this preview. No exporter or live
database query is run by this script, and an incomplete export blocks candidates.
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
EXPECTED_GROUPS = 119
EXPECTED_COURSE_IDS = 335
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


def _selected_study_variant(evidence: dict[str, Any]) -> tuple[str | None, str | None]:
    authority = (evidence.get("extraction_method") or {}).get("fee_variants") or {}
    selected = authority.get("selected")
    if not isinstance(selected, list) or not selected:
        return None, "missing selected study-variant evidence"
    variants = [entry.get("study_variant") for entry in selected if isinstance(entry, dict)]
    if len(variants) != len(selected) or any(not isinstance(v, str) or not v.strip() for v in variants):
        return None, "ambiguous study-variant evidence"
    if len(set(variants)) != 1:
        return None, "multiple study variants in one source row"
    return variants[0], None


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


def _validate_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict) or snapshot.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError(f"snapshot schema_version must be {SCHEMA_VERSION}")
    evidence = snapshot.get("evidence_rows")
    courses = snapshot.get("courses")
    if not isinstance(evidence, list) or not isinstance(courses, list):
        raise SnapshotError("snapshot must contain evidence_rows and courses arrays")
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
    course_ids: set[int] = set()

    for row in rows:
        scope = _scope_evidence(row) or {}
        variant, variant_error = _selected_study_variant(row)
        if variant_error:
            reasons.append(variant_error)
        location_values = scope.get("locations")
        if (not isinstance(location_values, list) or len(location_values) != 1
                or not isinstance(location_values[0], str) or not location_values[0].strip()):
            reasons.append("ambiguous or non-unique location evidence")
            location = None
        else:
            location = location_values[0].strip()
            location_key = _norm(location)
            if location_key in seen_locations:
                reasons.append(f"duplicate location evidence: {location}")
                if fee_by_location.get(location_key) != row.get("international_fee"):
                    reasons.append(f"conflicting fees for location: {location}")
            seen_locations[location_key] = row["id"]
            fee_by_location[location_key] = row.get("international_fee")
            if _norm(row.get("course_location")) != location_key:
                reasons.append(f"location evidence does not match staged row {row['id']}")

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
        identities["scrape_job_id"].add(_canonical_json(row.get("scrape_job_id")).decode())
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
        if isinstance(original_name, str) and _norm(course.get("name")) != _norm(original_name):
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
        if scope.get("split_from_id") != rows[0]["_split_from_id"]:
            reasons.append(f"split lineage mismatch for staged row {row['id']}")

        ref_scan_complete = snapshot.get("reference_scan_complete") is True
        if not ref_scan_complete:
            reasons.append("foreign-key reference scan is not certified complete")
        if snapshot.get("external_reference_scan_complete") is not True:
            reasons.append("external application/reference scan is not certified complete")
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
    if len(course_ids) != len(rows):
        reasons.append("split group does not map one-to-one to distinct published course IDs")

    # Any existing course-level identity, alias, or offering collision can
    # invalidate grouping, even when it is present on a member not reached due
    # to incomplete evidence.
    unique_reasons = list(dict.fromkeys(reasons))
    return members, unique_reasons


def build_review_manifest(snapshot: dict[str, Any], *,
                          expected_groups: int = EXPECTED_GROUPS,
                          expected_course_ids: int = EXPECTED_COURSE_IDS) -> dict[str, Any]:
    """Create a deterministic preview manifest; performs no I/O or database access."""
    snapshot = _validate_snapshot(snapshot)
    courses_by_id = {row["id"]: row for row in snapshot["courses"]}
    buckets: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    ignored = 0

    for row in snapshot["evidence_rows"]:
        scope = _scope_evidence(row)
        if (row.get("status") not in {"approved", "published"} or not scope
                or not isinstance(scope.get("original_name"), str)
                or not isinstance(scope.get("split_from_id"), int)
                or isinstance(scope.get("split_from_id"), bool)
                or scope.get("split_from_id") < 1):
            ignored += 1
            continue
        # Keep the private helper only in memory; it is never emitted.
        row = dict(row)
        row["_split_from_id"] = scope["split_from_id"]
        university_id = row.get("university_id")
        if not isinstance(university_id, int) or isinstance(university_id, bool):
            ignored += 1
            continue
        buckets[(university_id, scope["split_from_id"])].append(row)

    observed_ids = {
        row.get("course_id")
        for group in buckets.values() for row in group
        if isinstance(row.get("course_id"), int)
    }
    coverage_ok = (len(buckets) == expected_groups and len(observed_ids) == expected_course_ids)
    groups = []
    for (university_id, split_from_id), rows in sorted(buckets.items()):
        members, reasons = _group_reason(rows, courses_by_id, snapshot)
        if len(rows) < 2:
            reasons.append("split group has fewer than two published members")
        if not coverage_ok:
            reasons.append("snapshot coverage does not match required 119-group/335-ID cohort")

        member_ids = sorted({m["course_id"] for m in members})
        canonical_id = min(member_ids) if member_ids else None
        mapping = [
            {
                "old_course_id": m["course_id"],
                "canonical_course_id": canonical_id,
                "offering_location": m["location"],
                "source_fee": m["source_fee"],
            }
            for m in sorted(members, key=lambda item: item["course_id"])
        ]
        safe = not reasons and len(mapping) == len(rows)
        groups.append({
            "university_id": university_id,
            "split_from_id": split_from_id,
            "member_count": len(rows),
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
        "apply_implemented": False,
        "manifest_digest_scope": "sha256 of canonical JSON object excluding manifest_sha256",
        "expected_scope": {
            "groups": expected_groups,
            "course_ids": expected_course_ids,
        },
        "observed_scope": {
            "groups": len(buckets),
            "unique_course_ids": len(observed_ids),
            "preview_candidate_groups": safe_count,
            "preview_candidate_course_ids": safe_ids,
            "blocked_groups": len(groups) - safe_count,
            "ignored_evidence_rows": ignored,
            "coverage_matches_expected": coverage_ok,
        },
        "reference_scan_complete": snapshot.get("reference_scan_complete") is True,
        "external_reference_scan_complete": snapshot.get("external_reference_scan_complete") is True,
        "groups": groups,
        "review_instructions": [
            "Review every proposed mapping and source fee; this manifest does not approve any mapping.",
            "Canonical IDs are proposed as the smallest existing approved course ID in a fully validated group.",
            "No course ID is deleted or rewritten by this preview.",
            "External consumers are not covered by the local PostgreSQL FK census; an external-reference review is required.",
            "Do not create an apply command until alias/FK compatibility and the external application-portal contract are reviewed.",
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
    parser.add_argument("--out", required=True, help="local path for the review manifest JSON")
    args = parser.parse_args(argv)
    try:
        snapshot = load_snapshot(args.snapshot)
        manifest = build_review_manifest(snapshot)
        output = Path(args.out)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    except (OSError, SnapshotError) as exc:
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