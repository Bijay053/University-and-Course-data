"""Read-only, whole-review IELTS comparisons with bounded source checking.

Unlike ordinary conversational research, an explicit all-course preview must
not turn a small relevance sample into an executable whole-review proposal.
"""
import asyncio
import re
import time


def requested(prompt):
    from app.services.scrape_assistant_autorun import preview_requested
    return bool(
        preview_requested(prompt)
        and re.search(r"\bielts\b", prompt, re.I)
        and re.search(
            r"\b(?:all|every)\s+(?:(?:the|existing|pending|university)\s+)*"
            r"(?:courses?|records?)\b|\b(?:entire|whole)\s+(?:pending\s+)?review\b",
            prompt, re.I)
        and not re.search(r"\b(?:selected|named|these|those)\s+(?:courses?|records?)\b", prompt, re.I)
        and not re.search(r"https?://", prompt, re.I)
    )


async def collect(rows, read, notify, *, budget=900, clock=time.monotonic):
    """Check each distinct selected URL once; never manufacture timeout reads."""
    deadline = clock() + budget
    sources = []
    seen = set()
    urls = []
    for row in rows:
        url = row.get("url")
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    for offset in range(0, len(urls), 6):
        if clock() >= deadline:
            break
        batch = urls[offset:offset + 6]

        async def checked(url):
            remaining = deadline - clock()
            if remaining <= 0:
                return None
            try:
                return await asyncio.wait_for(read(url), timeout=min(15, remaining))
            except TimeoutError:
                return {"url": url, "verified": False, "sourceType": "html",
                        "error": "Source check reached its time budget."}

        results = await asyncio.gather(*(checked(url) for url in batch))
        sources.extend(result for result in results if result is not None)
        await notify(sources)
    return sources


def proposal(service, prompt, ctx, sources):
    """Only supported comparisons enter executable scope, irrespective of model."""
    from app.services.assistant_repair_proposals import (
        finalize_field_proposal, propose_verified_ielts_preview,
    )
    action = propose_verified_ielts_preview(service, prompt, ctx, sources)
    if not action:
        return None, []
    citations = action.pop("_supportingSources")
    finalize_field_proposal(action, prompt, ctx, sources)
    action["requiresConfirmation"] = True
    return action, citations


async def run(service, thread_id, turn_id, run_id, ctx, prompt):
    from app.services.assistant_source_cache import cached_source
    from app.services.assistant_official_sources import configured_english_urls
    from app.services.scrape_assistant_coverage import research_coverage
    from app.services.scrape_assistant_tools import inspect_source, official_url

    async def read(url):
        # The normal reader enforces source permissions, cache compatibility,
        # retained-evidence age and generation fences. Never read raw snapshots
        # or change transports to get around an access refusal.
        return await cached_source(
            service, thread_id, turn_id, run_id, url, ctx["home"],
            inspect_source, 12, html_english_scope_schema=1,
            fresh=bool(re.search(r"\b(?:fresh|re-?fetch)\b", prompt, re.I)),
        )

    async def notify(sources):
        coverage = research_coverage(ctx["samples"], sources)
        await service.progress(
            thread_id, turn_id,
            f"IELTS source checks: {coverage['checked']} read, "
            f"{coverage['failed']} unavailable, {coverage['unchecked']} not checked",
            run_id,
        )

    sources = await collect(ctx["samples"], read, notify)
    # Retain independent guidance and course referral ownership. Guidance alone
    # never establishes that a course follows the standard policy.
    guidance = list(configured_english_urls(ctx["home"], ctx.get("config")))
    for source in sources:
        if source.get("verified"):
            guidance.extend(source.get("englishLinks") or [])
    seen = {source.get("url") for source in sources}
    guidance_urls = []
    for value in guidance:
        try:
            url = official_url(value, ctx["home"])
        except (TypeError, ValueError):
            continue
        if url not in seen:
            seen.add(url)
            guidance_urls.append(url)
    # Bound linked pages separately; do not follow an unbounded link graph.
    for url in guidance_urls[:6]:
        try:
            sources.append(await asyncio.wait_for(read(url), timeout=15))
        except TimeoutError:
            sources.append({"url": url, "verified": False,
                            "error": "Guidance check reached its time budget."})
    coverage = research_coverage(ctx["samples"], sources)
    action, citations = proposal(service, prompt, ctx, sources)
    supported = len(action["repairPreview"]) if action else 0
    total = len(ctx["samples"])
    content = (
        f"IELTS review: {coverage['checked']} of {total} course pages read; "
        f"{coverage['failed']} unavailable; {coverage['unchecked']} not checked.\n\n"
        f"{supported} courses have complete source-supported comparisons for overall, "
        "Listening, Reading, Writing and Speaking. "
        f"{total - supported} do not have a proposed correction in this preview; "
        "this includes unchanged verified requirements and unresolved applicability "
        "or course-specific exceptions. General guidance was not copied to them.\n\n"
        "Only the supported courses listed in the action can be processed after "
        "confirmation. No course records or provider settings were changed, "
        "and nothing was approved or published."
    )
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id,
        "createdAt": service.now().isoformat(), "content": content,
        "sources": citations, "actions": [action] if action else [],
        "researchCoverage": coverage,
    }, detail=coverage["summary"],
       phase="IELTS preview ready — approval required" if action else "IELTS source checks finished")
