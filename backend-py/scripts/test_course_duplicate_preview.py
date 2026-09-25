"""Unit tests for the offline course-duplicate inventory preview."""
from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).with_name("course_duplicate_preview.py")
SPEC = importlib.util.spec_from_file_location("course_duplicate_preview", SCRIPT)
preview = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(preview)


def sample_snapshot():
    rows = []
    courses = []
    for offset, (location, fee) in enumerate((("London", 18000), ("Leeds", 17000)), 1):
        course_id = 900 + offset
        staged_id = 700 + offset
        scope = {
            "original_name": "MSc Data Science",
            "locations": [location],
            "key": f"scope-{offset}",
            "source_url": "https://example.edu/courses/data-science?year=2026",
            "split_from_id": 700,
        }
        rows.append({
            "id": staged_id,
            "status": "published",
            "university_id": 12,
            "scrape_job_id": "job-abc",
            "course_id": course_id,
            "course_name": f"MSc Data Science — {location}",
            "course_location": location,
            "course_website": scope["source_url"],
            "degree_level": "Master",
            "study_mode": "Full-time",
            "fee_scope_key": scope["key"],
            "international_fee": fee,
            "fee_year": 2026,
            "fee_term": "Annual",
            "currency": "GBP",
            "extraction_method": {
                "campus_fee_scope": scope,
                "fee_variants": {
                    "status": "uniform",
                    "selected": [{"study_variant": "Standard"}],
                },
                "private_payload": "SHOULD_NOT_APPEAR",
            },
        })
        courses.append({
            "id": course_id,
            "university_id": 12,
            "name": "MSc Data Science",
            "course_website": scope["source_url"],
            "degree_level": "Master",
            "study_mode": "Full-time",
            "status": "active",
            "approval_status": "approved",
            "offering_identity": None,
            "field_approval_counts": {"name": 1, "fee": 1},
            "reference_counts": {"scraped_courses": 1, "intakes": 2},
            "outside_reference_count": 0,
            "existing_offering_count": 0,
            "alias_count": 0,
        })
    return {
        "schema_version": 1,
        "snapshot_id": "offline-test",
        "captured_at": "2026-09-25T00:00:00Z",
        "reference_scan_complete": True,
        "external_reference_scan_complete": True,
        "evidence_rows": rows,
        "courses": courses,
    }


def manifest_for(snapshot):
    return preview.build_review_manifest(
        snapshot, expected_groups=1, expected_course_ids=2,
    )


def test_safe_group_is_a_review_candidate_with_explicit_mapping_and_hashes():
    manifest = manifest_for(sample_snapshot())
    group = manifest["groups"][0]
    assert group["eligibility"] == "preview_candidate"
    assert group["approval_required"] is True
    assert group["proposed_canonical_course_id"] == 901
    assert [(m["old_course_id"], m["canonical_course_id"])
            for m in group["proposed_mapping"]] == [(901, 901), (902, 901)]
    assert group["proposed_mapping"][1]["source_fee"]["amount"] == 17000
    assert all(len(member["precondition_sha256"]) == 64 for member in group["members"])
    assert manifest["production_writes_performed"] is False
    assert manifest["apply_implemented"] is False


def test_conflicting_fees_for_same_location_are_blocked():
    snapshot = sample_snapshot()
    duplicate = copy.deepcopy(snapshot["evidence_rows"][1])
    duplicate["id"] = 703
    duplicate["course_id"] = 903
    duplicate["international_fee"] = 20000
    duplicate["course_name"] = "MSc Data Science — London"
    duplicate["course_location"] = "London"
    duplicate["extraction_method"]["campus_fee_scope"]["locations"] = ["London"]
    duplicate["extraction_method"]["campus_fee_scope"]["key"] = "scope-3"
    duplicate["fee_scope_key"] = "scope-3"
    snapshot["evidence_rows"].append(duplicate)
    course = copy.deepcopy(snapshot["courses"][1])
    course["id"] = 903
    snapshot["courses"].append(course)
    manifest = preview.build_review_manifest(snapshot, expected_groups=1, expected_course_ids=3)
    group = manifest["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("duplicate location" in reason for reason in group["blocking_reasons"])
    assert any("conflicting fees" in reason for reason in group["blocking_reasons"])
    assert group["proposed_mapping"] == []


def test_unsafe_identity_alias_offering_and_outside_reference_block_group():
    mutations = [
        ("outside_reference_count", 1, "outside references"),
        ("alias_count", 1, "alias"),
        ("existing_offering_count", 1, "published offerings"),
        ("offering_identity", "existing-identity", "offering identity"),
    ]
    for field, value, reason_text in mutations:
        snapshot = sample_snapshot()
        snapshot["courses"][1][field] = value
        group = manifest_for(snapshot)["groups"][0]
        assert group["eligibility"] == "blocked"
        assert any(reason_text in reason for reason in group["blocking_reasons"])


def test_ambiguous_award_route_study_variant_or_cohort_blocks_group():
    mutations = [
        ("course_name", "MSc Applied Data Science — Leeds"),
        ("course_website", "https://example.edu/other?year=2026"),
        ("study_mode", "Part-time"),
        ("fee_year", 2027),
    ]
    for field, value in mutations:
        snapshot = sample_snapshot()
        snapshot["evidence_rows"][1][field] = value
        group = manifest_for(snapshot)["groups"][0]
        assert group["eligibility"] == "blocked", field
    snapshot = sample_snapshot()
    snapshot["evidence_rows"][1]["extraction_method"]["fee_variants"]["selected"][0][
        "study_variant"
    ] = "Placement"
    assert manifest_for(snapshot)["groups"][0]["eligibility"] == "blocked"


def test_incomplete_reference_scan_and_wrong_scope_coverage_block():
    snapshot = sample_snapshot()
    snapshot["reference_scan_complete"] = False
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("reference scan" in reason for reason in group["blocking_reasons"])

    snapshot = sample_snapshot()
    snapshot["external_reference_scan_complete"] = False
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("external application" in reason for reason in group["blocking_reasons"])

    snapshot = sample_snapshot()
    manifest = preview.build_review_manifest(snapshot, expected_groups=119, expected_course_ids=335)
    assert manifest["observed_scope"]["coverage_matches_expected"] is False
    assert manifest["groups"][0]["eligibility"] == "blocked"


def test_manifest_is_json_safe_and_does_not_emit_extraction_payload_or_route():
    manifest = manifest_for(sample_snapshot())
    serialized = json.dumps(manifest)
    assert "SHOULD_NOT_APPEAR" not in serialized
    assert "example.edu" not in serialized
    assert "approval_required" in serialized
    assert len(manifest["manifest_sha256"]) == 64


def test_invalid_snapshot_and_duplicate_primary_keys_refuse():
    try:
        preview.build_review_manifest({"schema_version": 0})
    except preview.SnapshotError:
        pass
    else:
        raise AssertionError("invalid schema must be refused")
    snapshot = sample_snapshot()
    snapshot["courses"][1]["id"] = snapshot["courses"][0]["id"]
    try:
        manifest_for(snapshot)
    except preview.SnapshotError:
        pass
    else:
        raise AssertionError("duplicate course IDs must be refused")