"""Promote a staged course into the live ``courses`` table.

Bug #1 fix: case-insensitive duplicate detection via ``func.lower()``.
The Node version did a literal equality check, so 'Bachelor of Arts' and
'bachelor of arts' would both get inserted, polluting the public search.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AcademicRequirement,
    AcademicLevelOption,
    Course,
    EnglishRequirement,
    Fee,
    Intake,
    ScrapedCourse,
)
from app.services.auto_publish import should_auto_publish
from app.services.scraper.category import infer_course_taxonomy
from app.services.scraper.taxonomy import canonical_parent
from app.services.sub_category_matcher import resolve_sub_category

import re


_ACADEMIC_MAPPING_BY_DEGREE = {
    "master": "bachelors_equivalent",
    "master's": "bachelors_equivalent",
    "graduate certificate & diploma": "bachelors_equivalent",
    "graduate certificate": "bachelors_equivalent",
    "graduate diploma": "bachelors_equivalent",
    "doctor/doctorate": "masters_equivalent",
    "doctorate": "masters_equivalent",
    "doctor": "masters_equivalent",
    "phd": "masters_equivalent",
    "doctorate/phd": "masters_equivalent",
    "doctorate / phd": "masters_equivalent",
    "associate degree or equivalent": "grade_12_equivalent",
    "associate degree": "grade_12_equivalent",
    "certificate & diploma": "grade_12_equivalent",
    "certificate": "grade_12_equivalent",
    "diploma": "grade_12_equivalent",
    "advanced diploma": "grade_12_equivalent",
    "bachelor": "grade_12_equivalent",
    "bachelor's": "grade_12_equivalent",
}


def _academic_mapping_key(degree: str | None) -> str | None:
    if not degree:
        return None
    return _ACADEMIC_MAPPING_BY_DEGREE.get(degree.strip().lower())


async def _academic_option_for_degree(db: AsyncSession, degree: str | None):
    key = _academic_mapping_key(degree)
    if not key:
        return None
    option = (await db.execute(
        select(AcademicLevelOption).where(AcademicLevelOption.mapping_key == key)
    )).scalar_one_or_none()
    if option is None:
        raise RuntimeError(
            f"Settings > Academic Levels is missing the required mapping {key!r}"
        )
    return option


async def _academic_option_for_staged_course(
    db: AsyncSession,
    sc: ScrapedCourse,
    degree: str | None,
) -> AcademicLevelOption | None:
    mapped = await _academic_option_for_degree(db, degree)
    if mapped is not None:
        return mapped
    if sc.academic_level_option_id is not None:
        option = await db.get(AcademicLevelOption, sc.academic_level_option_id)
        if option is None:
            raise RuntimeError("Staged academic level setting no longer exists")
        return option
    if sc.academic_level:
        return (await db.execute(
            select(AcademicLevelOption).where(
                AcademicLevelOption.name == sc.academic_level
            )
        )).scalar_one_or_none()
    return None

# A sub_category that is just the degree-level echo ("Doctor of Philosophy",
# "Master of Education", "Bachelor of Arts" …). These are degrees, not fields.
_DEGREE_NAME_RE = re.compile(
    r"^\s*(doctor|master|bachelor|graduate (certificate|diploma)|associate degree|"
    r"diploma|certificate( i+v?)?|phd|mphil|dphil)\b.*$",
    re.IGNORECASE,
)


_ENGLISH_TESTS = (
    ("ielts", "ielts_overall", "ielts_listening", "ielts_speaking", "ielts_writing", "ielts_reading"),
    ("pte", "pte_overall", "pte_listening", "pte_speaking", "pte_writing", "pte_reading"),
    ("toefl", "toefl_overall", "toefl_listening", "toefl_speaking", "toefl_writing", "toefl_reading"),
    ("cambridge", "cambridge_overall", None, None, None, None),
    ("duolingo", "duolingo_overall", None, None, None, None),
)


async def approve_scraped_course(
    db: AsyncSession, sc: ScrapedCourse, *, actor: str = "system"
) -> dict:
    """Idempotent: if a course with the same (university_id, name CI) exists,
    the row is updated rather than duplicated.

    Raises ``ValueError`` if ``sc.course_name`` is None or empty — historically
    this crashed at the case-insensitive lookup with a confusing AttributeError
    on ``None.lower()``, which then poisoned the SQLAlchemy session and made
    every subsequent row in a batch fail (Week 5: Charles Sturt promotion gap).
    """
    from app.services.scraper.fee_selection import fee_selection, unresolved_fee_selection
    if isinstance(getattr(sc, "extraction_method", None), dict) and sc.extraction_method.get("fee_variants"):
        sc = (await db.execute(
            select(ScrapedCourse).where(ScrapedCourse.id == sc.id)
            .with_for_update().execution_options(populate_existing=True)
        )).scalar_one()
    if unresolved_fee_selection(sc):
        raise ValueError("Select a current published fee option before approval")
    if not sc.course_name or not sc.course_name.strip():
        raise ValueError(
            f"scraped_course id={sc.id} has empty course_name; cannot promote"
        )
    from app.services.scraper.extractors.ulaw_fees import validated_fee_variants
    from app.services.scraper.campus_fee_split import SCOPE
    fee_metadata = sc.extraction_method or {}
    fee_authority = fee_metadata.get("fee_variants")
    # A reviewer may choose a source-owned alternative outside the extractor's
    # original uniform tuple (or resolve a range). Validate its fingerprint and
    # entire current tuple, not merely the presence of selection metadata.
    selected_fee = fee_selection(sc) if fee_authority else None
    if fee_authority and not (selected_fee and selected_fee["selectedOptionId"]) and (
        fee_authority.get("status") != "uniform" or not validated_fee_variants(sc)
    ):
        raise ValueError("Resolve the published fee options before approving this course")
    scope = fee_metadata.get(SCOPE)
    if scope:
        expected_locations = ", ".join(scope["locations"])
        expected_name = f"{scope['original_name']} — {expected_locations}"
        if sc.course_location != expected_locations or sc.course_name != expected_name or sc.fee_scope_key != scope["key"]:
            raise ValueError("Campus fee scope no longer matches the course; review required")
        if actor == "system":
            raise ValueError("Campus fee groups require explicit reviewer approval")
    from app.services.scraper.review_policy import blocks_automatic_followup
    # Automated callers use the default system actor; authenticated review
    # routes pass the operator identity (not necessarily the literal "human").
    if actor == "system" and await blocks_automatic_followup(db, getattr(sc, "scrape_job_id", None)):
        raise ValueError("Review-only source job forbids automatic promotion")
    if actor == "system" and getattr(sc, "auto_publish_status", None) == "data_quality_failure":
        raise ValueError("Critical data-quality failure forbids automatic promotion")

    # Synchronize promotion with offline review-row restoration.  Otherwise a
    # restore could check for approved/published rows immediately before this
    # transaction creates one, leaving a duplicate pending row behind.
    from app.services.scraper.replay_extraction import review_restore_lock_scope

    await db.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
        {"scope": review_restore_lock_scope(sc.university_id)},
    )

    from app.services.scraper.extractors.uel_variants import (
        is_uel_course_url, uel_source_url, uel_variant_key,
    )
    from app.services.scraper.url_identity import canonical_course_url_key

    _uel_variant = is_uel_course_url(sc.course_website or "") and bool(
        uel_variant_key(sc.course_website or "")
    )
    if scope:
        # Names are intentionally location-qualified. Also require exact source
        # and location ownership: a legacy unsplit parent is not this sibling.
        candidates = (
            await db.execute(
                select(Course).where(
                    Course.university_id == sc.university_id,
                    func.lower(Course.name) == sc.course_name.lower(),
                    Course.course_location == sc.course_location,
                )
            )
        ).scalars().all()
        exact = [candidate for candidate in candidates
                 if canonical_course_url_key(candidate.course_website) == canonical_course_url_key(sc.course_website)]
        if len(exact) > 1:
            raise ValueError("Ambiguous existing campus course identity; review required")
        existing = exact[0] if exact else None
    elif _uel_variant:
        # Award names can change independently of route identity. Never merge a
        # selected route into its sibling (or a legacy undifferentiated parent)
        # merely because the two display names happen to match.
        candidates = (
            await db.execute(
                select(Course).where(Course.university_id == sc.university_id)
            )
        ).scalars().all()
        identity = canonical_course_url_key(sc.course_website)
        exact = [
            candidate for candidate in candidates
            if canonical_course_url_key(candidate.course_website) == identity
        ]
        if len(exact) > 1:
            raise ValueError("Ambiguous UEL variant identity; manual review required")
        existing = exact[0] if exact else None
        if existing is None:
            # Adopt a legacy parent only when BOTH the exact canonical source
            # and normalized display name match. This preserves a standard
            # award's live ID without folding placement/MFA siblings into it.
            source_identity = canonical_course_url_key(uel_source_url(sc.course_website))
            normalized_name = " ".join(sc.course_name.split()).casefold()
            legacy = [
                candidate for candidate in candidates
                if canonical_course_url_key(candidate.course_website) == source_identity
                and not uel_variant_key(candidate.course_website or "")
                and " ".join(candidate.name.split()).casefold() == normalized_name
            ]
            if len(legacy) > 1:
                raise ValueError("Ambiguous legacy UEL course identity; manual review required")
            existing = legacy[0] if legacy else None
    else:
        existing = (
            await db.execute(
                select(Course).where(
                    Course.university_id == sc.university_id,
                    func.lower(Course.name) == sc.course_name.lower(),  # Bug #1
                )
            )
        ).scalar_one_or_none()

    decision = should_auto_publish(sc)

    if existing:
        course = existing
        if _uel_variant:
            course.name = sc.course_name
        course.last_edited_at = datetime.now(timezone.utc)
        course.last_edited_by = actor
    else:
        course = Course(
            university_id=sc.university_id,
            name=sc.course_name,
            status="active",
            approval_status="approved",
            approval_score=decision.score,
            approved_at=datetime.now(timezone.utc),
            last_edited_at=datetime.now(timezone.utc),
            last_edited_by=actor,
        )
        db.add(course)
        await db.flush()

    # A course excluded by one scrape may legitimately return in a later run.
    # Promotion is authoritative for the current catalogue, so reactivate it.
    course.status = "active"

    # Normalise degree_level and category to the canonical frontend values so
    # the edit-form dropdowns always show a selected option.
    _dl = getattr(sc, "degree_level", None)
    if _dl:
        _DL_MAP = {
            "bachelor's": "Bachelor", "bachelor": "Bachelor",
            "master's": "Master", "master": "Master",
            "diploma": "Certificate & Diploma",
            "graduate certificate": "Graduate Certificate & Diploma",
            "graduate diploma": "Graduate Certificate & Diploma",
            "doctorate": "Doctor/Doctorate", "doctor of philosophy": "Doctor/Doctorate",
            "phd": "Doctor/Doctorate",
            "associate degree": "Associate Degree or Equivalent",
        }
        course.degree_level = _DL_MAP.get(_dl.strip().lower(), _dl)
    else:
        course.degree_level = None

    # Drop degree-name echoes before taxonomy inference. The shared helper
    # supplies a neutral General child when no genuine discipline is present.
    _raw_sub = getattr(sc, "sub_category", None)
    if _raw_sub and _DEGREE_NAME_RE.match(_raw_sub.strip()):
        # The AI sometimes echoes the degree name into sub_category — drop it
        # since "Doctor of Philosophy" is a degree, not a research field.
        _raw_sub = None
    _cat = getattr(sc, "category", None)
    course.category = canonical_parent(_cat) if _cat else None

    # Approval is the final consistency boundary before the public catalogue.
    # Direct/imported staged rows can predate the staging safety net, so infer
    # missing taxonomy fields here as well.
    _inferred = infer_course_taxonomy(
        sc.course_name,
        category=course.category,
        sub_category=_raw_sub,
    )
    course.category = _inferred["category"]
    _raw_sub = _inferred["sub_category"]

    # Resolve sub_category via fuzzy matcher — tries to match an existing
    # option for this category; inserts a new row if no match is found so the
    # value is available for future search / course-matching queries.
    _resolved_cat = getattr(course, "category", None)

    if _raw_sub and _resolved_cat:
        course.sub_category = await resolve_sub_category(db, _resolved_cat, _raw_sub)
    else:
        course.sub_category = _raw_sub  # None or blank — keep as-is

    # Copy direct fields — always overwrite so approved scraped data fully
    # replaces any previously-stored values (including clearing stale data
    # that is now null in the scraped record).
    for fld in (
        "course_website",
        # NOTE: sub_category is handled above via resolve_sub_category (fuzzy match + auto-add)
        "duration",
        "duration_term",
        "study_mode",
        "study_load",
        "language",
        "description",
        "other_requirement",
        "course_location",
        "student_market",
        "delivery_mode",
        "international_eligible",
        "on_campus_available",
        "eligibility_status",
        "eligibility_reason",
        "eligibility_confidence",
    ):
        setattr(course, fld, getattr(sc, fld, None))

    # Replace satellite rows wholesale (simpler than diffing, matches Node behaviour).
    await db.execute(EnglishRequirement.__table__.delete().where(
        EnglishRequirement.course_id == course.id
    ))
    for test_type, overall, lst, spk, wrt, rd in _ENGLISH_TESTS:
        v = getattr(sc, overall, None)
        if v is None:
            continue
        db.add(
            EnglishRequirement(
                course_id=course.id,
                test_type=test_type,
                overall=v,
                listening=getattr(sc, lst, None) if lst else None,
                speaking=getattr(sc, spk, None) if spk else None,
                writing=getattr(sc, wrt, None) if wrt else None,
                reading=getattr(sc, rd, None) if rd else None,
            )
        )

    if _uel_variant or sc.intake_months:
        await db.execute(Intake.__table__.delete().where(Intake.course_id == course.id))
    if sc.intake_months:
        for m in sc.intake_months or []:
            db.add(
                Intake(
                    course_id=course.id,
                    intake_month=str(m),
                    intake_day=sc.intake_days,
                )
            )

    if _uel_variant or sc.international_fee is not None:
        await db.execute(Fee.__table__.delete().where(Fee.course_id == course.id))
    if sc.international_fee is not None:
        db.add(
            Fee(
                course_id=course.id,
                international_fee=sc.international_fee,
                fee_term=sc.fee_term,
                fee_year=sc.fee_year,
                currency=sc.currency,
            )
        )

    academic_option = await _academic_option_for_staged_course(
        db, sc, course.degree_level
    )
    if _uel_variant or sc.academic_level or sc.academic_score is not None or academic_option is not None:
        await db.execute(
            AcademicRequirement.__table__.delete().where(
                AcademicRequirement.course_id == course.id
            )
        )
    if sc.academic_level or sc.academic_score is not None or academic_option is not None:
        db.add(
            AcademicRequirement(
                course_id=course.id,
                academic_level=None,
                academic_level_option_id=academic_option.id if academic_option else None,
                academic_score=sc.academic_score,
                score_type=sc.score_type,
                academic_country=sc.academic_country,
            )
        )

    sc.status = "approved"
    sc.auto_publish_status = "approved" if decision.auto_publish else "manual_approved"
    sc.reviewed_at = datetime.now(timezone.utc)
    sc.course_id = course.id

    await db.commit()
    await db.refresh(course)
    return {
        "ok": True,
        "course_id": course.id,
        "scraped_course_id": sc.id,
        "auto_publish": decision.auto_publish,
        "reason": decision.reason,
    }
