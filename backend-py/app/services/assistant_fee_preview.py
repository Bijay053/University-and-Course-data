"""Read every selected pending row for a fee preview, without executing repairs."""
import asyncio
import re
from urllib.parse import urlsplit


def requested(prompt):
    """A later 'do not apply/update' protects a positive preview instruction."""
    from app.services.scrape_assistant_autorun import preview_requested
    if not preview_requested(prompt):
        return False
    text = re.sub(r"https://\S+", " ", prompt, flags=re.I)
    positive = " ".join(
        re.split(r"\b(?:do not|don't|never)\b", part, maxsplit=1, flags=re.I)[0]
        for part in re.split(r"[.;!?\n]|\b(?:but|then|however)\b", text, flags=re.I)
    )
    return bool(re.search(
        r"\b(?:add|update|set|fill|fix|correct|check|review)\s+"
        r"(?:(?:the|missing|international|course|all|existing|pending)\s+){0,5}"
        r"(?:fees?|tuition)\b|\b(?:fee|tuition)\s+(?:approval\s+)?preview\b",
        positive, re.I,
    ))


def proposal(row, source):
    result = {"id": row["id"], "name": row["name"], "url": row.get("url"),
              "status": "unverified", "changes": [],
              "reason": source.get("error") or "No applicable international tuition was verified."}
    if not source.get("verified") or not row.get("url"):
        return result
    def identity(url):
        parsed = urlsplit(url)
        return parsed.hostname, parsed.path.rstrip("/"), parsed.query
    if identity(source.get("url", "")) != identity(row["url"]):
        result["reason"] = "Source resolved to another page; no tuition was borrowed."
        return result
    candidates = []
    for fact in source.get("facts") or []:
        normalized = fact.get("normalized")
        if fact.get("field") != "international_fee" or not isinstance(normalized, dict):
            continue
        if urlsplit(row["url"]).hostname in {"tru.ca", "www.tru.ca"} and fact.get("method") != (
            "fee.tru_international_calculator_four_courses"
        ):
            continue
        amount = normalized.get("international_fee")
        currency = normalized.get("currency")
        term = normalized.get("fee_term")
        if (type(amount) not in (int, float) or not 0 < amount < 200000
                or not currency or not term or not fact.get("quote")):
            continue
        candidates.append((normalized, fact["quote"]))
    distinct = {tuple((k, str(v)) for k, v in sorted(n.items())) for n, _ in candidates}
    if len(distinct) != 1:
        if distinct:
            result["reason"] = "Conflicting tuition facts require review."
        return result
    values, quote = candidates[0]
    fields = [("international_fee", "fee"), ("currency", "currency"),
              ("fee_term", "feeTerm"), ("fee_year", "feeYear")]
    result.update(
        status="supported", reason="",
        source={"url": source["url"], "quote": quote},
        changes=[{"field": field, "oldValue": row.get(old), "newValue": values.get(field)}
                 for field, old in fields],
    )
    return result


async def handle(service, thread_id, turn_id, run_id, ctx, prompt):
    from app.services.assistant_eligibility import scope
    from app.services.assistant_source_cache import cached_source
    from app.services.scraper.extractors.tru_calculator import asset_session
    rows = scope(prompt, ctx["samples"])
    if not rows:
        await service.save_research_reply(thread_id, turn_id, run_id, {
            "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
            "content": "No existing pending course matched this fee preview. No discovery or update was run.",
            "actions": [], "sources": [],
        }, detail="No matching pending fee records.")
        return
    if len(rows) > 2000:
        raise ValueError("The fee preview exceeds the 2000-pending-row limit.")
    semaphore = asyncio.Semaphore(3)
    reads = {}
    completed = 0
    async def check(row):
        nonlocal completed
        url = row.get("url")
        if not url:
            source = {"verified": False, "error": "Pending course has no official page URL."}
        else:
            async def read():
                async with semaphore:
                    try:
                        return await cached_source(service, thread_id, turn_id, run_id,
                            url, ctx["home"], service.inspect_source, 25, fresh=True)
                    except (ValueError, OSError, TimeoutError) as exc:
                        return {"verified": False, "error": f"Source check failed: {type(exc).__name__}"}
            if url not in reads:
                reads[url] = asyncio.create_task(read())
            source = await reads[url]
        result = proposal(row, source)
        completed += 1
        if completed % 10 == 0 or completed == len(rows):
            await service.record_activity(thread_id, turn_id, run_id, service.event(
                "Checking course-specific international tuition", status="running",
                detail=f"{completed}/{len(rows)} pending rows assessed; no records changed."))
        return result
    async with asset_session():
        previews = await asyncio.gather(*(check(row) for row in rows))
    supported = [p for p in previews if p["status"] == "supported"]
    changed = [p for p in supported if any(
        c["newValue"] != c["oldValue"] for c in p["changes"])]
    action = service.action_proposal(
        {"kind": "fix_fields", "fields": ["international_fee"]}, [p["id"] for p in changed])
    if action:
        action.update(
            label="Review course-specific tuition proposals", requiresConfirmation=True,
            _versions=ctx["versions"], _configVersion=ctx["config_version"],
            repairPreview=previews,
            courseScope=[{"id": r["id"], "name": r["name"], "url": r.get("url")} for r in rows],
        )
    content = (
        f"Fee approval preview: {len(rows)}/{len(rows)} pending rows assessed using "
        f"{len(reads)} distinct course-page checks. {len(changed)} rows have proposed changes; "
        f"{len(supported)-len(changed)} are unchanged; {len(rows)-len(supported)} remain unverified. "
        "Each supported proposal uses its own applicable international tuition. "
        "Four-subject TRU calculator results are semester tuition, excluding books and ancillary fees. "
        "An unstated fee year remains unverified. No courses were changed, created, approved or published. "
        "Any later repair requires confirmation and fresh source checks."
    )
    if not action:
        content += "\n\n" + "\n".join(
            f"- {p['name']}: {p['reason'] or 'No change proposed.'}" for p in previews)
    await service.save_research_reply(thread_id, turn_id, run_id, {
        "role": "assistant", "turnId": turn_id, "createdAt": service.now().isoformat(),
        "content": content, "actions": [action] if action else [],
        "sources": [{"url": p["source"]["url"], "title": p["name"],
                     "excerpt": p["source"]["quote"]} for p in supported],
        "feeReview": {"checked": len(rows), "proposed": len(changed),
                      "unchanged": len(supported)-len(changed),
                      "unverified": len(rows)-len(supported), "rows": previews},
    }, detail=f"Fee preview finished: {len(rows)} assessed; no course updates.")
