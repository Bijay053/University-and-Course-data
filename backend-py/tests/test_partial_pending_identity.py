import pytest

from app.models import ScrapedCourse
from app.services.scraper.pending_imports import compatible_identity
from app.services.scraper.guards import _name_has_degree_qualifier


def course(**changes):
    return ScrapedCourse(
        university_id=114, course_website="https://example.edu/courses/pgce",
        course_name="PGCE Secondary Mathematics", degree_level="Postgraduate",
        **{"study_load": "Full Time", "study_mode": "On Campus",
           "course_location": "Cyncoed", "fee_year": 2027,
           "fee_term": "Full Course", "currency": "GBP", **changes},
    )


@pytest.mark.parametrize("field", [
    "study_load", "study_mode", "course_location", "fee_year", "fee_term", "currency",
])
def test_missing_metadata_is_not_a_distinct_offering(field):
    existing = course()
    incoming = course(**{field: None})
    assert compatible_identity(existing, incoming)
    assert compatible_identity(incoming, existing)


@pytest.mark.parametrize("changes", [
    {"study_load": "Part Time"}, {"study_mode": "Online"},
    {"course_location": "Another campus"}, {"fee_year": 2026},
    {"fee_term": "Annual"}, {"currency": "EUR"},
])
def test_known_variant_discriminators_must_agree(changes):
    assert not compatible_identity(course(), course(**changes))


@pytest.mark.parametrize("name", [
    "Global Design Master's Degree - MDes/PgD/PgC",
    "Sustainable Leadership Postgraduate Certificate - PgC",
    "Forensic Psychology (Practitioner Programme) - PgD",
    "Youth & Community Work - Master's Degrees",
    "Foundation Leading to Engineering & Computer Science",
    "Foundation Certificate in Youth & Community Work",
])
def test_real_qualification_formats_are_recognised(name):
    assert _name_has_degree_qualifier(name)


@pytest.mark.parametrize("name", [
    "Foundation Programmes", "Foundation Courses", "Foundation Events", "Foundation Year Courses",
    "Undergraduate Courses", "Postgraduate Courses", "Campus information",
])
def test_generic_hubs_do_not_gain_a_qualification(name):
    assert not _name_has_degree_qualifier(name)
