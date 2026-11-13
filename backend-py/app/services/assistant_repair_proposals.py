"""Server-owned scope, consent and evidence comparisons for field repairs."""
import re

IELTS_FIELDS = ("ielts_overall", "ielts_listening", "ielts_reading",
                "ielts_writing", "ielts_speaking")


def research_only(prompt):
    from app.services.scrape_assistant_autorun import execution_requested
    return (not execution_requested(prompt) or bool(re.search(
        r"^\s*(?:why|how|what|explain|show)\b|\b(?:preview|proposal|confirmation)\b",
        prompt, re.I)) or not re.search(
        r"\b(?:fix|correct|repair|update|set|change|add|recover|re-?extract)\b",
        prompt, re.I))


def _course_ielts(source):
    from app.services.scrape_assistant_tools import quote_in_source
    if not source.get("verified") or source.get("sourceType") == "pdf":
        return []
    found = []
    for fact in source.get("facts", []):
        values, quote = fact.get("normalized") or {}, fact.get("quote") or ""
        if (fact.get("field") != "ielts_overall" or not quote_in_source(quote, source)
                or not re.search(r"\bielts\b|English language requirements", quote, re.I)):
            continue
        if all(type(values.get(f)) in (int, float) and 0 < values[f] <= 9
               and float(values[f] * 2).is_integer() for f in IELTS_FIELDS):
            found.append((tuple(values[f] for f in IELTS_FIELDS), quote))
    return found


def finalize_field_proposal(action, prompt, ctx, sources):
    """Never let model prose expand the executable scope or imply write consent."""
    if not action or action["kind"] != "fix_fields":
        return None
    ids = list(dict.fromkeys(action["_ids"]))
    by_id = {r["id"]: r for r in ctx["samples"]}
    if any(i not in by_id for i in ids):
        raise ValueError("Repair proposal contains an unknown course.")
    rows = [by_id[i] for i in ids]
    action["_ids"], action["courseCount"] = ids, len(ids)
    action["courseScope"] = [
        {"id":r["id"], "name":r.get("name") or f"Course {r['id']}",
         "url":r.get("url") or ""} for r in rows]
    action["requiresConfirmation"] = bool(
        action.get("requiresConfirmation") or research_only(prompt) or len(ids) > 10)
    total = len(ctx["samples"])
    summary = (
        f"Prepared a field-repair proposal for exactly {len(ids)} existing pending "
        f"course(s) in this review. This review has {total} pending course(s); "
        f"{total - len(ids)} are not included in this action. "
        "Only the courses listed in the proposal can be processed."
    )
    if "study_mode" in action["fields"]:
        # Hold unless every exact course has a deterministic owned comparison.
        # Model confidence alone never resolves conflicting delivery wording.
        action["requiresConfirmation"] = True
        summary += (
            "\n\nThis is a delivery-mode recheck, not new course discovery. Each course "
            "must use its own official delivery evidence. Online modules or an Online "
            "start-date label alone do not establish online-only delivery when teaching "
            "evidence conflicts. No review-wide Online value will be copied. Unavailable "
            "or inconclusive evidence must remain unresolved; staging counts do not prove "
            "these course checks have completed."
        )
        if set(action["fields"]) == {"study_mode"}:
            from app.services.assistant_delivery_evidence import delivery_proof
            previews = []
            for row, scope in zip(rows, action["courseScope"]):
                proof = delivery_proof(row.get("url"), row.get("name"), sources)
                preview = {**scope, "status": "unverified", "changes": [],
                           "reason": "No unambiguous course-owned delivery proof was verified."}
                if proof:
                    value, quote, source = proof
                    preview.update(status="supported", reason="",
                        source={"url": source["url"], "quote": quote},
                        changes=[{"field": "study_mode", "oldValue": row.get("studyMode"),
                                  "newValue": value}])
                previews.append(preview)
            action["repairPreview"] = previews
            if all(p["status"] == "supported" for p in previews):
                action["label"] = "Apply verified delivery correction"
                action["requiresConfirmation"] = research_only(prompt) or len(ids) > 10
                summary += (
                    "\n\nEvery selected course has an exact course-owned delivery comparison. "
                    "Execution will reuse this checked evidence, audit the change and read "
                    "the saved mode back; it will not run another page extraction."
                )
    if "ielts_overall" in action["fields"]:
        previews = []
        for row, scope in zip(rows, action["courseScope"]):
            # A course URL owns its facts. General guidance, another course and
            # PDFs need a separate applicability workflow; never borrow scores.
            evidence = [
                (scores, quote, source["url"])
                for source in sources
                if scope["url"] and str(source.get("url") or "").rstrip("/") == scope["url"].rstrip("/")
                for scores, quote in _course_ielts(source)]
            distinct = {entry[0] for entry in evidence}
            preview = {**scope, "status":"unverified", "changes":[],
                       "reason":"No complete, course-specific five-score evidence was verified."}
            if len(distinct) == 1:
                scores, quote, url = evidence[0]
                bands = row.get("ieltsBands") or {}
                preview.update(status="supported", reason="",
                    source={"url":url, "quote":quote},
                    changes=[
                        {"field":field,
                         "oldValue":row.get("ielts") if field=="ielts_overall"
                             else bands.get(field.removeprefix("ielts_")),
                         "newValue":score}
                        for field, score in zip(IELTS_FIELDS, scores)])
            elif len(distinct) > 1:
                preview["reason"] = "Verified course evidence conflicts; no score proposal was selected."
            previews.append(preview)
        action["repairPreview"] = previews
        supported = sum(p["status"] == "supported" for p in previews)
        if supported != len(rows):
            action["requiresConfirmation"] = True
        summary += (
            f"\n\nIELTS comparisons: {supported} course(s) have verified overall, Listening, "
            f"Reading, Writing and Speaking proposals; {len(rows)-supported} remain unverified. "
            "Scores are course-specific, not a provider-wide default. The five current and "
            "source-backed proposed values are shown below. Confirmation starts a fresh "
            "re-extraction of this bundle; its final values must be checked against the "
            "source returned by that run."
        )
    if action["requiresConfirmation"]:
        summary += "\n\nConfirmation is required before this action runs. No course values have changed."
    else:
        summary += "\n\nThe requested pending-row repair is prepared; no result is claimed until execution is verified."
    return summary
