from __future__ import annotations

import ast
import inspect

import pytest

from app.models import ScrapedCourse
from app.services.scraper.extractors import eligibility
from app.services.scraper.extractors.gemini_primary import (
    GEMINI_PRIMARY_FIELD_TARGETS,
)
from app.services.scraper.pipelines.single_course import _EXTRACTORS
from app.services.scraper.stage_course import _reviewable_evidence
from app.services.scraper.payload_contract import (
    GUARD_ONLY_PIPELINE_PAYLOAD_FIELDS,
    InvalidPayloadKeyError,
    PERSISTABLE_STAGING_PAYLOAD_FIELDS,
    TRANSIENT_PIPELINE_PAYLOAD_FIELDS,
    validate_payload_keys,
)


def test_persistable_contract_only_names_scraped_course_columns() -> None:
    model_columns = set(ScrapedCourse.__table__.columns.keys())
    assert PERSISTABLE_STAGING_PAYLOAD_FIELDS <= model_columns


def test_full_ai_output_targets_obey_shared_staging_contract() -> None:
    validate_payload_keys(
        "extractor gemini_primary",
        GEMINI_PRIMARY_FIELD_TARGETS.values(),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<p>International students may apply.</p>", True),
        ("<p>This course is domestic students only.</p>", False),
    ],
)
async def test_eligibility_outputs_obey_staging_contract(
    html: str,
    expected: bool,
) -> None:
    result = (await eligibility.extract(html, "https://example.edu/course"))[0]
    validate_payload_keys("extractor eligibility", result.normalized.keys())
    assert result.normalized == {"international_eligible": expected}


def test_registered_extractors_literal_normalized_keys_obey_contract() -> None:
    """Check every statically declared ExtractionResult normalized contract."""
    checked = 0
    for module, _kwargs in _EXTRACTORS:
        tree = ast.parse(inspect.getsource(module))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if getattr(node.func, "id", None) != "ExtractionResult":
                continue
            normalized = next(
                (kw.value for kw in node.keywords if kw.arg == "normalized"),
                None,
            )
            if not isinstance(normalized, ast.Dict):
                continue
            keys = [
                key.value
                for key in normalized.keys
                if isinstance(key, ast.Constant) and isinstance(key.value, str)
            ]
            validate_payload_keys(f"extractor {module.__name__}", keys)
            checked += 1
    assert checked > 0


def test_documented_transient_keys_are_allowed_but_not_persistable() -> None:
    assert TRANSIENT_PIPELINE_PAYLOAD_FIELDS.isdisjoint(
        PERSISTABLE_STAGING_PAYLOAD_FIELDS
    )
    validate_payload_keys(
        "provider example_catalogue",
        {"course_name", "duration_text", "international_fee"},
    )


def test_domestic_fee_is_guard_only_and_never_reviewable() -> None:
    model_columns = set(ScrapedCourse.__table__.columns.keys())

    assert GUARD_ONLY_PIPELINE_PAYLOAD_FIELDS == {"domestic_fee"}
    assert "domestic_fee" in TRANSIENT_PIPELINE_PAYLOAD_FIELDS
    assert "domestic_fee" not in PERSISTABLE_STAGING_PAYLOAD_FIELDS
    assert "domestic_fee" not in model_columns
    validate_payload_keys("extractor fee", {"domestic_fee"})


def test_guard_only_domestic_fee_evidence_does_not_reach_review() -> None:
    evidence = [
        {"field_key": "domestic_fee", "value": 12_000},
        {"field_key": "international_fee", "value": 34_000},
    ]

    assert _reviewable_evidence(evidence) == [
        {"field_key": "international_fee", "value": 34_000}
    ]


def test_invalid_key_error_reports_producer_and_key() -> None:
    with pytest.raises(InvalidPayloadKeyError) as exc_info:
        validate_payload_keys(
            "provider example_catalogue",
            {"international_fee", "internatonal_fee"},
        )

    message = str(exc_info.value)
    assert "provider example_catalogue" in message
    assert "internatonal_fee" in message
