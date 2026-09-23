"""Bounded official catalogue replay shared by repair and normal discovery."""
import re
from urllib.parse import urljoin, urlsplit

from app.services.scraper.ai_repair_live import official_url, passes
from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url

NEXT_ACTION = "Provide an official university course catalogue or course page URL and try Repair discovery again."


async def discover_official_catalogue(evidence) -> dict:
    from app.services.scraper.guards import classify_course_candidate, OBVIOUS_NON_DEGREE, is_blocked_page
    seed = evidence.ctx["scrape_url"]
    discovery = evidence.config.discovery.model_dump()
    configured = discovery.get("sitemap_url")
    sources = list(dict.fromkeys(filter(None, [
        configured, urljoin(seed, "/sitemap.xml"), seed,
    ])))[:3]
    for source_index, source in enumerate(sources):
        if not official_url(source, seed, evidence.extra_hosts):
            continue
        record = await evidence.fetch(source)
        if record.get("classification") not in {"sitemap", "listing"}:
            continue
        candidates = list(dict.fromkeys(
            urljoin(source, item["url"]) for item in record.get("links", [])
            if isinstance(item, dict) and isinstance(item.get("url"), str)
            and official_url(urljoin(source, item["url"]), seed, evidence.extra_hosts)
            # Sitemap locations have no degree-qualified anchor text. Treat
            # detail-shaped paths only as candidates; course-owned live facts,
            # not URL heuristics, authorize persistence.
            and re.search(r"/(?:courses?|programmes?|programs?)/(?:[^/]+/)*[^/]+/?$",
                          urlsplit(urljoin(source, item["url"])).path)
            and urlsplit(urljoin(source, item["url"])).path.rstrip("/") != urlsplit(seed).path.rstrip("/")
            and urlsplit(urljoin(source, item["url"])).path.rstrip("/").rsplit("/", 1)[-1].lower()
                not in {"undergraduate", "postgraduate", "apprenticeships", "research", "course-hero-ctas"}
            and classify_course_candidate(urljoin(source, item["url"]))[0] != OBVIOUS_NON_DEGREE
            and not re.search(r"/(?:apprenticeships?|cpd(?:-and-short-courses)?|short-courses?)/",
                              urlsplit(urljoin(source, item["url"])).path, re.I)
            and not is_blocked_page(urljoin(source, item["url"]))[0]
            and passes(urljoin(source, item["url"]), discovery)
            and not is_intentionally_excluded_course_url(urljoin(source, item["url"]))
        ))
        # Never persist an explicitly paginated first page as a whole strategy.
        # Neither a complete sitemap nor an unpaginated listing proves every
        # course is eligible: normal extraction and the floor guard still run.
        if len(candidates) >= 2 and not record.get("has_pagination"):
            return {"source": source, "candidates": candidates,
                    "sample": candidates[:max(0, min(4, (evidence.max_pages - 2 * (source_index + 1)) // 2))]}
    return {"source": None, "candidates": [], "sample": []}