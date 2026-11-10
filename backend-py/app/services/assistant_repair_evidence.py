"""Bounded, deterministic observations from scraper logs; never execute log text."""
import re
from collections import Counter
from itertools import islice
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from app.models.scrape_runtime import ScrapeRuntimeLog

LOG_LIMIT = 250


def summarize(messages, home):
    host = (urlsplit(home or "").hostname or "").removeprefix("www.").lower()
    listings, incidental = set(), set()
    quality = Counter()
    listing_failures = 0
    for message in islice(messages, LOG_LIMIT):
        if not isinstance(message, str):
            continue
        # Only known scraper-generated diagnostic prefixes are eligible.
        if not re.match(r"^\[(?:STAGE|CENTRAL|DATA QUALITY|FIELD TRACE|EXTRACT)\]", message):
            continue
        listing_failures += int(message.startswith("[STAGE] extraction failed for Search for Courses:"))
        match = re.match(r"\[DATA QUALITY\]\[CRITICAL\] ([a-z_]+):", message)
        if match:
            quality[match[1]] += 1
        for raw in re.findall(r"https://[^\s<>\"']+", message[:4000]):
            try:
                parsed = urlsplit(raw.rstrip(".,;)"))
                port = parsed.port
            except ValueError:
                continue
            if not host or (parsed.hostname or "").removeprefix("www.").lower() != host:
                continue
            if parsed.username or parsed.password or port not in (None, 443):
                continue
            # Do not expose arbitrary query parameters or browser fragments.
            url = urlunsplit(("https", parsed.netloc, parsed.path, "", ""))
            if message.startswith("[FIELD TRACE]") and parsed.path.rstrip("/") in {"/courses", "/course-finder", "/study/course-finder"}:
                listings.add(url)
            if message.startswith("[CENTRAL]") and re.search(
                r"/(?:additional-costs|ancillary-fees|incidental-fees)(?:/|$)", parsed.path, re.I
            ):
                incidental.add(url)
    observations = []
    if listing_failures or listings:
        observations.append({
            "kind": "listing_pages_extracted", "logOccurrences": listing_failures,
            "urls": sorted(listings)[:5],
            "detail": "Course-search/listing pages reached extraction; these are not verified individual courses.",
        })
    if incidental:
        observations.append({
            "kind": "non_tuition_central_source", "urls": sorted(incidental)[:5],
            "detail": "An additional/ancillary-cost page was selected as a central fee source. It is not evidence of international tuition.",
        })
    if quality:
        observations.append({
            "kind": "critical_field_validation", "counts": dict(quality),
            "detail": "Stored run diagnostics report critical field problems; extraction success does not prove accurate fees or English requirements.",
        })
    return {
        "observations": observations, "logSampleLimit": LOG_LIMIT,
        "scope": "Observations from a bounded log sample, not a verified root cause or full catalogue inventory.",
    }


async def load(db, job_id, home):
    message = ScrapeRuntimeLog.payload["message"].astext
    rows = (await db.execute(select(message).where(
        ScrapeRuntimeLog.runtime_job_id == job_id,
        message.op("~")(r"^\[(STAGE|CENTRAL|DATA QUALITY|FIELD TRACE|EXTRACT)\]"),
    ).order_by(ScrapeRuntimeLog.sequence.desc()).limit(LOG_LIMIT))).scalars().all()
    return summarize(rows, home)
