"""A missing course field never authorizes importing new courses."""
import re
from urllib.parse import urldefrag

IMPORTS = {"scan_catalogue", "recover_catalogue", "review_courses", "repair_catalogue"}
REASON = "Not started: this request concerns existing course fields, not catalogue discovery or importing courses."
FIELDS = r"\b(?:fees?|tuition|ielts|bands?|english|durations?|locations?|campus|intakes?|study\s+mode|delivery)\b"


def discovery_negated(prompt):
    return bool(re.search(
        r"\b(?:do\s+not|don't|dont|never|no)\s+"
        r"(?:(?:run|start|launch|perform|execute|any|new|fresh|course|catalogue|a|the|full)\s+){0,6}"
        r"(?:discover(?:y)?|scan(?:ning)?|catalog(?:ue)?|scrap(?:e|ing)|import(?:s)?|(?:add|create)\s+(?:new\s+)?courses?)\b",
        prompt.replace("’", "'"), re.I))


def catalogue_forbidden(prompt):
    # Source URLs supply evidence, not commands or field-selection keywords.
    prompt = re.sub(r"https?://\S+", "", prompt).replace("_", " ")
    if discovery_negated(prompt):
        return True
    # Positive coverage requests can mention fields as a second goal.
    positive_coverage = re.search(
        r"\b(?:discover|crawl|scrape|find|import|add|fetch)\s+"
        r"(?:(?:the|all|new|missing|eligible|international|these|this)\s+){0,5}(?:courses?|catalog(?:ue)?)\b"
        r"|\b(?:run|start|launch)\s+(?:(?:fresh|new|course|catalogue)\s+){0,3}discovery\b"
        r"|\bmissing\s+(?:(?:eligible|international)\s+)?(?:courses?|programmes?|programs?|degrees?)\b"
        r"|\bcourses?\s+(?:are\s+)?not\s+(?:listed|shown|showing|found)\b"
        r"|\b(?:add|import|fetch)\s+(?:this\s+|new\s+|these\s+)?courses?\b",
        prompt, re.I)
    named_award = re.search(
        r"\b(?:add|import|fetch)\s+(?:the\s+)?"
        r"(?:bachelor|master|bsc|msc|ba|ma|llm|llb|certificate|diploma)\b",
        prompt, re.I)
    return bool(re.search(FIELDS, prompt, re.I) and not (positive_coverage or named_award))


def _bounded_requested_review(prompt, action):
    text = re.sub(r"https?://\S+", "", prompt)
    if not action or re.search(
        r"\b(?:pending|existing|selected|stored|unapproved|all|every|each|remaining)\b"
        r"|\b(?:this|current|open)\s+review\b", text, re.I
    ):
        return False
    requested = {urldefrag(u.rstrip(".,);]"))[0].rstrip("/")
                 for u in re.findall(r"https?://[^\s<>\"'`]+", prompt)}
    selected = action.get("_requestedUrls") or []
    return bool(requested and selected and len(selected) <= 3
                and all(isinstance(u, str) and urldefrag(u)[0].rstrip("/") in requested for u in selected))


def action_forbidden(prompt, kind, action=None):
    if kind not in IMPORTS:
        return False
    if discovery_negated(prompt):
        return True
    if kind == "review_courses" and _bounded_requested_review(prompt, action):
        return False
    if (kind == "repair_catalogue" and not discovery_negated(prompt)
            and re.search(r"\b(?:repair|fix|diagnose)\b.{0,60}\b(?:scraper|extractor)\b", prompt, re.I)):
        return False
    return catalogue_forbidden(prompt)


def action_request(messages, action_id):
    for index in range(len(messages)-1, -1, -1):
        if any(a.get("id") == action_id for a in messages[index].get("actions", [])):
            return next((m["content"] for m in reversed(messages[:index+1]) if m["role"] == "user"), "")
    return ""


def fence_reply(reply, prompt):
    blocked = []
    for action in reply.get("actions", []):
        if action.get("state") == "proposed" and action_forbidden(prompt, action.get("kind"), action):
            action.update(state="error", error=REASON)
            blocked.append(action)
    if blocked:
        if len(blocked) == len(reply.get("actions", [])):
            reply["content"] = REASON + " No course was fetched or created."
        else:
            reply["content"] += "\n\n" + REASON
