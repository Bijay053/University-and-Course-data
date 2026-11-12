"""Never queue a source-dependent bulk repair on zero verified pages."""
from app.services.assistant_source_cache import ACCESS_ISSUES

SOURCE_CACHE_SECONDS = 16
SOURCE_CHECK_SECONDS = SOURCE_CACHE_SECONDS + 2  # Reader plus registry/cache persistence.


def same_course_url(left, right):
    """Only tolerate a path's trailing slash; never merge query variants/hosts."""
    from urllib.parse import urlsplit
    try:
        a, b = urlsplit(left), urlsplit(right)
        return (a.scheme.lower() == b.scheme.lower() == "https"
                and not any((a.username, a.password, b.username, b.password))
                and a.hostname == b.hostname and a.port == b.port
                and a.path.rstrip("/") == b.path.rstrip("/")
                and a.query == b.query)
    except (TypeError, ValueError):
        return False


def repair_probe_urls(rows, action):
    """Use a plan's checked source for that same row, not an unchecked variant.

    These URLs come from the server-stored action, never the confirmation body.
    The ordinary cache/registry/robots checks still apply to the selected URL.
    """
    urls = []
    for row in rows:
        supported = [
            p["source"]["url"] for p in action.get("repairPreview", [])
            if p.get("id") == row.id and p.get("status") == "supported"
            and (p.get("source") or {}).get("url")
        ]
        candidates = supported + list(action.get("_evidenceUrls") or [])
        urls.append(next((u for u in candidates
                          if same_course_url(u, row.course_website)),
                         row.course_website))
    return urls


def access_blocked_answer(sources):
    """Explain this check's access failure, not an invented historical cause."""
    if not sources or any(s.get("verified") for s in sources):
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
        + (
            "I will not bypass access restrictions or invent a value. "
            "A permitted official source, or a value you explicitly supply as staff-provided, "
            "is needed to continue. Staff-provided updates still follow the normal confirmation rules."
            if any(s.get("accessIssue") in ACCESS_ISSUES for s in sources) else
            "This is an incomplete or unsuccessful source check, not proof that the university "
            "has no requirement or that the original extraction failed for the same reason. "
            "No wider course repair is promised from this failed check. "
            "A complete, permitted official source is still required before source-dependent values can be verified."
        )
    )


async def require_reachable_sources(urls,home,inspect=None):
    import asyncio
    if inspect is None:
        from app.services.scrape_assistant_tools import inspect_source
        inspect=inspect_source
    async def check(url):
        try:return await asyncio.wait_for(inspect(url,home),SOURCE_CHECK_SECONDS)
        except (Exception,):return {"verified":False, "error":"Verification did not finish within the source-check budget."}
    candidates=list(dict.fromkeys(u for u in urls if u))
    samples=candidates[:3]
    results=await asyncio.gather(*(check(u) for u in samples))
    if (not any(s.get("verified") for s in results) and len(candidates)>3
            and not any(s.get("accessIssue") in ACCESS_ISSUES for s in results)):
        # Three unreadable course pages do not establish a provider-wide outage.
        # Try at most three *different* pages, spread across the existing review.
        # Never retry a refusal, rediscover a catalogue, or invent field values.
        remaining=candidates[3:]
        extra=list(dict.fromkeys(remaining[i] for i in (0,len(remaining)//2,len(remaining)-1)))
        samples.extend(extra)
        results.extend(await asyncio.gather(*(check(u) for u in extra)))
    if not any(s.get("verified") for s in results):
        details = "; ".join(
            f"{url}: {str(result.get('error') or 'No verified source was returned.')[:300]}"
            for url, result in zip(samples, results))
        raise ValueError(
            "No sampled official page could be verified; bulk repair was not queued. "
            + details + " Provide a permitted official course URL or authorised document.")
    return results
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
