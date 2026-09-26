"""Unit tests for strict Task #629 reviewed apply-manifest validation."""
from __future__ import annotations

import copy
import asyncio
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
    overlap_component_signatures = []
    for group_index, approved in enumerate(source["groups"]):
        members, mappings = [], []
        ids = approved["ids"]
        if approved["parent"] in apply_tool.REVISED_UNIONS:
            chunks = [list(chunk) for chunk in (
                ids[:2], ids[2:4], ids[4:],
            )]
        else:
            chunks = [list(ids)]
        overlap_component_signatures.extend(chunks)
        component_by_id = {
            course_id: component_index
            for component_index, chunk in enumerate(chunks)
            for course_id in chunk
        }
        for member_index, course_id in enumerate(approved["ids"]):
            location = f"Campus {course_id}"
            evidence_id = staged_id
            staged_id += 1
            split_id = approved["parent"] + 10000 + component_by_id[course_id]
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
                "study_variant": "Standard",
                "study_mode": "Full-time",
                "degree_level": "Master",
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
            if group_index < 15 and member_index == 0:
                duplicate_id = staged_id
                staged_id += 1
                members[-1]["evidence_rows"].append({
                    "staged_row_id": duplicate_id,
                    "evidence_precondition_sha256": "e" * 64,
                    "scrape_job_id": "job-" + str(approved["parent"]),
                    "split_from_id": approved["parent"] + 30000,
                    "source_route_sha256": route_fingerprint,
                    "selected_for_offering": False,
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
            "evidence_row_count": sum(len(member["evidence_rows"]) for member in members),
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
        "expected_scope": {
            "raw_families": 119, "overlap_components": 104,
            "groups": 100, "course_ids": 307,
        },
        "approved_mapping_sha256": map_sha,
        "published_course_inventory_complete": True,
        "published_course_inventory_count": 337,
        "published_course_inventory_sha256": "f" * 64,
        "external_reference_scope": "out_of_scope_preserve_original_course_ids",
        "observed_scope": {
            "raw_families": 119, "overlap_components": 104,
            "groups": 100, "unique_course_ids": 307,
            "overlap_component_signatures": sorted(
                overlap_component_signatures, key=lambda signature: tuple(signature)
            ),
            "approved_family_signature_count": 119,
            "additional_approved_id_families": 0,
            "preview_candidate_groups": 100, "preview_candidate_course_ids": 307,
            "blocked_groups": 0, "ignored_evidence_rows": 0,
            "coverage_matches_approved_mapping": True, "coverage_matches_expected": True,
            "unscoped_aliases": 4,
            "original_course_ids_including_unscoped_aliases": 311,
            "total_aliases_including_unscoped_aliases": 211,
        },
        "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "groups": groups,
        "unscoped_aliases": [{
            **alias,
            "source_route_sha256": "sha256:" + hashlib.sha256(
                alias["source"].encode()
            ).hexdigest(),
            "course_precondition_sha256": "1" * 64,
            "canonical_precondition_sha256": "2" * 64,
            "evidence_rows": [{
                "staged_row_id": staged_id + index,
                "evidence_precondition_sha256": "3" * 64,
            }],
        } for index, alias in enumerate(source["unscoped_aliases"])],
    }
    value["manifest_sha256"] = apply_tool.sha256(value)
    value["approval"] = {
        "status": "approved",
        "revision": "629-r1",
        "approved_by": "Task #629 mapping reviewer",
        "approved_scope": {"groups": 100, "course_ids": 307, "aliases": 207},
        "approved_unscoped_scope": {
            "aliases": 4, "total_course_ids": 311, "total_aliases": 211,
        },
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
    assert len(approved["groups"]) == 100
    assert len(approved["course_ids"]) == 311
    assert len(approved["staged_ids"]) == 326
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


def test_unscoped_alias_study_mode_is_bound_to_exact_approval(tmp_path):
    value, _ = approved_manifest()
    value["unscoped_aliases"][0]["study_mode"] = "On Campus"
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "exact approved mapping/evidence" in str(exc)
    else:
        raise AssertionError("unscoped study mode must match the exact approved mapping")


def test_approval_requires_exact_separate_unscoped_scope(tmp_path):
    for edit in ("missing", "tampered"):
        value, _ = approved_manifest()
        if edit == "missing":
            del value["approval"]["approved_unscoped_scope"]
        else:
            value["approval"]["approved_unscoped_scope"]["total_aliases"] = 207
        try:
            load(tmp_path, value)
        except apply_tool.ApplyRefused as exc:
            assert "separately certify exactly four unscoped aliases" in str(exc)
        else:
            raise AssertionError(
                f"missing/tampered unscoped approval scope ({edit}) must be refused"
            )


def test_apply_rejects_changed_complete_course_inventory():
    inventory = [{"id": 1, "university_id": 92, "name": "Reviewed course"}]
    approved = {
        "manifest": {
            "published_course_inventory_count": 2,
            "published_course_inventory_sha256": apply_tool.sha256([
                *inventory, {"id": 2, "university_id": 92, "name": "Before"},
            ]),
        },
        "unscoped_aliases": [],
    }
    original = apply_tool._published_course_inventory

    async def current_inventory(conn):
        return inventory

    apply_tool._published_course_inventory = current_inventory
    try:
        try:
            asyncio.run(apply_tool._verify_unscoped_aliases(
                object(), approved, lock=False,
            ))
        except apply_tool.ApplyRefused as exc:
            assert "inventory changed after review" in str(exc)
        else:
            raise AssertionError("changed complete course inventory must be refused")
    finally:
        apply_tool._published_course_inventory = original


def test_apply_rejects_missing_or_extra_approved_or_published_unscoped_evidence():
    inventory = [{"id": 1, "university_id": 92, "name": "Reviewed course"}]
    alias = {
        "alias_course_id": 9395, "canonical_course_id": 9611,
        "evidence_rows": [{"staged_row_id": 101}],
    }
    approved = {
        "manifest": {
            "published_course_inventory_count": 1,
            "published_course_inventory_sha256": apply_tool.sha256(inventory),
        },
        "unscoped_aliases": [alias],
    }

    class Result:
        def __init__(self, rows):
            self.rows = rows

        def mappings(self):
            return self.rows

    class Connection:
        def __init__(self, rows):
            self.rows = rows

        async def execute(self, statement, params):
            return Result(self.rows)

    original = apply_tool._published_course_inventory

    async def current_inventory(conn):
        return inventory

    apply_tool._published_course_inventory = current_inventory
    try:
        for rows in (
            [],
            [{"id": 101, "course_id": 9395}, {"id": 102, "course_id": 9395}],
        ):
            try:
                asyncio.run(apply_tool._verify_unscoped_aliases(
                    Connection(rows), approved, lock=False,
                ))
            except apply_tool.ApplyRefused as exc:
                assert "staged evidence set changed" in str(exc)
            else:
                raise AssertionError("a missing or extra staged evidence row must be refused")
    finally:
        apply_tool._published_course_inventory = original


def _set_member_campus(group, course_id, campus, *, fee=None):
    member = next(item for item in group["members"] if item["course_id"] == course_id)
    mapping = next(
        item for item in group["proposed_mapping"]
        if item["old_course_id"] == course_id
    )
    member["location"] = campus
    member["locations"] = [campus]
    mapping["offering_location"] = campus
    mapping["offering_locations"] = [campus]
    if fee is not None:
        member["source_fee"] = dict(fee)
        mapping["source_fee"] = dict(fee)
    member["location_evidence"] = [{
        "location": campus,
        "staged_row_id": member["staged_row_id"],
        "evidence_precondition_sha256":
            member["evidence_rows"][0]["evidence_precondition_sha256"],
        "source_route_sha256": member["source_route_sha256"],
        "source_fee": member["source_fee"],
    }]


def test_apply_manifest_rejects_conflicting_duplicate_campus_fees(tmp_path):
    value, _ = approved_manifest()
    group = next(item for item in value["groups"]
                 if item["proposed_canonical_course_id"] == 9389)
    fee_a = {"amount": 17500, "currency": "GBP", "fee_year": 2026,
             "fee_term": "Full Course"}
    fee_b = {**fee_a, "amount": 17501}
    _set_member_campus(group, 9389, "Birmingham", fee=fee_a)
    _set_member_campus(group, 9408, "Birmingham", fee=fee_b)
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "exact reviewed fee/study match" in str(exc)
    else:
        raise AssertionError("a duplicate campus with a conflicting fee must be refused")


def test_apply_manifest_rejects_unreviewed_extra_duplicate_campus(tmp_path):
    value, _ = approved_manifest()
    group = next(item for item in value["groups"]
                 if item["proposed_canonical_course_id"] == 9389)
    fee = {"amount": 17500, "currency": "GBP", "fee_year": 2026,
           "fee_term": "Full Course"}
    _set_member_campus(group, 9389, "London Moorgate", fee=fee)
    _set_member_campus(group, 9408, "London Moorgate", fee=fee)
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "exact reviewed fee/study match" in str(exc)
    else:
        raise AssertionError("an unreviewed duplicate campus label must be refused")


def test_apply_manifest_rejects_raw_family_crossing_approved_parents(tmp_path):
    value, _ = approved_manifest()
    first = next(item for item in value["groups"]
                 if item["proposed_canonical_course_id"] == 9389)
    second = next(item for item in value["groups"]
                  if item["proposed_canonical_course_id"] == 9396)
    source_family = first["members"][0]["evidence_rows"][0]
    target_family = second["members"][0]["evidence_rows"][0]
    target_family["scrape_job_id"] = source_family["scrape_job_id"]
    target_family["split_from_id"] = source_family["split_from_id"]
    second["source_families"].append({
        "scrape_job_id": source_family["scrape_job_id"],
        "split_from_id": source_family["split_from_id"],
    })
    second["source_families"].sort(
        key=lambda item: (item["scrape_job_id"], item["split_from_id"])
    )
    try:
        load(tmp_path, value)
    except apply_tool.ApplyRefused as exc:
        assert "one raw per-job family is split" in str(exc)
    else:
        raise AssertionError("a source family crossing approved parents must be refused")


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