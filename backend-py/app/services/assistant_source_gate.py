"""Never queue a source-dependent bulk repair on zero verified pages."""
from app.services.assistant_source_cache import ACCESS_ISSUES


def access_blocked_answer(sources):
    """Explain this check's access failure, not an invented historical cause."""
    if not sources or any(s.get("verified") for s in sources):
        return None
    if not all(s.get("accessIssue") in ACCESS_ISSUES for s in sources):
        return None
    failures = "\n".join(
        f"- {s.get('url', 'Official source')}: {s.get('error', 'Access unavailable')}"
        + (" The document itself was not requested; its URL has not been diagnosed as wrong."
           if s.get("failureStage") == "robots" else "")
        + (" (recent check reused; no new fetch)" if s.get("_sourceCacheHit") else "")
        for s in sources[:3]
    )
    return (
        "I could not verify the requested official source on this check:\n"
        + failures
        + "\n\nThis does not establish why the stored field was originally missing. "
        "No source-dependent correction was queued or applied, and existing values were left unchanged. "
        "I will not bypass access restrictions or invent a value. "
        "A permitted official source, or a value you explicitly supply as staff-provided, "
        "is needed to continue. Staff-provided updates still follow the normal confirmation rules."
    )


async def require_reachable_sources(urls,home,inspect=None):
    import asyncio
    if inspect is None:
        from app.services.scrape_assistant_tools import inspect_source
        inspect=inspect_source
    async def check(url):
        try:return await asyncio.wait_for(inspect(url,home),12)
        except (Exception,):return {"verified":False}
    samples=list(dict.fromkeys(u for u in urls if u))[:3]
    results=await asyncio.gather(*(check(u) for u in samples))
    if not any(s.get("verified") for s in results):
        raise ValueError("No sampled official page could be verified; bulk repair was not queued. Restore permitted source access before retrying.")
    return results

def gate_unreachable_repair(reply,sources):
    if any(s.get("verified") for s in sources): return reply
    blocked=[a for a in reply.get("actions",[]) if a.get("kind") in
             {"fix_fields","repair_catalogue","review_courses","recover_catalogue","scan_catalogue"}
             and a.get("state")=="proposed"]
    for a in blocked:
        a["state"]="blocked"
        a["error"]="No official page could be verified. No repair was queued."
        a["requiresConfirmation"]=True
    if blocked or ("queue" in reply.get("content","").lower() and not reply.get("sources")):
        reply["content"]=access_blocked_answer(sources) or "No official page could be verified. No source-dependent repair was queued or applied. Existing values remain unverified; source access must be restored before retrying."
    return reply
