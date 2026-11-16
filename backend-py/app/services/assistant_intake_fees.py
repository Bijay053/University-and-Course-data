"""Lossless pending-review tuition schedules; never silently publish one cohort."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re

from sqlalchemy import select

KEY = "intake_fee_schedules"
HOLD = (
    "This course retains intake-specific tuition-year schedules. Publication is held "
    "because reducing them to a single fee year would lose intake applicability. "
    "The pending schedules and original evidence remain available."
)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def build(rows, families, months):
    """Preserve explicit stored years, not a year inferred from a start date."""
    years = [r.fee_year for r in rows]
    if (any(type(y) is not int or not 2000 <= y <= 2100 for y in years)
            or len(set(years)) < 2):
        raise ValueError("Explicit different fee years are required")
    fees = [(r.international_fee, r.currency, r.fee_term) for r in rows]
    amount, currency, basis = fees[0]
    if (amount is None or isinstance(amount, bool) or not math.isfinite(float(amount))
            or float(amount) <= 0 or not isinstance(currency, str)
            or not re.fullmatch(r"[A-Z]{3}", currency)
            or not isinstance(basis, str)
            or basis.casefold() not in {"annual", "full course", "semester", "term", "per credit"}
            or any(f != fees[0] for f in fees)):
        raise ValueError("Conflicting or incomplete tuition values require review")
    if any((r.extraction_method or {}).get(k) for r in rows
           for k in ("fee_variants", KEY, "campus_fee_scope")):
        raise ValueError("An existing fee authority must be reviewed separately")
    grouped, owners = {}, {}
    for row, family in zip(rows, families):
        year = row.fee_year
        # Offering names/URLs are proposal hints only. A disagreement with the
        # stored intake continues to block apply in the parent consolidation plan.
        stored = row.intake_months
        normalized = [next((m for m in months if isinstance(v, str) and m.casefold() == v.casefold()), None)
                      for v in stored] if isinstance(stored, list) else []
        valid = (isinstance(stored, list) and bool(stored)
                 and None not in normalized and family[3] in normalized)
        applicable = normalized if valid else [family[3]]
        for month in applicable:
            if month in owners and owners[month] != year:
                raise ValueError("One intake is assigned to conflicting fee years")
            owners[month] = year
        schedule = grouped.setdefault(year, {
            "feeYear": year, "amount": float(amount), "currency": currency, "basis": basis,
            "entryYear": family[2], "intakes": [], "sourceRowIds": [], "sourceUrls": [],
            "applicabilityNeedsReview": False,
        })
        schedule["intakes"] = [m for m in months if m in set(schedule["intakes"]) | set(applicable)]
        schedule["sourceRowIds"].append(row.id)
        schedule["sourceUrls"].append(row.course_website)
        schedule["applicabilityNeedsReview"] |= not valid
    keep = min(rows, key=lambda r: r.id)
    manifest = {
        "version": 1, "courseName": families[0][0], "canonicalUrl": families[0][1],
        "entryYear": families[0][2], "retainedRowId": keep.id, "primaryFeeYear": keep.fee_year,
        "authority": "existing-records", "verification": "unverified",
        "publicationHold": True, "schedules": [grouped[y] for y in sorted(grouped)],
    }
    manifest["fingerprint"] = digest(manifest)
    return manifest


def intact(manifest):
    if (not isinstance(manifest, dict) or manifest.get("version") != 1
            or not isinstance(manifest.get("schedules"), list)
            or len(manifest["schedules"]) < 2):
        return False
    for schedule in manifest["schedules"]:
        if (not isinstance(schedule, dict)
                or type(schedule.get("feeYear")) is not int
                or type(schedule.get("entryYear")) is not int
                or type(schedule.get("amount")) not in (float, int)
                or not math.isfinite(schedule["amount"]) or schedule["amount"] <= 0
                or not all(isinstance(schedule.get(k), str) for k in ("currency", "basis"))
                or not isinstance(schedule.get("sourceRowIds"), list)
                or not all(type(i) is int for i in schedule["sourceRowIds"])
                or not all(isinstance(schedule.get(k), list) and
                           all(isinstance(v, str) for v in schedule[k]) for k in ("intakes", "sourceUrls"))):
            return False
    return manifest.get("fingerprint") == digest({k: v for k, v in manifest.items() if k != "fingerprint"})


def projection(course):
    """Small snapshot shared by full and summary review responses."""
    metadata = course.get("extraction_method") or course.get("extractionMethod") or {}
    manifest = metadata.get(KEY) if isinstance(metadata, dict) else None
    if not isinstance(manifest, dict):
        return None
    if not intact(manifest):
        return {"schedules": [], "verification": "unverified", "publicationHold": True,
                "snapshotMatchesCurrent": False}
    current = (
        intact(manifest)
        and course.get("id") == manifest.get("retainedRowId")
        and course.get("courseName", course.get("course_name")) == manifest.get("courseName")
        and course.get("courseWebsite", course.get("course_website")) == manifest.get("canonicalUrl")
    )
    schedules = manifest.get("schedules", [])
    if current:
        current = any(
            (course.get("internationalFee", course.get("international_fee")),
             course.get("currency"), course.get("feeTerm", course.get("fee_term")),
             course.get("feeYear", course.get("fee_year")))
            == (s.get("amount"), s.get("currency"), s.get("basis"), s.get("feeYear"))
            for s in schedules
        )
        expected = {m for s in schedules for m in s["intakes"]}
        current = current and set(course.get("intakeMonths", course.get("intake_months")) or []) == expected
    return {**deepcopy(manifest), "snapshotMatchesCurrent": bool(current)}


async def publication_hold(db, row):
    if (row.status == "rejected" and
            (row.rejection_reason or "").startswith("Intake variant consolidated into pending row ")):
        return "Undo the intake consolidation before approving an excluded source row separately."
    if (row.extraction_method or {}).get(KEY):
        return HOLD
    # A later re-extraction must not make the hold disappear by replacing JSON
    # metadata or deselecting derived evidence. Undo writes its own later audit.
    from app.models.audit import CourseAuditLog
    latest = (await db.execute(select(CourseAuditLog).where(
        CourseAuditLog.scraped_course_id == row.id,
        CourseAuditLog.field_key == "intake_consolidation",
    ).order_by(CourseAuditLog.id.desc()).limit(1))).scalar_one_or_none()
    if latest and latest.action == "assistant_intake_merge":
        if f'"{KEY}"' in (latest.new_value or ""):
            return HOLD
        if "Intake variant consolidated into pending row " in (latest.new_value or ""):
            return "Undo the intake consolidation before approving an excluded source row separately."
    return None
