"""Resolve delivery-review scope from staff instructions, never source prose."""
import re


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
    if "study_mode" not in action.get("fields", []):
        return {}
    return {
        "forceFields": ["study_mode"],
        "forceReasons": {
            "study_mode": "Staff requested an evidence-backed delivery-mode recheck; "
                          "retain each course's applicable mode, not a review-wide Online default."
        },
    }
