"""Promote only the explicitly approved, byte-pinned offline proposal."""
import hashlib
import json
from pathlib import Path

PROPOSAL_SHA256 = "3cc6e40e65f93a17b59543c750f6c7491477f1041bd757fb54958028d8693617"
ORIGINAL_SHA256 = "c895a22170b62cbae095c6d689735689b8542d4eab563e36d1be8a96bf22f8a5"


def promote():
    directory = Path(__file__).resolve().parent
    proposal_bytes = (directory.parents[1] / "reports/university-of-law/law-proposed-61.json").read_bytes()
    if hashlib.sha256(proposal_bytes).hexdigest() != PROPOSAL_SHA256:
        raise ValueError("Proposal fingerprint differs from explicit user approval")
    target = directory / "approved_law_legacy_mapping.json"
    archive = directory / "archived_law_100_mapping.json"
    original = archive.read_bytes() if archive.exists() else target.read_bytes()
    if hashlib.sha256(original).hexdigest() != ORIGINAL_SHA256:
        raise ValueError("Original mapping fingerprint differs from reviewed baseline")
    proposal = json.loads(proposal_bytes)
    baseline = json.loads(original)
    assert proposal["unscoped_aliases"] == baseline["unscoped_aliases"]
    assert len(proposal["groups"]) == 61
    assert len(proposal["aliases"]) == 246
    promoted = {
        "schema_version": 1, "university_id": 92,
        "approval": "User explicitly approved the exact 61-group proposal: 307 scoped IDs, 246 scoped aliases plus four unchanged unscoped aliases; 311 historical IDs and 250 aliases. Production execution requires a fresh independently approved live manifest.",
        "approved_proposal_sha256": PROPOSAL_SHA256,
        "groups": proposal["groups"],
        "unscoped_aliases": proposal["unscoped_aliases"],
    }
    if not archive.exists():
        archive.write_bytes(original)
    target.write_text(json.dumps(promoted, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    promote()