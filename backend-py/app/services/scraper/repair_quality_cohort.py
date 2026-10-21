"""Compare field coverage only for unambiguously identical staged courses."""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urldefrag

FIELDS = (
    "fee_pct", "ielts_pct", "location_pct", "duration_pct", "course_name_pct",
    "intakes_pct", "mode_pct", "degree_level_pct",
)


def course_presence(rows: list[dict]) -> dict:
    result = {}
    ambiguous = set()
    for row in rows:
        if not row.get("url") or not row.get("name"):
            continue
        identity = json.dumps([
            urldefrag(row["url"])[0], row["name"], row.get("location"),
        ], separators=(",", ":"), ensure_ascii=False)
        key = hashlib.sha256(identity.encode()).hexdigest()[:32]
        if key in result:
            ambiguous.add(key)
        result[key] = row["fields"]
    return {key: fields for key, fields in result.items() if key not in ambiguous}


def matched_comparison(before: dict, after: dict) -> dict:
    left, right = before.get("course_presence") or {}, after.get("course_presence") or {}
    common = sorted(left.keys() & right.keys())
    losses, gains = {}, {}
    for index, field in enumerate(FIELDS):
        losses[field] = sum(bool(left[k][index]) and not bool(right[k][index]) for k in common)
        gains[field] = sum(not bool(left[k][index]) and bool(right[k][index]) for k in common)
    comparable = bool(common) and len(common) == int(after.get("total_staged") or 0)
    return {
        "matched_courses": len(common),
        "unmatched_verification_courses": int(after.get("total_staged") or 0) - len(common),
        "comparison_verified": comparable,
        "regressions": [field for field in FIELDS if losses[field]],
        "improved_fields": [field for field in FIELDS if gains[field] and not losses[field]],
        "lost_field_counts": losses, "recovered_field_counts": gains,
        "comparison_basis": "same-course field presence" if common else "no matched baseline evidence",
    }
