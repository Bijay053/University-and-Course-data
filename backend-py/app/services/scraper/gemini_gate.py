"""Gemini cost gate — skip or downgrade Gemini calls when other extractors
already have the data.

Three decisions:
  - ``"all_high_value_fields_populated"`` → skip Gemini entirely
  - ``"classification_only"``             → run with a cheap 80-token prompt
  - ``"full_extraction_needed"``          → run full prompt (existing behaviour)

Savings model:
  UniSQ-style (static HTML, regex-rich): ~70% of calls become skip/cheap.
  ASA/VIT-style (image-heavy):           no change — full extraction still runs.
  Overall estimated reduction: 30-40% of Gemini primary text-extraction cost.
"""
from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Field catalogue
# ---------------------------------------------------------------------------

# Fields Gemini primary is most useful for.  If all of these are already
# populated at sufficient confidence by other extractors, Gemini adds little.
GEMINI_HIGH_VALUE_FIELDS: frozenset[str] = frozenset({
    "international_fee",
    "ielts_overall",
    "duration",
    "intake_months",
    "course_name",
    "study_mode",
    "category",
})

# Even when most high-value fields are populated, run Gemini if these are
# missing — it is uniquely good at taxonomy classification.
GEMINI_UNIQUE_FIELDS: frozenset[str] = frozenset({
    "category",
    "sub_category",
})

# Confidence level below which a field is considered NOT populated.
CONFIDENCE_THRESHOLD: float = 0.70

# Fraction of GEMINI_HIGH_VALUE_FIELDS that must be populated to trigger
# the "skip / classification-only" branch.
COVERAGE_FLOOR: float = 0.90


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_skip_gemini_primary(
    payload: dict[str, Any],
    evidence: list[dict[str, Any]],
) -> tuple[bool, str]:
    """Decide whether to call Gemini's primary text extractor for a course.

    Parameters
    ----------
    payload:
        The current extraction payload (field → value).
    evidence:
        The list of evidence dicts accumulated so far.  Each entry must have
        ``field_key`` and ``confidence`` keys.

    Returns
    -------
    (should_skip, reason) where:
      - ``should_skip=True``  → caller MUST skip the Gemini call entirely
      - ``should_skip=False`` → caller should run Gemini (reason says how)

    Reasons:
      ``"all_high_value_fields_populated"`` — skip; all fields covered
      ``"classification_only"``             — run, cheap prompt only
      ``"full_extraction_needed"``          — run, full prompt
    """
    # Build fast lookup: field_key → max confidence seen in evidence list.
    best_conf: dict[str, float] = {}
    for ev in evidence:
        fk = ev.get("field_key") or ""
        conf = float(ev.get("confidence") or 0.0)
        if conf > best_conf.get(fk, 0.0):
            best_conf[fk] = conf

    # Coverage is computed against the NON-classification fields only.
    # Classification (category / sub_category) is treated as a separate dimension:
    # a fully covered course that only needs a category label uses a cheap prompt,
    # not the full extraction prompt.
    non_class_fields = GEMINI_HIGH_VALUE_FIELDS - GEMINI_UNIQUE_FIELDS
    populated_non_class: set[str] = {
        field
        for field in non_class_fields
        if (
            payload.get(field) not in (None, "", 0, [])
            and best_conf.get(field, 0.0) >= CONFIDENCE_THRESHOLD
        )
    }

    coverage = len(populated_non_class) / len(non_class_fields)

    if coverage >= COVERAGE_FLOOR:
        # 90%+ of the extraction-only fields are populated at high confidence.
        # Check whether classification (category / sub_category) is still missing.
        needs_classification = (
            not payload.get("category") or not payload.get("sub_category")
        )
        if needs_classification:
            log.debug(
                "[GEMINI GATE] classification_only — non-class coverage=%.0f%%, "
                "category=%r, sub_category=%r",
                coverage * 100,
                payload.get("category"),
                payload.get("sub_category"),
            )
            return False, "classification_only"

        log.debug(
            "[GEMINI GATE] skip — non-class coverage=%.0f%%, all fields populated",
            coverage * 100,
        )
        return True, "all_high_value_fields_populated"

    log.debug(
        "[GEMINI GATE] full_extraction — non-class coverage=%.0f%% (<%d%%)",
        coverage * 100,
        int(COVERAGE_FLOOR * 100),
    )
    return False, "full_extraction_needed"


def build_classification_only_prompt(
    course_name: str,
    page_text: str,
) -> str:
    """Build a cheap classification-only prompt for Gemini.

    Used when other extractors already filled fee/IELTS/duration/intake/mode.
    Sends only the first 1 500 characters of page text to Gemini and asks for
    category + sub_category only — ~75% smaller schema → ~75% fewer output
    tokens.

    The JSON schema matches what ``gemini_primary.extract_primary`` returns for
    those two fields so the calling code can process it uniformly.
    """
    snippet = (page_text or "")[:1500]
    return (
        f"Given this Australian university course, classify it into a "
        f"taxonomy category and sub-category.\n\n"
        f"Course name: {course_name}\n"
        f"Page excerpt:\n{snippet}\n\n"
        f"Respond with ONLY valid JSON, no markdown fences:\n"
        f'{{"category": "...", "sub_category": "..."}}\n'
        f"\nExamples for category:\n"
        f"  Business & Management, Engineering & Technology, Health Sciences,\n"
        f"  IT & Computer Science, Arts & Humanities, Law & Legal Studies,\n"
        f"  Education & Teaching, Science & Environment, Architecture & Design."
    )
