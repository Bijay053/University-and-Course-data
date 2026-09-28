"""Bounded official catalogue replay shared by repair and normal discovery."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit

from app.services.scraper.ai_repair_live import official_url, passes
from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url

NEXT_ACTION = "Provide an official university course catalogue or course page URL and try Repair discovery again."
_SITEMAP_DISCOVERY_TIMEOUT_S = 20
_SITEMAP_MAX_CANDIDATES = 200


async def discover_official_catalogue(evidence) -> dict:
    from app.services.scraper.guards import classify_course_candidate, OBVIOUS_NON_DEGREE, is_blocked_page
    seed = evidence.ctx["scrape_url"]
    discovery = evidence.config.discovery.model_dump()
    configured = discovery.get("sitemap_url")
    canonicalizations = discovery.get("sitemap_loc_host_canonicalizations") or []
    if configured and canonicalizations and official_url(
        configured, seed, evidence.extra_hosts
    ):
        # Reuse the regular sitemap parser so explicitly configured publishing
        # host locs are canonicalized before any candidate can be fetched.
        # Keep this repair replay separately bounded from normal discovery.
        from app.services.scraper.sitemap import discover_from_sitemap

        try:
            sitemap_candidates = await asyncio.wait_for(
                discover_from_sitemap(
                    f"{urlsplit(seed).scheme}://{urlsplit(seed).netloc}",
                    sitemap_url=configured,
                    loc_host_canonicalizations=canonicalizations,
                ),
                timeout=min(_SITEMAP_DISCOVERY_TIMEOUT_S, evidence.fetch_seconds),
            )
        except Exception:
            sitemap_candidates = []
        max_candidates = min(
            _SITEMAP_MAX_CANDIDATES,
            max(0, int(discovery.get("max_candidates") or _SITEMAP_MAX_CANDIDATES)),
        )
        sitemap_urls = list(dict.fromkeys(
            item["url"] for item in sitemap_candidates[:_SITEMAP_MAX_CANDIDATES]
            if isinstance(item, dict) and isinstance(item.get("url"), str)
            and official_url(item["url"], seed, evidence.extra_hosts)
            and urlsplit(item["url"]).path.rstrip("/") != urlsplit(seed).path.rstrip("/")
            and urlsplit(item["url"]).path.rstrip("/").rsplit("/", 1)[-1].lower()
                not in {"undergraduate", "postgraduate", "apprenticeships", "research", "course-hero-ctas"}
            and classify_course_candidate(item["url"])[0] != OBVIOUS_NON_DEGREE
            and not re.search(
                r"/(?:apprenticeships?|cpd(?:-and-short-courses)?|short-courses?)/",
                urlsplit(item["url"]).path, re.I,
            )
            and not is_blocked_page(item["url"])[0]
            and not is_intentionally_excluded_course_url(item["url"])
        ))
        filter_patch = {}
        if evidence.ctx.get("provider_failure") and sitemap_urls:
            blocks = discovery.get("block_url_patterns") or []
            kept = [p for p in blocks if p not in {
                "^/courses/", "^/postgraduate/", "^/study/",
            }]
            if kept != blocks:
                filter_patch["block_url_patterns"] = kept or ["(?!)"]
            allows = discovery.get("allow_url_patterns") or []
            if allows and all(p.startswith("^/") for p in allows):
                filter_patch["allow_url_patterns"] = [
                    "^https?://[^/]+" + p[1:] for p in allows
                ]
        sitemap_urls = [
            url for url in sitemap_urls if passes(url, {**discovery, **filter_patch})
        ][:max_candidates]
        if len(sitemap_urls) >= 2:
            return {
                "source": configured,
                "candidates": sitemap_urls,
                "sample": sitemap_urls[:max(0, min(4, (evidence.max_pages - 2) // 2))],
            }

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
            and not is_intentionally_excluded_course_url(urljoin(source, item["url"]))
        ))
        filter_patch = {}
        if evidence.ctx.get("provider_failure") and candidates:
            # Old UI filters sometimes blocked the catalogue root itself and
            # used path-only allows even though runtime evaluates full URLs.
            # Repair only these narrow, recognizable mistakes; preserve all
            # other blocks, detail patterns, must-contain and global guards.
            blocks = discovery.get("block_url_patterns") or []
            kept = [p for p in blocks if p not in {
                "^/courses/", "^/postgraduate/", "^/study/",
            }]
            if kept != blocks:
                filter_patch["block_url_patterns"] = kept or ["(?!)"]
            allows = discovery.get("allow_url_patterns") or []
            if allows and all(p.startswith("^/") for p in allows):
                filter_patch["allow_url_patterns"] = [
                    "^https?://[^/]+" + p[1:] for p in allows
                ]
        candidates = [url for url in candidates if passes(url, {**discovery, **filter_patch})]
        # Never persist an explicitly paginated first page as a whole strategy.
        # Neither a complete sitemap nor an unpaginated listing proves every
        # course is eligible: normal extraction and the floor guard still run.
        if len(candidates) >= 2 and not record.get("has_pagination"):
            result = {"source": source, "candidates": candidates,
                      "sample": candidates[:max(0, min(4, (evidence.max_pages - 2 * (source_index + 1)) // 2))]}
            if filter_patch:
                result["filter_patch"] = filter_patch
            return result
    return {"source": None, "candidates": [], "sample": []}