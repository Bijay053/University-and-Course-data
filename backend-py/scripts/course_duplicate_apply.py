#!/usr/bin/env python3
"""Dry-run or explicitly apply the reviewed task #629 course-ID mapping.

This command never discovers mapping members. The approved JSON mapping is
authoritative for 102 logical components, 305 existing IDs, 203 aliases, award
names, and source URLs. The enriched manifest explicitly fingerprints every
staged row across at least the 119-family approved baseline. The live database is
consulted solely to verify those exact IDs and reject stale/conflicting evidence.
External application-portal references are explicitly out of scope; old course
IDs and course rows are preserved, without claiming an external scan completed.

See the module's CLI help for the command contract. Applying requires migration
389, exact manifest and mapping file SHA-256 values, approval revision, actor,
and exact database name as independent confirmations.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import re
import ssl
import sys
from types import SimpleNamespace
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import create_async_engine

from course_duplicate_snapshot_export import (
    _alias_counts,
    _course_fk_census,
    _field_approval_counts,
    _foreign_key_counts,
    _outside_pathway_counts,
    _simple_counts,
)
from course_duplicate_preview import (
    _identity_degree_level,
    _is_allowed_regional_fee_label,
    _regional_fee_kind,
    _slash_campus_tokens,
    validate_approved_mapping,
    SnapshotError,
)


EXPECTED_GROUPS = 102
EXPECTED_COURSE_IDS = 305
EXPECTED_ALIAS_COUNT = 203
MANIFEST_VERSION = 1
SCOPE = "campus_fee_scope"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")


class ApplyRefused(RuntimeError):
    """A reviewed mapping or live precondition is not safe to apply."""


class _RollbackCanary(Exception):
    def __init__(self, result, before):
        self.result = result
        self.before = before


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False, default=_json_default,
    ).encode("utf-8")


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"unsupported JSON value: {type(value).__name__}")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def _same_amount(left: Any, right: Any) -> bool:
    try:
        return Decimal(str(left)) == Decimal(str(right))
    except Exception:
        return False


def _published_award_name_matches(name: Any, award: Any, locations: Any) -> bool:
    if not isinstance(name, str) or not isinstance(award, str):
        return False
    if isinstance(locations, str):
        location_values = [value.strip() for value in locations.split(",") if value.strip()]
    elif isinstance(locations, list):
        location_values = [value for value in locations if isinstance(value, str)]
    else:
        location_values = []
    accepted = {_norm(award)}
    accepted.add(_norm(f"{award} — {', '.join(location_values)}"))
    accepted.update(_norm(f"{award} — {location}") for location in location_values)
    return _norm(name) in accepted


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ApplyRefused(f"{label} must be a positive integer")
    return value


def load_approved_manifest(path: Path, expected_file_sha: str,
                           expected_revision: str, mapping_path: Path,
                           expected_mapping_sha: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_file_sha):
        raise ApplyRefused("expected manifest SHA-256 must be 64 lowercase hex characters")
    if file_sha256(path) != expected_file_sha:
        raise ApplyRefused("manifest file SHA-256 does not match explicit confirmation")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_mapping_sha):
        raise ApplyRefused("approved mapping SHA-256 must be 64 lowercase hex characters")
    if file_sha256(mapping_path) != expected_mapping_sha:
        raise ApplyRefused("approved mapping file SHA-256 does not match explicit confirmation")
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mapping_value = json.loads(mapping_path.read_text(encoding="utf-8"))
        approved_mapping = validate_approved_mapping(mapping_value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyRefused(f"approved manifest is not readable JSON: {exc}") from exc
    except SnapshotError as exc:
        raise ApplyRefused(f"approved mapping is invalid: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != MANIFEST_VERSION:
        raise ApplyRefused("unsupported reviewed manifest version")
    if (manifest.get("mode") != "read_only_preview"
            or manifest.get("approval_required") is not True
            or manifest.get("production_writes_performed") is not False
            or manifest.get("reference_scan_complete") is not True):
        raise ApplyRefused("manifest must be a read-only review with a complete local FK scan")
    if manifest.get("external_reference_scope") != "out_of_scope_preserve_original_course_ids":
        raise ApplyRefused("external references require explicit out-of-scope/preserve-ID policy")
    if manifest.get("external_reference_scan_complete") is not False:
        raise ApplyRefused("manifest must not claim external application references were scanned")
    if manifest.get("approved_mapping_sha256") != expected_mapping_sha:
        raise ApplyRefused("manifest was not generated from the explicitly approved mapping file")
    if manifest.get("expected_scope") != {
            "raw_families": 119, "groups": 102, "course_ids": 305}:
        raise ApplyRefused("manifest expected scope is not the approved task #629 cohort")
    observed = manifest.get("observed_scope")
    if (not isinstance(observed, dict) or observed.get("groups") != EXPECTED_GROUPS
            or not isinstance(observed.get("raw_families"), int)
            or isinstance(observed.get("raw_families"), bool)
            or not 119 <= observed["raw_families"] <= 500
            or observed.get("unique_course_ids") != EXPECTED_COURSE_IDS
            or observed.get("coverage_matches_approved_mapping") is not True
            or observed.get("coverage_matches_expected") is not True):
        raise ApplyRefused(
            "review report does not certify a verified 119+ family inventory / "
            "102 components / 305 IDs"
        )
    approval = manifest.get("approval")
    if not isinstance(approval, dict) or approval.get("status") != "approved":
        raise ApplyRefused("manifest does not contain explicit reviewer approval")
    if approval.get("revision") != expected_revision or not expected_revision.strip():
        raise ApplyRefused("approval revision does not match explicit confirmation")
    if not isinstance(approval.get("approved_by"), str) or not approval["approved_by"].strip():
        raise ApplyRefused("approval must identify the reviewer")
    review_digest = manifest.get("manifest_sha256")
    if not isinstance(review_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", review_digest):
        raise ApplyRefused("manifest is missing the original review fingerprint")
    # Approval is an appended attestation; it must not alter the reviewed
    # snapshot or any of the exact proposed member IDs.
    review_payload = {key: value for key, value in manifest.items()
                      if key not in {"manifest_sha256", "approval"}}
    if sha256(review_payload) != review_digest:
        raise ApplyRefused("review manifest fingerprint is invalid")
    if (approval.get("approved_mapping_sha256") != expected_mapping_sha
            or approval.get("external_reference_scope")
            != "out_of_scope_preserve_original_course_ids"):
        raise ApplyRefused("approval does not bind the approved map and out-of-scope policy")
    groups = manifest.get("groups")
    if not isinstance(groups, list) or len(groups) != EXPECTED_GROUPS:
        raise ApplyRefused(f"manifest must contain exactly {EXPECTED_GROUPS} reviewed groups")
    expected_scope = {
        "groups": EXPECTED_GROUPS,
        "course_ids": EXPECTED_COURSE_IDS,
        "aliases": EXPECTED_ALIAS_COUNT,
    }
    if approval.get("approved_scope") != expected_scope:
        raise ApplyRefused("approval scope must explicitly certify 102 groups, 305 IDs, 203 aliases")

    approved_by_parent = {group["parent"]: group for group in approved_mapping["groups"]}
    all_courses: list[int] = []
    all_staged: list[int] = []
    seen_parents: set[int] = set()
    seen_raw_families: set[tuple[int, str, int]] = set()
    family_course_ids: dict[tuple[int, str, int], set[int]] = defaultdict(set)
    normalized_groups = []
    for group in groups:
        if not isinstance(group, dict) or group.get("eligibility") != "preview_candidate":
            raise ApplyRefused("every approved group must be an unblocked preview candidate")
        university_id = _integer(group.get("university_id"), "university_id")
        parent = _integer(group.get("proposed_canonical_course_id"), "canonical_course_id")
        source_group = approved_by_parent.get(parent)
        if source_group is None or parent in seen_parents or university_id != 92:
            raise ApplyRefused("manifest group is not a unique approved logical mapping")
        seen_parents.add(parent)
        members = group.get("members")
        mappings = group.get("proposed_mapping")
        ids = group.get("course_ids")
        if (not isinstance(members, list) or len(members) < 2
                or not isinstance(mappings, list) or len(mappings) != len(members)
                or not isinstance(ids, list)):
            raise ApplyRefused("each approved group needs exact member rows and an explicit mapping")
        if not all(isinstance(member, dict) for member in members):
            raise ApplyRefused("group members must be objects")
        if any(not isinstance(value, int) or isinstance(value, bool) for value in ids):
            raise ApplyRefused("group course_ids must be integer IDs")
        canonical_id = source_group["parent"]
        approved_source_sha256 = hashlib.sha256(
            source_group["source"].encode("utf-8")
        ).hexdigest()
        approved_route_fingerprint = "sha256:" + approved_source_sha256
        if (group.get("approved_award") != source_group["award"]
                or group.get("approved_source_sha256") != approved_source_sha256
                or canonical_id != min(source_group["ids"])
                or group.get("member_count") != len(members)
                or sorted(ids) != sorted(member.get("course_id") for member in members)
                or sorted(ids) != source_group["ids"] or len(ids) != len(set(ids))):
            raise ApplyRefused("group IDs/award/source differ from authoritative approved JSON")
        if group.get("proposed_canonical_course_id") != canonical_id:
            raise ApplyRefused("canonical ID differs from authoritative approved JSON")
        map_by_id = {}
        for mapping in mappings:
            if not isinstance(mapping, dict):
                raise ApplyRefused("mapping entries must be objects")
            old_id = _integer(mapping.get("old_course_id"), "old_course_id")
            if old_id in map_by_id:
                raise ApplyRefused("mapping contains duplicate old IDs")
            if mapping.get("canonical_course_id") != canonical_id:
                raise ApplyRefused("all IDs must map directly to the lowest existing course ID")
            map_by_id[old_id] = mapping
        member_ids = set()
        group_locations: set[str] = set()
        group_families: set[tuple[int, str, int]] = set()
        for member in members:
            course_id = _integer(member.get("course_id"), "member course_id")
            staged_id = _integer(member.get("staged_row_id"), "staged_row_id")
            if course_id in member_ids or course_id not in map_by_id:
                raise ApplyRefused("member IDs must map one-to-one to approved mapping rows")
            if map_by_id[course_id].get("offering_location") != member.get("location"):
                raise ApplyRefused("approved mapping campus differs from its reviewed member row")
            locations = member.get("locations")
            if (not isinstance(locations, list) or not locations
                    or any(not isinstance(location, str) or not location.strip()
                           for location in locations)
                    or len({_norm(location) for location in locations}) != len(locations)
                    or map_by_id[course_id].get("offering_locations") != locations):
                raise ApplyRefused("approved mapping does not explicitly bind every scoped campus")
            normalized_locations = {_norm(location) for location in locations}
            if group_locations.intersection(normalized_locations):
                raise ApplyRefused("two approved IDs claim the same offering campus")
            group_locations.update(normalized_locations)
            if member.get("source_route_sha256") != approved_route_fingerprint:
                raise ApplyRefused("member source route differs from authoritative approved JSON")
            if not isinstance(member.get("precondition_sha256"), str) or not re.fullmatch(
                    r"[0-9a-f]{64}", member["precondition_sha256"]):
                raise ApplyRefused("member is missing its live course fingerprint")
            evidence_rows = member.get("evidence_rows")
            if not isinstance(evidence_rows, list) or not evidence_rows:
                raise ApplyRefused("member must explicitly fingerprint every staged evidence row")
            selected_ids = []
            for evidence in evidence_rows:
                if not isinstance(evidence, dict):
                    raise ApplyRefused("staged evidence entries must be objects")
                evidence_id = _integer(evidence.get("staged_row_id"), "staged_row_id")
                split_id = _integer(evidence.get("split_from_id"), "split_from_id")
                job_id = evidence.get("scrape_job_id")
                if not isinstance(job_id, str) or not job_id.strip():
                    raise ApplyRefused("evidence family must identify its scrape job")
                family_key = (university_id, job_id, split_id)
                group_families.add(family_key)
                family_course_ids.setdefault(family_key, set()).add(course_id)
                evidence_hash = evidence.get("evidence_precondition_sha256")
                if not isinstance(evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
                    raise ApplyRefused("staged evidence row is missing its fingerprint")
                if evidence.get("source_route_sha256") != approved_route_fingerprint:
                    raise ApplyRefused("staged evidence source differs from approved JSON")
                if evidence.get("selected_for_offering") is True:
                    selected_ids.append(evidence_id)
                all_staged.append(evidence_id)
            if (len(selected_ids) != 1 or selected_ids[0] != staged_id
                    or len({entry["staged_row_id"] for entry in evidence_rows}) != len(evidence_rows)):
                raise ApplyRefused("exactly one listed evidence row must be selected per course")
            selected_evidence = next(
                entry for entry in evidence_rows
                if entry.get("staged_row_id") == staged_id
                and entry.get("selected_for_offering") is True
            )
            location_evidence = member.get("location_evidence")
            expected_location_evidence = [
                {
                    "location": location,
                    "staged_row_id": staged_id,
                    "evidence_precondition_sha256":
                        selected_evidence["evidence_precondition_sha256"],
                    "source_route_sha256": approved_route_fingerprint,
                    "source_fee": map_by_id[course_id].get("source_fee"),
                }
                for location in locations
            ]
            if location_evidence != expected_location_evidence:
                raise ApplyRefused("each campus must bind to its exact validated staged fee evidence")
            member_ids.add(course_id)
            all_courses.append(course_id)
        manifest_families = group.get("source_families")
        expected_families = [
            {"scrape_job_id": job_id, "split_from_id": split_id}
            for _, job_id, split_id in sorted(group_families)
        ]
        if manifest_families != expected_families:
            raise ApplyRefused("group per-job family inventory differs from explicit evidence rows")
        if seen_raw_families.intersection(group_families):
            raise ApplyRefused("one raw per-job family is split across approved logical groups")
        seen_raw_families.update(group_families)
        if member_ids != set(map_by_id) or set(ids) != member_ids:
            raise ApplyRefused("mapping, course_ids, and member IDs do not match exactly")
        if not isinstance(group.get("blocking_reasons"), list) or group["blocking_reasons"]:
            raise ApplyRefused("approved group has blocking reasons")
        normalized_groups.append(group)
    family_signatures = {
        tuple(sorted(course_ids)) for course_ids in family_course_ids.values()
    }
    approved_parent_by_id = {
        course_id: group["parent"]
        for group in approved_mapping["groups"] for course_id in group["ids"]
    }
    if (any(
                not signature
                or any(course_id not in approved_parent_by_id for course_id in signature)
                or len({approved_parent_by_id[course_id] for course_id in signature}) != 1
                for signature in family_signatures
            )
            or observed.get("approved_family_signature_count") != len(family_signatures)
            or observed.get("additional_approved_id_families")
            != max(0, observed["raw_families"] - 119)):
        raise ApplyRefused(
            "excess per-job families must contain only approved IDs from one mapped component"
        )
    if (len(seen_parents) != EXPECTED_GROUPS
            or len(set(all_courses)) != EXPECTED_COURSE_IDS
            or len(seen_raw_families) != observed["raw_families"]
            or len(all_staged) < EXPECTED_COURSE_IDS
            or len(set(all_staged)) != len(all_staged)):
        raise ApplyRefused("manifest must identify all 305 IDs and every unique staged evidence row")
    return {
        "manifest": manifest,
        "groups": normalized_groups,
        "course_ids": sorted(all_courses),
        "staged_ids": sorted(all_staged),
        "file_sha256": expected_file_sha,
        "review_sha256": review_digest,
        "approval": approval,
        "approved_mapping": approved_mapping,
        "mapping_file_sha256": expected_mapping_sha,
    }


def _evidence_projection(row: dict[str, Any]) -> dict[str, Any]:
    from course_duplicate_snapshot_export import (
        _digest_route, _json_safe_fee_variant, _json_safe_scope, _safe_label,
    )
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants

    route_hash = _digest_route(row.get("course_website"))
    scope = _json_safe_scope((row.get("extraction_method") or {}).get(SCOPE), route_hash)
    validated_authority = validated_fee_variants(row)
    variants = _json_safe_fee_variant(
        (row.get("extraction_method") or {}).get("fee_variants"),
        authority_verified=bool(
            validated_authority and validated_authority.get("status") == "uniform"
        ),
    )
    currency = _safe_label(row.get("currency"), "fee currency", max_length=3)
    projected = {
        "id": row.get("id"),
        "scrape_job_id": _safe_label(row.get("scrape_job_id"), "scrape job ID", max_length=128),
        "university_id": row.get("university_id"),
        "course_id": row.get("course_id"),
        "status": row.get("status"),
        "course_name": _safe_label(row.get("course_name"), "staged course name"),
        "course_location": _safe_label(row.get("course_location"), "staged course location"),
        "course_website": route_hash,
        "degree_level": _safe_label(row.get("degree_level"), "degree"),
        "study_mode": _safe_label(row.get("study_mode"), "study mode"),
        "international_fee": row.get("international_fee"),
        "fee_year": row.get("fee_year"),
        "fee_term": _safe_label(row.get("fee_term"), "fee term", max_length=40),
        "currency": currency,
        "fee_scope_key": row.get("fee_scope_key"),
        "extraction_method": {"campus_fee_scope": scope, "fee_variants": variants},
    }
    projected["_split_from_id"] = (
        (row.get("extraction_method") or {}).get(SCOPE, {}).get("split_from_id")
    )
    return projected


def _course_projection(row: dict[str, Any], *, field_counts: dict[str, int],
                       reference_counts: dict[str, int], outside: int,
                       offerings: int, aliases: int,
                       legacy_fee_rows: list[dict[str, Any]]) -> dict[str, Any]:
    from course_duplicate_snapshot_export import _digest_route, _safe_label
    return {
        "id": row["id"],
        "university_id": row["university_id"],
        "name": _safe_label(row["name"], "published award name"),
        "course_website": _digest_route(row.get("course_website")),
        "degree_level": _safe_label(row.get("degree_level"), "published degree"),
        "study_mode": _safe_label(row.get("study_mode"), "published study mode"),
        "course_location": _safe_label(row.get("course_location"), "published location"),
        "status": row["status"],
        "approval_status": row["approval_status"],
        "offering_identity": "present" if row.get("offering_identity") else None,
        "field_approval_counts": field_counts,
        "reference_counts": reference_counts,
        "outside_reference_count": outside,
        "existing_offering_count": offerings,
        "alias_count": aliases,
        "legacy_fee_rows": legacy_fee_rows,
    }


def _group_evidence(group: dict[str, Any], rows: dict[int, dict[str, Any]]) -> tuple[list[dict], tuple]:
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants

    identities = set()
    locations_by_course = {}
    fees_by_course = {}
    regional_scopes: dict[str, set[str]] = {}
    regional_fees: dict[str, set[tuple]] = defaultdict(set)
    slash_alias_checks: list[tuple[frozenset[str], set[str], tuple]] = []
    scoped_fee_rows: list[tuple[set[str], tuple]] = []
    members_by_course = {member["course_id"]: member for member in group["members"]}
    representatives = []
    approved_route = "sha256:" + group["approved_source_sha256"]
    for member in group["members"]:
        all_evidence = member.get("evidence_rows", [])
        selected_rows = []
        for evidence in all_evidence:
            row = rows.get(evidence["staged_row_id"])
            if row is None:
                raise ApplyRefused("an explicitly approved staged evidence row no longer exists")
            projection = _evidence_projection(row)
            if sha256(projection) != evidence["evidence_precondition_sha256"]:
                raise ApplyRefused(f"staged row {row['id']} changed after review")
            scope = (row.get("extraction_method") or {}).get(SCOPE)
            authority = validated_fee_variants(row)
            if not isinstance(scope, dict) or not authority or authority.get("status") != "uniform":
                raise ApplyRefused(f"staged row {row['id']} has no current validated uniform fee authority")
            selected = authority.get("selected")
            if (not isinstance(selected, list) or not selected
                    or any(not isinstance(item, dict) for item in selected)):
                raise ApplyRefused(f"staged row {row['id']} has malformed validated fee options")
            variants = {item.get("study_variant") for item in selected}
            if len(variants) != 1 or not next(iter(variants), None):
                raise ApplyRefused(f"staged row {row['id']} has ambiguous fee study-variant evidence")
            source_url = row.get("course_website")
            scope_source_url = scope.get("source_url")
            source_fingerprint = (
                "sha256:" + hashlib.sha256(source_url.encode("utf-8")).hexdigest()
                if isinstance(source_url, str) else None
            )
            scope_locations = scope.get("locations")
            if (not isinstance(scope_locations, list) or not scope_locations
                    or any(not isinstance(value, str) or not value.strip()
                           for value in scope_locations)):
                raise ApplyRefused(f"staged row {row['id']} has invalid campus scope")
            location = str(row.get("course_location") or "").strip()
            scope_location_keys = {_norm(value) for value in scope_locations}
            if (not location or location.casefold() == "none"
                    or len(scope_location_keys) != len(scope_locations)
                    or _norm(location) != _norm(", ".join(scope_locations))):
                raise ApplyRefused(
                    f"staged row {row['id']} course_location does not exactly join scoped locations"
                )
            selected_campuses = [item.get("campus") for item in selected]
            if (len(selected_campuses) != len(selected)
                    or any(not isinstance(campus, str) or not campus.strip()
                           for campus in selected_campuses)):
                raise ApplyRefused(
                    f"staged row {row['id']} has invalid selected fee-option campus"
                )
            campus_keys = {_norm(campus) for campus in selected_campuses}
            label = next(iter(campus_keys), "") if len(campus_keys) == 1 else ""
            slash_tokens = _slash_campus_tokens(label)
            exact_authority = (
                authority.get("status") == "uniform"
                and isinstance(source_url, str)
                and all(
                    _same_amount(item.get("amount"), row.get("international_fee"))
                    and item.get("currency") == row.get("currency")
                    and item.get("year") == row.get("fee_year")
                    and item.get("period") == row.get("fee_term")
                    and item.get("source_url") == source_url
                    for item in selected
                )
            )
            regional_label = None
            if slash_tokens is not None:
                if (not slash_tokens or len(campus_keys) != 1
                        or not scope_location_keys.issubset(slash_tokens)
                        or not exact_authority):
                    raise ApplyRefused(
                        f"staged row {row['id']} slash campus label does not match scoped fee authority"
                    )
                regional_label = label
            elif _regional_fee_kind(label) is not None:
                if (not _is_allowed_regional_fee_label(label, tuple(scope_locations))
                        or not exact_authority):
                    raise ApplyRefused(
                        f"staged row {row['id']} regional fee label does not match scoped campuses"
                    )
                regional_label = label
            elif not campus_keys.issubset(scope_location_keys):
                raise ApplyRefused(
                    f"staged row {row['id']} selected fee option is not bound to scoped campuses"
                )
            elif not scope_location_keys.issubset(campus_keys) or not exact_authority:
                raise ApplyRefused(
                    f"staged row {row['id']} selected fee options do not cover every scoped campus"
                )
            accepted_staged_names = {
                _norm(f"{group['approved_award']} — {', '.join(scope_locations)}"),
                *(_norm(f"{group['approved_award']} — {campus}")
                  for campus in scope_locations),
            }
            if (row.get("status") not in {"approved", "published"}
                    or row.get("course_id") != member["course_id"]
                    or row.get("university_id") != group["university_id"]
                    or row.get("course_location") != location
                    or scope.get("original_name") != group["approved_award"]
                    or _norm(row.get("course_name")) not in accepted_staged_names
                    or not isinstance(source_url, str)
                    or source_url != scope_source_url
                    or source_fingerprint != approved_route
                    or row.get("scrape_job_id") != evidence.get("scrape_job_id")
                    or scope.get("split_from_id") != evidence.get("split_from_id")
                    or row.get("fee_scope_key") != scope.get("key")
                    or not scope.get("key")):
                raise ApplyRefused(f"staged row {row['id']} no longer matches approved campus evidence")
            if not all(isinstance(row.get(field), str) and row[field].strip()
                       for field in ("course_website", "degree_level", "study_mode", "scrape_job_id")):
                raise ApplyRefused(f"staged row {row['id']} has incomplete offering identity")
            if (row.get("international_fee") is None or not row.get("fee_year")
                    or not row.get("fee_term") or not row.get("currency")):
                raise ApplyRefused(f"staged row {row['id']} has an incomplete fee cohort")
            identity = (
                row["university_id"], row["course_website"], _norm(scope.get("original_name")),
                _identity_degree_level(row["degree_level"], scope.get("original_name")),
                next(iter(variants)), _norm(row["study_mode"]),
                row["fee_year"], row["fee_term"], row["currency"],
            )
            identities.add(identity)
            locations_by_course.setdefault(member["course_id"], set()).add(_norm(location))
            fee_key = (
                row["international_fee"], row["fee_year"], row["fee_term"], row["currency"],
            )
            scoped_fee_rows.append((set(scope_location_keys), fee_key))
            fees_by_course.setdefault(member["course_id"], set()).add(fee_key)
            if regional_label:
                regional_scopes.setdefault(regional_label, set()).update(scope_location_keys)
                regional_fees[regional_label].add(fee_key)
            if slash_tokens is not None:
                slash_alias_checks.append((slash_tokens, set(scope_location_keys), fee_key))
            if evidence.get("selected_for_offering") is True:
                selected_rows.append(row)
        if len(selected_rows) != 1 or selected_rows[0]["id"] != member["staged_row_id"]:
            raise ApplyRefused("selected staged row differs from reviewed evidence")
        representatives.append(selected_rows[0])
    if len(identities) != 1 or set(members_by_course) != {
            row.get("course_id") for row in representatives}:
        raise ApplyRefused("award, URL, degree/variant, study mode, and fee cohort must be identical")
    if any(len(value) != 1 for value in locations_by_course.values()):
        raise ApplyRefused("campus location changed across scrape runs")
    if any(len(value) != 1 for value in fees_by_course.values()):
        raise ApplyRefused("fee amount/cohort conflicts across scrape runs")
    for tokens, scoped_here, fee_key in slash_alias_checks:
        for token in tokens - scoped_here:
            if not any(token in other_scope and other_fee == fee_key
                       for other_scope, other_fee in scoped_fee_rows):
                raise ApplyRefused(
                    "slash campus label includes a city not scoped at the same verified fee"
                )
    for regional_label, scope_locations in regional_scopes.items():
        if len(regional_fees[regional_label]) != 1:
            raise ApplyRefused(
                f"regional fee label {regional_label} has conflicting verified fee values"
            )
    representative_campuses = []
    for row in representatives:
        member = members_by_course[row["course_id"]]
        scope_locations = (
            (row.get("extraction_method") or {}).get(SCOPE) or {}
        ).get("locations")
        if scope_locations != member.get("locations"):
            raise ApplyRefused("reviewed campus list differs from validated source scope")
        representative_campuses.extend(_norm(location) for location in scope_locations)
    if len(set(representative_campuses)) != len(representative_campuses):
        raise ApplyRefused("approved IDs claim a duplicate offering campus")
    return sorted(representatives, key=lambda row: _norm(row["course_location"])), next(iter(identities))


async def _fetch_rows(conn, table: str, ids: list[int], id_column: str = "id",
                      *, lock: bool = False) -> list[dict]:
    if not IDENTIFIER_RE.fullmatch(table) or not IDENTIFIER_RE.fullmatch(id_column):
        raise ApplyRefused("unsafe database identifier")
    if not ids:
        return []
    stmt = text(f'SELECT * FROM "{table}" WHERE "{id_column}" IN :ids ORDER BY "{id_column}"'
                + (" FOR UPDATE" if lock else "")
                ).bindparams(bindparam("ids", expanding=True))
    return [dict(row) for row in (await conn.execute(stmt, {"ids": ids})).mappings()]


async def _conflicting_evidence(conn, groups, approved_staged_ids: set[int],
                                approved_course_ids: set[int]) -> None:
    """Reject additional linked rows for an approved award across scrape jobs."""
    universities = sorted({group["university_id"] for group in groups})
    # Source routes are intentionally exact in the approved evidence itself.
    exact_routes: dict[int, set[str]] = defaultdict(set)
    staged_rows = await _fetch_rows(
        conn, "scraped_courses", sorted(approved_staged_ids)
    )
    for row in staged_rows:
        exact_routes[row["university_id"]].add(row.get("course_website"))
    for university_id in universities:
        route_values = sorted(route for route in exact_routes[university_id] if route)
        if not route_values:
            raise ApplyRefused("approved source URL is missing from live evidence")
        approved_uni_ids = sorted({
            course_id for group in groups if group["university_id"] == university_id
            for course_id in group["course_ids"]
        })
        stmt = text("""
            SELECT * FROM scraped_courses
             WHERE university_id = :university_id
               AND status IN ('approved', 'published')
               AND jsonb_typeof(extraction_method->'campus_fee_scope') = 'object'
               AND (
                    course_website IN :routes
                    OR course_id IN :approved_course_ids
               )
        """).bindparams(
            bindparam("routes", expanding=True),
            bindparam("approved_course_ids", expanding=True),
        )
        candidates = (await conn.execute(stmt, {
            "university_id": university_id, "routes": route_values,
            "approved_course_ids": approved_uni_ids,
        })).mappings().all()
        approved_for_uni = {
            row["id"] for row in staged_rows if row["university_id"] == university_id
        }
        for candidate in candidates:
            candidate = dict(candidate)
            if candidate["id"] in approved_for_uni:
                continue
            if candidate.get("course_id") in approved_course_ids:
                raise ApplyRefused(
                    "new or unreviewed staged row exists for an approved old course ID"
                )
            scope = (candidate.get("extraction_method") or {}).get(SCOPE) or {}
            if not isinstance(scope, dict):
                continue
            for group in groups:
                if group["university_id"] != university_id:
                    continue
                member_rows = [r for r in staged_rows
                               if r["id"] in {m["staged_row_id"] for m in group["members"]}]
                if not member_rows:
                    continue
                sample = member_rows[0]
                sample_scope = (sample.get("extraction_method") or {}).get(SCOPE) or {}
                candidate_selected = (
                    (candidate.get("extraction_method") or {}).get("fee_variants") or {}
                ).get("selected") or []
                sample_selected = (
                    (sample.get("extraction_method") or {}).get("fee_variants") or {}
                ).get("selected") or []
                candidate_variants = {
                    entry.get("study_variant") for entry in candidate_selected
                    if isinstance(entry, dict)
                }
                sample_variants = {
                    entry.get("study_variant") for entry in sample_selected
                    if isinstance(entry, dict)
                }
                candidate_scope_locations = scope.get("locations")
                candidate_locations = {
                    _norm(value) for value in (
                        candidate_scope_locations if isinstance(candidate_scope_locations, list) else []
                    )
                    if isinstance(value, str) and value.strip()
                }
                if isinstance(candidate.get("course_location"), str):
                    candidate_locations.add(_norm(candidate["course_location"]))
                sample_locations = set()
                for member_row in member_rows:
                    member_scope = (
                        (member_row.get("extraction_method") or {}).get(SCOPE) or {}
                    )
                    member_scope_locations = member_scope.get("locations")
                    sample_locations.update(
                        _norm(value) for value in (
                            member_scope_locations
                            if isinstance(member_scope_locations, list) else []
                        )
                        if isinstance(value, str) and value.strip()
                    )
                    if isinstance(member_row.get("course_location"), str):
                        sample_locations.add(_norm(member_row["course_location"]))
                same_award_route_campus = (
                    candidate.get("course_website") == sample.get("course_website")
                    and _norm(scope.get("original_name"))
                    == _norm(sample_scope.get("original_name"))
                    and _identity_degree_level(
                        candidate.get("degree_level"), scope.get("original_name")
                    ) == _identity_degree_level(
                        sample.get("degree_level"), sample_scope.get("original_name")
                    )
                    and bool(candidate_locations.intersection(sample_locations))
                )
                if same_award_route_campus:
                    raise ApplyRefused(
                        "unreviewed course ID shares an approved award/source/campus scope"
                    )
                if (
                    candidate.get("course_website") == sample.get("course_website")
                    and _norm(scope.get("original_name")) == _norm(sample_scope.get("original_name"))
                    and _identity_degree_level(
                        candidate.get("degree_level"), scope.get("original_name")
                    ) == _identity_degree_level(
                        sample.get("degree_level"), sample_scope.get("original_name")
                    )
                    and len(candidate_variants) == 1
                    and len(sample_variants) == 1
                    and candidate_variants == sample_variants
                ):
                    if candidate.get("course_id") not in approved_course_ids:
                        raise ApplyRefused(
                            "new or unreviewed course ID conflicts with an approved award across scrape runs"
                        )
                    raise ApplyRefused(
                        "unreviewed scrape-run row duplicates an approved award/campus identity"
                    )


async def _fk_row_snapshots(conn, refs, course_ids: list[int]) -> dict[str, list[dict]]:
    preparer = conn.dialect.identifier_preparer
    by_table: dict[tuple[str, str], set[str]] = defaultdict(set)
    for schema, table, column in refs:
        by_table[(schema, table)].add(column)
    output = {}
    for (schema, table), columns in sorted(by_table.items()):
        if not all(IDENTIFIER_RE.fullmatch(value) for value in (schema, table, *columns)):
            raise ApplyRefused("unsafe FK identifier encountered")
        qualified = f"{preparer.quote_schema(schema)}.{preparer.quote(table)}"
        where = " OR ".join(
            f"{preparer.quote(column)} = ANY(:course_ids)" for column in sorted(columns)
        )
        stmt = text(
            f"SELECT to_jsonb(source_row) AS row_data FROM {qualified} AS source_row "
            f"WHERE {where} ORDER BY to_jsonb(source_row)::text"
        )
        rows = (await conn.execute(stmt, {"course_ids": course_ids})).mappings().all()
        output[f"{schema}.{table}"] = [dict(row["row_data"]) for row in rows]
    return output


async def _affected_state_snapshot(conn, approved: dict[str, Any]) -> dict[str, Any]:
    """Return one complete, identically shaped reconciliation state snapshot."""
    course_ids = approved["course_ids"]
    return {
        "courses": await _fetch_rows(conn, "courses", course_ids),
        "fees": await _fetch_rows(conn, "fees", course_ids, "course_id"),
        "scraped_courses": await _fetch_rows(
            conn, "scraped_courses", approved["staged_ids"]
        ),
        "foreign_key_rows": await _fk_row_snapshots(
            conn, await _course_fk_census(conn), course_ids
        ),
        "course_offerings": await _fetch_rows(
            conn, "course_offerings", course_ids, "course_id"
        ),
        "course_id_aliases": await _fetch_alias_rows(conn, course_ids),
        "_course_ids": course_ids,
    }


async def _fetch_alias_rows(conn, course_ids: list[int]) -> list[dict[str, Any]]:
    result = await conn.execute(text("""
        SELECT to_jsonb(alias_row) AS row_data
          FROM course_id_aliases AS alias_row
         WHERE alias_course_id = ANY(:course_ids)
            OR canonical_course_id = ANY(:course_ids)
         ORDER BY alias_course_id, canonical_course_id
    """), {"course_ids": course_ids})
    return [dict(row["row_data"]) for row in result.mappings()]


async def _audit_snapshot(conn, *, audit_id: str, manifest_sha: str,
                          approval_revision: str, actor: str, event: str,
                          payloads: dict[str, Any]) -> None:
    for table, value in sorted(payloads.items()):
        if table.startswith("_"):
            continue
        await conn.execute(text("""
            INSERT INTO course_identity_reconciliation_audit
                (audit_id, manifest_sha256, approval_revision, actor, event_type,
                 entity_table, entity_key, row_payload)
            VALUES (CAST(:audit_id AS uuid), :manifest_sha, :revision, :actor, :event, :table,
                    CAST(:entity_key AS jsonb), CAST(:payload AS jsonb))
        """), {
            "audit_id": audit_id, "manifest_sha": manifest_sha,
            "revision": approval_revision, "actor": actor, "event": event,
            "table": table,
            "entity_key": json.dumps({"course_ids": payloads.get("_course_ids", [])}),
            "payload": json.dumps(value, ensure_ascii=False, default=_json_default),
        })


async def _verify_and_plan(conn, approved: dict, *, lock: bool = False) -> list[dict]:
    course_ids = approved["course_ids"]
    staged_ids = approved["staged_ids"]
    stage_rows = await _fetch_rows(conn, "scraped_courses", staged_ids, lock=lock)
    courses = await _fetch_rows(conn, "courses", course_ids, lock=lock)
    legacy_fee_rows = await _fetch_rows(conn, "fees", course_ids, "course_id", lock=lock)
    if len(stage_rows) != len(staged_ids) or len(courses) != len(course_ids):
        raise ApplyRefused("one or more explicitly approved source/course rows are missing")
    stage_by_id = {row["id"]: row for row in stage_rows}
    course_by_id = {row["id"]: row for row in courses}
    from course_duplicate_snapshot_export import _safe_legacy_fee_rows

    legacy_fees_by_course: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for fee in legacy_fee_rows:
        legacy_fees_by_course[fee["course_id"]].append(fee)
    safe_legacy_fees_by_course = {
        course_id: _safe_legacy_fee_rows(fees)
        for course_id, fees in legacy_fees_by_course.items()
    }
    refs = await _course_fk_census(conn)
    field_counts = await _field_approval_counts(conn, course_ids)
    offerings = await _simple_counts(conn, "course_offerings", "course_id", course_ids)
    aliases = await _alias_counts(conn, course_ids)
    fk_counts = await _foreign_key_counts(conn, refs, course_ids)

    plans = []
    outside = await _outside_pathway_counts(conn, list(stage_by_id.values()), course_ids)
    for group in approved["groups"]:
        rows, _ = _group_evidence(group, stage_by_id)
        canonical_id = group["proposed_canonical_course_id"]
        locations = {}
        identity_values = set()
        original_award = None
        route = None
        university_id = group["university_id"]
        for member in group["members"]:
            stage = stage_by_id[member["staged_row_id"]]
            course = course_by_id[member["course_id"]]
            scope = (stage.get("extraction_method") or {}).get(SCOPE) or {}
            approved_mapping = next(
                mapping for mapping in group["proposed_mapping"]
                if mapping["old_course_id"] == member["course_id"]
            )
            variant = (stage["extraction_method"]["fee_variants"]["selected"][0]
                       ["study_variant"])
            original_award = scope.get("original_name")
            route = stage["course_website"]
            if (course["university_id"] != university_id
                    or stage["university_id"] != university_id
                    or stage["course_id"] != course["id"]
                    or course["status"] != "active"
                    or course["approval_status"] != "approved"
                    or course.get("offering_identity") is not None
                    or offerings.get(course["id"], 0)
                    or aliases.get(course["id"], 0)):
                raise ApplyRefused(f"course {course['id']} is no longer an untouched legacy record")
            scope_locations = scope.get("locations")
            if (course["course_website"] != route
                    or not _published_award_name_matches(
                        course["name"], original_award, scope_locations)
                    or _identity_degree_level(course.get("degree_level"), original_award)
                    != _identity_degree_level(stage.get("degree_level"), original_award)
                    or _norm(course.get("study_mode")) != _norm(stage.get("study_mode"))
                    or _norm(course.get("course_location")) != _norm(stage.get("course_location"))):
                raise ApplyRefused(f"current Course identity properties differ for ID {course['id']}")
            legacy_fees = safe_legacy_fees_by_course.get(course["id"], [])
            expected_legacy_fee = {
                "amount": stage["international_fee"], "currency": stage["currency"],
                "fee_year": stage["fee_year"], "fee_term": stage["fee_term"],
            }
            if (not legacy_fees or any(
                    not _same_amount(fee["amount"], expected_legacy_fee["amount"])
                    or any(fee[key] != expected_legacy_fee[key]
                           for key in ("currency", "fee_year", "fee_term"))
                    for fee in legacy_fees)):
                raise ApplyRefused(
                    f"published legacy fee differs from staged price for course ID {course['id']}; "
                    "separate price approval is required"
                )
            projected = _course_projection(
                course, field_counts=field_counts.get(course["id"], {}),
                reference_counts=fk_counts.get(course["id"], {}),
                outside=int(outside.get(course["id"], 0)),
                offerings=int(offerings.get(course["id"], 0)),
                aliases=int(aliases.get(course["id"], 0)),
                legacy_fee_rows=safe_legacy_fees_by_course.get(course["id"], []),
            )
            if sha256(projected) != member["precondition_sha256"]:
                raise ApplyRefused(f"Course or FK/approval preconditions changed for ID {course['id']}")
            if int(outside.get(course["id"], 0)):
                raise ApplyRefused("a mapped course has a pathway reference outside its reviewed group")
            source_hash = "sha256:" + hashlib.sha256(route.encode("utf-8")).hexdigest()
            if member["source_route_sha256"] != source_hash:
                raise ApplyRefused("exact source URL differs from the approved route fingerprint")
            loc = stage["course_location"]
            live_fee = {
                "amount": stage["international_fee"],
                "currency": stage["currency"],
                "fee_year": stage["fee_year"],
                "fee_term": stage["fee_term"],
            }
            reviewed_fee = approved_mapping.get("source_fee") or {}
            if (not _same_amount(reviewed_fee.get("amount"), live_fee["amount"])
                    or any(reviewed_fee.get(key) != live_fee[key]
                           for key in ("currency", "fee_year", "fee_term"))):
                raise ApplyRefused("live staged fee evidence differs from the explicitly reviewed fee")
            if approved_mapping.get("offering_locations") != scope_locations:
                raise ApplyRefused("reviewed offering campuses differ from live scoped campuses")
            for campus in scope_locations:
                key = _norm(campus)
                if key in locations:
                    raise ApplyRefused(
                        f"multiple approved courses claim offering campus {campus}"
                    )
                locations[key] = (campus, stage)
            identity_values.add((
                university_id, route, original_award,
                _identity_degree_level(stage["degree_level"], original_award), variant,
                stage["study_mode"], stage["fee_year"], stage["fee_term"], stage["currency"],
            ))
        expected_locations = {
            _norm(location)
            for member in group["members"]
            for location in member["locations"]
        }
        if (len(identity_values) != 1 or set(locations) != expected_locations
                or len(expected_locations) != sum(
                    len(member["locations"]) for member in group["members"]
                )):
            raise ApplyRefused("group identity or campus uniqueness is inconsistent")
        from app.services.scraper.published_offerings import offering_identity
        identity_row = rows[0]
        identity = offering_identity(
            SimpleNamespace(**identity_row),
            identity_row["extraction_method"][SCOPE],
        )
        collision_query = """
            SELECT id, name, degree_level, study_mode, course_location, offering_identity
              FROM courses
             WHERE university_id = :university_id AND course_website = :source_url
        """ + (" FOR UPDATE" if lock else "")
        existing_identity_rows = (await conn.execute(text(collision_query), {
            "university_id": university_id, "source_url": route,
        })).mappings().all()
        approved_course_ids = set(approved["course_ids"])
        for existing in existing_identity_rows:
            if (_published_award_name_matches(
                        existing["name"], original_award, existing.get("course_location"))
                    and _identity_degree_level(existing["degree_level"], original_award)
                    == _identity_degree_level(identity_row["degree_level"], original_award)
                    and existing["id"] not in approved_course_ids):
                if existing.get("offering_identity") == identity:
                    raise ApplyRefused(
                        "unreviewed published course ID duplicates this exact offering identity"
                    )
                if existing.get("offering_identity"):
                    continue
                candidate_rows = (await conn.execute(text("""
                    SELECT course_website, university_id, degree_level, extraction_method
                      FROM scraped_courses
                     WHERE course_id = :course_id
                       AND status IN ('approved', 'published')
                       AND course_website = :source_url
                """), {
                    "course_id": existing["id"], "source_url": route,
                })).mappings().all()
                candidate_identities = set()
                candidate_ambiguous = False
                for candidate in candidate_rows:
                    scope = (candidate.get("extraction_method") or {}).get(SCOPE) or {}
                    if (_norm(scope.get("original_name")) != _norm(original_award)
                            or candidate.get("university_id") != university_id
                            or _identity_degree_level(
                                candidate.get("degree_level"), original_award
                            ) != _identity_degree_level(
                                identity_row["degree_level"], original_award
                            )):
                        continue
                    selected = ((candidate.get("extraction_method") or {}).get(
                        "fee_variants", {}
                    ).get("selected") or [])
                    if (not selected or not isinstance(selected[0], dict)
                            or not isinstance(selected[0].get("study_variant"), str)
                            or not selected[0]["study_variant"].strip()):
                        candidate_ambiguous = True
                        continue
                    candidate_identity_row = SimpleNamespace(
                        university_id=candidate["university_id"],
                        course_website=candidate["course_website"],
                        degree_level=candidate["degree_level"],
                        extraction_method=candidate["extraction_method"],
                    )
                    try:
                        candidate_identities.add(offering_identity(candidate_identity_row, scope))
                    except (KeyError, IndexError, TypeError, AttributeError):
                        candidate_ambiguous = True
                if identity in candidate_identities:
                    raise ApplyRefused(
                        "unreviewed published course ID duplicates this exact offering identity"
                    )
                if candidate_ambiguous or not candidate_identities:
                    raise ApplyRefused(
                        "unreviewed award/source collision lacks enough variant evidence"
                    )
        plans.append({
            "group": group,
            "canonical_id": canonical_id,
            "identity": identity,
            "award": original_award,
            "route": route,
            "locations": [locations[key] for key in sorted(locations)],
            "rows": rows,
        })
    if len({plan["identity"] for plan in plans}) != len(plans):
        raise ApplyRefused("two approved groups collide on backend offering_identity")
    await _conflicting_evidence(conn, approved["groups"], set(staged_ids), set(course_ids))
    return plans


async def _run(approved: dict, *, apply: bool, actor: str, expected_database: str,
               confirm_write: bool, rollback_after_apply: bool = False,
               _test_database_url: str | None = None,
               _test_schema: str | None = None) -> dict[str, Any]:
    from app.config import settings

    if not expected_database.strip():
        raise ApplyRefused("--expected-database is required")
    if apply and (not confirm_write or not actor.strip()):
        raise ApplyRefused("apply requires --confirm-write and a non-empty --actor")
    if rollback_after_apply and not apply:
        raise ApplyRefused("canary rollback requires the apply transaction path")
    if (_test_database_url is None) != (_test_schema is None):
        raise ApplyRefused("integration database URL and schema must be supplied together")
    if _test_schema is not None and not re.fullmatch(
            r"task629_test_[0-9a-f]{12}", _test_schema):
        raise ApplyRefused("integration schema name is outside the isolated test namespace")
    database_url = _test_database_url or settings.database_url
    if not database_url.startswith("postgresql+asyncpg://"):
        raise ApplyRefused("configured target is not PostgreSQL/asyncpg")
    connect_args = {"ssl": ssl.create_default_context()} if settings.database_require_tls else {}
    if _test_schema is not None:
        connect_args.setdefault("server_settings", {})["search_path"] = _test_schema
    engine = create_async_engine(
        database_url, echo=False, pool_size=1, max_overflow=0,
        connect_args=connect_args,
    )
    try:
        async with engine.connect() as conn:
            try:
              async with conn.begin():
                await conn.execute(text(
                    "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
                    + ("" if apply else ", READ ONLY")
                ))
                await conn.execute(text("SET LOCAL statement_timeout = '120000ms'"))
                db_name = (await conn.execute(text("SELECT current_database()"))).scalar_one()
                if db_name != expected_database:
                    raise ApplyRefused("live database name differs from --expected-database")
                if rollback_after_apply and not (
                    db_name.startswith("canary_") or db_name.endswith("_test")
                ):
                    raise ApplyRefused(
                        "canary rollback mode is restricted to canary_* or *_test databases"
                    )
                if apply:
                    await conn.execute(text("SELECT pg_advisory_xact_lock(629, 1)"))
                    version = (await conn.execute(text(
                        "SELECT version_num FROM alembic_version"
                    ))).scalar_one_or_none()
                    if version != "389_course_identity_audit":
                        raise ApplyRefused("required audit migration 389 is not the live schema head")
                    plans = await _verify_and_plan(conn, approved, lock=True)
                    plan_digest = sha256([{
                        "canonical_id": p["canonical_id"], "identity": p["identity"],
                        "locations": [loc for loc, _ in p["locations"]],
                        "alias_ids": sorted(m["course_id"] for m in p["group"]["members"]
                                            if m["course_id"] != p["canonical_id"]),
                    } for p in plans])
                    from uuid import uuid4
                    audit_id = str(uuid4())
                    before = await _affected_state_snapshot(conn, approved)
                    await _audit_snapshot(
                        conn, audit_id=audit_id, manifest_sha=approved["file_sha256"],
                        approval_revision=approved["approval"]["revision"], actor=actor,
                        event="before", payloads=before,
                    )
                    for plan in plans:
                        canonical_id = plan["canonical_id"]
                        await conn.execute(text("""
                            UPDATE courses
                               SET name = :name, course_location = :locations,
                                   offering_identity = :identity, last_edited_at = now(),
                                   last_edited_by = :actor
                             WHERE id = :course_id
                        """), {
                            "name": plan["award"],
                            "locations": ", ".join(loc for loc, _ in plan["locations"]),
                            "identity": plan["identity"], "actor": actor,
                            "course_id": canonical_id,
                        })
                        # Legacy scalar fees do not encode a campus. Keep every
                        # non-canonical fee/FK row untouched; canonical tuition
                        # is represented only by verified location offerings.
                        await conn.execute(text("DELETE FROM fees WHERE course_id = :id"),
                                           {"id": canonical_id})
                        for location, source in plan["locations"]:
                            await conn.execute(text("""
                                INSERT INTO course_offerings
                                    (course_id, location_key, location, fee_amount,
                                     fee_currency, fee_term, fee_year, source_url)
                                VALUES (:course_id, :location_key, :location, :amount,
                                        :currency, :term, :year, :source_url)
                            """), {
                                "course_id": canonical_id, "location_key": _norm(location),
                                "location": location, "amount": source["international_fee"],
                                "currency": source["currency"], "term": source["fee_term"],
                                "year": source["fee_year"], "source_url": source["course_website"],
                            })
                        published_locations = (await conn.execute(text("""
                            SELECT location FROM course_offerings
                             WHERE course_id = :course_id ORDER BY location
                        """), {"course_id": canonical_id})).scalars().all()
                        await conn.execute(text("""
                            UPDATE courses SET course_location = :locations WHERE id = :course_id
                        """), {
                            "locations": ", ".join(published_locations),
                            "course_id": canonical_id,
                        })
                        for member in plan["group"]["members"]:
                            alias_id = member["course_id"]
                            if alias_id == canonical_id:
                                continue
                            await conn.execute(text("""
                                INSERT INTO course_id_aliases
                                    (alias_course_id, canonical_course_id, reason, created_by,
                                     audit_metadata)
                                VALUES (:alias_id, :canonical_id, :reason, :actor,
                                        CAST(:metadata AS jsonb))
                            """), {
                                "alias_id": alias_id, "canonical_id": canonical_id,
                                "reason": "Task #629 reviewed campus-course ID consolidation",
                                "actor": actor,
                                "metadata": json.dumps({
                                    "task": 629,
                                    "manifest_sha256": approved["file_sha256"],
                                    "review_manifest_sha256": approved["review_sha256"],
                                    "approval_revision": approved["approval"]["revision"],
                                    "approved_by": approved["approval"]["approved_by"],
                                    "university_id": plan["group"]["university_id"],
                                     "source_families": plan["group"]["source_families"],
                                     "approved_mapping_sha256": approved["mapping_file_sha256"],
                                     "external_reference_scope":
                                         "out_of_scope_preserve_original_course_ids",
                                }, sort_keys=True),
                            })
                    after = await _affected_state_snapshot(conn, approved)
                    await _audit_snapshot(
                        conn, audit_id=audit_id, manifest_sha=approved["file_sha256"],
                        approval_revision=approved["approval"]["revision"], actor=actor,
                        event="after", payloads=after,
                    )
                    await conn.execute(text("""
                        INSERT INTO course_identity_reconciliation_audit
                            (audit_id, manifest_sha256, approval_revision, actor, event_type,
                             entity_table, entity_key, row_payload)
                        VALUES (CAST(:audit_id AS uuid), :manifest_sha, :revision, :actor, 'run',
                                '_run', CAST(:entity_key AS jsonb), CAST(:payload AS jsonb))
                    """), {
                        "audit_id": audit_id, "manifest_sha": approved["file_sha256"],
                        "revision": approved["approval"]["revision"], "actor": actor,
                        "entity_key": json.dumps({"database": db_name}),
                        "payload": json.dumps({
                            "review_manifest_sha256": approved["review_sha256"],
                            "plan_sha256": plan_digest,
                            "groups": len(plans), "course_ids": len(approved["course_ids"]),
                            "aliases": EXPECTED_ALIAS_COUNT,
                            "approved_mapping_sha256": approved["mapping_file_sha256"],
                            "external_reference_scope":
                                "out_of_scope_preserve_original_course_ids",
                        }, sort_keys=True),
                    })
                    result = {
                        "mode": "canary-rollback" if rollback_after_apply else "apply",
                        "database": db_name, "audit_id": audit_id,
                        "manifest_sha256": approved["file_sha256"],
                        "plan_sha256": plan_digest, "groups": len(plans),
                        "course_ids": len(approved["course_ids"]),
                        "aliases": EXPECTED_ALIAS_COUNT,
                    }
                    if rollback_after_apply:
                        raise _RollbackCanary(result, before)
                else:
                    plans = await _verify_and_plan(conn, approved)
                    plan_digest = sha256([{
                        "canonical_id": p["canonical_id"], "identity": p["identity"],
                        "locations": [loc for loc, _ in p["locations"]],
                    } for p in plans])
                    result = {
                        "mode": "dry-run", "database": db_name,
                        "manifest_sha256": approved["file_sha256"],
                        "plan_sha256": plan_digest, "groups": len(plans),
                        "course_ids": len(approved["course_ids"]),
                        "aliases": EXPECTED_ALIAS_COUNT, "writes": 0,
                    }
            except _RollbackCanary as canary:
                async with conn.begin():
                    await conn.execute(text(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    ))
                    current = await _affected_state_snapshot(conn, approved)
                    if sha256(current) != sha256(canary.before):
                        raise ApplyRefused("isolated canary rollback did not restore exact pre-state")
                    audit_rows = (await conn.execute(text("""
                        SELECT COUNT(*) FROM course_identity_reconciliation_audit
                         WHERE audit_id = CAST(:audit_id AS uuid)
                    """), {"audit_id": canary.result["audit_id"]})).scalar_one()
                    if audit_rows:
                        raise ApplyRefused("isolated canary rollback left reconciliation audit writes")
                canary.result["rollback_verified"] = True
                canary.result.pop("audit_id", None)
                return canary.result
            return result
    finally:
        await engine.dispose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path,
                        help="reviewed JSON manifest with approval attestation")
    parser.add_argument("--approved-manifest-sha256", required=True,
                        help="exact SHA-256 of the input file; independent human confirmation")
    parser.add_argument(
        "--approved-mapping", type=Path,
        default=Path(__file__).with_name("approved_law_legacy_mapping.json"),
        help="authoritative approved Task #629 ID mapping JSON",
    )
    parser.add_argument("--approved-mapping-sha256", required=True,
                        help="exact SHA-256 of approved_law_legacy_mapping.json")
    parser.add_argument("--approval-revision", required=True,
                        help="revision recorded in the manifest approval block")
    parser.add_argument("--expected-database", required=True,
                        help="exact PostgreSQL current_database() value")
    parser.add_argument("--actor", default="", help="authenticated operator identifier for audit")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="verify live rows and print plan digest")
    mode.add_argument("--apply", action="store_true", help="perform reviewed transaction")
    mode.add_argument("--canary-rollback", action="store_true",
                      help="execute full mutation path then rollback; only canary_* or *_test DB")
    parser.add_argument("--confirm-write", action="store_true",
                        help="required additional acknowledgement for --apply")
    return parser.parse_args(argv)


async def async_main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        if (args.apply or args.canary_rollback) and not args.confirm_write:
            raise ApplyRefused("write/canary mode requires --confirm-write")
        if (args.apply or args.canary_rollback) and not args.actor.strip():
            raise ApplyRefused("write/canary mode requires --actor")
        if args.dry_run and args.confirm_write:
            raise ApplyRefused("--confirm-write is only valid with --apply")
        approved = load_approved_manifest(
            args.manifest, args.approved_manifest_sha256, args.approval_revision,
            args.approved_mapping, args.approved_mapping_sha256,
        )
        result = await _run(
            approved, apply=args.apply or args.canary_rollback, actor=args.actor,
            expected_database=args.expected_database, confirm_write=args.confirm_write,
            rollback_after_apply=args.canary_rollback,
        )
    except ApplyRefused as exc:
        print(f"course duplicate operation refused: {exc}", file=sys.stderr)
        return 2
    except Exception:
        print("course duplicate operation failed; transaction rolled back; details suppressed",
              file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())