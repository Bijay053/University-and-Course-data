"""One fenced confirmation for independent intake families, with atomic undo."""
from copy import deepcopy
import re
import uuid

from app.services.assistant_staff_locations import version


async def prepare(service, thread_id, turn_id, run_id, ctx, prompt, groups):
    from app.services.assistant_intake_consolidation import make_plan
    from app.services.scrape_assistant_autorun import execution_requested
    children, blocked, invalid = [], [], []
    for rows in sorted(groups, key=lambda group: min(r.id for r in group)):
        try:
            plan = make_plan(rows, ctx["home"])
        except ValueError as exc:
            invalid.append(f"{rows[0].course_name} (rows {', '.join(str(r.id) for r in rows)}): {exc}")
            continue
        public = {k: v for k, v in plan.items() if not k.startswith("_")}
        if plan["conflicts"]:
            blocked.append(public)
            continue
        children.append({
            "id": uuid.uuid4().hex, "kind": "merge_intakes",
            "intakeConsolidation": public,
            "_ids": [r.id for r in rows],
            "_versions": {str(r.id): version(service, r, "pending") for r in rows},
            "_home": ctx["home"], "_jobId": ctx["job"]["id"], "_turnId": turn_id,
        })
    ids = [i for child in children for i in child["_ids"]]
    total = sum(len(group) for group in groups)
    readonly = (
        not execution_requested(prompt)
        or bool(re.search(r"\b(?:do not|don't|dont|never)\s+(?:merge|combine|consolidate|remove|rename|exclude)", prompt, re.I))
        or not re.search(r"\b(?:merge|consolidate|combine|remove|make|fix|correct|repair)\b", prompt, re.I)
        or bool(re.search(r"^\s*(?:why|explain|show|compare|what|how)\b|\bpreview\b", prompt, re.I))
    )
    action = {
        "id": uuid.uuid4().hex, "kind": "merge_intakes",
        "label": "Consolidate matching intake families",
        "fields": ["course_name", "intake_months"], "courseCount": len(ids),
        "state": "proposed" if children else "needs_review",
        # Never split a large requested batch into unconfirmed small writes.
        "requiresConfirmation": readonly or total > 10 or any(
            child["intakeConsolidation"].get("feeSchedules") for child in children),
        "intakeConsolidations": [child["intakeConsolidation"] for child in children],
        "blockedIntakeConsolidations": blocked, "_intakeGroups": children,
        "_ids": ids, "_jobId": ctx["job"]["id"], "_turnId": turn_id,
    }
    if not children:
        action["error"] = "All matching families have conflicting stored values; no merge can run."
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "createdAt": service.now().isoformat(),
        "content": (
            f"Checked {len(groups)} intake families ({total} existing pending rows) without fetching pages. "
            f"{len(children)} families ({len(ids)} rows) have merge proposals; "
            f"{len(blocked) + len(invalid)} families remain blocked by conflicting stored values or identity constraints. "
            "Distinct awards such as BSc and MSc remain separate. "
            "This is intake consolidation, not a delivery-mode repair or catalogue discovery. "
            "Each proposal preserves fee years, source snapshots, audit and undo. Nothing is published."
            + ("\n\nUnmerged families:\n" + "\n".join(invalid) if invalid else "")
        ), "actions": [action], "sources": [],
    }, detail="Checked existing intake families without paid extraction.")


async def apply_batch(service, db, action, university_id, actor_id):
    from app.services.assistant_intake_consolidation import apply
    children = deepcopy(action["_intakeGroups"])
    ids = [i for child in children for i in child["_ids"]]
    if not children or ids != action["_ids"] or len(ids) != len(set(ids)):
        raise ValueError("The intake batch scope is invalid; refresh the preview")
    async with db.begin_nested():
        for child in children:
            await apply(service, db, child, university_id, actor_id)
    # Each child wrote into the same transaction. Combine its exact receipts so
    # the existing fenced undo restores every family in one transaction too.
    for key in ("_undo", "_evidenceIds"):
        action[key] = [value for child in children for value in child[key]]
    for key in ("_evidenceVersions", "_evidenceRevisions"):
        action[key] = {k: v for child in children for k, v in child[key].items()}
    action.update(state="completed", jobId=action["_jobId"],
        resultSummary=f"Consolidated {len(ids)} pending rows across {len(children)} intake families. "
                      "Source evidence and fee schedules retained. Nothing published.")
