"""Prepare executable, pending-only intake repairs without course discovery."""
import asyncio
import re


def requested(prompt):
    from app.services.assistant_image_review import action_requested
    from app.services.assistant_field_edits import evidence_edit_fields
    if len(evidence_edit_fields(prompt)) > 1:
        return False
    return bool(action_requested(prompt) and re.search(
        r"\b(?:intakes?|start\s+dates?)\b", prompt, re.I))


def selected_rows(prompt, rows):
    """Write scope comes from the real instruction, not image text or 'others'."""
    ids = {int(i) for i in re.findall(r"\b(?:row|record)\s*#?\s*(\d+)\b", prompt, re.I)}
    if ids:
        selected = [r for r in rows if r["id"] in ids]
    elif (re.search(r"\b(?:all|every)\s+(?:(?:pending|the|existing)\s+)*courses?\b", prompt, re.I)
          and not re.search(r"\b(?:not all|only this|only one)\b", prompt, re.I)):
        selected = rows
    else:
        urls = {u.rstrip("/.,") for u in re.findall(r"https://[^\s)]+", prompt)}
        fold = lambda s: " ".join(re.findall(r"\w+", s.casefold()))
        selected = [r for r in rows if (
            urls and (r.get("url") or "").rstrip("/") in urls
            or not urls and r.get("name") and fold(r["name"]) in fold(prompt))]
    if re.search(r"\b(?:only\s+missing|missing\s+(?:course\s+)?intakes?)\b", prompt, re.I):
        selected = [r for r in selected if not r.get("intakes")]
    return selected


async def handle(service, thread_id, turn_id, run_id, ctx, prompt, messages):
    if not requested(prompt):
        return False
    # A literal value provided by staff retains staff-provided authority and
    # its existing audit/confirmation path. Screenshot dates are never literals.
    from app.services.assistant_staff_values import parse
    staff = parse(prompt, messages)
    if staff and staff.values and not staff.error:
        return False
    rows = selected_rows(prompt, ctx["samples"])
    if not rows:
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": "No existing pending course matched this intake request. No discovery or new course was created.",
            "actions": [], "sources": [],
        }, detail="No pending intake target matched.")
        return True
    from app.services.assistant_source_cache import cached_source
    checked = []
    page_checks = {}
    limit = len(rows) if len(rows) <= 10 else 3
    semaphore = asyncio.Semaphore(3)

    async def check(row):
        if not isinstance(row.get("url"), str) or not row["url"].strip():
            checked.append((row["id"], {
                "verified": False, "error": "This pending course has no official source URL."}))
            return
        async def read():
            from app.services.scraper.extractors.selected_intake import INTAKE_FACTS_VERSION
            async with semaphore:
                return await cached_source(
                    service, thread_id, turn_id, run_id, row["url"], ctx["home"],
                    service.inspect_source, 12, html_intake_schema=INTAKE_FACTS_VERSION)
        if row["url"] not in page_checks:
            page_checks[row["url"]] = asyncio.create_task(read())
        source = await page_checks[row["url"]]
        checked.append((row["id"], source))
    await asyncio.gather(*(check(row) for row in rows[:limit]))
    verified = [s for _, s in checked if s.get("verified")]
    if not verified:
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": f"No permitted official intake source was readable in {len(checked)} checks. "
                       f"{len(rows)} existing pending rows remain unchanged; no discovery was started.",
            "actions": [], "sources": [],
        }, detail="Intake source checks failed; no update claimed.")
        return True
    by_id = dict(checked)
    previews = []
    from app.services.assistant_intake_evidence import intake_proof
    for row in rows:
        source = by_id.get(row["id"], {})
        proof = intake_proof(source, row["url"], row["name"])
        preview = {"id": row["id"], "name": row["name"], "url": row["url"],
                   "status": "unverified", "changes": [],
                   "reason": "Requires a course-owned intake check during this repair."}
        if proof:
            preview.update(status="supported", reason="",
                source={"url": source["url"], "quote": proof[1]},
                changes=[{"field": "intake_months", "oldValue": row.get("intakes") or [],
                          "newValue": list(proof[0])}])
        previews.append(preview)
    action = service.action_proposal(
        {"kind": "fix_fields", "fields": ["intake_months"]}, [r["id"] for r in rows])
    action.update(
        label="Check and repair course intakes",
        _versions=ctx["versions"], _configVersion=ctx["config_version"],
        _sourceCheckedIds=list(by_id),
        requiresConfirmation=len(rows) > 10 or any(p["status"] != "supported" for p in previews),
        repairPreview=previews,
        courseScope=[{"id": r["id"], "name": r["name"], "url": r["url"]} for r in rows],
    )
    content = (
        f"Prepared an intake repair for {len(rows)} existing pending courses in this review. "
        f"{len(checked)} course rows assessed using {len(page_checks)} distinct official page checks; "
        f"{len(rows)-len(checked)} rows have not been checked yet. "
        "Execution checks each course's own page, preserves separate start-date variants, "
        "and updates only intake fields with an audit log. Screenshot dates are not copied "
        "to other courses. No courses will be created or published."
    )
    if action["requiresConfirmation"]:
        content += " This batch requires approval before it runs; no values have changed."
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "actions": [action],
        "sources": [{"url": s["url"], "title": s.get("title") or "Official course",
                     "excerpt": s.get("text", "")[:700]} for s in verified],
    }, detail="Existing-course intake repair prepared; no discovery.")
    return True
