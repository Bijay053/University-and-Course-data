"""Campus evidence is not a separate award. Never reconcile old IDs implicitly."""
import hashlib
import json

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert

from app.models.course_offering import CourseOffering


async def read_offerings(db, course_ids):
    """Location-only contract for application consumers; tuition is search-only."""
    result = {cid: [] for cid in course_ids}
    if not course_ids:
        return result
    rows = (await db.execute(select(CourseOffering).where(
        CourseOffering.course_id.in_(course_ids)
    ).order_by(CourseOffering.location, CourseOffering.id))).scalars().all()
    for row in rows:
        result[row.course_id].append({
            "id": str(row.id), "location": row.location,
        })
    return result


def offering_identity(row, scope):
    authority = row.extraction_method["fee_variants"]
    selected = authority["selected"]
    degree = (row.degree_level or "").strip().casefold()
    degree = {"master's": "master", "bachelor's": "bachelor", "doctor": "doctorate"}.get(degree, degree)
    # Deliberately retain the exact route/query/year, plus award name: common
    # source URLs alone never establish that two awards are interchangeable.
    parts = [
        row.university_id, row.course_website,
        " ".join(scope["original_name"].casefold().split()),
        degree,
        selected[0]["study_variant"],
    ]
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()


def validate_offering_cohort(rows, identity):
    from app.services.scraper.approve_course import ApprovalValidationError
    from app.services.scraper.campus_fee_split import SCOPE
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    tuples = set()
    locations = {}
    for row in rows:
        scope = (row.extraction_method or {}).get(SCOPE)
        authority = validated_fee_variants(row)
        if (not scope or not authority or authority["status"] != "uniform"
                or offering_identity(row, scope) != identity
                or scope.get("source_url") != row.course_website
                or row.fee_scope_key != scope["key"]
                or row.course_location != ", ".join(scope["locations"])
                or row.course_name != f"{scope['original_name']} — {row.course_location}"):
            raise ApprovalValidationError("Campus cohort evidence is inconsistent; review required")
        tuples.add((row.fee_year, row.fee_term, row.currency))
        for location in scope["locations"]:
            key = " ".join(location.casefold().split())
            if key in locations:
                raise ApprovalValidationError("Duplicate or contradictory campus cohort; review required")
            locations[key] = (location, row)
    if len(tuples) != 1:
        raise ApprovalValidationError("Mixed fee years, periods or currencies require review")
    return locations, next(iter(tuples))


async def persist_offerings(db, course, row, scope, *, cohort=None):
    from app.services.scraper.approve_course import ApprovalValidationError
    locations, incoming = validate_offering_cohort(cohort or [row], course.offering_identity)
    current = (await db.execute(select(CourseOffering).where(
        CourseOffering.course_id == course.id
    ).execution_options(populate_existing=True))).scalars().all()
    for offering in current:
        if offering.fee_year is not None and (incoming[0] is None or incoming[0] < offering.fee_year):
            raise ApprovalValidationError("Stale fee year cannot overwrite published campus fees")
    changed_cohort = any(
        (o.fee_year, o.fee_term, o.fee_currency) != incoming for o in current
    )
    if changed_cohort and (not cohort or not {o.location_key for o in current} <= locations.keys()):
        raise ApprovalValidationError("Fee cohort changes require all published campuses in one verified approval")
    # The complete cohort is written in the caller's transaction. A subsequent
    # sibling failure rolls all campuses back, never leaving mixed-year tuition.
    for key, (location, source) in locations.items():
        statement = insert(CourseOffering).values(
            course_id=course.id, location_key=key,
            location=location, fee_amount=source.international_fee,
            fee_currency=source.currency, fee_term=source.fee_term,
            fee_year=source.fee_year, source_url=source.course_website,
        )
        await db.execute(statement.on_conflict_do_update(
            constraint="uq_course_offering_location",
            set_={key: getattr(statement.excluded, key) for key in (
                "location", "fee_amount", "fee_currency", "fee_term", "fee_year", "source_url"
            )},
        ))
    offerings = (await db.execute(select(CourseOffering).where(
        CourseOffering.course_id == course.id
    ).order_by(CourseOffering.location))).scalars().all()
    course.course_location = ", ".join(o.location for o in offerings)