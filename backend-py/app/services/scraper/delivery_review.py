"""Delivery restrictions retain pending records, but never grant publication."""
from .guards import is_online_only_for_staging, is_part_time_only_for_staging


def apply_delivery_review(row, name, payload, source_url=None):
    """Apply before persistence, including split children. Protect published rows."""
    if row.course_id is not None or row.status not in {"pending", "review"}:
        return False
    warnings = []
    if is_online_only_for_staging(name, payload, source_url):
        warnings.append("Online-only delivery: student-visa eligibility requires review.")
        row.study_mode = "Online"
    if is_part_time_only_for_staging(payload) or payload.get("parttime_only"):
        warnings.append("Part-time-only study: no full-time route confirmed; visa eligibility requires review.")
        row.study_load = "Part Time"
    if not warnings:
        return False
    row.status = "pending"
    row.auto_publish_status = "review"
    row.eligibility_status = "unverified"
    row.eligibility_reason = " ".join(warnings)
    row.pub_decision = None
    row.decision_score = None
    row.pub_decision_reason = row.eligibility_reason
    row.scrape_warnings = list(dict.fromkeys([*(row.scrape_warnings or []), *warnings]))
    return True
