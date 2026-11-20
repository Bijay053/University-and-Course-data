"""Report persisted field changes separately from missing-issue resolution."""

METADATA_FIELDS = frozenset({
    "scrape_warnings", "requirement_status", "completeness", "completeness_score",
    "auto_publish_status", "decision_score", "extraction_method",
})

COMPANIONS = {
    "international_fee": {"currency", "fee_term", "fee_year"},
    "duration": {"duration_term"},
    "intake_months": {"intake_days"},
    "academic_level": {"academic_score", "score_type", "academic_country"},
    "academic_score": {"academic_level", "score_type", "academic_country", "other_requirement"},
    "other_requirement": {"academic_level", "academic_score", "score_type", "academic_country"},
}


def matches_scope(field, scope):
    if not scope or field in scope:
        return True
    if any(field in COMPANIONS.get(target, ()) for target in scope):
        return True
    family = field.split("_", 1)[0]
    return family in {"ielts", "pte", "toefl", "cambridge", "det"} and (
        "english_requirements" in scope or f"{family}_overall" in scope
    )


def field_change_counts(results, target_fields=None):
    counts = {"valueUpdated": 0, "valueUnchanged": 0, "evidenceRefreshedOnly": 0}
    targets = set(target_fields) if target_fields else None
    for result in results:
        if (result.get("ok") is False or result.get("attempted") is False
                or result.get("outcome") in {"failed", "skipped", "already_resolved"}):
            continue
        scope = targets or set(result.get("target_fields") or [])

        def relevant(fields):
            return {
                field for field in (fields or [])
                if field not in METADATA_FIELDS and matches_scope(field, scope)
            }

        # Workers retain the complete persisted change list before projecting
        # legacy target aliases. An IELTS-overall request also owns its bands.
        updated = result.get("all_updated_fields", result.get("updated_fields"))
        refreshed = result.get("all_refreshed_evidence_fields", result.get("refreshed_evidence_fields"))
        if relevant(updated):
            counts["valueUpdated"] += 1
        else:
            counts["valueUnchanged"] += 1
            if relevant(refreshed):
                counts["evidenceRefreshedOnly"] += 1
    return counts
