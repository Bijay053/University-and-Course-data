"""Bounded official guidance alternatives, never URL extension rewriting."""
import re


def discovered_english_urls(home, sources):
    """Only actual links from verified official pages, never guessed paths."""
    from app.services.scrape_assistant_tools import official_url
    links = []
    for source in sources[:20]:
        if source.get("verified") is not True:
            continue
        try:
            official_url(source.get("url"), home)
        except (ValueError, TypeError, AttributeError):
            continue
        for candidate in (source.get("englishLinks") or [])[:8]:
            try:
                candidate = official_url(candidate, home)
            except (ValueError, TypeError, AttributeError):
                continue
            if candidate not in links:
                links.append(candidate)
    return links[:5]

def is_english_guidance(source):
    return source.get("guidanceTopic") == "english" or source.get("sourceType") == "pdf" and bool(re.search(
        r"eng(?:lish)?[-_ ]?lang|english-language", source.get("url", ""), re.I,
    ))


def configured_english_urls(home, config):
    """Recheck ownership at use time, including when context was copied/replayed."""
    from app.services.scrape_assistant_tools import official_url
    from app.services.scraper.config.schema import ResearchGuidanceConfig
    try:
        research = ResearchGuidanceConfig.model_validate(
            config.research.model_dump() if config is not None else {}
        )
        urls = research.english_guidance_urls
        if any(official_url(url, home) != url for url in urls):
            return []
        return list(dict.fromkeys(urls))
    except (ValueError, TypeError, AttributeError):
        return []


def mark_configured_guidance(result, home, config):
    if result.get("url") in configured_english_urls(home, config):
        result = {**result, "guidanceTopic": "english",
                  "guidanceNote": "General research only; course applicability is not verified."}
    return result


def english_guidance_alternatives(home, prompt, sources, config=None):
    """Candidates are unverified until the ordinary robots-fenced reader checks them."""
    candidates = configured_english_urls(home, config)
    sources = [mark_configured_guidance(source, home, config) for source in sources]
    if not sources or not any(is_english_guidance(source) for source in sources):
        return []
    if any(source.get("verified") and is_english_guidance(source) for source in sources):
        return []
    seen = {source.get("url", "").split("#", 1)[0] for source in sources}
    return [url for url in candidates if url not in seen][:2]


def source_check_detail(result):
    """Keep permission failures separate from HTTP/path and document failures."""
    if result.get("verified"):
        return "Published URL preserved. Official document read; course applicability remains separate."
    stage = result.get("failureStage")
    detail = result.get("error", "Source could not be verified.")
    if stage == "robots":
        return f"URL preserved; robots.txt check failed before the document was requested. {detail}"
    if stage == "source_http":
        return f"URL preserved; document request returned HTTP {result.get('httpStatus')}. {detail}"
    if stage == "url_validation":
        return f"URL validation failed before a network request. {detail}"
    return detail


def gate_general_english_proposals(reply, sources):
    """A central PDF is research, not per-course authority for a whole-review fix."""
    if not any(source.get("verified") and is_english_guidance(source) for source in sources):
        return reply
    blocked = []
    for action in reply.get("actions", []):
        if (action.get("kind") == "fix_fields" and action.get("state") == "proposed"
                and "ielts_overall" in action.get("fields", [])):
            action.update(
                state="blocked", requiresConfirmation=True,
                error="General guidance is not course-specific English evidence. "
                "Check each course, including health and postgraduate research exceptions, before proposing updates.",
            )
            blocked.append(action)
    if blocked:
        reply["content"] += (
            "\n\nNo whole-review IELTS correction was queued. The central guidance must first be "
            "matched to each course's requirements, especially health courses and postgraduate research."
        )
    return reply
