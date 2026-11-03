"""No network, live-row mutations, or scraper inference in semantic contracts."""
import pytest
from types import SimpleNamespace
from pydantic import ValidationError
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, insert, select, update

from app.study_modes import StudyModeText, normalize_study_mode, sanitize_study_mode_payload

PROSE = (
    "This degree equips you with the technical knowledge and practical skills needed "
    "to begin your career as a chartered accountant. Focusing on UK accounting "
    "standards in real-world contexts, the course prepares you for a range of "
    "finance roles in global business environments."
)
VALID = [
    "On Campus", "On-campus", "Online", "Blended", "Hybrid", "Mixed", "Both",
    "On Campus, Online", "Online / On Campus", "Full Time", "Full-time, Part-time",
    "Both on-campus and online", "Mode of study: On Campus / Online",
    "100% Online", "Fully on-campus", "FT / PT",
    "Flexible learning (online and campus-based workshops)",
    "FullTime/PartTime", "PartTimeOnly", "Part-time only",
    "Face-to-face", "In person", "Distance learning", "External",
    "On Campus (full-time) and Online (part-time)",
    "Study mode: Blended learning (face-to-face workshops and live online sessions)",
    "On-campus teaching supported by online learning materials; attendance in person "
    "is required for practical workshops, laboratory sessions and scheduled "
    "assessments throughout each semester, with additional synchronous online "
    "tutorials and asynchronous learning between the campus sessions.",
    "This course is delivered fully online with two compulsory campus workshops",
]
INVALID = [
    PROSE, "Key features", "Accounting", "Full", "On campus jobs",
    "You can study flexibly, around your new career in Interior Design.",
    "Online learning equips students with knowledge and practical skills.",
    "On-campus students will develop skills in financial accounting.",
    "Mixed media and visual arts", "Online business degree designed for your career",
    123, ["On Campus"], {"mode": "Online"},
]


@pytest.mark.parametrize("value", VALID)
def test_delivery_and_load_labels_are_preserved(value):
    assert normalize_study_mode(value) == value


@pytest.mark.parametrize("value", INVALID)
def test_semantic_rejection_is_not_just_a_length_limit(value):
    assert normalize_study_mode(value) is None


@pytest.mark.parametrize("value", [None, "", "Unknown", "N/A", "Not specified"])
def test_unknown_stays_unknown(value):
    assert normalize_study_mode(value) is None


def test_payload_keeps_course_and_raw_review_evidence_without_mutating_source():
    evidence = {"field_key": "study_mode", "value": PROSE, "normalized": PROSE,
                "decision_status": "selected", "method": "ai_rule:css"}
    payload = {"course_name": "Accounting Top Up BSc (Hons)", "study_mode": PROSE,
               "scrape_warnings": ["unrelated"], "evidence": [evidence]}
    cleaned = sanitize_study_mode_payload(payload)
    assert cleaned["course_name"] == payload["course_name"]
    assert cleaned["study_mode"] is None
    assert cleaned["scrape_warnings"] == ["unrelated", "invalid_study_mode"]
    assert cleaned["evidence"][0]["value"] == PROSE
    assert cleaned["evidence"][0]["decision_status"] == "needs_review"
    assert cleaned["evidence"][0]["selected"] is False
    assert payload["study_mode"] == PROSE
    assert evidence["decision_status"] == "selected"
    assert sanitize_study_mode_payload(cleaned) == cleaned


def test_partial_writes_do_not_invent_a_mode_or_modify_unrelated_fields():
    payload = {"international_fee": 32000}
    assert sanitize_study_mode_payload(payload) is payload
    assert sanitize_study_mode_payload({"study_mode": "On Campus, Online"}) == {"study_mode": "On Campus, Online"}


def test_valid_alternative_evidence_is_not_mislabeled_and_recovery_clears_only_its_warning():
    valid = {"field_key": "study_mode", "normalized": {"study_mode": "Blended"},
             "decision_status": "superseded"}
    cleaned = sanitize_study_mode_payload({"study_mode": PROSE, "evidence": [valid]})
    assert cleaned["evidence"] == [valid]
    assert cleaned["study_mode"] is None  # Do not guess from an alternative.
    recovered = sanitize_study_mode_payload({
        "study_mode": "Blended", "scrape_warnings": ["unrelated", "invalid_study_mode"],
    })
    assert recovered["scrape_warnings"] == ["unrelated"]


def test_create_and_update_validate_but_existing_read_contract_is_unchanged():
    from app.schemas.course import CourseBase, CourseCreate, CourseUpdate

    for schema, extra in [(CourseCreate, {"name": "Accounting", "university_id": 1}), (CourseUpdate, {})]:
        with pytest.raises(ValidationError, match="Study mode must describe"):
            schema(**extra, study_mode=PROSE)
        for value in VALID:
            assert schema(**extra, study_mode=value).study_mode == value
        assert schema(**extra, study_mode=None).study_mode is None
    assert CourseBase(name="Legacy", university_id=1, study_mode=PROSE).study_mode == PROSE


@pytest.mark.parametrize("table_name", ["courses", "scraped_courses"])
def test_actual_mapped_bind_type_guards_inserts_and_bulk_updates_without_rewriting_reads(table_name):
    from app.models.course import Course
    from app.models.scraped_course import ScrapedCourse

    model = Course if table_name == "courses" else ScrapedCourse
    mode_type = model.__table__.c.study_mode.type
    assert isinstance(mode_type, StudyModeText)
    assert str(mode_type.compile()) == "TEXT"
    # Private in-memory table uses the actual production column's bind type.
    engine = create_engine("sqlite:///:memory:")
    table = Table("mode_probe", MetaData(), Column("id", Integer, primary_key=True),
                  Column("study_mode", mode_type), Column("note", Text))
    table.create(engine)
    try:
        with engine.begin() as db:
            db.execute(insert(table), [{"id": 1, "study_mode": PROSE},
                                       {"id": 2, "study_mode": VALID[-2]}])
            assert db.scalar(select(table.c.study_mode).where(table.c.id == 1)) is None
            assert db.scalar(select(table.c.study_mode).where(table.c.id == 2)) == VALID[-2]
            db.execute(update(table).where(table.c.id == 2).values(study_mode=PROSE))
            assert db.scalar(select(table.c.study_mode).where(table.c.id == 2)) is None
            db.exec_driver_sql("INSERT INTO mode_probe (id,study_mode) VALUES (?,?)", (3, PROSE))
            db.execute(update(table).where(table.c.id == 3).values(note="unrelated edit"))
            assert db.scalar(select(table.c.study_mode).where(table.c.id == 3)) == PROSE
            assert db.scalar(select(table.c.id).where(table.c.study_mode.like("%chartered%"))) == 3
    finally:
        engine.dispose()


def test_original_online_and_full_time_guards_still_reject_ineligible_labels():
    from app.services.scraper.guards import is_online_only_for_staging
    from app.services.scraper.approve_course import is_part_time_only_course

    assert is_online_only_for_staging("Accounting", {"study_mode": "Online"})
    assert not is_online_only_for_staging("Accounting", {"study_mode": "On Campus, Online"})
    # Valid legacy load wording must not be blanked before load eligibility.
    assert normalize_study_mode("PartTimeOnly") == "PartTimeOnly"
    assert is_part_time_only_course(SimpleNamespace(study_mode="Part-time only", study_load=None))
    assert not is_part_time_only_course(SimpleNamespace(study_mode="Full-time, Part-time", study_load=None))


def test_staging_sanitizes_only_after_eligibility_and_preservation():
    import inspect
    from app.services.scraper.stage_course import stage_course

    source = inspect.getsource(stage_course)
    boundary = source.index("mode_guard = sanitize_study_mode_payload")
    assert boundary > source.index("should_stage_course(name, payload")
    assert boundary > source.index("# Approved-row preservation above")
    assert boundary < source.index("sc = ScrapedCourse(")


@pytest.mark.asyncio
async def test_confirmed_paragraph_stages_as_unknown_without_discarding_course():
    from unittest.mock import AsyncMock, MagicMock
    from app.services.scraper.stage_course import stage_course
    from app.models.scraped_course import ScrapedCourse

    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    result.scalar.return_value = None
    result.first.return_value = None
    result.scalars.return_value.all.return_value = []
    db = MagicMock()
    db.execute = AsyncMock(return_value=result)
    db.get = AsyncMock(return_value=None)
    db.flush = AsyncMock()
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.add = MagicMock()
    await stage_course(
        db=db, scrape_job_id=2, university_id=1,
        course_name="Accounting Top Up BSc (Hons)",
        source_url="https://example.edu/courses/accounting-bsc",
        payload={"study_mode": PROSE, "degree_level": "Bachelor",
                 "international_fee": 17000, "currency": "GBP", "fee_term": "Annual"},
        evidence=[{"field_key": "study_mode", "value": PROSE, "normalized": PROSE,
                   "method": "ai_rule:css", "decision_status": "selected",
                   "source_url": "https://example.edu/courses/accounting-bsc",
                   "page_type": "course_page", "snippet": PROSE, "raw_text": PROSE}],
    )
    staged = [call.args[0] for call in db.add.call_args_list if isinstance(call.args[0], ScrapedCourse)]
    assert len(staged) == 1
    assert staged[0].course_name == "Accounting Top Up BSc (Hons)"
    assert staged[0].study_mode is None
    assert "invalid_study_mode" in staged[0].scrape_warnings


@pytest.mark.asyncio
async def test_invalid_online_prose_cannot_erase_original_staging_eligibility_signal():
    from unittest.mock import AsyncMock, MagicMock
    from app.services.scraper.stage_course import stage_course

    db = MagicMock()
    db.execute = AsyncMock()
    result = await stage_course(
        db=db, scrape_job_id=2, university_id=1,
        course_name="Bachelor of Accounting",
        source_url="https://example.edu/courses/accounting-bsc",
        payload={"study_mode": "Online learning equips students with knowledge and practical skills."},
        evidence=[],
    )
    db.add.assert_not_called()
    db.execute.assert_not_awaited()
    assert "online_only" in result.reason
