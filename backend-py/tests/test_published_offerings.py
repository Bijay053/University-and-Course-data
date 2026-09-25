from copy import deepcopy
from types import SimpleNamespace

from app.services.scraper.published_offerings import offering_identity
from app.schemas.search import SearchOffering


def test_identity_preserves_award_route_but_not_tuition_cohort():
    row = SimpleNamespace(
        university_id=42, course_website="https://example.edu/course?year=2026",
        degree_level="Master", fee_year=2026, fee_term="Full Course",
        extraction_method={"fee_variants": {"selected": [{"study_variant": "Standard"}]}},
    )
    scope = {"original_name": "MSc Healthcare Management", "locations": ["London"]}
    original = offering_identity(row, scope)
    assert offering_identity(row, {**scope, "locations": ["Leeds"]}) == original
    assert offering_identity(row, {**scope, "original_name": "MBA Healthcare Management"}) != original
    for key, value in [("fee_year", 2027), ("fee_term", "Annual")]:
        other = deepcopy(row)
        setattr(other, key, value)
        assert offering_identity(other, scope) == original
    for key, value in [("course_website", "https://example.edu/course?year=2027"),
                       ("degree_level", "Diploma")]:
        other = deepcopy(row)
        setattr(other, key, value)
        assert offering_identity(other, scope) != original
    row.extraction_method["fee_variants"]["selected"][0]["study_variant"] = "Placement"
    assert offering_identity(row, scope) != original


def test_offering_public_contract_preserves_nulls():
    assert SearchOffering(id="1", location="London").model_dump() == {
        "id": "1", "location": "London", "feeAmount": None, "feeCurrency": None,
        "feeTerm": None, "feeYear": None,
    }


def test_cohort_rejects_duplicate_campus_and_mixed_period():
    import pytest
    from app.models import ScrapedCourse
    from app.services.scraper.campus_fee_split import _apply_group, plan_campus_fees, SCOPE
    from app.services.scraper.published_offerings import validate_offering_cohort
    from app.services.scraper.approve_course import ApprovalValidationError
    from tests.test_campus_fee_split import row_values
    values = row_values()
    groups, _ = plan_campus_fees(values)
    rows = []
    for group in groups:
        row = ScrapedCourse(university_id=1, **deepcopy(values))
        _apply_group(row, group, values["course_name"], values["course_location"])
        rows.append(row)
    identity = offering_identity(rows[0], rows[0].extraction_method[SCOPE])
    with pytest.raises(ApprovalValidationError, match="Duplicate or contradictory"):
        validate_offering_cohort([rows[0], rows[0]], identity)
    row = rows[-1]
    row.fee_term = "Annual"
    row.extraction_method["fee_variants"]["fee_term"] = "Annual"
    for option in row.extraction_method["fee_variants"]["options"]:
        option["period"] = "Annual"
    for option in row.extraction_method["fee_variants"]["selected"]:
        option["period"] = "Annual"
    with pytest.raises(ApprovalValidationError, match="Mixed fee years, periods"):
        validate_offering_cohort(rows, identity)