"""Evidence-owned campus fee partitions; never choose one price for all locations."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha256
import re

from sqlalchemy import select

from app.services.scraper.extractors.ulaw_fees import validated_fee_variants

SCOPE = "campus_fee_scope"


def _norm(value):
    return " ".join(str(value).casefold().split())


def _locations(value):
    if not isinstance(value, str):
        return []
    return list(dict.fromkeys(part.strip() for part in re.split(r"[,;|\n]", value) if part.strip()))


def _matches(label, campus):
    label, campus = _norm(label), _norm(campus)
    if campus in {"bloomsbury", "moorgate"}:
        campus = f"london {campus}"
    london = campus == "london" or campus.startswith("london ")
    if label in {"outside london", "outside of london", "non-london", "non london"}:
        return not london
    if label == "london":
        return london
    if label in {"all locations", "all campuses", "london and outside london"}:
        return True
    return campus in {_norm(part) for part in label.split("/")}


def plan_campus_fees(row):
    """Return all groups, or an explicit review reason. No writes or guesses."""
    get = row.get if isinstance(row, dict) else lambda key: getattr(row, key, None)
    authority = validated_fee_variants(row)
    if not authority:
        return [], "Published fee evidence is missing or no longer matches this course."
    locations = _locations(get("course_location"))
    if not locations or any(_norm(c) in {
        "online", "on campus", "uk", "united kingdom", "various", "tbc", "outside london",
    } for c in locations):
        return [], "Actual course-owned campus names are required before splitting."
    selected = authority["selected"]
    if len({(o["year"], o["period"], o["study_variant"]) for o in selected}) != 1:
        return [], "Years, fee periods or study routes differ; manual review is required."
    campus_proof = (get("extraction_method") or {}).get("campus_authority") or {}
    course_owned = (
        campus_proof.get("method") == "location.ulaw_course_authority"
        and campus_proof.get("source_url") == get("course_website")
        and {_norm(c) for c in campus_proof.get("locations", [])} == {_norm(c) for c in locations}
        and campus_proof.get("fee_year") == authority["fee_year"]
        and campus_proof.get("fee_term") == authority["fee_term"]
        and campus_proof.get("study_variant") == selected[0]["study_variant"]
        and bool(campus_proof.get("snippet"))
    )
    if not course_owned and any(not any(_matches(o["campus"], c) for c in locations) for o in selected):
        return [], "A published campus option cannot be mapped to this course's locations."
    groups = {}
    for campus in locations:
        options = [o for o in selected if _matches(o["campus"], campus)]
        prices = {o["amount"] for o in options}
        if len(prices) != 1:
            return [], f"Fee for {campus} is missing or conflicting."
        amount = next(iter(prices))
        group = groups.setdefault(amount, {"locations": [], "selected": [], "amount": amount})
        group["locations"].append(campus)
        for option in options:
            if option not in group["selected"]:
                group["selected"].append(option)
    result = []
    for group in groups.values():
        group["locations"].sort(key=_norm)
        group["key"] = sha256("|".join(_norm(c) for c in group["locations"]).encode()).hexdigest()[:20]
        group["authority"] = {
            **deepcopy(authority), "selected": deepcopy(group["selected"]),
            "status": "uniform", "international_fee": group["amount"],
        }
        result.append(group)
    return sorted(result, key=lambda g: (not any(_norm(c).startswith("london") for c in g["locations"]), g["key"])), None


def _apply_group(row, group, original_name, original_locations):
    row.fee_scope_key = group["key"]
    row.course_name = f"{original_name} — {', '.join(group['locations'])}"
    row.course_location = ", ".join(group["locations"])
    row.international_fee = group["amount"]
    row.extraction_method = {
        **deepcopy(row.extraction_method or {}),
        "fee_variants": deepcopy(group["authority"]),
        SCOPE: {
            "key": group["key"], "locations": group["locations"],
            "original_name": original_name, "original_locations": original_locations,
            "source_url": row.course_website,
        },
    }
    row.scrape_warnings = [w for w in (row.scrape_warnings or []) if w != "international_fee_varies_by_campus"]
    row.status = "pending"
    if row.auto_publish_status != "data_quality_failure":
        row.auto_publish_status = "review"
    row.course_id = None
    row.reviewed_at = None
    row.pub_decision = None
    row.pub_decision_reason = None
    row.pub_score = None
    row.pub_score_breakdown = None
    from app.services.scraper.completeness import compute_completeness, decide_eligibility
    completeness = compute_completeness(row)
    row.completeness = completeness.score
    eligibility = decide_eligibility(row, completeness)
    row.eligibility_status = eligibility.status
    row.eligibility_reason = eligibility.reason


async def split_pending_course(db, row, *, actor="scraper"):
    """Caller holds the row lock and commits the whole transaction."""
    from app.models import ScrapedCourse, ScrapedFieldEvidence, FieldConflict
    if row.status not in {"pending", "review_ready"}:
        return {"id": row.id, "status": "unchanged", "courseIds": [row.id], "reason": "Only pending courses can be split."}
    if (row.extraction_method or {}).get(SCOPE):
        return {"id": row.id, "status": "unchanged", "courseIds": [row.id], "reason": "Already split by campus."}
    groups, reason = plan_campus_fees(row)
    if not groups:
        return {"id": row.id, "status": "needs_review", "courseIds": [row.id], "reason": reason}
    if len(groups) == 1 and (row.extraction_method or {}).get("fee_variants", {}).get("status") != "range":
        return {"id": row.id, "status": "unchanged", "courseIds": [row.id], "reason": "Applicable campuses have the same fee."}
    evidence = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == row.id,
    ))).scalars().all()
    conflicts = (await db.execute(select(FieldConflict).where(
        FieldConflict.scraped_course_id == row.id,
    ))).scalars().all()
    values = {c.name: deepcopy(getattr(row, c.name)) for c in ScrapedCourse.__table__.columns
              if c.name not in {"id", "created_at", "canonical_course_url"}}
    name, locations = row.course_name, row.course_location
    children = [row]
    for group in groups[1:]:
        child = ScrapedCourse(**deepcopy(values))
        _apply_group(child, group, name, locations)
        db.add(child)
        children.append(child)
    _apply_group(row, groups[0], name, locations)
    for child in children:
        child.extraction_method = {
            **child.extraction_method,
            SCOPE: {
                **child.extraction_method[SCOPE],
                "split_from_id": row.id, "split_actor": actor,
                "split_at": datetime.now(timezone.utc).isoformat(),
            },
        }
    await db.flush()
    for child in children[1:]:
        copied_evidence = {}
        for item in evidence:
            ev = {c.name: deepcopy(getattr(item, c.name)) for c in ScrapedFieldEvidence.__table__.columns
                  if c.name not in {"id", "created_at", "scraped_course_id"}}
            copy = ScrapedFieldEvidence(scraped_course_id=child.id, **ev)
            db.add(copy)
            copied_evidence[item.id] = copy
        await db.flush()
        for conflict in conflicts:
            values = {c.name: deepcopy(getattr(conflict, c.name)) for c in FieldConflict.__table__.columns
                      if c.name not in {"id", "created_at", "scraped_course_id", "course_id", "evidence_a_id", "evidence_b_id"}}
            db.add(FieldConflict(
                scraped_course_id=child.id, **values,
                evidence_a_id=getattr(copied_evidence.get(conflict.evidence_a_id), "id", None),
                evidence_b_id=getattr(copied_evidence.get(conflict.evidence_b_id), "id", None),
            ))
    return {"id": row.id, "status": "split", "courseIds": [child.id for child in children]}


def scope_refresh_payload(row, payload):
    """A re-extract must not expand a child back into the unsplit parent."""
    scope = (row.extraction_method or {}).get(SCOPE)
    if not scope:
        return payload
    payload = deepcopy(payload)
    payload.pop("course_name", None)
    payload.pop("course_location", None)
    payload.pop("canonical_course_url", None)
    payload.pop("fee_scope_key", None)
    if "international_fee" not in payload and "fee_variants" not in (payload.get("extraction_method") or {}):
        if "extraction_method" in payload:
            payload["extraction_method"] = {**deepcopy(row.extraction_method or {}), **payload["extraction_method"], SCOPE: deepcopy(scope)}
        return payload
    fresh = {c.name: getattr(row, c.name) for c in row.__table__.columns}
    fresh.update(payload)
    fresh["course_location"] = scope["original_locations"]
    groups, reason = plan_campus_fees(fresh)
    owned = {_norm(c) for c in scope["locations"]}
    matching = [g for g in groups if owned <= {_norm(c) for c in g["locations"]}]
    metadata = {**deepcopy(row.extraction_method or {}), **deepcopy(payload.get("extraction_method") or {})}
    metadata[SCOPE] = deepcopy(scope)
    if len(matching) == 1:
        group = matching[0]
        authority = deepcopy(group["authority"])
        authority["selected"] = [o for o in authority["selected"] if any(_matches(o["campus"], c) for c in scope["locations"])]
        metadata["fee_variants"] = authority
        payload.update(international_fee=group["amount"], fee_year=authority["fee_year"],
                       fee_term=authority["fee_term"], currency=authority["currency"])
    else:
        metadata["fee_variants"] = {**(metadata.get("fee_variants") or {}), "status": "unresolved", "international_fee": None, "selected": []}
        payload["international_fee"] = None
        payload["scrape_warnings"] = list(payload.get("scrape_warnings") or []) + ["campus_fee_scope_requires_review"]
    payload["extraction_method"] = metadata
    return payload