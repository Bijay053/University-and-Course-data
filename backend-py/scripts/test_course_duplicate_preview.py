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
    source = "https://example.edu/courses/data-science?year=2026"
    route_hash = "sha256:" + preview.hashlib.sha256(source.encode()).hexdigest()
    for offset, (location, fee) in enumerate((("London", 18000), ("Leeds", 17000)), 1):
        course_id = 900 + offset
        staged_id = 700 + offset
        scope = {
            "original_name": "MSc Data Science",
            "locations": [location],
            "key": f"scope-{offset}",
            "source_url": route_hash,
            "split_from_id": 700,
        }
        rows.append({
            "id": staged_id,
            "status": "published",
            "university_id": 92,
            "scrape_job_id": "job-abc",
            "course_id": course_id,
            "course_name": f"MSc Data Science — {location}",
            "course_location": location,
            "course_website": scope["source_url"],
            "degree_level": "Master",
            "study_mode": "Full-time",
            "course_location": location,
            "fee_scope_key": scope["key"],
            "international_fee": fee,
            "fee_year": 2026,
            "fee_term": "Annual",
            "currency": "GBP",
            "extraction_method": {
                "campus_fee_scope": scope,
                "fee_variants": {
                    "status": "uniform",
                    "selected": [{
                        "study_variant": "Standard", "campus": location,
                        "amount": fee, "currency": "GBP", "year": 2026,
                        "period": "Annual", "source_url": scope["source_url"],
                    }],
                    "validated_uniform_authority": True,
                },
                "private_payload": "SHOULD_NOT_APPEAR",
            },
        })
        courses.append({
            "id": course_id,
            "university_id": 92,
            "name": "MSc Data Science",
            "course_website": scope["source_url"],
            "degree_level": "Master",
            "study_mode": "Full-time",
            "course_location": location,
            "status": "active",
            "approval_status": "approved",
            "offering_identity": None,
            "field_approval_counts": {"name": 1, "fee": 1},
            "reference_counts": {"scraped_courses": 1, "intakes": 2},
            "outside_reference_count": 0,
            "existing_offering_count": 0,
            "alias_count": 0,
            "legacy_fee_rows": [{
                "id": 1000 + course_id, "amount": fee,
                "currency": "GBP", "fee_year": 2026, "fee_term": "Annual",
            }],
        })
    return {
        "schema_version": 1,
        "snapshot_id": "offline-test",
        "captured_at": "2026-09-25T00:00:00Z",
        "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "evidence_rows": rows,
        "courses": courses,
    }


def manifest_for(snapshot, expected_families=1):
    return preview.build_review_manifest(
        snapshot, approved_mapping={
            "schema_version": 1, "university_id": 92, "approval": "approved test map",
            "groups": [{
                "parent": 901, "ids": [901, 902], "award": "MSc Data Science",
                "source": "https://example.edu/courses/data-science?year=2026",
            }],
        }, approved_mapping_sha256="a" * 64, expected_groups=1,
        expected_course_ids=2, expected_families=expected_families,
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
    assert all(member["source_route_sha256"].startswith("sha256:")
               for member in group["members"])
    assert manifest["production_writes_performed"] is False
    assert manifest["apply_implemented"] is True


def test_legacy_fee_mismatch_blocks_repricing_before_preview_approval():
    snapshot = sample_snapshot()
    snapshot["courses"][0]["legacy_fee_rows"][0]["amount"] += 1
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("published legacy fee" in reason for reason in group["blocking_reasons"])


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
    manifest = manifest_for(snapshot)
    group = manifest["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("out-of-map course IDs" in reason for reason in group["blocking_reasons"])
    assert group["proposed_mapping"] == []


def test_unsafe_identity_alias_offering_and_outside_reference_block_group():
    mutations = [
        ("outside_reference_count", 1, "outside local FK"),
        ("alias_count", 1, "participates in an alias"),
        ("existing_offering_count", 1, "published offerings"),
        ("offering_identity", "existing-identity", "offering_identity"),
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


def test_selected_fee_variant_must_belong_to_exact_scoped_campus():
    snapshot = sample_snapshot()
    snapshot["evidence_rows"][0]["extraction_method"]["fee_variants"]["selected"][0][
        "campus"
    ] = "Manchester"
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("selected fee option campus differs" in reason
               for reason in group["blocking_reasons"])


def test_verified_regional_fee_binds_multiple_scope_campuses_and_legacy_course_names():
    snapshot = sample_snapshot()
    locations = ["Birmingham", "Leeds", "Manchester"]
    row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
    joined = ", ".join(locations)
    row["course_location"] = joined
    row["course_name"] = "MSc Data Science — Birmingham"
    row["international_fee"] = 18000
    row["extraction_method"]["campus_fee_scope"]["locations"] = locations
    course["course_location"] = joined
    course["name"] = "MSc Data Science — Birmingham"
    course["legacy_fee_rows"][0]["amount"] = 18000
    selected = row["extraction_method"]["fee_variants"]["selected"][0]
    selected.update({
        "campus": "Outside London", "amount": 18000, "currency": "GBP",
        "year": 2026, "period": "Annual", "source_url": row["course_website"],
    })

    row, course = snapshot["evidence_rows"][1], snapshot["courses"][1]
    row["course_location"] = "London"
    row["course_name"] = "MSc Data Science — London"
    row["extraction_method"]["campus_fee_scope"]["locations"] = ["London"]
    course["course_location"] = "London"
    course["name"] = "MSc Data Science — London"
    row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = "London"
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "preview_candidate"
    regional_member = next(member for member in group["members"] if member["course_id"] == 901)
    assert regional_member["location"] == joined
    assert regional_member["locations"] == locations
    assert [proof["location"] for proof in regional_member["location_evidence"]] == locations
    proposed_locations = [
        location for mapping in group["proposed_mapping"]
        for location in mapping["offering_locations"]
    ]
    assert set(proposed_locations) == {
        "Birmingham", "Leeds", "Manchester", "London",
    }
    assert len(proposed_locations) == len(set(proposed_locations)) == 4
    assert regional_member["source_fee"]["amount"] == 18000


def test_regional_fee_binding_blocks_stale_amount_and_out_of_scope_campus():
    snapshot = sample_snapshot()
    row = snapshot["evidence_rows"][0]
    row["course_location"] = "Birmingham, Leeds, Manchester"
    row["extraction_method"]["campus_fee_scope"]["locations"] = [
        "Birmingham", "Leeds", "Manchester",
    ]
    selected = row["extraction_method"]["fee_variants"]["selected"][0]
    selected.update({
        "campus": "Outside London", "amount": row["international_fee"] + 1,
        "currency": "GBP", "year": 2026, "period": "Annual",
        "source_url": row["course_website"],
    })
    row["extraction_method"]["fee_variants"]["validated_uniform_authority"] = True
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"

    snapshot = sample_snapshot()
    row = snapshot["evidence_rows"][0]
    course = snapshot["courses"][0]
    row["course_location"] = "Birmingham"
    row["extraction_method"]["campus_fee_scope"]["locations"] = [
        "Birmingham", "Leeds", "Manchester",
    ]
    course["course_location"] = "Birmingham"
    selected = row["extraction_method"]["fee_variants"]["selected"][0]
    selected.update({
        "campus": "Outside London", "amount": row["international_fee"],
        "currency": row["currency"], "year": row["fee_year"],
        "period": row["fee_term"], "source_url": row["course_website"],
    })
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("outside its authoritative scope locations" in reason
               for reason in group["blocking_reasons"])

    snapshot = sample_snapshot()
    row = snapshot["evidence_rows"][0]
    row["course_location"] = "York"
    row["extraction_method"]["campus_fee_scope"]["locations"] = [
        "Birmingham", "Leeds", "Manchester",
    ]
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"


def test_unrelated_extra_id_is_inventory_only_but_related_extra_id_blocks():
    snapshot = sample_snapshot()
    extra = copy.deepcopy(snapshot["evidence_rows"][0])
    extra.update({
        "id": 704, "course_id": 903, "course_location": "Bristol",
        "course_name": "MSc Unrelated — Bristol",
        "course_website": "sha256:" + preview.hashlib.sha256(
            b"https://other.example.edu/course"
        ).hexdigest(),
        "scrape_job_id": "job-extra",
    })
    extra_scope = extra["extraction_method"]["campus_fee_scope"]
    extra_scope.update({
        "original_name": "MSc Unrelated", "locations": ["Bristol"],
        "source_url": extra["course_website"], "split_from_id": 903,
    })
    extra_scope["key"] = extra["fee_scope_key"] = "extra-scope"
    extra["extraction_method"]["fee_variants"]["selected"][0]["campus"] = "Bristol"
    snapshot["evidence_rows"].append(extra)
    extra_course = copy.deepcopy(snapshot["courses"][0])
    extra_course.update({
        "id": 903, "name": "MSc Unrelated — Bristol",
        "course_location": "Bristol", "course_website": extra["course_website"],
    })
    snapshot["courses"].append(extra_course)
    manifest = manifest_for(snapshot)
    assert manifest["groups"][0]["eligibility"] == "preview_candidate"
    assert manifest["observed_scope"]["extra_course_ids"] == 1
    assert manifest["observed_scope"]["excluded_unrelated_course_ids"] == [903]


def test_incomplete_reference_scan_and_wrong_scope_coverage_block():
    snapshot = sample_snapshot()
    snapshot["reference_scan_complete"] = False
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "blocked"
    assert any("FK census" in reason for reason in group["blocking_reasons"])

    snapshot = sample_snapshot()
    snapshot["external_reference_scan_complete"] = False
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "preview_candidate"

    snapshot = sample_snapshot()
    manifest = manifest_for(snapshot, expected_families=119)
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
        preview._validate_snapshot({"schema_version": 0})
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


def test_overlapping_per_job_families_resolve_to_one_approved_component():
    snapshot = sample_snapshot()
    overlap = copy.deepcopy(snapshot["evidence_rows"][0])
    overlap["id"] = 703
    overlap["scrape_job_id"] = "job-second-run"
    overlap["extraction_method"]["campus_fee_scope"]["split_from_id"] = 701
    snapshot["evidence_rows"].append(overlap)
    manifest = manifest_for(snapshot, expected_families=2)
    group = manifest["groups"][0]
    assert manifest["observed_scope"]["raw_families"] == 2
    assert manifest["observed_scope"]["groups"] == 1
    assert group["eligibility"] == "preview_candidate"
    assert group["source_families"] == [
        {"scrape_job_id": "job-abc", "split_from_id": 700},
        {"scrape_job_id": "job-second-run", "split_from_id": 701},
    ]
    assert group["evidence_row_count"] == 3
    assert len(group["members"][0]["evidence_rows"]) == 2


def test_same_award_and_source_do_not_merge_disjoint_approved_groups():
    snapshot = sample_snapshot()
    source = "https://example.edu/courses/data-science?year=2026"
    for course_id, staged_id, location in ((903, 704, "Bristol"), (904, 705, "York")):
        row = copy.deepcopy(snapshot["evidence_rows"][0])
        row.update({
            "id": staged_id, "course_id": course_id, "course_location": location,
            "course_name": f"MSc Data Science — {location}", "scrape_job_id": "job-other",
        })
        scope = row["extraction_method"]["campus_fee_scope"]
        scope.update({"split_from_id": 701, "locations": [location], "key": f"scope-{course_id}"})
        row["fee_scope_key"] = scope["key"]
        row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = location
        snapshot["evidence_rows"].append(row)
        course = copy.deepcopy(snapshot["courses"][0])
        course.update({"id": course_id, "course_location": location})
        snapshot["courses"].append(course)
    mapping = {
        "schema_version": 1, "university_id": 92, "approval": "approved test map",
        "groups": [
            {"parent": 901, "ids": [901, 902], "award": "MSc Data Science", "source": source},
            {"parent": 903, "ids": [903, 904], "award": "MSc Data Science", "source": source},
        ],
    }
    manifest = preview.build_review_manifest(
        snapshot, approved_mapping=mapping, approved_mapping_sha256="a" * 64,
        expected_groups=2, expected_course_ids=4, expected_families=2,
    )
    assert manifest["observed_scope"]["groups"] == 2
    assert [group["course_ids"] for group in manifest["groups"]] == [[901, 902], [903, 904]]
    assert all(group["eligibility"] == "preview_candidate" for group in manifest["groups"])


def test_current_overlap_component_mismatch_with_approved_ids_is_blocked():
    snapshot = sample_snapshot()
    source = "https://example.edu/courses/data-science?year=2026"
    wrong_mapping = {
        "schema_version": 1, "university_id": 92, "approval": "approved test map",
        "groups": [{
            "parent": 901, "ids": [901, 903], "award": "MSc Data Science", "source": source,
        }],
    }
    manifest = preview.build_review_manifest(
        snapshot, approved_mapping=wrong_mapping, approved_mapping_sha256="a" * 64,
        expected_groups=1, expected_course_ids=2, expected_families=1,
    )
    assert manifest["groups"][0]["eligibility"] == "blocked"
    assert any("differs from approved JSON" in reason
               for reason in manifest["groups"][0]["blocking_reasons"])


def test_external_application_scan_cannot_be_claimed_by_local_preview():
    snapshot = sample_snapshot()
    snapshot["external_reference_scan_complete"] = True
    try:
        manifest_for(snapshot)
    except preview.SnapshotError as exc:
        assert "out of scope" in str(exc)
    else:
        raise AssertionError("local snapshot must not claim external portal coverage")