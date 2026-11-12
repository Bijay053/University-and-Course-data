"""Describe partial recovery without weakening the verification verdict."""


def partial_recovery(session: dict) -> dict | None:
    state = session.get("autonomous") or {}
    comparison = state.get("comparison") or {}
    quality = comparison.get("verification") or {}
    staged = quality.get("total_staged")
    if state.get("phase") != "needs_review" or type(staged) is not int or staged <= 0:
        return None
    counters = comparison.get("cumulative_counters") or comparison.get("counters") or {}
    processed, skipped = counters.get("current"), counters.get("skipped")
    result = {"staged": staged}
    if all(type(value) is int and value >= 0 for value in (processed, skipped)):
        imported = counters.get("imported")
        imported = imported if type(imported) is int and imported >= 0 else staged
        result.update(processed=processed, skipped=skipped,
                      unresolved=max(0, processed - imported - skipped))
    safety = comparison.get("combined_safety") or {}
    critical = max(int(comparison.get("critical_quality_count") or 0),
                   int(safety.get("critical_quality_count") or 0))
    result["criticalIssues"] = critical
    result["incompleteFields"] = list(comparison.get("unresolved_fields") or [])
    summary = f"Partial recovery: {staged} courses staged for review."
    if "processed" in result:
        summary += (f" {processed} checked; {skipped} skipped; "
                    f"{result['unresolved']} unresolved attempts.")
    if critical:
        summary += f" Verification recorded {critical} critical quality issues; a course can have multiple issues."
    if comparison.get("regressions"):
        summary += " Matched-course quality regressions also require review."
    if result["incompleteFields"]:
        labels = ", ".join(key.removesuffix("_pct").replace("_", " ")
                           for key in result["incompleteFields"])
        summary += f" Fields still incomplete: {labels}."
    summary += " Recovery is not verified complete. Nothing was approved or published."
    result["summary"] = summary
    return result
