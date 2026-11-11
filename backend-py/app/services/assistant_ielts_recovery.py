"""Source-backed, confirmation-only fallback when research overlooks a supported repair."""
import re


def proposal_for_missing_bands(service, prompt, ctx, sources):
    if (not re.search(r"\bielts\b", prompt, re.I)
            or not re.search(r"\b(?:bands?|sub[\s-]?tests?|components?)\b", prompt, re.I)
            or not re.search(r"\b(?:missing|null|empty|blank|not recorded)\b", prompt, re.I)
            or re.search(r"\b(?:read.only|only explain|just explain|no changes|do not|don't)\b",
                         prompt, re.I)):
        return None
    components = ("listening", "reading", "writing", "speaking")
    proven_urls = set()
    evidence = {}
    for source in sources:
        if not source.get("verified"):
            continue
        for fact in source.get("facts", []):
            values = fact.get("normalized") or {}
            quote = fact.get("quote") or ""
            from app.services.scrape_assistant_tools import quote_in_source
            if (fact.get("field") == "ielts_overall" and quote_in_source(quote, source)
                    and re.search(r"\bielts\b|English language requirements", quote, re.I)
                    and all(isinstance(values.get("ielts_" + part), (int, float))
                            and 0 < values["ielts_" + part] <= 9 for part in components)):
                proven_urls.add(str(source.get("url") or "").rstrip("/"))
                evidence[str(source.get("url") or "").rstrip("/")] = {
                    "url": source["url"], "title": source.get("title") or source["url"],
                    "excerpt": quote,
                }
    matched = [row for row in ctx["samples"]
               if str(row.get("url") or "").rstrip("/") in proven_urls
               and any((row.get("ieltsBands") or {}).get(part) is None for part in components)]
    if not matched:
        return None
    # A research question is not permission to write. Offer a checked-row plan,
    # not a model-generated value or a claim about every unvisited course.
    action = service.action_proposal(
        {"kind": "fix_fields", "fields": ["ielts_overall"]}, [r["id"] for r in matched])
    action.update(_versions=ctx["versions"], _configVersion=ctx["config_version"],
                  _evidenceUrls=[r["url"] for r in matched],
                  _supportingSources=[evidence[r["url"].rstrip("/")] for r in matched],
                  requiresConfirmation=True)
    return action
