"""Polite, SSRF-fenced web reads. No proxy escalation or CAPTCHA bypass."""
import asyncio
import hashlib
import json
import mimetypes
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from urllib.parse import urlsplit, urljoin
from urllib.robotparser import RobotFileParser
from sqlalchemy import select
from app.models.scrape_assistant import ScrapeAssistantThread
from app.services.scrape_assistant_tools import public_request, inspect_source, official_url, SourceReadError
from app.services.assistant_source_cache import FAILURE_REUSE_SECONDS

AGENT="StudyInfoCentre-ScrapeAssistant"


def reusable_page(result):
    if not result:
        return False
    try:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(result["checkedAt"])
    except (KeyError, TypeError, ValueError):
        return False
    lifetime = timedelta(hours=24) if result.get("verified") else timedelta(seconds=FAILURE_REUSE_SECONDS)
    return timedelta(0) <= age < lifetime


async def throttle(host):
    from redis.asyncio import Redis
    from app.config import settings
    async with Redis.from_url(settings.redis_url) as redis:
        key="agent_v2:site-rate:"+hashlib.sha256(host.encode()).hexdigest()
        for _ in range(30):
            if await redis.set(key,"1",nx=True,ex=2):
                return
            await asyncio.sleep(0.25)
        raise ValueError("University request-rate budget unavailable")


async def render(url, fetch, tab=None):
    from playwright.async_api import async_playwright
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(headless=True)
        try:
            context=await browser.new_context(service_workers="block",accept_downloads=False,
                                               user_agent=AGENT+"/2.0")
            # Browser DNS must never circumvent the pinned public HTTP transport.
            async def intercept(route):
                try:
                    if route.request.method!="GET":
                        await route.abort(); return
                    body=await fetch(route.request.url)
                    await route.fulfill(body=body,content_type=mimetypes.guess_type(route.request.url)[0] or "text/html")
                except Exception:
                    await route.abort()
            await context.route("**/*",intercept)
            await context.route_web_socket("**/*",lambda socket:socket.close())
            page=await context.new_page()
            await page.goto(url,wait_until="domcontentloaded",timeout=20000)
            if tab:
                if tab not in {"International","Fees","Entry requirements","Intakes"}:
                    raise ValueError("Only course information tabs are supported")
                await page.get_by_role("tab",name=tab,exact=True).click(timeout=4000)
            await page.wait_for_timeout(300)
            return (await page.content()).encode()
        finally:
            await browser.close()


async def web_tool(tools,name,args):
    url=official_url(args["url"],tools.ctx["home"])
    key=url+"|"+("render" if name in {"render_page","click_tab"} else "static")+"|"+args.get("tab","")
    async with tools.service.AsyncSessionLocal() as db:
        thread=await db.get(ScrapeAssistantThread,tools.thread_id)
        request=next(m for m in reversed(thread.messages) if m["role"]=="user")
        cache={}
        for message in thread.messages:
            cache.update(message.get("_agentV2Pages",{}))
    cached=cache.get(key)
    if reusable_page(cached):
        result={**cached,"cached":True}
        if not result.get("verified"):
            from app.services.assistant_official_sources import english_guidance_alternatives
            result["officialAlternatives"] = english_guidance_alternatives(
                tools.ctx["home"], "", [result],
            )
        if name=="search_site":
            result["matches"]=[line for line in result.get("text","").split(". ") if args["query"].casefold() in line.casefold()][:20]
        return result
    robots_key=urljoin(url,"/robots.txt")
    async def allowed_fetch(target,*,is_robots=False):
        target=official_url(target,tools.ctx["home"])
        if not is_robots:
            rules=cache.get(robots_key)
            if not rules or datetime.fromisoformat(rules["checkedAt"])<datetime.now(timezone.utc)-timedelta(hours=24):
                raw=await allowed_fetch(robots_key,is_robots=True)
                rules={"text":raw.decode("utf-8",errors="replace"),"checkedAt":datetime.now(timezone.utc).isoformat()}
                cache[robots_key]=rules
            parser=RobotFileParser();parser.parse(rules["text"].splitlines())
            if not parser.can_fetch(AGENT,target):
                raise SourceReadError("robots.txt disallows this source; no browser or proxy bypass was attempted.",
                                      access_issue="robots_disallowed")
            delay=parser.crawl_delay(AGENT)
            if delay and delay>2:
                # Do not silently ignore site-specific longer delays.
                raise ValueError("Site requires a longer crawl interval; background scheduling required")
        await throttle(urlsplit(target).hostname)
        try:
            return await public_request(target,user_agent=AGENT+"/2.0")
        except Exception as exc:
            status = getattr(exc, "http_status", None) or getattr(getattr(exc,"response",None),"status_code",None)
            if is_robots and status in (404, 410):
                return b"User-agent: *\nDisallow:\n"
            if is_robots and isinstance(exc, SourceReadError):
                raise SourceReadError(
                    f"robots.txt could not be verified (HTTP {status}); source access stopped without bypassing protection.",
                    access_issue="robots_unavailable", http_status=status,
                ) from None
            raise
    try:
        raw=await render(url,allowed_fetch,args.get("tab")) if name in {"render_page","click_tab"} else await allowed_fetch(url)
        result=await inspect_source(url,tools.ctx["home"],raw_body=raw)
        result={k:v for k,v in result.items() if k!="_fullText"}
        result["ok"]=bool(result.get("verified"))
    except Exception as exc:
        result={"ok":False,"verified":False,"url":url,"reason":str(exc)[:250],"sourceStatus":"unavailable"}
        if isinstance(exc, SourceReadError) and exc.access_issue:
            result.update(accessIssue=exc.access_issue, httpStatus=exc.http_status, error=str(exc))
        result.update(
            requestedUrl=args["url"], urlHandling="preserved" if url == args["url"] else "fragment_removed",
            sourceType="pdf" if urlsplit(url).path.lower().endswith(".pdf") else "html",
            failureStage=getattr(exc, "failure_stage", None) or "source_fetch",
            sourceRequestAttempted=(
                False if getattr(exc, "failure_stage", None) == "robots" else
                True if getattr(exc, "failure_stage", None) == "source_http" else None
            ),
        )
        if isinstance(exc, SourceReadError) and exc.http_status is not None:
            result["httpStatus"] = exc.http_status
    if not result.get("verified"):
        from app.services.assistant_official_sources import english_guidance_alternatives
        result["officialAlternatives"] = english_guidance_alternatives(
            tools.ctx["home"], "", [result],
        )
        result["alternativesNote"] = "Unverified candidates only; fetch each through the same robots-fenced tools."
    result["checkedAt"]=datetime.now(timezone.utc).isoformat()
    cache[key]=result
    async with tools.service.AsyncSessionLocal() as db:
        thread=(await db.execute(select(ScrapeAssistantThread).where(
            ScrapeAssistantThread.id==tools.thread_id,ScrapeAssistantThread.turn_id==tools.turn_id,
            ScrapeAssistantThread.run_id==tools.run_id,ScrapeAssistantThread.status=="researching",
        ).with_for_update())).scalar_one_or_none()
        if not thread:
            raise ValueError("Agent turn superseded")
        messages=deepcopy(thread.messages)
        next(m for m in reversed(messages) if m["role"]=="user")["_agentV2Pages"]=cache
        thread.messages=messages
        await db.commit()
    if name=="search_site":
        result={**result,"matches":[line for line in result.get("text","").split(". ")
                                   if args["query"].casefold() in line.casefold()][:20]}
    return result
