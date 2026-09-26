from __future__ import annotations

import copy
import hashlib
import json

import pytest

import propose_law_61_mapping as proposal


def fixture():
    raw = (proposal.__file__.replace(
        "propose_law_61_mapping.py", "approved_law_legacy_mapping.json"
    ))
    mapping, mapping_digest = proposal.load_mapping(raw)
    groups = []
    inventory = []
    variant_by_parent = {
        parent: f"Observed variant {index}"
        for index, cluster in enumerate(proposal.EXPECTED_PARENTS)
        for parent in cluster
    }
    for original in mapping["groups"]:
        parent = original["parent"]
        route = "sha256:" + hashlib.sha256(original["source"].encode()).hexdigest()
        members = []
        for course_id in original["ids"]:
            campus = f"Campus {course_id}"
            fee = {"amount": 20000, "currency": "GBP", "fee_year": 2026,
                   "fee_term": "Full Course"}
            members.append({
                "course_id": course_id,
                "location": campus,
                "locations": [campus],
                "source_fee": fee.copy(),
                "source_route_sha256": route,
                "study_variant": variant_by_parent[parent],
                "study_mode": "On Campus",
                "degree_level": "Bachelor",
            })
            inventory.append({
                "id": course_id, "university_id": 92,
                "course_website": route,
                "name": f"{original['award']} — {campus}",
                "degree_level": "Bachelor", "study_mode": "On Campus",
            })
        groups.append({
            "proposed_canonical_course_id": parent,
            "eligibility": "preview_candidate", "approval_required": True,
            "course_ids": sorted(original["ids"]), "member_count": len(original["ids"]),
            "approved_award": original["award"],
            "approved_source_sha256": hashlib.sha256(original["source"].encode()).hexdigest(),
            "members": members,
        })
    snapshot = {
        "schema_version": 1,
        "snapshot_id": "offline-test-snapshot",
        "reference_scan_complete": True,
        "external_reference_scan_complete": False,
        "courses": [],
        "evidence_rows": [],
        "published_course_inventory_complete": True,
        "published_course_inventory_count": len(inventory),
        "published_course_inventory": inventory,
    }
    review = {
        "mode": "read_only_preview",
        "production_writes_performed": False,
        "approved_mapping_sha256": mapping_digest,
        "generated_from_snapshot_sha256": proposal.preview._sha256(snapshot),
        "published_course_inventory_complete": True,
        "published_course_inventory_count": len(inventory),
        "published_course_inventory_sha256": proposal.preview._sha256(inventory),
        "groups": groups,
        "unscoped_aliases": copy.deepcopy(mapping["unscoped_aliases"]),
        "expected_scope": {
            "raw_families": 100, "overlap_components": 100,
            "groups": 100, "course_ids": 307,
        },
        "observed_scope": {
            "groups": 100, "unique_course_ids": 307,
            "preview_candidate_groups": 100, "preview_candidate_course_ids": 307,
            "blocked_groups": 0, "coverage_matches_approved_mapping": True,
            "coverage_matches_expected": True, "unscoped_aliases": 4,
            "original_course_ids_including_unscoped_aliases": 311,
        },
    }
    review["manifest_sha256"] = proposal.sha256(review)
    return mapping, mapping_digest, review, snapshot


def reseal(review, snapshot):
    review.pop("manifest_sha256", None)
    review["generated_from_snapshot_sha256"] = proposal.preview._sha256(snapshot)
    review["manifest_sha256"] = proposal.sha256(review)


def test_builds_deterministic_61_group_311_id_proposal():
    mapping, digest, review, snapshot = fixture()
    first, summary = proposal.build_proposal(mapping, digest, review, snapshot)
    second, _ = proposal.build_proposal(mapping, digest, review, snapshot)
    assert proposal.canonical_json(first) == proposal.canonical_json(second)
    assert first["status"] == "proposed_unapproved_for_human_review"
    assert first["approval"] is None
    assert first["summary"] == {
        "proposed_groups": 61, "scoped_historical_ids": 307,
        "scoped_aliases": 246, "preserved_unscoped_aliases": 4,
        "total_historical_ids": 311, "total_aliases": 250,
        "production_writes_performed": False,
    }
    assert summary["human_review_required"] is True
    assert first["unscoped_aliases"] == mapping["unscoped_aliases"]
    assert sum(len(group["ids"]) for group in first["groups"]) == 307


def test_rejects_original_mapping_sha_drift(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text('{"schema_version": 1}\n')
    with pytest.raises(proposal.ProposalRefused, match="SHA-256 drifted"):
        proposal.load_mapping(path)


def test_rejects_fee_conflict_for_duplicate_normalized_campus():
    mapping, digest, review, snapshot = fixture()
    parents = proposal.EXPECTED_PARENTS[5]
    first, second = (next(g for g in review["groups"]
                          if g["proposed_canonical_course_id"] == parent)
                     for parent in parents)
    a, b = first["members"][0], second["members"][0]
    b["locations"] = [a["locations"][0]]
    b["location"] = a["locations"][0]
    b["source_fee"]["amount"] += 1
    inventory_row = next(row for row in snapshot["published_course_inventory"]
                         if row["id"] == b["course_id"])
    original = next(row for row in mapping["groups"]
                    if row["parent"] == parents[1])
    inventory_row["name"] = f"{original['award']} — {a['locations'][0]}"
    review["published_course_inventory_sha256"] = proposal.preview._sha256(
        snapshot["published_course_inventory"]
    )
    reseal(review, snapshot)
    with pytest.raises(proposal.ProposalRefused, match="fee conflict"):
        proposal.build_proposal(mapping, digest, review, snapshot)


def test_rejects_study_mode_mismatch_inside_cluster():
    mapping, digest, review, snapshot = fixture()
    parents = proposal.EXPECTED_PARENTS[5]
    member = next(g for g in review["groups"]
                  if g["proposed_canonical_course_id"] == parents[1])["members"][0]
    member["study_mode"] = "Online"
    reseal(review, snapshot)
    with pytest.raises(proposal.ProposalRefused, match="identity drift|study mode mismatch"):
        proposal.build_proposal(mapping, digest, review, snapshot)


def test_rejects_identity_key_drift_inside_cluster():
    mapping, digest, review, snapshot = fixture()
    parents = proposal.EXPECTED_PARENTS[5]
    member = next(g for g in review["groups"]
                  if g["proposed_canonical_course_id"] == parents[1])["members"][0]
    member["study_variant"] = "Extended"
    reseal(review, snapshot)
    with pytest.raises(proposal.ProposalRefused, match="identity drift"):
        proposal.build_proposal(mapping, digest, review, snapshot)


def test_rejects_missing_historical_id():
    mapping, digest, review, snapshot = fixture()
    group = review["groups"][0]
    missing = group["course_ids"].pop()
    group["member_count"] -= 1
    group["members"] = [row for row in group["members"] if row["course_id"] != missing]
    reseal(review, snapshot)
    with pytest.raises(proposal.ProposalRefused, match="exact eligible original group"):
        proposal.build_proposal(mapping, digest, review, snapshot)


def test_rejects_missing_companion_snapshot():
    mapping, digest, review, _ = fixture()
    with pytest.raises(proposal.ProposalRefused, match="companion snapshot is required"):
        proposal.build_proposal(mapping, digest, review, None)


def test_rejects_tampered_snapshot_hash():
    mapping, digest, review, snapshot = fixture()
    tampered_snapshot = copy.deepcopy(snapshot)
    tampered_snapshot["snapshot_id"] = "tampered"
    with pytest.raises(proposal.ProposalRefused, match="snapshot SHA-256"):
        proposal.build_proposal(mapping, digest, review, tampered_snapshot)


def test_rejects_changed_snapshot_inventory_even_if_snapshot_hash_is_resealed():
    mapping, digest, review, snapshot = fixture()
    changed_snapshot = copy.deepcopy(snapshot)
    changed_snapshot["published_course_inventory"][0]["name"] = "Changed published name"
    review["generated_from_snapshot_sha256"] = proposal.preview._sha256(changed_snapshot)
    reseal(review, changed_snapshot)
    with pytest.raises(proposal.ProposalRefused, match="published inventory differs"):
        proposal.build_proposal(mapping, digest, review, changed_snapshot)