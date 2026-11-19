"""Actor-opted-in pending automation; publication and ambiguous work stay gated."""
import asyncio
import logging
import re
from sqlalchemy import select
from app.models import ScrapedCourse

_tasks = {}


def schedule(service, thread_id, turn_id):
    """Recover the current automatic turn; database fences prevent double runs."""
    key = (thread_id, turn_id)
    if key in _tasks:
        return
    task = asyncio.create_task(service.run_automatic_action(thread_id, turn_id))
    _tasks[key] = task
    def finished(task):
        _tasks.pop(key, None)
        if not task.cancelled() and task.exception() is not None:
            logging.getLogger(__name__).warning("Automatic pending execution recovery failed.")
    task.add_done_callback(finished)


def enabled(settings, actor_id):
    allowed = {v.strip() for v in getattr(
        settings, "scrape_assistant_auto_pending_user_ids", "").split(",") if v.strip()}
    return str(actor_id) in allowed


def supported(action):
    if (action.get("state") != "proposed" or action.get("_confirmationOnly") or action.get("publishedCount", 0)
            or action.get("error") or action.get("conflicts")):
        return False
    count = action.get("courseCount", 0)
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        return False
    kind = action.get("kind")
    if kind == "cleanup_names":
        targets = action.get("_targets", [])
        return (len(targets) == count == len({target.get("id") for target in targets})
                and all(target.get("kind") == "pending" and target.get("version")
                        and target.get("beforeName") and target.get("afterName") for target in targets))
    if kind == "merge_intakes":
        plans = action.get("intakeConsolidations") or [action.get("intakeConsolidation")]
        ids = []
        for plan in plans:
            if not plan or plan.get("conflicts") or not plan.get("excludeIds"):
                return False
            ids.extend([plan.get("keepId"), *plan["excludeIds"]])
        # The writer rechecks pending ownership, every row version, identity,
        # fee-year manifests and collisions in one transaction.
        return (None not in ids and len(ids) == count == len(set(ids))
                and set(ids) == set(action.get("_ids", [])))
    if kind == "fix_fields":
        ids = action.get("_ids", [])
        if len(ids) != count or len(set(ids)) != count or not action.get("fields"):
            return False
        previews = action.get("repairPreview")
        if previews is not None:
            checked = set(action.get("_sourceCheckedIds", [p["id"] for p in previews]))
            # Unchecked rows are checked by the existing repair worker before
            # writing. An actually checked, unsupported source is not consent.
            return (any(p.get("status") == "supported" for p in previews)
                    and all(p.get("status") == "supported" for p in previews if p["id"] in checked))
        return bool(action.get("_evidenceUrls")) and not action.get("error")
    if kind in {"staff_edit", "staff_location"}:
        targets = action.get("_targets", [])
        return len(targets) == count and all(t.get("kind") == "pending" for t in targets)
    if kind == "document_update":
        from app.services.english_html_tables import APPLICABILITY_VERSION
        targets = action.get("_targets", [])
        # This writer accepts pending records only and fences their reviewed
        # versions before applying the saved, versioned applicability proof.
        expected_version = APPLICABILITY_VERSION if action.get("_sourceType") == "html" else 1
        return (action.get("_applicabilityVersion") == expected_version and len(targets) == count
                and all(t.get("kind", "pending") == "pending" and t.get("version")
                        and t.get("changes") for t in targets))
    return False


def prepare(reply, request, settings, actor_id):
    from app.services.scrape_assistant_autorun import execution_requested
    if not (enabled(settings, actor_id) and request.get("autoRun")
            and execution_requested(request.get("content", ""))):
        return
    for action in reply.get("actions", []):
        if action.get("kind") == "merge_intakes" and re.search(
            r"\b(?:do not|don't|dont|never)\s+(?:merge|combine|consolidate|remove|rename|exclude)\b",
            request.get("content", ""), re.I,
        ):
            continue
        if supported(action):
            action.update(requiresConfirmation=False, automaticPending=True)


def supersede_document_previews(messages, current_turn):
    """A document writer cannot accept another turn's saved proposal."""
    changed = False
    for message in messages:
        for action in message.get("actions", []):
            if (action.get("kind") == "document_update" and action.get("state") == "proposed"
                    and action.get("_turnId") and action["_turnId"] != current_turn):
                action.update(state="superseded", resultSummary=(
                    "Earlier document proposal was not applied. A newer request replaced "
                    "this preview; use the latest document check and its results."))
                changed = True
    return changed


async def supersede_changed_merges(service, db, messages):
    """Retire old previews, never replay them or claim another action's success."""
    pending = []
    for message in messages:
        for action in message.get("actions", []):
            if action.get("kind") != "merge_intakes" or action.get("state") != "proposed":
                continue
            versions = action.get("_versions", {})
            if action.get("_intakeGroups"):
                versions = {k:v for child in action["_intakeGroups"]
                            for k,v in child.get("_versions", {}).items()}
            ids = action.get("_ids", [])
            if ids and set(versions) == {str(i) for i in ids}:
                pending.append((action, ids, versions))
    if not pending:
        return False
    ids = {i for _, group, _ in pending for i in group}
    rows = {r.id:r for r in (await db.execute(
        select(ScrapedCourse).where(ScrapedCourse.id.in_(ids)))).scalars()}
    from app.services.assistant_staff_locations import version
    changed = False
    for action, group, versions in pending:
        if any(i not in rows or rows[i].status not in service.REVIEW_STATUSES
               or version(service, rows[i], "pending") != versions[str(i)] for i in group):
            action.update(state="superseded", resultSummary=(
                "Outdated preview: these records changed or were already consolidated. "
                "This operation was not run again."))
            changed = True
    return changed
