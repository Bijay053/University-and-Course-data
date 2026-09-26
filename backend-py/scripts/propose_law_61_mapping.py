#!/usr/bin/env python3
"""Prepare a deterministic, read-only proposal for the 61 ULaw identities.

This tool consumes a fresh course_duplicate_preview review manifest. It does
not change the approved mapping, write to a database, or implement/apply aliases.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import course_duplicate_preview as preview


APPROVED_MAPPING_SHA256 = "c895a22170b62cbae095c6d689735689b8542d4eab563e36d1be8a96bf22f8a5"
EXPECTED_PARENTS = [
    [9389], [9393], [9394], [9396], [9397], [9399, 9547], [9400], [9403],
    [9404, 9512, 9513], [9405, 9511], [9406], [9407, 9503], [9411, 9614, 9615],
    [9413], [9415, 9610], [9417, 9609], [9419], [9422], [9424], [9426], [9428],
    [9430], [9433, 9607], [9435, 9606], [9437, 9605], [9439, 9604],
    [9441, 9602], [9443, 9600], [9445, 9598], [9447, 9597], [9449, 9595],
    [9451, 9594], [9453, 9593], [9455, 9578, 9579], [9457, 9563, 9564],
    [9460, 9544], [9463, 9542, 9543], [9465], [9467, 9541], [9469, 9537],
    [9471, 9535], [9473, 9539], [9475], [9477], [9480, 9518, 9519],
    [9483, 9517], [9486, 9510], [9488, 9508], [9491], [9493, 9507],
    [9496, 9501], [9498, 9500], [9533], [9611], [9630], [9631], [9632],
    [9635], [9636], [9639], [9642],
]
EXPECTED_SCOPED_GROUPS = 100
EXPECTED_SCOPED_IDS = 307
EXPECTED_UNSCOPED_ALIASES = 4
EXPECTED_TOTAL_IDS = 311
EXPECTED_TOTAL_ALIASES = 250


class ProposalRefused(ValueError):
    """The review evidence cannot support this proposal."""


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str).encode("utf-8")


def sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def norm(value: Any) -> str:
    return " ".join(str(value or "").casefold().split())


def mapping_sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def load_mapping(path: str | Path) -> tuple[dict[str, Any], str]:
    raw = Path(path).read_bytes()
    digest = mapping_sha256(raw)
    if digest != APPROVED_MAPPING_SHA256:
        raise ProposalRefused(
            "approved mapping SHA-256 drifted; this tool is pinned to the "
            "reviewed original mapping"
        )
    try:
        mapping = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProposalRefused(f"approved mapping is invalid JSON: {exc}") from exc
    validate_mapping(mapping)
    return mapping, digest


def validate_mapping(mapping: dict[str, Any]) -> None:
    groups = mapping.get("groups")
    aliases = mapping.get("unscoped_aliases")
    if (mapping.get("schema_version") != 1 or mapping.get("university_id") != 92
            or not isinstance(groups, list) or len(groups) != EXPECTED_SCOPED_GROUPS
            or not isinstance(aliases, list) or len(aliases) != EXPECTED_UNSCOPED_ALIASES):
        raise ProposalRefused("original mapping must contain exactly 100 scoped groups and four unscoped aliases")
    parents = [group.get("parent") for group in groups if isinstance(group, dict)]
    ids = [course_id for group in groups if isinstance(group, dict)
           for course_id in group.get("ids", [])]
    if (len(parents) != EXPECTED_SCOPED_GROUPS or len(set(parents)) != EXPECTED_SCOPED_GROUPS
            or len(ids) != EXPECTED_SCOPED_IDS or len(set(ids)) != EXPECTED_SCOPED_IDS):
        raise ProposalRefused("original scoped parent/ID inventory is malformed")
    for group in groups:
        if (not isinstance(group.get("parent"), int)
                or group["parent"] not in group.get("ids", [])
                or not isinstance(group.get("award"), str)
                or not isinstance(group.get("source"), str)):
            raise ProposalRefused("original mapping contains an invalid scoped group")
    if {alias.get("alias_course_id") for alias in aliases if isinstance(alias, dict)} != {
        9395, 9392, 9391, 9390,
    }:
        raise ProposalRefused("original four unscoped aliases have changed")


def validate_manifest(manifest: dict[str, Any], mapping: dict[str, Any],
                      mapping_digest: str) -> dict[int, dict[str, Any]]:
    if manifest.get("mode") != "read_only_preview":
        raise ProposalRefused("review input is not a read-only preview manifest")
    if manifest.get("production_writes_performed") is not False:
        raise ProposalRefused("review manifest does not certify zero production writes")
    if manifest.get("approved_mapping_sha256") != mapping_digest:
        raise ProposalRefused("review manifest was generated from a different approved mapping")
    manifest_digest = manifest.get("manifest_sha256")
    payload = {key: value for key, value in manifest.items() if key != "manifest_sha256"}
    if (not isinstance(manifest_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_digest)
            or sha256(payload) != manifest_digest):
        raise ProposalRefused("review manifest SHA-256 is missing or invalid")
    scope = manifest.get("observed_scope")
    if (not isinstance(scope, dict)
            or scope.get("groups") != EXPECTED_SCOPED_GROUPS
            or scope.get("unique_course_ids") != EXPECTED_SCOPED_IDS
            or scope.get("preview_candidate_groups") != EXPECTED_SCOPED_GROUPS
            or scope.get("preview_candidate_course_ids") != EXPECTED_SCOPED_IDS
            or scope.get("blocked_groups") != 0
            or scope.get("coverage_matches_approved_mapping") is not True
            or scope.get("coverage_matches_expected") is not True
            or scope.get("unscoped_aliases") != EXPECTED_UNSCOPED_ALIASES
            or scope.get("original_course_ids_including_unscoped_aliases") != EXPECTED_TOTAL_IDS):
        raise ProposalRefused("review eligibility/coverage is not exactly 100 groups, 307 scoped + 4 unscoped IDs")
    if manifest.get("published_course_inventory_complete") is not True:
        raise ProposalRefused("published course identity inventory is not complete")
    if manifest.get("published_course_inventory_count") is None:
        raise ProposalRefused("published course identity inventory count is missing")
    expected_scope = manifest.get("expected_scope")
    if (not isinstance(expected_scope, dict)
            or expected_scope.get("groups") != EXPECTED_SCOPED_GROUPS
            or expected_scope.get("course_ids") != EXPECTED_SCOPED_IDS):
        raise ProposalRefused("review manifest expected scope differs from the 100-group, 307-ID review")
    groups = manifest.get("groups")
    if not isinstance(groups, list) or len(groups) != EXPECTED_SCOPED_GROUPS:
        raise ProposalRefused("review manifest must contain exactly 100 scoped groups")
    expected_by_parent = {group["parent"]: group for group in mapping["groups"]}
    observed_by_parent: dict[int, dict[str, Any]] = {}
    all_ids: list[int] = []
    for reviewed in groups:
        ids = reviewed.get("course_ids")
        if not isinstance(ids, list) or not ids:
            raise ProposalRefused("review manifest contains a group without course IDs")
        canonical = reviewed.get("proposed_canonical_course_id")
        source_parent = canonical
        if source_parent not in expected_by_parent or source_parent in observed_by_parent:
            raise ProposalRefused("review group parent is missing, duplicated, or outside the original map")
        expected = expected_by_parent[source_parent]
        if (reviewed.get("eligibility") != "preview_candidate"
                or reviewed.get("approval_required") is not True
                or ids != sorted(expected["ids"])
                or reviewed.get("member_count") != len(expected["ids"])
                or reviewed.get("approved_award") != expected["award"]
                or reviewed.get("approved_source_sha256")
                != hashlib.sha256(expected["source"].encode("utf-8")).hexdigest()):
            raise ProposalRefused(f"reviewed group {source_parent} is not an exact eligible original group")
        members = reviewed.get("members")
        if not isinstance(members, list) or {m.get("course_id") for m in members} != set(ids):
            raise ProposalRefused(f"review group {source_parent} has missing or extra member IDs")
        observed_by_parent[source_parent] = reviewed
        all_ids.extend(ids)
    if set(observed_by_parent) != set(expected_by_parent):
        raise ProposalRefused("review manifest omitted one or more original groups")
    if len(all_ids) != EXPECTED_SCOPED_IDS or len(set(all_ids)) != EXPECTED_SCOPED_IDS:
        raise ProposalRefused("review manifest scoped ID inventory is missing or duplicated")
    aliases = manifest.get("unscoped_aliases")
    if (not isinstance(aliases, list) or len(aliases) != EXPECTED_UNSCOPED_ALIASES
            or len({row.get("alias_course_id") for row in aliases if isinstance(row, dict)})
            != EXPECTED_UNSCOPED_ALIASES):
        raise ProposalRefused("review manifest must retain exactly four unscoped aliases")
    approved_aliases = {row["alias_course_id"]: row for row in mapping["unscoped_aliases"]}
    for alias in aliases:
        expected = approved_aliases.get(alias.get("alias_course_id"))
        if expected is None or any(alias.get(k) != expected.get(k) for k in (
            "alias_course_id", "canonical_course_id", "award", "study_mode",
            "study_variant", "amount", "currency", "fee_year", "fee_term", "source",
        )):
            raise ProposalRefused("review manifest changed an unscoped alias or its target")
    return observed_by_parent


def identity_key(parent: int, group: dict[str, Any]) -> tuple[str, str, str, str]:
    members = group["members"]
    identities = set()
    for member in members:
        award = group["approved_award"]
        degree = preview._identity_degree_level(member.get("degree_level"), award)
        route = member.get("source_route_sha256")
        variant = member.get("study_variant")
        if (not isinstance(route, str) or not route.startswith("sha256:")
                or not isinstance(variant, str) or not variant.strip()
                or not isinstance(member.get("study_mode"), str)
                or not member["study_mode"].strip()):
            raise ProposalRefused(f"group {parent} has incomplete route/degree/variant/mode identity")
        identities.add((norm(degree), route, norm(award), norm(variant)))
    if len(identities) != 1:
        raise ProposalRefused(f"group {parent} contains identity drift between members")
    return next(iter(identities))


def build_proposal(mapping: dict[str, Any], mapping_digest: str,
                   manifest: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    reviewed_by_parent = validate_manifest(manifest, mapping, mapping_digest)
    original_by_parent = {group["parent"]: group for group in mapping["groups"]}
    parent_to_cluster = {
        parent: cluster[0] for cluster in EXPECTED_PARENTS for parent in cluster
    }
    all_parents = set(original_by_parent)
    if (len(EXPECTED_PARENTS) != 61
            or len(parent_to_cluster) != EXPECTED_SCOPED_GROUPS
            or set(parent_to_cluster) != all_parents):
        raise ProposalRefused("configured 61 clusters do not partition the original 100 parents exactly")

    published_inventory = manifest.get("published_course_inventory")
    if not isinstance(published_inventory, list):
        raise ProposalRefused("complete published course inventory records are required")
    if (manifest.get("published_course_inventory_count") != len(published_inventory)
            or manifest.get("published_course_inventory_sha256") != preview._sha256(published_inventory)):
        raise ProposalRefused("published course inventory count or fingerprint is invalid")
    published_by_id = {row.get("id"): row for row in published_inventory if isinstance(row, dict)}
    if len(published_by_id) != len(published_inventory):
        raise ProposalRefused("published inventory contains duplicate or malformed IDs")

    review_identity: dict[int, tuple[str, str, str, str]] = {}
    members_by_course: dict[int, dict[str, Any]] = {}
    for parent, group in reviewed_by_parent.items():
        key = identity_key(parent, group)
        review_identity[parent] = key
        for member in group["members"]:
            cid = member["course_id"]
            if cid in members_by_course:
                raise ProposalRefused(f"duplicate reviewed member ID {cid}")
            members_by_course[cid] = member
            published = published_by_id.get(cid)
            original = original_by_parent[parent]
            award = original["award"]
            if published is None:
                raise ProposalRefused(f"published inventory is missing reviewed course ID {cid}")
            if (published.get("university_id") != 92
                    or published.get("course_website") != member.get("source_route_sha256")
                    or preview._identity_degree_level(published.get("degree_level"), award)
                    != preview._identity_degree_level(member.get("degree_level"), award)
                    or norm(published.get("study_mode")) != norm(member.get("study_mode"))
                    or not preview._published_award_name_matches(
                        published.get("name"), award, tuple(member.get("locations") or [])
                    )):
                raise ProposalRefused(f"published identity drift for course ID {cid}")
    if set(members_by_course) != {
        cid for group in mapping["groups"] for cid in group["ids"]
    }:
        raise ProposalRefused("review member inventory has missing or extra IDs")

    # Every source parent assigned to one proposed identity must have the
    # same published canonical-degree/route/award/variant key.
    observed_cluster_keys = set()
    for cluster in EXPECTED_PARENTS:
        keys = {review_identity[parent] for parent in cluster}
        if len(keys) != 1:
            raise ProposalRefused(f"identity drift across proposed parent cluster {cluster}")
        cluster_key = next(iter(keys))
        if cluster_key in observed_cluster_keys:
            raise ProposalRefused(
                f"distinct proposed clusters share one published offering identity: {cluster}"
            )
        observed_cluster_keys.add(cluster_key)
        modes = {
            norm(member.get("study_mode"))
            for parent in cluster for member in reviewed_by_parent[parent]["members"]
        }
        if len(modes) != 1:
            raise ProposalRefused(f"study mode mismatch within proposed cluster {cluster}")

    # Campus repeats across old parents are safe only when spelling and the
    # complete fee tuple agree exactly after normalized-campus matching.
    output_groups = []
    for cluster in EXPECTED_PARENTS:
        target_parent = cluster[0]
        source_groups = [original_by_parent[parent] for parent in cluster]
        identity = review_identity[target_parent]
        campus_observations: dict[str, list[tuple[str, tuple[Any, ...], int]]] = defaultdict(list)
        cluster_ids: list[int] = []
        for parent in cluster:
            for member in reviewed_by_parent[parent]["members"]:
                cid = member["course_id"]
                cluster_ids.append(cid)
                fee = member.get("source_fee")
                required_fee = ("amount", "currency", "fee_year", "fee_term")
                if not isinstance(fee, dict) or any(fee.get(key) is None for key in required_fee):
                    raise ProposalRefused(f"missing exact fee tuple for course ID {cid}")
                fee_tuple = tuple(fee[key] for key in required_fee)
                locations = member.get("locations")
                if not isinstance(locations, list) or not locations:
                    raise ProposalRefused(f"missing campus labels for course ID {cid}")
                for label in locations:
                    if not isinstance(label, str) or not label.strip():
                        raise ProposalRefused(f"invalid campus label for course ID {cid}")
                    campus_observations[norm(label)].append((label, fee_tuple, cid))
        for campus_key, observations in campus_observations.items():
            labels = {label for label, _, _ in observations}
            if len(labels) != 1:
                raise ProposalRefused(
                    f"conflicting campus labels for normalized campus {campus_key!r} "
                    f"in cluster {cluster}"
                )
            fees = {fee for _, fee, _ in observations}
            if len(observations) > 1 and len(fees) != 1:
                raise ProposalRefused(
                    f"fee conflict for duplicate normalized campus {campus_key!r} "
                    f"in cluster {cluster}"
                )
        output_groups.append({
            "parent": target_parent,
            "ids": sorted(cluster_ids),
            "award": source_groups[0]["award"],
            "source": source_groups[0]["source"],
            "merged_original_parents": cluster,
            "identity_key": {
                "canonical_degree": identity[0], "source_route_sha256": identity[1],
                "normalized_award": identity[2], "study_variant": identity[3],
            },
            "study_mode": reviewed_by_parent[target_parent]["members"][0]["study_mode"],
        })

    output_ids = [cid for group in output_groups for cid in group["ids"]]
    if len(output_ids) != EXPECTED_SCOPED_IDS or len(set(output_ids)) != EXPECTED_SCOPED_IDS:
        raise ProposalRefused("proposed 61-group mapping does not cover exactly 307 scoped IDs")
    aliases = [
        {"alias_course_id": cid, "canonical_course_id": group["parent"]}
        for group in output_groups for cid in group["ids"] if cid != group["parent"]
    ]
    if len(aliases) != 246:
        raise ProposalRefused("scoped proposal must contain exactly 246 aliases")

    proposal = {
        "schema_version": 1,
        "status": "proposed_unapproved_for_human_review",
        "approval": None,
        "university_id": 92,
        "source_approved_mapping_sha256": mapping_digest,
        "source_review_manifest_sha256": manifest["manifest_sha256"],
        "groups": output_groups,
        "aliases": aliases,
        "unscoped_aliases": mapping["unscoped_aliases"],
        "summary": {
            "proposed_groups": 61, "scoped_historical_ids": 307,
            "scoped_aliases": 246, "preserved_unscoped_aliases": 4,
            "total_historical_ids": 311, "total_aliases": EXPECTED_TOTAL_ALIASES,
            "production_writes_performed": False,
        },
        "human_review_required": True,
    }
    summary = {
        "status": proposal["status"],
        "human_review_required": True,
        "production_writes_performed": False,
        "source_approved_mapping_sha256": mapping_digest,
        "source_review_manifest_sha256": manifest["manifest_sha256"],
        "checks": {
            "mapping_sha_valid": True,
            "review_eligibility_exact_100_groups_307_scoped_plus_4_unscoped": True,
            "exact_original_parent_and_ID_coverage": True,
            "published_identity_key_consistent_and_unique": True,
            "study_mode_consistent_within_clusters": True,
            "duplicate_normalized_campus_fee_tuples_exact": True,
            "no_missing_or_extra_ids_or_conflicting_campus_labels": True,
            "unscoped_alias_targets_unchanged": True,
        },
        "counts": proposal["summary"],
        "clusters": [
            {"parent": row["parent"], "merged_original_parents": row["merged_original_parents"],
             "historical_ids": row["ids"], "award": row["award"]}
            for row in output_groups
        ],
        "review_instruction": (
            "This is preparation for review only. Do not apply or modify the approved "
            "100-group mapping without separate explicit approval."
        ),
    }
    return proposal, summary


def write_json(path: str | Path, value: Any) -> None:
    Path(path).write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review", required=True, help="fresh read-only review manifest JSON")
    parser.add_argument("--approved-mapping", default=str(
        Path(__file__).with_name("approved_law_legacy_mapping.json")
    ))
    parser.add_argument("--out", required=True, help="proposed mapping JSON output")
    parser.add_argument("--summary", required=True, help="human-review summary JSON output")
    args = parser.parse_args(argv)
    try:
        protected = {
            Path(args.approved_mapping).resolve(),
            Path(args.review).resolve(),
        }
        if Path(args.out).resolve() in protected or Path(args.summary).resolve() in protected:
            raise ProposalRefused("output paths must not overwrite the approved mapping or review manifest")
        if Path(args.out).resolve() == Path(args.summary).resolve():
            raise ProposalRefused("proposal and summary outputs must use different paths")
        mapping, digest = load_mapping(args.approved_mapping)
        manifest = json.loads(Path(args.review).read_text(encoding="utf-8"))
        proposal, summary = build_proposal(mapping, digest, manifest)
        write_json(args.out, proposal)
        write_json(args.summary, summary)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ProposalRefused) as exc:
        print(f"ULaw 61-group proposal refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "status": proposal["status"], "groups": 61, "historical_ids": 311,
        "aliases": 250, "production_writes_performed": False,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())