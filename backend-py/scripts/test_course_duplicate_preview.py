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


def manifest_for(snapshot, expected_families=1, award="MSc Data Science"):
    return preview.build_review_manifest(
        snapshot, approved_mapping={
            "schema_version": 1, "university_id": 92, "approval": "approved test map",
            "groups": [{
                "parent": 901, "ids": [901, 902], "award": award,
                "source": "https://example.edu/courses/data-science?year=2026",
            }],
        }, approved_mapping_sha256="a" * 64, expected_groups=1,
        expected_course_ids=2, expected_families=expected_families,
        expected_components=1,
    )


def revised_union_snapshot(groups):
    rows, courses, mapping_groups = [], [], []
    staged_id = 20_000
    for group_index, (parent, ids, award, source, campuses, amount) in enumerate(groups):
        route_hash = "sha256:" + preview.hashlib.sha256(source.encode()).hexdigest()
        mapping_groups.append({
            "parent": parent, "ids": ids, "award": award, "source": source,
        })
        for index, (course_id, location) in enumerate(zip(ids, campuses)):
            component_index = index // 2
            split_id = parent + 10_000 + component_index
            base = copy.deepcopy(sample_snapshot()["evidence_rows"][0])
            staged_id += 1
            base.update({
                "id": staged_id, "status": "published", "university_id": 92,
                "scrape_job_id": f"job-{group_index}-{component_index}",
                "course_id": course_id,
                "course_name": f"{award} — {location}", "course_location": location,
                "course_website": route_hash, "degree_level": "Master",
                "study_mode": "Full-time", "fee_scope_key": f"scope-{course_id}",
                "international_fee": amount, "fee_year": 2026,
                "fee_term": "Full Course", "currency": "GBP",
            })
            scope = base["extraction_method"]["campus_fee_scope"]
            scope.update({
                "original_name": award, "locations": [location],
                "key": f"scope-{course_id}", "source_url": route_hash,
                "split_from_id": split_id,
            })
            variants = base["extraction_method"]["fee_variants"]
            variants["selected"] = [{
                "study_variant": "Standard", "campus": location,
                "amount": amount, "currency": "GBP", "year": 2026,
                "period": "Full Course", "source_url": route_hash,
            }]
            rows.append(base)
            course = copy.deepcopy(sample_snapshot()["courses"][0])
            course.update({
                "id": course_id, "name": f"{award} — {location}",
                "course_website": route_hash, "course_location": location,
                "degree_level": "Master", "study_mode": "Full-time",
                "legacy_fee_rows": [{
                    "id": 30_000 + course_id, "amount": amount,
                    "currency": "GBP", "fee_year": 2026,
                    "fee_term": "Full Course",
                }],
            })
            courses.append(course)
    return ({
        "schema_version": 1, "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "evidence_rows": rows, "courses": courses,
    }, {
        "schema_version": 1, "university_id": 92,
        "approval": "explicit reviewed test unions", "groups": mapping_groups,
    })


REVISED_GROUP_FIXTURES = [
    (9389, [9389, 9408, 9645, 9646, 9647, 9648],
     "MSc Healthcare Management", "https://example.edu/healthcare",
     ["Birmingham", "Birmingham", "Leeds", "Leeds", "Manchester", "Manchester"], 17500),
    (9396, [9396, 9421, 9608, 9675, 9676],
     "LLM International Criminal Law", "https://example.edu/criminal-law",
     ["Birmingham", "Birmingham", "Manchester", "Manchester", "Manchester"], 18250),
]


def revised_manifest(groups=REVISED_GROUP_FIXTURES):
    snapshot, mapping = revised_union_snapshot(groups)
    family_count, components = preview._connected_components(snapshot["evidence_rows"])
    return preview.build_review_manifest(
        snapshot, approved_mapping=mapping, approved_mapping_sha256="f" * 64,
        expected_groups=len(groups),
        expected_course_ids=sum(len(group[1]) for group in groups),
        expected_families=family_count,
        expected_components=len(components),
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


def test_only_exact_reviewed_unions_allow_identical_duplicate_campuses():
    manifest = revised_manifest()
    assert [group["course_ids"] for group in manifest["groups"]] == [
        list(REVISED_GROUP_FIXTURES[0][1]), list(REVISED_GROUP_FIXTURES[1][1]),
    ]
    assert all(group["eligibility"] == "preview_candidate" for group in manifest["groups"])
    assert manifest["observed_scope"]["overlap_components"] == 6
    assert manifest["observed_scope"]["groups"] == 2
    assert preview._norm("London") != preview._norm("London Moorgate")
    assert preview._norm("London") != preview._norm("London Bloomsbury")


def test_revised_union_rejects_duplicate_campus_fee_conflict():
    snapshot, mapping = revised_union_snapshot(REVISED_GROUP_FIXTURES)
    conflict = next(
        row for row in snapshot["evidence_rows"]
        if row["course_id"] == 9408
    )
    conflict["international_fee"] = 17_501
    conflict["extraction_method"]["fee_variants"]["selected"][0]["amount"] = 17_501
    next(course for course in snapshot["courses"] if course["id"] == 9408)[
        "legacy_fee_rows"
    ][0]["amount"] = 17_501
    family_count, components = preview._connected_components(snapshot["evidence_rows"])
    manifest = preview.build_review_manifest(
        snapshot, approved_mapping=mapping, approved_mapping_sha256="f" * 64,
        expected_groups=2, expected_course_ids=11, expected_families=family_count,
        expected_components=len(components),
    )
    first = manifest["groups"][0]
    assert first["eligibility"] == "blocked"
    assert any("campus is assigned to multiple approved IDs" in reason
               for reason in first["blocking_reasons"])


def test_revised_union_rejects_cross_parent_overlap_family():
    snapshot, mapping = revised_union_snapshot(REVISED_GROUP_FIXTURES)
    second_parent_row = next(
        row for row in snapshot["evidence_rows"] if row["course_id"] == 9396
    )
    first_parent_row = next(
        row for row in snapshot["evidence_rows"] if row["course_id"] == 9389
    )
    second_parent_row["scrape_job_id"] = first_parent_row["scrape_job_id"]
    second_parent_row["extraction_method"]["campus_fee_scope"]["split_from_id"] = (
        first_parent_row["extraction_method"]["campus_fee_scope"]["split_from_id"]
    )
    family_count, components = preview._connected_components(snapshot["evidence_rows"])
    manifest = preview.build_review_manifest(
        snapshot, approved_mapping=mapping, approved_mapping_sha256="f" * 64,
        expected_groups=2, expected_course_ids=11, expected_families=family_count,
        expected_components=len(components),
    )
    assert all(group["eligibility"] == "blocked" for group in manifest["groups"])
    assert any("component differs" in reason
               for group in manifest["groups"] for reason in group["blocking_reasons"])


def test_revised_union_rejects_duplicate_extra_campus_label():
    altered = copy.deepcopy(REVISED_GROUP_FIXTURES)
    altered[0][4][0] = "London Moorgate"
    altered[0][4][1] = "London Moorgate"
    manifest = revised_manifest(altered)
    first = manifest["groups"][0]
    assert first["eligibility"] == "blocked"
    assert any("campus is assigned to multiple approved IDs" in reason
               for reason in first["blocking_reasons"])


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


def test_authoritative_degree_canonicalizer_accepts_only_known_suffix_equivalents():
    for published_degree, staged_degree in (
        ("Master", "Master's"),
        ("Bachelor", "Bachelor's"),
    ):
        snapshot = sample_snapshot()
        for course, row in zip(snapshot["courses"], snapshot["evidence_rows"]):
            course["degree_level"] = published_degree
            row["degree_level"] = staged_degree
        assert manifest_for(snapshot)["groups"][0]["eligibility"] == "preview_candidate"

    snapshot = sample_snapshot()
    for course, row in zip(snapshot["courses"], snapshot["evidence_rows"]):
        course["degree_level"] = "Master"
        row["degree_level"] = "Master Degree"
    assert manifest_for(snapshot)["groups"][0]["eligibility"] == "blocked"


def test_diploma_degree_equivalence_is_award_scoped_and_exact():
    award = "Postgraduate Diploma in Legal Practice"
    snapshot = sample_snapshot()
    for row, course in zip(snapshot["evidence_rows"], snapshot["courses"]):
        row["extraction_method"]["campus_fee_scope"]["original_name"] = award
        row["course_name"] = f"{award} — {row['course_location']}"
        row["degree_level"] = "Graduate Diploma"
        course["name"] = f"{award} — {course['course_location']}"
        course["degree_level"] = "Graduate Certificate & Diploma"
    assert manifest_for(snapshot, award=award)["groups"][0]["eligibility"] == "preview_candidate"

    for award, published_degree in (
        ("Graduate Certificate", "Graduate Certificate & Diploma"),
        ("Postgraduate Diploma in Legal Practice", "Graduate Certificate"),
    ):
        snapshot = sample_snapshot()
        for row, course in zip(snapshot["evidence_rows"], snapshot["courses"]):
            row["extraction_method"]["campus_fee_scope"]["original_name"] = award
            row["course_name"] = f"{award} — {row['course_location']}"
            row["degree_level"] = "Graduate Diploma"
            course["name"] = f"{award} — {course['course_location']}"
            course["degree_level"] = published_degree
        assert manifest_for(snapshot, award=award)["groups"][0]["eligibility"] == "blocked"


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


def test_outside_london_authority_can_bind_one_explicit_hull_campus():
    snapshot = sample_snapshot()
    row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
    row["course_location"] = "Hull"
    row["course_name"] = "MSc Data Science — Hull"
    row["extraction_method"]["campus_fee_scope"]["locations"] = ["Hull"]
    course["course_location"] = "Hull"
    course["name"] = "MSc Data Science — Hull"
    row["extraction_method"]["fee_variants"]["selected"][0].update({
        "campus": "Outside London", "amount": row["international_fee"],
        "currency": row["currency"], "year": row["fee_year"],
        "period": row["fee_term"], "source_url": row["course_website"],
    })
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "preview_candidate"
    hull = next(member for member in group["members"] if member["course_id"] == 901)
    assert hull["locations"] == ["Hull"]
    assert hull["source_fee"]["amount"] == snapshot["evidence_rows"][0]["international_fee"]


def test_known_regional_aliases_bind_only_their_explicit_city_scope():
    snapshot = sample_snapshot()
    row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
    locations = ["London Bloomsbury", "London Moorgate"]
    joined = ", ".join(locations)
    row["course_location"] = joined
    row["course_name"] = "MSc Data Science — London Bloomsbury"
    row["extraction_method"]["campus_fee_scope"]["locations"] = locations
    row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = "London"
    course["course_location"] = joined
    course["name"] = "MSc Data Science — London Bloomsbury"
    group = manifest_for(snapshot)["groups"][0]
    assert group["eligibility"] == "preview_candidate"
    london = next(member for member in group["members"] if member["course_id"] == 901)
    assert london["locations"] == locations
    assert len(london["location_evidence"]) == 2

    for label in ("Outside London", "Outside of London", "Non-London"):
        snapshot = sample_snapshot()
        row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
        row["course_location"] = "Manchester"
        row["course_name"] = "MSc Data Science — Manchester"
        row["extraction_method"]["campus_fee_scope"]["locations"] = ["Manchester"]
        row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = label
        course["course_location"] = "Manchester"
        course["name"] = "MSc Data Science — Manchester"
        assert manifest_for(snapshot)["groups"][0]["eligibility"] == "preview_candidate"


def test_regional_aliases_reject_mixed_or_wrong_city_scopes_and_unknown_labels():
    cases = (
        ("London", ["Manchester"]),
        ("Outside London", ["London Bloomsbury"]),
        ("Outside of London", ["Manchester", "London Moorgate"]),
        ("Non-London", ["London", "Birmingham"]),
        ("Outside London Campus", ["Hull"]),
    )
    for label, locations in cases:
        snapshot = sample_snapshot()
        row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
        joined = ", ".join(locations)
        row["course_location"] = joined
        row["course_name"] = f"MSc Data Science — {locations[0]}"
        row["extraction_method"]["campus_fee_scope"]["locations"] = locations
        row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = label
        course["course_location"] = joined
        course["name"] = f"MSc Data Science — {locations[0]}"
        group = manifest_for(snapshot)["groups"][0]
        assert group["eligibility"] == "blocked", (label, locations)


def test_slash_campus_alias_requires_exact_scope_and_same_fee_group_coverage():
    snapshot = sample_snapshot()
    row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
    row["course_location"] = "Manchester"
    row["course_name"] = "MSc Data Science — Manchester"
    row["extraction_method"]["campus_fee_scope"]["locations"] = ["Manchester"]
    course["course_location"] = "Manchester"
    course["name"] = "MSc Data Science — Manchester"
    row["extraction_method"]["fee_variants"]["selected"][0].update({
        "campus": "Birmingham/Manchester",
        "amount": row["international_fee"],
        "currency": row["currency"],
        "year": row["fee_year"],
        "period": row["fee_term"],
        "source_url": row["course_website"],
    })

    second, second_course = snapshot["evidence_rows"][1], snapshot["courses"][1]
    second["course_location"] = "Birmingham"
    second["course_name"] = "MSc Data Science — Birmingham"
    second["extraction_method"]["campus_fee_scope"]["locations"] = ["Birmingham"]
    second["international_fee"] = row["international_fee"]
    second["extraction_method"]["fee_variants"]["selected"][0].update({
        "campus": "Birmingham",
        "amount": row["international_fee"],
    })
    second_course["course_location"] = "Birmingham"
    second_course["name"] = "MSc Data Science — Birmingham"
    second_course["legacy_fee_rows"][0]["amount"] = row["international_fee"]
    assert manifest_for(snapshot)["groups"][0]["eligibility"] == "preview_candidate"

    snapshot["evidence_rows"][1]["international_fee"] = 17_000
    snapshot["evidence_rows"][1]["extraction_method"]["fee_variants"]["selected"][0][
        "amount"
    ] = 17_000
    snapshot["courses"][1]["legacy_fee_rows"][0]["amount"] = 17_000
    assert manifest_for(snapshot)["groups"][0]["eligibility"] == "blocked"

    snapshot = sample_snapshot()
    row = snapshot["evidence_rows"][0]
    row["course_location"] = "Manchester"
    row["course_name"] = "MSc Data Science — Manchester"
    row["extraction_method"]["campus_fee_scope"]["locations"] = ["Manchester"]
    row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = (
        "Birmingham/Manchester"
    )
    snapshot["courses"][0]["course_location"] = "Manchester"
    snapshot["courses"][0]["name"] = "MSc Data Science — Manchester"
    assert manifest_for(snapshot)["groups"][0]["eligibility"] == "blocked"


def test_arbitrary_regional_labels_and_london_scope_are_rejected():
    for label, location in (("Outside London Campus", "Hull"), ("Outside London", "London")):
        snapshot = sample_snapshot()
        row, course = snapshot["evidence_rows"][0], snapshot["courses"][0]
        row["course_location"] = location
        row["extraction_method"]["campus_fee_scope"]["locations"] = [location]
        course["course_location"] = location
        row["extraction_method"]["fee_variants"]["selected"][0]["campus"] = label
        group = manifest_for(snapshot)["groups"][0]
        assert group["eligibility"] == "blocked"


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
        expected_components=2,
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
        expected_components=1,
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