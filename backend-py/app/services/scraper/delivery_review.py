"""Delivery restrictions retain pending records, but never grant publication."""
from .guards import is_online_only_for_staging, is_part_time_only_for_staging
from app.study_modes import normalize_study_mode


def publication_hold_reason(row):
    """Stored review restrictions survive scoring and field-proof cleanup."""
    warnings = getattr(row, "scrape_warnings", None) or []
    retained = [w for w in warnings if isinstance(w, str) and w.startswith(
        ("Online-only delivery:", "Part-time-only study:"))]
    if retained:
        return " ".join(retained)
    payload = {key: getattr(row, key, None) for key in
               ("study_mode", "study_load", "course_name")}
    if is_online_only_for_staging(payload["course_name"] or "", payload,
                                  getattr(row, "course_website", None)):
        return "Online-only delivery requires explicit eligibility review."
    if is_part_time_only_for_staging(payload):
        return "Part-time-only study requires explicit eligibility review."
    return None


def apply_delivery_review(row, name, payload, source_url=None):
    """Apply before persistence, including split children. Protect published rows."""
    if row.course_id is not None or row.status not in {"pending", "review"}:
        return False
    warnings = []
    if is_online_only_for_staging(name, payload, source_url):
        warnings.append("Online-only delivery: student-visa eligibility requires review.")
        # A conservative restriction can survive rejected prose without
        # turning that prose into a verified, persisted delivery value.
        if payload.get("online_only") or normalize_study_mode(payload.get("study_mode")) == "Online":
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
