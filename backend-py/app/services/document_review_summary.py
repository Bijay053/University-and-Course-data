"""Summaries are derived from actual matching outcomes, not proposed-write counts."""


def summarize(rows):
    not_checked = sum(row["status"] == "not_checked" for row in rows)
    return {
        "checked": len(rows) - not_checked,
        "notChecked": not_checked,
        "proposed": sum(row["status"] == "proposed" for row in rows),
        "matched": sum(row.get("applicability") == "verified" for row in rows),
        "unchanged": sum(row["status"] == "unchanged" for row in rows),
        "unmatched": sum(row["status"] in {"unmatched", "unverified", "conflict", "excluded"} for row in rows),
        "conditional": sum(bool(row.get("bandConstraint")) and row.get("applicability") == "verified" for row in rows),
    }


def describe(counts, selected):
    return (
        f"Selected {selected} pending records for this provider. "
        f"{counts['checked']} had readable course evidence; {counts['notChecked']} could not be checked. "
        f"{counts['matched']} matched applicable official requirements; "
        f"{counts['proposed']} have proposed IELTS changes; "
        f"{counts['unchanged']} matched records have no supported numeric change. "
        f"{counts['unmatched']} remain unresolved or excluded and have not been corrected. "
        + (f"{counts['conditional']} matched records use a combined band rule rather than four fixed skill scores. "
           if counts["conditional"] else "")
        + "No course values have been changed. "
        + ("Review the proposed changes and confirm the batch. " if counts["proposed"]
           else "There are no proposed changes to approve. ")
        + "Published records are not included in this pending-record batch."
    )
