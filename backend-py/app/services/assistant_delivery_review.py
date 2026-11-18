"""Resolve delivery-review scope from staff instructions, never source prose."""
import re
from urllib.parse import urlsplit


def pending_delivery_question(prompt):
    """A queue complaint is a new review-wide subject, not a stale URL's edit."""
    instruction = re.sub(r"https?://\S+", "", prompt)
    delivery_courses = re.search(
        r"\b(?:online(?:[- ]only)?|distance(?:[- ]learning)?)\s+courses?\b"
        r"|\bcourses?\s+(?:are\s+)?(?:still\s+)?online\b", instruction, re.I,
    )
    return bool(delivery_courses and re.search(r"\bpending\b", instruction, re.I))


def guidance_source(source):
    """Central guidance is evidence for courses, never itself a course import."""
    text = (source.get("title") or "") + " " + urlsplit(source.get("url") or "").path
    text = re.sub(r"[-_/]+", " ", text)
    return bool(re.search(
        r"\b(?:english(?: language)? requirements|language qualifications|"
        r"admission requirements|fees and funding|academic calendar|key dates)\b",
        text, re.I,
    ))


async def handle_pending_delivery_question(service, thread_id, turn_id, run_id, ctx, prompt):
    if not pending_delivery_question(prompt):
        return False
    if re.search(r"\b(?:add|import|fetch|discover|scrape|rescrape|exclude|remove|reject|delete)\b",
                 prompt, re.I):
        return False
    # Literal edits and explicit policy commands are handled before this path.
    # This complaint does not authorise a new exclusion policy or a new import.
    eligible = set(ctx["eligible_ids"])
    labelled = sum(
        row.get("id") in eligible
        and str(row.get("studyMode") or row.get("study_mode") or "").casefold() == "online"
        for row in ctx["samples"]
    )
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": (
            "This is a question about existing pending courses, not a request to add a course. "
            f"{labelled} pending record(s) in this review are currently labelled Online; "
            "stored labels alone do not verify online-only delivery. "
            "The existing delivery policy retains online-only courses in pending review with "
            "eligibility warnings, rather than silently removing them. They must not be "
            "automatically published. Courses offering both campus and online study are distinct "
            "from online-only courses. Excluding online-only courses would change that policy "
            "and needs an explicit scope: this university or all universities. "
            "No course was fetched, added, removed or changed, and no catalogue discovery was run. "
            "Guidance pages such as English requirements are not courses and will not be imported."
        ),
        "sources": [], "actions": [],
    }, detail="Existing pending delivery policy explained; no import or record changes.")
    return True


def delivery_review_proposal(service, prompt, ctx, sources):
    """A linked example must not erase an explicit review-wide delivery request."""
    instruction = re.sub(r"https?://\S+", "", prompt)
    if re.search(r"\bread[- ]only\b|\b(?:only|just)\s+explain\b", instruction, re.I):
        return None
    if not re.search(r"\b(?:online|blended|on[- ]campus|distance|delivery|study mode)\b", instruction, re.I):
        return None
    if not re.search(r"\b(?:review|check|fix|correct|verify)\b", instruction, re.I):
        return None
    if re.search(r"\b(?:do not|don't|never)\s+(?:review|check|fix|correct|verify)\b", instruction, re.I):
        return None
    if re.search(r"\b(?:not\s+all|only\s+(?:this|one))\b", instruction, re.I):
        return None
    if not re.search(r"\b(?:all|every)\s+(?:pending\s+)?courses?\b", instruction, re.I):
        return None
    # Reuse the normal access gate. One readable example permits preparing a
    # bounded job, not claiming that the rest of the review was checked.
    supporting = [
        {"url": source["url"], "title": source.get("title") or "Official source",
         "excerpt": excerpt["text"]}
        for source in sources
        if source.get("verified") and source.get("sourceType") != "pdf"
        for excerpt in source.get("excerpts", [])[:1]
        if service.quote_in_source(excerpt.get("text") or "", source)
    ]
    if not supporting:
        return None
    eligible = set(ctx["eligible_ids"])
    ids = list(dict.fromkeys(row["id"] for row in ctx["samples"] if row["id"] in eligible))
    action = service.action_proposal({"kind": "fix_fields", "fields": ["study_mode"]}, ids)
    if not action:
        return None
    action["label"] = "Check delivery mode across this review"
    # Conflicting descriptions need review; never turn the user's example into
    # an instruction to set every row to Online or bypass staff confirmation.
    action["requiresConfirmation"] = True
    action["_deliveryReview"] = True
    action["_supportingSources"] = supporting[:3]
    return action


def delivery_recheck_options(action):
    """Explicit rechecks must inspect populated modes, not just missing fields."""
    if set(action.get("fields", [])) == {"intake_months"}:
        return {
            "forceFields": ["intake_months"],
            "forceReasons": {"intake_months":
                "Staff requested an official-source intake recheck on existing pending rows. "
                "Use each course's own start dates and selected offering; never copy the example's dates."},
        }
    if "study_mode" not in action.get("fields", []):
        return {}
    return {
        "forceFields": ["study_mode"],
        "forceReasons": {
            "study_mode": "Staff requested an evidence-backed delivery-mode recheck; "
                          "retain each course's applicable mode, not a review-wide Online default."
        },
    }
