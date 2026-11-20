import pytest

from app.services.scraper.requirement_status import _component_evidence_matches


@pytest.mark.parametrize("component", ["listening", "reading", "writing", "speaking"])
@pytest.mark.parametrize("floor", [
    "no sub score lower than 7.0",
    "no sub-score lower than 7.0",
    "at least 7.0 in all elements",
])
def test_explicit_all_component_phrases_preserve_overall_band_distinction(component, floor):
    text = f"IELTS 7.5 overall with {floor}."
    assert _component_evidence_matches("ielts_" + component, 7.0, text)
    assert not _component_evidence_matches("ielts_" + component, 7.5, text)


@pytest.mark.parametrize("component,score", [
    ("reading", 6.5), ("writing", 6.5),
    ("listening", 6.0), ("speaking", 6.0),
])
def test_named_pair_and_explicit_remaining_components(component, score):
    text = ("6.5 overall with 6.5 in both reading and writing, "
            "and a minimum of 6.0 in the remaining components")
    assert _component_evidence_matches("ielts_" + component, score, text)
    assert not _component_evidence_matches("ielts_" + component, 6.0 if score == 6.5 else 6.5, text)


@pytest.mark.parametrize("text", [
    "IELTS 6.5 overall. Minimum 6.0 in the remaining components.",
    "6.5 in both reading and reading, and a minimum of 6.0 in the remaining components",
    "No overall score lower than 6.0.",
    "Reading 6.5, writing 6.5. Another programme requires 6.0.",
])
def test_unknown_or_unrelated_remaining_component_citations_stay_unverified(text):
    assert not _component_evidence_matches("ielts_speaking", 6.0, text)


@pytest.mark.parametrize("score,published", [(6.0, "6.5"), (7.0, "7.5"), (6.5, "6.55")])
def test_component_proof_never_matches_a_numeric_prefix(score, published):
    assert not _component_evidence_matches("ielts_listening", score, f"Listening: {published}.")


def test_sentence_punctuation_does_not_hide_a_valid_component_score():
    assert _component_evidence_matches("ielts_listening", 6.0, "Listening: 6.0.")
