"""Fenced consolidation of pending intake siblings, never catalogue discovery."""
from __future__ import annotations

from copy import deepcopy
import json
import re
import uuid
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select, text

from app.models import ScrapedCourse
from app.models.audit import CourseAuditLog
from app.models.evidence import ScrapedFieldEvidence
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services.assistant_staff_locations import revision, version
from app.services.assistant_staff_edits import permissions, plain
from app.services.scrape_assistant_tools import official_url

MONTHS = "January February March April May June July August September October November December".split()
MONTH = "|".join(MONTHS)
DATED_NAME = re.compile(rf"^(.*?)\s*\(\s*({MONTH})\s+(20\d{{2}})\s*\)\s*$", re.I)
DATED_PATH = re.compile(rf"^(.*)/({MONTH})-(20\d{{2}})/?$", re.I)
IDENTITY = {
    "id", "scrape_job_id", "course_name", "course_website", "canonical_course_url",
    "intake_months", "status", "created_at", "updated_at", "reviewed_at",
    "extraction_method", "scrape_warnings", "requirement_status", "completeness",
    "decision_score", "auto_publish_status", "last_qualification_approval",
    "rejection_reason", "pub_score", "pub_score_breakdown", "pub_decision",
    "pub_decision_reason", "avg_verification_confidence",
}
WRITTEN = ("course_name", "course_website", "canonical_course_url", "intake_months",
           "status", "rejection_reason", "extraction_method", "scrape_warnings")


def family(row, home):
    name = DATED_NAME.fullmatch(row.course_name or "")
    if not name:
        return None
    try:
        parts = urlsplit(official_url(row.course_website, home))
    except (TypeError, ValueError):
        return None
    path = DATED_PATH.fullmatch(parts.path)
    if (not path or parts.query or not name[1].strip()
            or name[2].casefold() != path[2].casefold() or name[3] != path[3]):
        return None
    base = urlunsplit((parts.scheme, parts.netloc, path[1], "", ""))
    return (name[1].strip(), base, int(name[3]), name[2].capitalize())


def requested(prompt, messages=()):
    examples, instructions = {}, []
    for line in prompt.splitlines():
        name = DATED_NAME.fullmatch(re.sub(r"^\s*(?:[-*•]|\d+[.)])\s+", "", line).strip())
        if name:
            examples.setdefault((name[1].strip().casefold(), name[3]), set()).add(name[2].casefold())
        else:
            instructions.append(line)
    instruction = "\n".join(instructions)
    # "Same course" is also a normal follow-up in IELTS/fee conversations.
    # It is not, by itself, an instruction to consolidate intake records.
    if (re.search(r"\b(?:ielts|english|fees?|tuition|duration|location|campus|academic)\b", instruction, re.I)
            and not re.search(r"\b(?:intakes?|merge|consolidate|combine|course\s+name)\b", instruction, re.I)):
        return False
    if (re.search(r"\b(?:add|set|change)\b.*\bintakes?\b", prompt, re.I)
            and not re.search(r"\b(?:merge|consolidate|combine|course\s+name)\b", prompt, re.I)):
        return False
    if any(len(months) > 1 for months in examples.values()):
        # Dated examples are stronger than a stale field from the conversation.
        # This only routes to a database preview; it is not merge permission.
        return True
    if examples and re.search(
        r"\b(?:duplicates?|duplicated|repeated|same|(?:still\s+)?show(?:n|ing)?\s+(?:[2-9]|two|three))\b",
        instruction, re.I,
    ):
        return True
    explicit = bool(re.search(
        r"\b(?:same\s+course|(?:merge|consolidate|combine)\b.*\b(?:intakes?|courses?|rows?|duplicates?)"
        r"|(?:intakes?\b.*\bcourse\s+name)|course\b.*\bmultiple\s+intakes?)", prompt, re.I))
    if explicit and re.search(r"\b(?:intakes?|same|merge|consolidate|combine)\b", prompt, re.I):
        return True
    if not re.fullmatch(r"\s*(?:yes|merge them|combine them|do it)[.!]?\s*", prompt, re.I):
        return False
    prior = next((m for m in reversed(messages) if m.get("role") == "assistant"), {})
    if prior.get("intakeConsolidation"):
        return True
    choices = prior.get("courseChoices", [])
    identities = []
    for choice in choices:
        name = DATED_NAME.fullmatch(choice.get("title", ""))
        parts = urlsplit(choice.get("url", ""))
        path = DATED_PATH.fullmatch(parts.path)
        if not name or not path or name[2].casefold() != path[2].casefold() or name[3] != path[3]:
            return False
        identities.append((name[1].strip().casefold(), parts.netloc, path[1], name[3]))
    return len(identities) > 1 and len(set(identities)) == 1


def all_families_requested(prompt, *, named=False):
    # "All intakes/rows for Course X" is still one named family. Widen named
    # examples only when staff explicitly says all courses or calls them samples.
    if named and not re.search(
        r"\b(?:samples?|examples?)\b"
        r"|\b(?:all|every)\s+(?:(?:the|other|remaining|pending|these)\s+)*courses?\b",
        prompt, re.I,
    ):
        return False
    return bool(re.search(
        r"\b(?:all|every)\s+(?:(?:the|other|remaining|pending|these)\s+)*(?:courses?|rows?|intakes?|duplicates?|this)\b"
        r"|\b(?:fix|repair|merge|consolidate|combine|review)\s+(?:them\s+)?all\b",
        prompt, re.I))


def make_plan(rows, home):
    """Date suffixes are identity hints, not permission to discard other facts."""
    families = [family(row, home) for row in rows]
    if len(rows) < 2 or any(f is None for f in families):
        raise ValueError("At least two intake-specific pending rows are required")
    name, url, year = families[0][:3]
    if any((f[0].casefold(), f[1], f[2]) != (name.casefold(), url, year) for f in families):
        raise ValueError("Different awards, source pages or entry years cannot be consolidated")
    if any(row.course_id is not None or row.status not in {"pending", "review", "review_ready"} for row in rows):
        raise ValueError("Only unlinked pending records can be consolidated")
    if len({(r.university_id, r.scrape_job_id) for r in rows}) != 1:
        raise ValueError("The records must belong to the same provider and review")
    conflicts, values = {}, {}
    for column in ScrapedCourse.__table__.columns:
        key = column.key
        if key in IDENTITY:
            continue
        present = [(r.id, getattr(r, key)) for r in rows]
        distinct = {json.dumps(v, sort_keys=True, default=str) for _, v in present}
        if len(distinct) > 1:
            conflicts[key] = [{"id": i, "value": str(v)} for i, v in present]
        elif present and present[0][1] is not None:
            values[key] = present[0][1]
    combined_months = set()
    for row, detail in zip(rows, families):
        # Never infer September from the URL when the record still says January.
        # A separate intake repair must resolve that mismatch first.
        stored = row.intake_months
        normalized = [
            next((m for m in MONTHS if isinstance(value, str) and m.casefold() == value.casefold()), None)
            for value in stored
        ] if isinstance(stored, list) else []
        combined_months.update(m for m in normalized if m)
        if not normalized or None in normalized or detail[3] not in normalized:
            conflicts.setdefault("intake_months", []).append({
                "id": row.id, "value": row.intake_months, "expectedIntake": detail[3],
            })
    keep = min(rows, key=lambda r: r.id)
    months = [m for m in MONTHS if m in combined_months or any(f[3] == m for f in families)]
    schedules = None
    if "fee_year" in conflicts:
        from app.services.assistant_intake_fees import build
        try:
            schedules = build(rows, families, MONTHS)
        except (ValueError, TypeError, OverflowError):
            pass
        else:
            del conflicts["fee_year"]
    return {
        "courseName": name, "canonicalUrl": url, "entryYear": year,
        "intakes": months, "keepId": keep.id,
        "excludeIds": sorted(r.id for r in rows if r.id != keep.id),
        "conflicts": conflicts, "_values": values,
        **({"feeSchedules": schedules["schedules"], "_feeSchedules": schedules} if schedules else {}),
        "rows": [{"id": r.id, "name": r.course_name, "url": r.course_website,
                  "intakes": r.intake_months, "fee": r.international_fee,
                  "currency": r.currency, "feeYear": r.fee_year} for r in rows],
    }


async def handle(service, thread_id, turn_id, run_id, ctx, prompt, messages):
    if not requested(prompt, messages):
        return False
    job_id = ctx["job"]["id"]
    async with service.AsyncSessionLocal() as db:
        rows = list((await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.university_id == ctx["university_id"],
            ScrapedCourse.scrape_job_id == job_id,
            ScrapedCourse.status.in_(service.REVIEW_STATUSES),
        ).order_by(ScrapedCourse.id))).scalars())
        groups = {}
        for row in rows:
            identity = family(row, ctx["home"])
            if identity:
                key = (identity[0].casefold(), identity[1], identity[2])
                groups.setdefault(key, []).append(row)
        from app.services.assistant_intake_context import matches, followup
        selected = matches(prompt, groups)
        if all_families_requested(prompt, named=bool(selected)):
            selected = [group for group in groups.values() if len(group) > 1]
        elif not selected:
            selected = followup(prompt, messages, groups)
        contextual = bool(re.fullmatch(
            r"\s*(?:yes|merge them|combine them|do it|[\d\s,]*(?:these |they )?(?:all )?are (?:the )?same course)[.!]?\s*",
            prompt, re.I,
        ))
        if not selected and contextual:
            prior = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), {})
            anchor = prior.get("intakeConsolidation", {}).get("canonicalUrl")
            choices = prior.get("courseChoices", [])
            urls = {c.get("url") for c in choices}
            selected = [group for key, group in groups.items() if len(group) > 1 and (
                key[1] == anchor or len(urls) > 1 and urls == {r.course_website for r in group}
            )]
        if len(selected) > 1:
            from app.services.assistant_intake_batches import prepare
            await prepare(service, thread_id, turn_id, run_id, ctx, prompt, selected)
            return True
        if len(selected) != 1:
            await service.save_research_reply(thread_id, turn_id, run_id, {
                "role": "assistant", "createdAt": service.now().isoformat(),
                "content": "No single intake-only course family matched in this review. "
                           "Give the course's name or canonical URL. No discovery or fee repair was started.",
                "actions": [], "sources": [],
            }, detail="Existing-course consolidation checked without discovery.")
            return True
        group = selected[0]
        try:
            plan = make_plan(group, ctx["home"])
        except ValueError as exc:
            await service.save_research_reply(thread_id, turn_id, run_id, {
                "role": "assistant", "createdAt": service.now().isoformat(),
                "content": str(exc) + ". No rows were changed.", "actions": [], "sources": [],
            }, detail="Course consolidation needs review.")
            return True
        from app.services.scrape_assistant_autorun import execution_requested
        imperative = bool(re.search(r"\b(?:remove|merge|combine|consolidate|make|fix|correct|repair)\b", prompt, re.I))
        readonly = (not execution_requested(prompt)
                    or bool(re.search(
                        r"\b(?:do not|don't|dont|never)\s+(?:merge|combine|consolidate|remove|rename|exclude)",
                        prompt, re.I))
                    or bool(re.match(r"\s*(?:why|explain|show|compare|what|how)\b", prompt, re.I))
                    or not imperative)
        # A bare "yes" to the old "which course?" question is not consent to
        # merge all of its choices. Replace the loop with a real preview.
        if re.fullmatch(r"\s*yes[.!]?\s*", prompt, re.I):
            previous = next((m for m in reversed(messages[:-1]) if m.get("role") == "assistant"), {})
            previous_action = next((a for a in previous.get("actions", []) if a.get("kind") == "merge_intakes"), {})
            # A plain "yes" never approves changed, newly discovered or
            # previously conflicting records.
            readonly = (
                not previous.get("intakeConsolidation")
                or previous_action.get("state") != "proposed"
                or previous_action.get("_versions") != {
                    str(r.id): version(service, r, "pending") for r in group
                }
            )
        public = {k: v for k, v in plan.items() if not k.startswith("_")}
        conflicts = plan["conflicts"]
        if conflicts and set(conflicts) == {"intake_months"} and not readonly:
            from app.services.assistant_intake_context import repair_conflict
            if await repair_conflict(service, thread_id, turn_id, run_id, ctx, plan, messages):
                return True
        action = {
            "id": uuid.uuid4().hex, "kind": "merge_intakes",
            "label": "Consolidate intake variants into one pending course",
            "courseCount": len(group), "fields": ["course_name", "intake_months"],
            "state": "needs_review" if conflicts else "proposed",
            "requiresConfirmation": readonly or len(group) > 10 or bool(plan.get("_feeSchedules")),
            "intakeConsolidation": public, "_ids": [r.id for r in group],
            "_versions": {str(r.id): version(service, r, "pending") for r in group},
            "_jobId": job_id, "_home": ctx["home"], "_turnId": turn_id,
        }
        if conflicts:
            action["error"] = ("Conflicting stored values require review: "
                               + ", ".join(conflicts) + ". No merge was applied.")
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "createdAt": service.now().isoformat(),
            "content": (
                f"These {len(group)} records describe {plan['courseName']} with intake-specific URLs, "
                f"not {len(group)} different awards. Proposed intakes: {', '.join(plan['intakes'])} {plan['entryYear']}. "
                "The preview retains the oldest row, removes the date from its name and softly excludes "
                "the other rows, retaining their original evidence and an undo action. "
                + (action["error"] if conflicts else
                   "No course will be published. No catalogue discovery or fee extraction is needed.")
                + (" Both tuition-year schedules and their intake applicability will be retained. "
                   "They are existing-record snapshots, not newly verified official evidence. "
                   "Publication remains held to prevent a single-year fee overwrite."
                   if plan.get("_feeSchedules") else "")
            ),
            "actions": [action], "sources": [], "intakeConsolidation": public,
        }, detail="Intake consolidation preview prepared for existing records.")
    return True


async def apply(service, db, action, university_id, actor_id, *, undo=False):
    if action.get("_intakeGroups") and not undo:
        from app.services.assistant_intake_batches import apply_batch
        return await apply_batch(service, db, action, university_id, actor_id)
    from app.models.user import User
    permissions(await db.get(User, actor_id))
    from app.services.scraper.replay_extraction import review_restore_lock_scope
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:scope))"),
                     {"scope": review_restore_lock_scope(university_id)})
    async with db.begin_nested():
        ids = action["_ids"]
        rows = list((await db.execute(select(ScrapedCourse).where(
            ScrapedCourse.id.in_(ids), ScrapedCourse.university_id == university_id,
            ScrapedCourse.scrape_job_id == action["_jobId"],
        ).order_by(ScrapedCourse.id).with_for_update().execution_options(populate_existing=True))).scalars())
        if len(rows) != len(ids):
            raise ValueError("A listed row is no longer in this review")
        if undo:
            receipts = {r["id"]: r for r in action["_undo"]}
            for row in rows:
                receipt = receipts[row.id]
                if (version(service, row, "pending") != receipt["version"]
                        or await revision(db, ScrapedCourse, row.id) != receipt["revision"]):
                    raise ValueError("A merged row changed; undo refused")
            proofs = list((await db.execute(select(ScrapedFieldEvidence).where(
                ScrapedFieldEvidence.id.in_(action["_evidenceIds"]),
            ).with_for_update().execution_options(populate_existing=True))).scalars())
            if len(proofs) != len(action["_evidenceIds"]):
                raise ValueError("Consolidation evidence changed; undo refused")
            for proof in proofs:
                if (version(service, proof, "evidence") != action["_evidenceVersions"].get(str(proof.id))
                        or await revision(db, ScrapedFieldEvidence, proof.id)
                        != action["_evidenceRevisions"].get(str(proof.id))):
                    raise ValueError("Consolidation evidence changed; undo refused")
            for row in rows:
                before = receipts[row.id]["before"]
                for key, value in before.items():
                    setattr(row, key, deepcopy(value))
                db.add(CourseAuditLog(scraped_course_id=row.id, field_key="intake_consolidation",
                    action="assistant_intake_merge_undo", actor=str(actor_id),
                    old_value=None, new_value=json.dumps(before, default=str),
                    reason="Undid the exact pending intake consolidation"))
            for proof in proofs:
                proof.selected = False
                proof.validation_status = "superseded"
            action.update(state="completed", resultSummary="Restored the original pending intake records.")
            return
        for row in rows:
            if version(service, row, "pending") != action["_versions"][str(row.id)]:
                raise ValueError("A listed row changed; refresh the consolidation preview")
        plan = make_plan(rows, action["_home"])
        if plan["conflicts"]:
            raise ValueError("Conflicting course values prevent a safe intake merge")
        existing = (await db.execute(select(ScrapedCourse.id).where(
            ScrapedCourse.university_id == university_id,
            ScrapedCourse.scrape_job_id == action["_jobId"],
            ScrapedCourse.id.not_in(ids),
            ScrapedCourse.course_website == plan["canonicalUrl"],
            ScrapedCourse.status.in_(service.REVIEW_STATUSES),
        ))).first()
        if existing:
            raise ValueError("A canonical pending course already exists; refresh the preview")
        keep = next(r for r in rows if r.id == plan["keepId"])
        fields = set(WRITTEN) | set(plan["_values"])
        snapshots = {r.id: {k: deepcopy(plain(getattr(r, k))) for k in fields} for r in rows}
        keep.course_name, keep.course_website = plan["courseName"], plan["canonicalUrl"]
        keep.canonical_course_url = plan["canonicalUrl"]
        keep.intake_months = plan["intakes"]
        for key, value in plan["_values"].items():
            setattr(keep, key, value)
        keep.extraction_method = {**(keep.extraction_method or {}), "intake_consolidation": {
            "row_ids": ids, "source_urls": [r["url"] for r in plan["rows"]],
            "entry_year": plan["entryYear"], "authority": "existing-records",
        }}
        if plan.get("_feeSchedules"):
            from app.services.assistant_intake_fees import KEY
            keep.extraction_method = {**keep.extraction_method, KEY: deepcopy(plan["_feeSchedules"])}
        # Existing source rows and evidence remain intact. The combined intake
        # evidence is explicitly derived, never passed off as newly verified HTML.
        proof = ScrapedFieldEvidence(
            scraped_course_id=keep.id, field_key="intake_months",
            candidate_value=json.dumps(plan["intakes"]), normalized_value=json.dumps(plan["intakes"]),
            source_url=plan["canonicalUrl"],
            extraction_method="intake:consolidated_existing_records:" + action["id"],
            snippet="Combined existing intake records: " + ", ".join(plan["intakes"]),
            selected=True, validation_status="unverified",
        )
        db.add(proof)
        proofs = [proof]
        if plan.get("_feeSchedules"):
            schedule_proof = ScrapedFieldEvidence(
                scraped_course_id=keep.id, field_key="intake_fee_schedules",
                candidate_value=json.dumps(plan["_feeSchedules"], sort_keys=True),
                normalized_value=json.dumps(plan["_feeSchedules"], sort_keys=True),
                source_url=plan["canonicalUrl"],
                extraction_method="fees:consolidated_intake_schedules:" + action["id"],
                snippet="Retained separate tuition years and intake applicability from existing pending records.",
                selected=True, validation_status="unverified",
            )
            db.add(schedule_proof)
            proofs.append(schedule_proof)
        warnings = []
        for row in rows:
            warnings.extend(w for w in (row.scrape_warnings or []) if w not in warnings)
        if plan.get("_feeSchedules"):
            warnings.append("intake_fee_schedules_publication_hold")
        keep.scrape_warnings = warnings
        for row in rows:
            if row.id != keep.id:
                row.status = "rejected"
                row.rejection_reason = f"Intake variant consolidated into pending row {keep.id}; original evidence retained."
            db.add(CourseAuditLog(
                scraped_course_id=row.id, field_key="intake_consolidation",
                action="assistant_intake_merge", actor=str(actor_id),
                old_value=json.dumps(snapshots[row.id], default=str),
                new_value=json.dumps({k: plain(getattr(row, k)) for k in fields}, default=str),
                reason=f"Staff requested one pending course with multiple intakes; retained row {keep.id}; no publication",
            ))
        await db.flush()
        # Reuse the existing complete-row and PostgreSQL tuple-version fences.
        action["_undo"] = [{
            "id": r.id, "before": snapshots[r.id], "version": version(service, r, "pending"),
            "revision": await revision(db, ScrapedCourse, r.id),
        } for r in rows]
        action["_evidenceIds"] = [p.id for p in proofs]
        action["_evidenceVersions"] = {str(p.id): version(service, p, "evidence") for p in proofs}
        action["_evidenceRevisions"] = {
            str(p.id): await revision(db, ScrapedFieldEvidence, p.id) for p in proofs
        }
        action.update(state="completed", jobId=action["_jobId"],
                      resultSummary=f"Consolidated {len(rows)} pending rows into {keep.course_name}: "
                                    + ", ".join(keep.intake_months) + ". Nothing published.")


def undo_proposal(action):
    return {
        "id": uuid.uuid4().hex, "kind": "undo_intake_merge", "state": "proposed",
        "label": "Undo intake consolidation", "fields": [], "courseCount": len(action["_ids"]),
        "requiresConfirmation": True,
        **{k: deepcopy(action[k]) for k in (
            "_ids", "_jobId", "_undo", "_evidenceIds", "_evidenceVersions", "_evidenceRevisions",
        )},
    }
