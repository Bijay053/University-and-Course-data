"""Apply an authorised, version-fenced retained-source IELTS proposal."""
from app.services.assistant_repair_proposals import IELTS_FIELDS, _course_ielts
from app.services.assistant_source_gate import same_course_url


async def apply_retained_ielts(service, db, thread, action, rows, sources, university_id, actor):
    if (set(action.get("fields") or []) != {"ielts_overall"}
            or {row.id for row in rows} != set(action.get("_ids") or [])
            or not rows):
        return False
    saved = [s for s in (sources or []) if s.get("verified")
             and s.get("sourceOrigin") == "retained_scrape_page_text"]
    if not all(any(same_course_url(s.get("url"), row.course_website) for s in saved) for row in rows):
        return False
    from app.services.assistant_retained_source import retained_course_source
    from app.services.assistant_document_review import apply
    plans = []
    changed = 0
    for row in sorted(rows, key=lambda r:r.id):
        if row.university_id != university_id or row.status != "pending":
            raise ValueError("Retained-source repair accepts existing pending courses only.")
        recorded = next(s for s in saved if same_course_url(s.get("url"), row.course_website))
        fresh = await retained_course_source(
            service, thread.id, thread.turn_id, thread.run_id, recorded["url"], {
                "verified":False, "sourceType":"html", "httpStatus":200,
                "failureStage":"source_body", "restrictionKind":"incomplete_response",
            })
        if not fresh or fresh.get("_retainedSnapshotId") != recorded.get("_retainedSnapshotId"):
            raise ValueError("Recorded course evidence changed or expired. Prepare a fresh preview.")
        bundles = _course_ielts(fresh)
        if len({scores for scores,quote in bundles}) != 1:
            raise ValueError("Recorded course requirements are ambiguous; no values were applied.")
        scores, quote = bundles[0]
        preview = next((p for p in action.get("repairPreview",[]) if p.get("id")==row.id), {})
        expected = {c["field"]:c["newValue"] for c in preview.get("changes",[])}
        if (preview.get("status") != "supported"
                or expected != dict(zip(IELTS_FIELDS,scores))
                or preview.get("source",{}).get("quote") != quote
                or not same_course_url(preview.get("source",{}).get("url"),fresh["url"])):
            raise ValueError("The recorded source no longer matches the approved five-score preview.")
        version = (action.get("_versions") or {}).get(str(row.id))
        if version != service.row_version(row):
            raise ValueError("A proposed course changed. Prepare a fresh preview; nothing was applied.")
        changes = [
            {"field":field,"oldValue":getattr(row,field),"newValue":score,"quote":quote,"page":None}
            for field,score in zip(IELTS_FIELDS,scores)
        ]
        changed += sum(c["oldValue"] != c["newValue"] for c in changes)
        plans.append({
            "id":action["id"], "_turnId":thread.turn_id, "_applicabilityVersion":1,
            "_targets":[{"id":row.id,"version":version,"changes":changes}],
            "_source":fresh["url"], "_sourceType":"html",
            "_sourceCapturedAt":fresh["sourceCapturedAt"],
            "_sourceSnapshotId":fresh["_retainedSnapshotId"],
            "_provenance":"retained-official-course-text",
        })
    # The existing approval writer supplies permissions, row locks, audit,
    # selected evidence and read-back. It does not commit: the route commits
    # the whole action, or rolls back every course on any failure.
    for plan in plans:
        await apply(db,thread,plan,university_id,actor)
    action.update(
        state="completed", valuesChanged=changed, evidenceFieldsRefreshed=5*len(rows),
        resultSummary=(
            f"Updated {changed} IELTS value(s) across {len(rows)} existing pending course(s) "
            "from their recorded official course text. All five scores were read back. "
            "No courses were created or published; no new university-page fetch or model call was needed."
        ),
    )
    return True
