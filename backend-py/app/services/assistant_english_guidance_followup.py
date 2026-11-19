"""Resolve conversational references to university English guidance, not course titles."""
import re
from urllib.parse import urlsplit

from app.services.scrape_assistant_tools import official_url

URL = re.compile(r"https?://[^\s<>()]+", re.I)
ENGLISH = re.compile(r"\b(?:ielts|english)\b", re.I)
REQUIREMENT = re.compile(r"\b(?:requir(?:e)?ments?|guidance|bands?|scores?)\b", re.I)
SHARED = re.compile(r"\b(?:shared|sent|provided|gave|posted|linked)\b", re.I)
ALL = re.compile(r"\b(?:all|every|each)\s+(?:pending\s+)?courses?\b", re.I)
VERIFY = re.compile(r"\b(?:verified|valid|official|correct|right|reliable)\b", re.I)


def kind(text):
    words = URL.sub("", text)
    # A field update or named course question must keep its own operation.
    if re.search(r"\b(?:add|apply|update|set|change|publish|approve|ignore|instead|not|don't)\b",
                 words, re.I):
        return None
    if ENGLISH.search(words) and ALL.search(words) and re.search(r"\bunverified\b", words, re.I):
        return "status"
    if (ENGLISH.search(words) and REQUIREMENT.search(words) and SHARED.search(words)
            and re.search(r"\b(?:link|url|page|source|scourse)\b", words, re.I)):
        return "reminder"
    if URL.search(text) and VERIFY.search(words) and re.search(r"\b(?:source|scourse|link|page)\b", words, re.I):
        return "source"
    return None


def guidance_path(url):
    path = urlsplit(url).path.lower()
    return (not re.search(r"/(?:courses?|course-finder)/", path)
            and bool(re.search(r"(?:english|ielts|eng[-_]lang).*(?:requir|qualif|guid|score)|"
                               r"(?:requir|qualif).*english", path)))


def resolve(text, messages, home, configured):
    """Earlier user turns can identify a source; they cannot grant write authority."""
    intent = kind(text)
    if not intent:
        return None
    prior = [m.get("content", "") for m in messages if m.get("role") == "user"]
    if prior and prior[-1] == text:
        prior.pop()
    # Only uninterrupted follow-ups in this thread inherit the all-course scope.
    scope_all = intent == "status"
    for old in reversed(prior):
        old_kind = kind(old)
        if old_kind == "status":
            scope_all = True
            break
        if old_kind not in {"reminder", "source"}:
            break
    def valid(url):
        try:
            clean = official_url(url.rstrip(".,;"), home)
            return clean if clean in configured or guidance_path(clean) else None
        except ValueError:
            return None
    explicit = URL.findall(text)
    if explicit:
        urls = list(dict.fromkeys(filter(None, (valid(x) for x in explicit))))
        if len(urls) != 1 or not all(valid(x) for x in explicit):
            return None if intent == "source" else (intent, scope_all, None)
        return intent, scope_all, urls[0]
    for old in reversed(prior):
        urls = list(dict.fromkeys(filter(None, (valid(x) for x in URL.findall(old)))))
        if urls:
            return intent, scope_all, urls[0] if len(urls) == 1 else None
    return intent, scope_all, configured[0] if len(configured) == 1 else None


async def handle(service, ids, thread, message, references):
    from app.services.assistant_official_sources import configured_english_urls
    ctx = await service.context(thread.job_id)
    configured = configured_english_urls(ctx["home"], ctx.get("config"))
    # Cross-review references come from the actor-private same-provider lookup.
    # Use these for URLs only, never for the current conversation's scope.
    current = resolve(message["content"], thread.messages, ctx["home"], configured)
    if not current:
        return False
    intent, scope_all, url = current
    if not URL.search(message["content"]) and (not url or url in configured):
        historical = resolve(message["content"], [*references, *thread.messages], ctx["home"], [])
        if historical and historical[2]:
            url = historical[2]
    rows = ctx["samples"]
    overall = sum(r.get("ielts") is not None for r in rows)
    verified = sum(r.get("englishVerification") == "verified" for r in rows)
    prefix = (f"In this review, {overall} of {len(rows)} pending rows have a stored overall IELTS "
              f"score; {verified} have verified English component requirements. An overall score "
              "and verified component requirements are separate. ")
    if url and scope_all and intent != "status":
        from app.services.assistant_document_review import review
        await review(*ids, thread, {**message, "content": f"Check IELTS applicability for this review using {url}",
                                    "_guidanceSource": url, "_readOnlyGuidanceContext": prefix})
        return True
    sources = []
    if url:
        from app.services.assistant_source_cache import cached_source
        from app.services.english_html_tables import SCHEMA
        source = await cached_source(service, *ids, url, ctx["home"], service.inspect_source,
                                     15, html_table_schema=SCHEMA)
        if source.get("verified"):
            content = (f"I checked {url}. It is a readable source on this university’s official "
                       "website. That does not verify an individual course’s IELTS requirements: "
                       "its programme, level and exceptions must match. No course pages were checked.")
            sources = [{"url": url, "title": source.get("title") or "Official guidance",
                        "excerpt": source.get("text", "")[:500]}]
        else:
            content = f"I could not verify {url}: {source.get('error') or 'source unavailable'}."
    else:
        content = ("I could not identify one official English requirements link. Please paste the "
                   "guidance URL, not a course name. No course pages were checked.")
    await service.save_research_reply(*ids, {
        "role": "assistant", "turnId": ids[1], "createdAt": service.now().isoformat(),
        "content": prefix + content + " No course records were changed.", "sources": sources, "actions": [],
    }, detail="English guidance enquiry; no course changes.")
    return True
