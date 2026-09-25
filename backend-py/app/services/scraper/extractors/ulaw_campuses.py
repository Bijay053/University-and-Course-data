"""Course-owned ULaw locations, never the institution-wide navigation list."""
from __future__ import annotations

import calendar
import json
import re
from datetime import date
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

METHOD = "location.ulaw_course_authority"


def _norm(value):
    return " ".join(re.sub(r"[^\w ]", " ", value.casefold()).split())


def parse_course_campuses(html, url, *, course_name=None, fee_authority=None):
    """Read exact-route intake tables or the course's own Key Facts.

    Intake tables are restricted to the selected fee cohort. Generic location
    links, fee labels such as Outside London, and online delivery are not
    physical campus authority.
    """
    from .ulaw_fees import is_ulaw_course, _cohort_dates

    if not is_ulaw_course(url):
        return None
    soup = BeautifulSoup(html, "html.parser")
    canonical = soup.find("link", rel="canonical")
    if canonical and urljoin(url, canonical.get("href", "")).rstrip("/") != url.rstrip("/"):
        return None
    title = soup.find("h1")
    if not title:
        return None
    title = title.get_text(" ", strip=True)
    wanted = course_name or title
    if _norm(wanted) != _norm(title) or re.search(r"\bonline\b", wanted, re.I):
        return None
    selected = (fee_authority or {}).get("selected", [])
    if not selected or len({(o.get("year"), o.get("period"), o.get("study_variant")) for o in selected}) != 1:
        return None
    bounds = {_cohort_dates(o.get("snippet", "")) for o in selected}
    bounds.discard(None)
    if len(bounds) > 1:
        return None
    locations, snippets = [], []
    tabs = soup.select('a[role="tab"][href*="-start-dates-"]')
    if tabs:
        # Do not fall back to broader facts if a route-specific table exists
        # but does not prove locations for the applicable cohort.
        for tab in tabs:
            label = tab.get_text(" ", strip=True)
            try:
                month, year = label.split()
                month = list(calendar.month_name).index(month)
                start = date(int(year), month, 1)
            except (ValueError, TypeError):
                continue
            if bounds:
                low, high = next(iter(bounds))
                end = date(start.year, start.month, calendar.monthrange(start.year, start.month)[1])
                if end < low or start > high:
                    continue
            elif start.year != selected[0].get("year"):
                continue
            panel = soup.find(id=tab.get("href", "")[1:])
            if panel is None:
                continue
            for table in panel.find_all("table"):
                heading = table.find_previous(["h5", "h4", "h3"])
                if heading is None or heading not in panel.descendants or _norm(heading.get_text(" ", strip=True)) != _norm(wanted):
                    continue
                names = [li.get_text(" ", strip=True) for li in table.select("tbody td li")]
                locations.extend(names)
                snippets.append(f"{label} | {wanted} | {', '.join(names)}")
    else:
        for block in soup.select(".key-facts .key-facts__locations"):
            heading = block.find(["h3", "h4"])
            if not heading or _norm(heading.get_text(" ", strip=True)) != "locations":
                continue
            for link in block.find_all("a", href=True):
                target = urlparse(urljoin(url, link["href"]))
                if target.hostname not in {"law.ac.uk", "www.law.ac.uk"} or not target.path.startswith("/locations/"):
                    continue
                locations.append(link.get_text(" ", strip=True))
            snippets.append(block.get_text(" ", strip=True))
    locations = list(dict.fromkeys(locations))
    # Online is a separate delivery route, not an Outside London campus.
    physical = [name for name in locations if _norm(name) not in {"online", "ulaw online"}]
    if not physical or any(not re.fullmatch(r"[A-Za-z][A-Za-z ()'-]{1,70}", name) or _norm(name) in {
        "outside london", "all locations", "all campuses", "on campus", "uk", "various",
    } for name in physical):
        return None
    return {
        "source_url": url, "course_name": wanted, "locations": physical,
        "fee_year": selected[0]["year"], "fee_term": selected[0]["period"],
        "study_variant": selected[0]["study_variant"],
        "snippet": " | ".join(snippets), "method": METHOD,
    }


def apply_course_campus_authority(html, url, payload, evidence, fee_authority):
    authority = parse_course_campuses(
        html, url, course_name=payload.get("course_name"), fee_authority=fee_authority,
    )
    if not authority:
        return None
    payload["course_location"] = ", ".join(authority["locations"])
    payload.setdefault("extraction_method", {})["campus_authority"] = authority
    evidence[:] = [e for e in evidence if e.get("field_key") != "course_location"]
    evidence.append({
        "field_key": "course_location", "value": payload["course_location"],
        "confidence": 0.99, "method": METHOD, "source_url": url,
        "snippet": json.dumps(authority),
    })
    return authority


async def enrich_course_campuses(db, row):
    """Enrich a locked staged row; caller owns transaction and publication.

    Live recovery is bounded, same-URL only, and requires identical fee options
    to those under review. This prevents a new location cohort being paired
    with stale fees. Failure is explicit and leaves the row untouched.
    """
    from .ulaw_fees import is_ulaw_course, parse_course_fees, validated_fee_variants
    from app.services.scraper.campus_fee_split import plan_campus_fees

    if (row.extraction_method or {}).get("campus_fee_scope"):
        return {"status": "unchanged"}
    groups, _ = plan_campus_fees(row)
    if groups:
        return {"status": "unchanged"}
    old = validated_fee_variants(row)
    if not old or not is_ulaw_course(row.course_website):
        return {"status": "needs_review", "reason": "Verified course fee evidence is required."}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            async with client.stream("GET", row.course_website) as response:
                response.raise_for_status()
                if str(response.url).rstrip("/") != row.course_website.rstrip("/"):
                    return {"status": "needs_review", "reason": "Course location source URL changed."}
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 2_000_000:
                        return {"status": "needs_review", "reason": "Course location source exceeds the safety limit."}
                    chunks.append(chunk)
                html = b"".join(chunks).decode("utf-8", errors="replace")
    except httpx.HTTPError:
        return {"status": "needs_review", "reason": "Official course locations could not be verified. Try again later."}
    fresh = parse_course_fees(html, row.course_website)
    def signature(authority):
        return sorted((o["amount"], o["currency"], o["campus"], o["year"], o["period"], o["study_variant"])
                      for o in authority.get("selected", []))
    if not fresh or signature(fresh) != signature(old):
        return {"status": "needs_review", "reason": "Published course fees changed; refresh this course before approval."}
    authority = parse_course_campuses(html, row.course_website, course_name=row.course_name, fee_authority=fresh)
    if not authority:
        return {"status": "needs_review", "reason": "Actual course campuses could not be verified for this fee cohort."}
    # Prove the full plan before touching the staged row.
    candidate = {key: getattr(row, key, None) for key in (
        "course_website", "extraction_method", "international_fee", "currency", "fee_year", "fee_term",
    )}
    candidate["course_location"] = ", ".join(authority["locations"])
    groups, reason = plan_campus_fees(candidate)
    if not groups:
        return {"status": "needs_review", "reason": reason}
    from sqlalchemy import select
    from app.models import ScrapedFieldEvidence
    row.course_location = candidate["course_location"]
    row.extraction_method = {**row.extraction_method, "course_location": METHOD, "campus_authority": authority}
    existing = (await db.execute(select(ScrapedFieldEvidence).where(
        ScrapedFieldEvidence.scraped_course_id == row.id,
        ScrapedFieldEvidence.field_key == "course_location",
        ScrapedFieldEvidence.extraction_method == METHOD,
        ScrapedFieldEvidence.source_url == row.course_website,
    ))).scalar_one_or_none()
    if existing is None:
        existing = ScrapedFieldEvidence(
            scraped_course_id=row.id, field_key="course_location",
            extraction_method=METHOD, source_url=row.course_website,
        )
        db.add(existing)
    existing.candidate_value = row.course_location
    existing.normalized_value = row.course_location
    existing.snippet = json.dumps(authority)
    existing.confidence = 0.99
    existing.selected = True
    return {"status": "resolved"}