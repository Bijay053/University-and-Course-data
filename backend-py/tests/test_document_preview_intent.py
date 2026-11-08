"""Preview requests must not be mistaken for a prohibition on reading a PDF."""
import pytest
from app.services.assistant_document_review import requested

PDF = "https://university.example/english-requirements.pdf"


@pytest.mark.parametrize("prompt", [
    f"{PDF}\nPrepare an IELTS approval preview. Do not apply or publish.",
    f"Update IELTS bands from {PDF}; don't apply changes until I approve.",
    f"Check {PDF} but do not update records.",
    f"Show the IELTS document approval preview using {PDF}. Never write without approval.",
    f"Do not apply anything; prepare an IELTS preview from {PDF}.",
    f"I want an IELTS preview from {PDF}, do not apply it yet.",
])
def test_preview_and_negated_commit_still_route_to_document_review(prompt):
    assert requested({"content": prompt})


@pytest.mark.parametrize("prompt", [
    f"Do not update IELTS from {PDF}",
    f"Don't apply or preview {PDF}",
    f"Explain {PDF}. Never update records.",
    "Set IELTS overall to 6.5 for row 123",
    # Action words inside a link are source data, not a requested operation.
    "Do not update IELTS using https://university.example/update.pdf",
])
def test_no_positive_document_operation_does_not_start_bulk_matching(prompt):
    assert not requested({"content": prompt})
