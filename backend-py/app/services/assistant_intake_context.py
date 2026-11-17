"""Resolve intake follow-ups from the nearest topic, never stale course history."""
import re
from urllib.parse import urlsplit, urlunsplit


def matches(prompt, groups):
    urls = set()
    for raw in re.findall(r"https?://[^\s<>]+", prompt, re.I):
        p = urlsplit(raw.rstrip(".,);]"))
        urls.add(urlunsplit((p.scheme, p.netloc, p.path.rstrip("/"), p.query, "")))
    found = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        if urls:
            matched = key[1] in urls or bool(urls & {r.course_website.rstrip("/") for r in rows})
        else:
            named = re.search(r"(?<!\w)" + re.escape(key[0]) + r"(?!\w)", prompt, re.I)
            matched = named and not re.match(
                r"\s+(?:with|conversion|online|part[- ]time)\b", prompt[named.end():], re.I)
        if matched:
            found.append(rows)
    return found


def followup(prompt, messages, groups):
    if (not re.search(r"\b(?:this|these|that|them|same|it)\b|\bone course\b", prompt, re.I)
            or re.search(r"https?://|\b(?:MSc|BSc|MBA|BEng|MEng|PhD|MA|BA|LLB|LLM)\b", prompt)):
        return []
    previous = list(messages)
    if previous and previous[-1].get("role") == "user" and previous[-1].get("content") == prompt:
        previous.pop()
    # Stop at the nearest user turn, even if an older course would match.
    prior = next((m for m in reversed(previous) if m.get("role") == "user"), None)
    if prior:
        selected = matches(prior.get("content", ""), groups)
        if selected:
            return selected if len(selected) == 1 else []
        # An explicit new course/topic must not inherit an older preview.
        if re.search(r"https?://|\b(?:MSc|BSc|MBA|BEng|MEng|PhD|MA|BA|LLB|LLM)\b",
                     prior.get("content", "")):
            return []
    reply = next((m for m in reversed(previous) if m.get("role") == "assistant"), {})
    anchor = reply.get("intakeConsolidation", {}).get("canonicalUrl")
    return [rows for key, rows in groups.items() if len(rows) > 1 and key[1] == anchor]


async def repair_conflict(service, thread_id, turn_id, run_id, ctx, plan, messages):
    """Use the existing source-backed repair path on mismatched rows only."""
    from app.services import assistant_intake_review
    ids = [p["id"] for p in plan["conflicts"]["intake_months"]]
    if not set(ids) <= {r["id"] for r in ctx.get("samples", [])}:
        return False
    public = {k: v for k, v in plan.items() if not k.startswith("_")}
    details = "; ".join(
        f"row {p['id']} stores {p['value']!r}, while its dated course identity says {p['expectedIntake']}"
        for p in plan["conflicts"]["intake_months"])

    class ScopedService:
        def __getattr__(self, key):
            return getattr(service, key)

        async def save_research_reply(self, tid, turn, run, payload, **kwargs):
            payload["intakeConsolidation"] = public
            payload["content"] = (
                f"Matched {plan['courseName']} to its existing intake rows. "
                f"A merge is blocked because {details}. "
                "Checking only those intake mismatches before consolidation; URL dates are not evidence. "
                + payload["content"])
            return await service.save_research_reply(tid, turn, run, payload, **kwargs)

    prompt = "Fix course intakes for " + ", ".join(f"row {i}" for i in ids)
    return await assistant_intake_review.handle(
        ScopedService(), thread_id, turn_id, run_id, ctx, prompt, messages)
