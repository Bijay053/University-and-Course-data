"""Shared contract for course payloads that cross the staging boundary."""
from __future__ import annotations

from collections.abc import Iterable


class InvalidPayloadKeyError(ValueError):
    """A producer emitted a key that staging cannot persist or intentionally use."""


# Fields accepted from scraper producers and persisted on ``ScrapedCourse``.
# Staging-owned identity, status, scoring, and review columns are deliberately
# absent: producers must not be able to set them through the payload.
PERSISTABLE_STAGING_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "academic_country",
    "academic_level",
    "academic_score",
    "cambridge_accepted",
    "cambridge_overall",
    "category",
    "course_location",
    "course_website",
    "cricos_code",
    "currency",
    "degree_level",
    "delivery_mode",
    "description",
    "duolingo_accepted",
    "duolingo_overall",
    "duration",
    "duration_term",
    "extraction_method",
    "fee_term",
    "fee_year",
    "has_central_fee_page",
    "ielts_listening",
    "ielts_overall",
    "ielts_reading",
    "ielts_speaking",
    "ielts_writing",
    "intake_days",
    "intake_months",
    "international_eligible",
    "international_fee",
    "language",
    "notes",
    "on_campus_available",
    "other_requirement",
    "pte_accepted",
    "pte_listening",
    "pte_overall",
    "pte_reading",
    "pte_speaking",
    "pte_writing",
    "scholarship",
    "score_type",
    "scrape_warnings",
    "student_market",
    "study_load",
    "study_mode",
    "sub_category",
    "toefl_accepted",
    "toefl_listening",
    "toefl_overall",
    "toefl_reading",
    "toefl_speaking",
    "toefl_writing",
})

# Guard-only values are deliberately available to extraction and data-quality
# checks but never cross the staging boundary.  ``domestic_fee`` exists only to
# prevent a domestic amount from being mistaken for international tuition; it
# is not reviewer-facing catalogue data and must not be carried forward from an
# approved row.
GUARD_ONLY_PIPELINE_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "domestic_fee",
})

# Intentional pipeline-only keys. These support gates, aliases, diagnostics, or
# paired-field conversion and are consumed before model construction.
TRANSIENT_PIPELINE_PAYLOAD_FIELDS: frozenset[str] = frozenset({
    "_confidence_level",
    "_confidence_score",
    "_rejection_reason",
    "course_name",
    *GUARD_ONLY_PIPELINE_PAYLOAD_FIELDS,
    "domestic_only",
    "duration_text",
    "english_test",
    "fee_currency",
    "fee_table_confirmed_no_international",
    "intake_text",
    "international_full_time_source_verified",
    "is_pathway",
    "location_text",
    "not_accepting",
    "online_only",
    "online_only_adelaide",
    "online_only_audited_host",
    "online_only_authoritative",
    "online_only_deakin",
    "online_only_segi",
    "online_only_unisq",
    "online_only_uon",
    "online_only_utas",
    "page_title",
    "parser_error",
    "parser_error_fields",
    "parttime_only",
    "research_authority_url",
    "research_fee_source_url",
    "source_url",
})

ALLOWED_PIPELINE_PAYLOAD_FIELDS = (
    PERSISTABLE_STAGING_PAYLOAD_FIELDS | TRANSIENT_PIPELINE_PAYLOAD_FIELDS
)


def validate_payload_keys(producer: str, keys: Iterable[str]) -> None:
    """Raise with producer context when payload keys would vanish at staging."""
    invalid = sorted(set(keys) - ALLOWED_PIPELINE_PAYLOAD_FIELDS)
    if invalid:
        rendered = ", ".join(repr(key) for key in invalid)
        raise InvalidPayloadKeyError(
            f"{producer} emitted non-persistable payload key(s): {rendered}"
        )
