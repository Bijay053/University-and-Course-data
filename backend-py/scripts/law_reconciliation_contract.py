"""Byte-pinned human review contract; never infer unions from matching names."""
import hashlib
import json
from pathlib import Path

from promote_law_61_mapping import ORIGINAL_SHA256, PROPOSAL_SHA256


def _pinned(path, digest):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError(f"Reviewed Law contract fingerprint changed: {path.name}")
    return json.loads(raw)


_directory = Path(__file__).resolve().parent
PROPOSAL = _pinned(
    _directory.parents[1] / "reports/university-of-law/law-proposed-61.json",
    PROPOSAL_SHA256,
)
ORIGINAL = _pinned(_directory / "archived_law_100_mapping.json", ORIGINAL_SHA256)
REVIEWED_GROUPS = {group["parent"]: group for group in PROPOSAL["groups"]}
ORIGINAL_GROUPS = {group["parent"]: group for group in ORIGINAL["groups"]}


def reviewed_union(parent, ids):
    group = REVIEWED_GROUPS.get(parent)
    return bool(group and sorted(ids) == group["ids"] and (
        len(group["merged_original_parents"]) > 1 or parent in {9389, 9396}
    ))


def component_count(parent):
    group = REVIEWED_GROUPS.get(parent)
    if group is None:
        return 1
    return sum(3 if old in {9389, 9396} else 1
               for old in group["merged_original_parents"])


def duplicate_campus_allowed(parent, ids, location, fee):
    if not reviewed_union(parent, ids):
        return False
    # These unchanged original unions retain their narrower campus/fee approvals.
    if parent in {9389, 9396}:
        campuses, amount = (
            ({"birmingham", "leeds", "manchester"}, 17500) if parent == 9389
            else ({"birmingham", "manchester"}, 18250)
        )
        return location in campuses and fee == {
            "amount": amount, "currency": "GBP", "fee_year": 2026,
            "fee_term": "Full Course",
        }
    return True


def original_topology_matches(signatures):
    """Preserve all 104 evidence components, including the original 100 partition."""
    counts = {parent: 0 for parent in ORIGINAL_GROUPS}
    seen = set()
    for signature in signatures:
        ids = set(signature)
        owners = [parent for parent, group in ORIGINAL_GROUPS.items()
                  if ids and ids.issubset(group["ids"])]
        if len(owners) != 1 or seen.intersection(ids):
            return False
        seen.update(ids)
        counts[owners[0]] += 1
    return (
        seen == {cid for group in ORIGINAL_GROUPS.values() for cid in group["ids"]}
        and all(count == (3 if parent in {9389, 9396} else 1)
                for parent, count in counts.items())
    )


def reviewed_identity_matches(parent, degree, variant, mode):
    group = REVIEWED_GROUPS[parent]
    norm = lambda value: " ".join(str(value or "").casefold().split())
    return (
        norm(degree) == group["identity_key"]["canonical_degree"]
        and norm(variant) == group["identity_key"]["study_variant"]
        and norm(mode) == norm(group["study_mode"])
    )