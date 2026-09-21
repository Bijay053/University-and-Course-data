from app.services.scraper.approve_course import _academic_mapping_key


def test_requested_degree_levels_map_to_stable_setting_keys():
    expected = {
        "Master": "bachelors_equivalent",
        "Graduate Certificate & Diploma": "bachelors_equivalent",
        "Doctor/Doctorate": "masters_equivalent",
        "Associate Degree or Equivalent": "grade_12_equivalent",
        "Certificate & Diploma": "grade_12_equivalent",
        "Bachelor": "grade_12_equivalent",
    }

    for degree_level, mapping_key in expected.items():
        assert _academic_mapping_key(degree_level) == mapping_key


def test_scraper_degree_variants_use_the_same_setting_keys():
    assert _academic_mapping_key("Master's") == "bachelors_equivalent"
    assert _academic_mapping_key("Graduate Certificate") == "bachelors_equivalent"
    assert _academic_mapping_key("Graduate Diploma") == "bachelors_equivalent"
    assert _academic_mapping_key("Doctorate") == "masters_equivalent"
    assert _academic_mapping_key("PhD") == "masters_equivalent"
    assert _academic_mapping_key("Doctorate/PhD") == "masters_equivalent"
    assert _academic_mapping_key("Associate Degree") == "grade_12_equivalent"
    assert _academic_mapping_key("Bachelor's") == "grade_12_equivalent"
    assert _academic_mapping_key("Advanced Diploma") == "grade_12_equivalent"


def test_unmapped_degree_level_does_not_invent_an_academic_level():
    assert _academic_mapping_key("Foundation") is None
    assert _academic_mapping_key(None) is None