"""Never queue a source-dependent bulk repair on zero verified pages."""
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
        reply["content"]="No official page could be verified. No source-dependent repair was queued or applied. Existing values remain unverified; source access must be restored before retrying."
    return reply
