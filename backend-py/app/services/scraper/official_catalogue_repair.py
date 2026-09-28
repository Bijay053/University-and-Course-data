"""Bounded official catalogue replay shared by repair and normal discovery."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit

from app.services.scraper.ai_repair_live import official_url, passes
from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url

NEXT_ACTION = "Provide an official university course catalogue or course page URL and try Repair discovery again."
_SITEMAP_DISCOVERY_TIMEOUT_S = 20
_SITEMAP_MAX_CANDIDATES = 200
_SITEMAP_NAVIGATION_TERMINAL = re.compile(
    r"(?:^|/)(?:subject-areas?|benefits-of-postgraduate-study|"
    r"conversion-courses|undergraduate|postgraduate|courses?|programmes?|"
    r"programs?|degrees?|study)/?$",
    re.I,
)
_SITEMAP_NAVIGATION_INTERMEDIATE = re.compile(
    r"/(?:subject-areas?|benefits-of-postgraduate-study|"
    r"conversion-courses|clearing|how-to-apply)/",
    re.I,
)


def _sitemap_candidate_rank(url: str, title: str = "") -> tuple[int, int, int, str]:
    """Rank sitemap URLs without excluding otherwise eligible unknown pages."""
    from app.services.scraper.guards import classify_course_candidate, LIKELY_DEGREE

    path = urlsplit(url).path.rstrip("/").lower()
    parts = [part for part in path.split("/") if part]
    outcome, _reason = classify_course_candidate(url, title)
    detail = bool(
        re.search(r"/(?:courses?|programmes?|programs?)/.+", path)
        or re.search(r"/study/(?:undergraduate|postgraduate)/[^/]+$", path)
    )
    navigation = bool(
        _SITEMAP_NAVIGATION_TERMINAL.search(path)
        or _SITEMAP_NAVIGATION_INTERMEDIATE.search(path + "/")
    )

    # Lower stratum is preferred: award-bearing details, then other detail
    # pages, then unknown eligible URLs. Category/navigation pages remain in
    # the candidate set, but are last-resort samples.
    if navigation:
        stratum = 4
    elif outcome == LIKELY_DEGREE and detail:
        stratum = 0
    elif detail:
        stratum = 1
    elif outcome == LIKELY_DEGREE:
        stratum = 2
    else:
        stratum = 3
    return (stratum, -len(parts), len(path), url)


def _bounded_sitemap_urls(urls: list[str], titles: dict[str, str], cap: int) -> list[str]:
    """Apply the output cap after ranking the complete, filtered sitemap set."""
    if len(urls) <= cap:
        return urls
    selected = set(sorted(
        urls,
        key=lambda url: _sitemap_candidate_rank(url, titles.get(url, "")),
    )[:cap])
    # Keep sitemap order among retained candidates for predictable discovery.
    return [url for url in urls if url in selected]


def _sitemap_sample(urls: list[str], titles: dict[str, str], sample_cap: int) -> list[str]:
    """Choose a deterministic, likelihood-stratified sample spread across URLs."""
    if sample_cap <= 0 or not urls:
        return []
    strata: dict[int, list[str]] = {index: [] for index in range(5)}
    for url in urls:
        strata[_sitemap_candidate_rank(url, titles.get(url, ""))[0]].append(url)

    sample: list[str] = []
    for stratum in strata.values():
        if len(sample) >= sample_cap:
            break
        slots = min(sample_cap - len(sample), len(stratum))
        # Evenly-spaced representatives prevent a prefix of sitemap
        # navigation URLs from monopolizing the live evidence probes.
        for slot in range(slots):
            position = min(len(stratum) - 1, ((2 * slot + 1) * len(stratum)) // (2 * slots))
            sample.append(stratum[position])
    return sample


async def discover_official_catalogue(evidence) -> dict:
    from app.services.scraper.guards import classify_course_candidate, OBVIOUS_NON_DEGREE, is_blocked_page
    seed = evidence.ctx["scrape_url"]
    discovery = evidence.config.discovery.model_dump()
    configured = discovery.get("sitemap_url")
    canonicalizations = discovery.get("sitemap_loc_host_canonicalizations") or []
    try:
        configured_cap = int(discovery.get("max_candidates", _SITEMAP_MAX_CANDIDATES))
    except (TypeError, ValueError):
        configured_cap = _SITEMAP_MAX_CANDIDATES
    max_candidates = min(_SITEMAP_MAX_CANDIDATES, max(0, configured_cap))
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
        # Filter and deduplicate the entire result before applying our output
        # bound. In particular, a sitemap's first 200 rows may be mostly
        # navigation, with course detail URLs appearing later.
        sitemap_urls = []
        sitemap_titles = {}
        seen_sitemap_urls = set()
        for item in sitemap_candidates:
            if not isinstance(item, dict) or not isinstance(item.get("url"), str):
                continue
            url = item["url"]
            if (
                url in seen_sitemap_urls
                or not official_url(url, seed, evidence.extra_hosts)
                or urlsplit(url).path.rstrip("/") == urlsplit(seed).path.rstrip("/")
                or urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1].lower()
                    in {"undergraduate", "postgraduate", "apprenticeships", "research", "course-hero-ctas"}
                or classify_course_candidate(url, item.get("name", ""))[0] == OBVIOUS_NON_DEGREE
                or re.search(
                    r"/(?:apprenticeships?|cpd(?:-and-short-courses)?|short-courses?)/",
                    urlsplit(url).path, re.I,
                )
                or is_blocked_page(url)[0]
                or is_intentionally_excluded_course_url(url)
            ):
                continue
            seen_sitemap_urls.add(url)
            sitemap_urls.append(url)
            sitemap_titles[url] = item.get("name", "")
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
        ]
        sitemap_urls = _bounded_sitemap_urls(sitemap_urls, sitemap_titles, max_candidates)
        if max_candidates > 0 and len(sitemap_urls) >= min(2, max_candidates):
            return {
                "source": configured,
                "candidates": sitemap_urls,
                "sample": _sitemap_sample(
                    sitemap_urls, sitemap_titles,
                    max(0, min(4, (evidence.max_pages - 2) // 2)),
                ),
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
        candidates = candidates[:max_candidates]
        # Never persist an explicitly paginated first page as a whole strategy.
        # Neither a complete sitemap nor an unpaginated listing proves every
        # course is eligible: normal extraction and the floor guard still run.
        if (
            max_candidates > 0
            and len(candidates) >= min(2, max_candidates)
            and not record.get("has_pagination")
        ):
            result = {"source": source, "candidates": candidates,
                      "sample": candidates[:max(0, min(4, (evidence.max_pages - 2 * (source_index + 1)) // 2))]}
            if filter_patch:
                result["filter_patch"] = filter_patch
            return result
    return {"source": None, "candidates": [], "sample": []}