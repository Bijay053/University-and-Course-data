"""IELTS grouped components stay within the explicitly owned route panel."""
import pytest

from app.services.scraper.extractors.uel_variants import uel_requirement_bands


def panel(text, state="matched"):
    return f'<section data-uel-requirements="{state}"><p>{text}</p></section>'


@pytest.mark.parametrize("text", [
    "IELTS 6.0 (Writing and Speaking 6.0, Listening and Reading 5.5)",
    "IELTS overall 6 (Writing, Speaking 6; Listening / Reading 5.5)",
    "IELTS 6.0 (writing & speaking 6.0, listening and reading 5.5)",
    "IELTS overall 6.0 with a minimum of 6.0 in Writing and Speaking; "
    "5.5 in Reading and Listening.",
])
def test_explicit_grouped_scores(text):
    assert uel_requirement_bands(panel(text)) == {
        "ielts_writing": 6, "ielts_speaking": 6,
        "ielts_listening": 5.5, "ielts_reading": 5.5,
    }


@pytest.mark.parametrize("text", [
    "IELTS 6.0. PTE 60 (Writing and Speaking 60, Listening and Reading 55)",
    "PTE 6 (Writing and Speaking 6, Listening and Reading 5.5)",
    "IELTS 6 (Writing 16.0, Speaking 6.01, Listening 0, Reading 55)",
])
def test_unrelated_tests_and_invalid_values_are_not_components(text):
    assert uel_requirement_bands(panel(text)) == {}


def test_no_inferred_unnamed_skills_or_sibling_panel():
    assert uel_requirement_bands(panel("IELTS 6 (Writing 6)")) == {"ielts_writing": 6}
    assert uel_requirement_bands(panel(
        "IELTS 6 (Writing and Speaking 6, Listening and Reading 5.5)", "missing"
    )) == {}
    assert uel_requirement_bands(
        panel("Academic qualification required.") +
        "<p>IELTS 9 (Writing and Speaking 9, Listening and Reading 9)</p>"
    ) == {}


def test_conflicting_explicit_groups_fail_closed():
    with pytest.raises(ValueError, match="conflicting IELTS"):
        uel_requirement_bands(panel("IELTS 6 (Writing 6, Writing 5.5)"))