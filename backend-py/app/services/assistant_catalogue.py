"""Read-only, bounded official catalogue comparison. Candidates are not eligibility proof."""
from __future__ import annotations

import asyncio
import json
import re
from urllib.parse import urljoin, urlsplit, urlunsplit, parse_qsl, urlencode

from bs4 import BeautifulSoup
from sqlalchemy import select

from app.models.course import Course
from app.models import ScrapedCourse
from app.services.scrape_assistant_tools import public_request, official_url

MAX_CANDIDATES = 200
MAX_PAGES = 8
AWARD = re.compile(r"\b(?:bachelor|master|doctor|diploma|certificate|associate|phd|bsc|msc|mba|llm)\b", re.I)


def is_course_candidate_url(url):
    path = urlsplit(url).path.lower()
    segments = [value for value in path.split("/") if value]
    editorial = {"news", "news-and-opinion", "news-and-events", "newsroom", "blog", "blogs", "events", "articles", "stories", "press-releases"}
    return not (set(segments[:2]) & editorial or re.search(r"\.(?:pdf|docx?|xlsx?|png|jpe?g|zip)$", path))


def identity(url):
    value = urlsplit(url)
    query = [(key, val) for key, val in parse_qsl(value.query, keep_blank_values=True)
             if not key.lower().startswith("utm_") and key.lower() not in {"gclid", "fbclid"}]
    return urlunsplit(("https", value.netloc.lower().removeprefix("www."),
                      value.path.rstrip("/"), urlencode(sorted(query)), ""))


def path_value(item, path):
    for key in path.split("."):
        if isinstance(item, dict):
            item = item.get(key)
        elif isinstance(item, list) and key.isdigit() and int(key) < len(item):
            item = item[int(key)]
        else:
            return None
    return item


async def compare_candidates(candidates, university_id, factory):
    """Read metadata only; approval/history elsewhere must not become 'missing'."""
    async with factory() as db:
        published = (await db.execute(select(Course.id, Course.course_website, Course.status,
                                            Course.approval_status).where(
            Course.university_id == university_id))).all()
        pending = (await db.execute(select(ScrapedCourse.course_website, ScrapedCourse.status,
                                          ScrapedCourse.scrape_job_id).where(
            ScrapedCourse.university_id == university_id))).all()
    index = {}
    for row in published:
        if not row.course_website:
            continue
        entry = index.setdefault(identity(row.course_website), {"courseIds": [], "reviewJobIds": [], "statuses": set()})
        entry["courseIds"].append(row.id)
        entry["statuses"].add("published" if row.status == "active" and row.approval_status == "approved" else "recorded")
    for row in pending:
        if not row.course_website:
            continue
        entry = index.setdefault(identity(row.course_website), {"courseIds": [], "reviewJobIds": [], "statuses": set()})
        entry["statuses"].add("pending" if row.status in {"pending", "review", "review_ready"} else "recorded")
        if row.scrape_job_id and row.status in {"pending", "review", "review_ready"}:
            entry["reviewJobIds"].append(row.scrape_job_id)
    rows = []
    for candidate in candidates:
        known = index.get(identity(candidate["url"]), {})
        status = next((key for key in ("published", "pending", "recorded") if key in known.get("statuses", set())), "missing")
        rows.append({**candidate, "status": status, "courseIds": known.get("courseIds", [])[:10],
                     "reviewJobIds": list(dict.fromkeys(known.get("reviewJobIds", [])))[:10]})
    return rows


async def inspect_comparison(config, home, university_id, factory):
    candidates, sources, seen = {}, [], set()
    discovery = config.discovery
    queue = list(dict.fromkeys(list(discovery.seed_urls or []) + [home]))[:MAX_PAGES]

    def add(url, title, base):
        try:
            url = official_url(urljoin(base, str(url)), home)
        except (ValueError, TypeError):
            return
        if not is_course_candidate_url(url):
            return
        if any(re.search(pattern, url, re.I) for pattern in (discovery.block_url_patterns or [])):
            return
        if len(candidates) < MAX_CANDIDATES:
            candidates.setdefault(identity(url), {"url": url, "title": str(title or url)[:240]})

    async def read_page(url):
        try:
            url = official_url(url, home)
            raw = await asyncio.wait_for(public_request(url), 8)
            soup = BeautifulSoup(raw, "lxml")
            title = soup.title.get_text(" ", strip=True) if soup.title else ""
            if raw.lstrip().startswith(b"%PDF") or re.search(r"just a moment|access denied|verify.*human", title, re.I):
                raise ValueError("Page could not be verified")
            if len(soup.get_text(" ", strip=True)) < 120:
                raise ValueError("Page has insufficient readable content")
            for anchor in soup.select("a[href]"):
                href = urljoin(url, anchor["href"])
                label = anchor.get_text(" ", strip=True)
                slug = urlsplit(href).path.replace("-", " ")
                if AWARD.search(slug) and AWARD.search(label):
                    add(href, label, url)
                if (anchor.get("rel") and "next" in anchor["rel"]) or label.lower() in {"next", "next page"}:
                    try:
                        next_url = official_url(href, home)
                        if next_url not in queue and next_url not in seen:
                            queue.append(next_url)
                    except ValueError:
                        pass
            sources.append({"url": url, "status": "checked"})
        except Exception:
            # Do not expose provider internals or exception text to the client.
            sources.append({"url": url, "status": "unverified", "note": "Could not read this official catalogue page."})

    async def read_api():
        api = discovery.generic_search_api
        if not api or not api.enabled:
            return
        try:
            raw = await asyncio.wait_for(public_request(api.url, method=api.method.upper(),
                                                        body=api.body, params=api.params), 8)
            document = json.loads(raw)
            items = path_value(document, api.root_path) if api.root_path else document
            if not isinstance(items, list):
                raise ValueError("No course records")
            for item in items[:MAX_CANDIDATES]:
                url = next((path_value(item, key) for key in api.url_fields if path_value(item, key)), None)
                title = next((path_value(item, key) for key in api.title_fields if path_value(item, key)), "")
                if isinstance(url, str) and isinstance(title, str) and AWARD.search(title):
                    add(url, title, home)
            sources.append({"url": api.url, "status": "checked", "note": "First API page only."})
        except Exception:
            sources.append({"url": api.url, "status": "unverified", "note": "Catalogue API could not be verified."})

    try:
        async with asyncio.timeout(25):
            await read_api()
            while queue and len(seen) < MAX_PAGES and len(candidates) < MAX_CANDIDATES:
                batch = []
                while queue and len(batch) < 4 and len(seen) < MAX_PAGES:
                    url = queue.pop(0)
                    if url not in seen:
                        seen.add(url)
                        batch.append(url)
                await asyncio.gather(*(read_page(url) for url in batch))
    except TimeoutError:
        sources.append({"url": home, "status": "unverified", "note": "Comparison time budget reached."})
    rows = await compare_candidates(list(candidates.values()), university_id, factory)
    return {
        "available": bool(rows), "complete": False, "observed": len(rows),
        "note": "Partial comparison of observed official course links, not proof of full catalogue coverage or international eligibility. Unseen courses are not removed.",
        "sources": sources, "courses": rows,
        "counts": {status: sum(row["status"] == status for row in rows)
                   for status in ("published", "pending", "recorded", "missing")},
    }
