"""Unit tests for strict Task #629 reviewed apply-manifest validation."""
from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).with_name("course_duplicate_apply.py")
SPEC = importlib.util.spec_from_file_location("course_duplicate_apply", SCRIPT)
apply_tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(apply_tool)
APPROVED_MAPPING = Path(__file__).with_name("approved_law_legacy_mapping.json")


def approved_manifest(mapping_path=APPROVED_MAPPING):
    mapping_bytes = mapping_path.read_bytes()
    source = json.loads(mapping_bytes)
    map_sha = hashlib.sha256(mapping_bytes).hexdigest()
    groups = []
    staged_id = 1
    for group_index, approved in enumerate(source["groups"]):
        members, mappings = [], []
        for member_index, course_id in enumerate(approved["ids"]):
            location = f"Campus {course_id}"
            evidence_id = staged_id
            staged_id += 1
            split_id = approved["parent"] + (
                10000 if member_index == 0 or group_index >= 17 else 20000
            )
            route_fingerprint = "sha256:" + hashlib.sha256(
                approved["source"].encode()
            ).hexdigest()
            source_fee = {
                "amount": 12000, "currency": "GBP", "fee_year": 2026,
                "fee_term": "Annual",
            }
            members.append({
                "course_id": course_id,
                "staged_row_id": evidence_id,
                "location": location,
                "locations": [location],
                "location_evidence": [{
                    "location": location, "staged_row_id": evidence_id,
                    "evidence_precondition_sha256": "c" * 64,
                    "source_route_sha256": route_fingerprint,
                    "source_fee": source_fee,
                }],
                "source_fee": source_fee,
                "precondition_sha256": "b" * 64,
                "source_route_sha256": route_fingerprint,
                "evidence_rows": [{
                    "staged_row_id": evidence_id,
                    "evidence_precondition_sha256": "c" * 64,
                    "scrape_job_id": "job-" + str(approved["parent"]),
                    "split_from_id": split_id,
                    "source_route_sha256": route_fingerprint,
                    "selected_for_offering": True,
                }],
            })
            mappings.append({
                "old_course_id": course_id, "canonical_course_id": approved["parent"],
                "offering_location": location, "offering_locations": [location],
                "source_fee": members[-1]["source_fee"],
            })
        groups.append({
            "university_id": 92,
            "approved_award": approved["award"],
            "approved_source_sha256": hashlib.sha256(approved["source"].encode()).hexdigest(),
            "source_families": [
                {
                    "scrape_job_id": "job-" + str(approved["parent"]),
                    "split_from_id": split_id,
                }
                for split_id in sorted({
                    entry["split_from_id"]
                    for member in members for entry in member["evidence_rows"]
                })
            ],
            "member_count": len(members),
            "evidence_row_count": len(members),
            "course_ids": list(approved["ids"]),
            "proposed_canonical_course_id": approved["parent"],
            "proposed_mapping": mappings,
            "members": members,
            "eligibility": "preview_candidate",
            "blocking_reasons": [],
            "approval_required": True,
        })
    value = {
        "manifest_version": 1,
        "mode": "read_only_preview",
        "generated_from_snapshot_sha256": "d" * 64,
        "approval_required": True,
        "production_writes_performed": False,
        "apply_implemented": True,
        "manifest_digest_scope": "canonical JSON excluding manifest_sha256 and approval",
        "expected_scope": {"raw_families": 119, "groups": 102, "course_ids": 305},
        "approved_mapping_sha256": map_sha,
        "external_reference_scope": "out_of_scope_preserve_original_course_ids",
        "observed_scope": {
            "raw_families": 119, "groups": 102, "unique_course_ids": 305,
            "preview_candidate_groups": 102, "preview_candidate_course_ids": 305,
            "blocked_groups": 0, "ignored_evidence_rows": 0,
            "coverage_matches_approved_mapping": True, "coverage_matches_expected": True,
        },
        "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "groups": groups,
    }
    value["manifest_sha256"] = apply_tool.sha256(value)
    value["approval"] = {
        "status": "approved",
        "revision": "629-r1",
        "approved_by": "Task #629 mapping reviewer",
        "approved_scope": {"groups": 102, "course_ids": 305, "aliases": 203},
        "approved_mapping_sha256": map_sha,
        "external_reference_scope": "out_of_scope_preserve_original_course_ids",
    }
    return value, map_sha


def save_manifest(tmp_path, value):
    payload = {key: item for key, item in value.items()
               if key not in {"manifest_sha256", "approval"}}
    value["manifest_sha256"] = apply_tool.sha256(payload)
    path = tmp_path / "reviewed.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path, apply_tool.file_sha256(path)


def load(tmp_path, value):
    path, digest = save_manifest(tmp_path, value)
    _, map_sha = approved_manifest()
    return apply_tool.load_approved_manifest(
        path, digest, "629-r1", APPROVED_MAPPING, map_sha,
    )


def test_authoritative_mapping_fingerprint_and_all_explicit_rows_are_required(tmp_path):
    value, _ = approved_manifest()
    approved = load(tmp_path, value)
    assert len(approved["groups"]) == 102
    assert len(approved["course_ids"]) == 305
    assert len(approved["staged_ids"]) == 305
    assert approved["manifest"]["external_reference_scan_complete"] is False


def test_mismatched_mapping_ids_or_group_identity_refuse(tmp_path):
    value, _ = approved_manifest()
    value["groups"][0]["course_ids"][-1] += 1
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "authoritative approved JSON" in str(exc)
    else:
        raise AssertionError("manifest IDs must match approved mapping")


def test_external_scan_is_out_of_scope_not_a_no_conflicts_attestation(tmp_path):
    value, _ = approved_manifest()
    value["approval"]["external_reference_scope"] = "no_conflicts"
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "out-of-scope policy" in str(exc)
    else:
        raise AssertionError("must require explicit preserve-ID scope")


def test_all_stage_rows_must_have_unique_fingerprints_and_selected_row(tmp_path):
    value, _ = approved_manifest()
    member = value["groups"][0]["members"][0]
    member["evidence_rows"][0]["selected_for_offering"] = False
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "exactly one listed evidence row" in str(exc)
    else:
        raise AssertionError("must require a selected explicitly reviewed staged row")


def test_manifest_and_mapping_file_hashes_are_explicit(tmp_path):
    value, map_sha = approved_manifest()
    path, digest = save_manifest(tmp_path, value)
    try:
        apply_tool.load_approved_manifest(
            path, "0" * 64, "629-r1", APPROVED_MAPPING, map_sha,
        )
    except apply_tool.ApplyRefused as exc:
        assert "manifest file SHA-256" in str(exc)
    else:
        raise AssertionError("wrong review file digest must be refused")
    try:
        apply_tool.load_approved_manifest(
            path, digest, "629-r1", APPROVED_MAPPING, "0" * 64,
        )
    except apply_tool.ApplyRefused as exc:
        assert "approved mapping file SHA-256" in str(exc)
    else:
        raise AssertionError("wrong mapping file digest must be refused")


def test_cli_requires_confirmed_mapping_revision_and_target():
    args = apply_tool.parse_args([
        "--manifest", "manifest.json",
        "--approved-manifest-sha256", "a" * 64,
        "--approved-mapping-sha256", "b" * 64,
        "--approval-revision", "629-r1",
        "--expected-database", "canary_db",
        "--dry-run",
    ])
    assert args.dry_run is True
    assert args.approved_mapping == APPROVED_MAPPING
    try:
        apply_tool.parse_args([
            "--manifest", "manifest.json",
            "--approved-manifest-sha256", "a" * 64,
            "--approved-mapping-sha256", "b" * 64,
            "--approval-revision", "629-r1",
            "--expected-database", "canary_db",
        ])
    except SystemExit:
        pass
    else:
        raise AssertionError("exactly one operation mode must be selected")