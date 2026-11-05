"""Missing Australian registration evidence is a review flag, not exclusion."""


def flag_registration_review(row, country, payload):
    if str(country or "").strip().casefold() not in {"australia", "au", "aus"}:
        return
    row.cricos_code = payload.get("cricos_code") or row.cricos_code
    if not row.cricos_code:
        row.eligibility_status = "unknown"
        row.eligibility_reason = "CRICOS registration unverified: no course code established."
        row.auto_publish_status = "review"
