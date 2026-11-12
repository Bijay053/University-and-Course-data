"""Bounded official catalogue replay shared by repair and normal discovery."""
import asyncio
import re
from urllib.parse import urljoin, urlsplit

from app.services.scraper.ai_repair_live import official_url, passes
from app.services.scraper.auto_repair_candidates import is_intentionally_excluded_course_url
from app.services.scraper.repair_source_urls import is_catalogue_source, catalogue_allow_proposal, programme_identity

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


def discovery_patches(fallback: dict) -> list[dict]:
    """Keep directory plans within the existing three-change safety limit."""
    if not fallback.get("source"):
        return []
    from app.services.scraper.directory_catalogue import directory_roots
    extra = fallback.get("filter_patch") or {}
    roots = directory_roots(extra.get("seed_urls") or [])
    directory = (
        fallback.get("coverage_scope") == "configured_subject_directories"
        and bool(roots) and fallback["source"] == roots[0]
    )
    fields = {"official_catalogue_fallback": True}
    if not directory:
        fields["sitemap_url"] = fallback["source"]
    fields.update(extra)
    return [{"section": "discovery", "field": field, "value": value}
            for field, value in fields.items()]


def _catalogue_hints(record: dict, source: str, seed: str, extra_hosts) -> list[str]:
    """Follow university-published catalogue navigation, never search queries."""
    from app.services.scraper.guards import _sanitized_visible_document
    html = record.get("_source_html") or record.get("html") or ""
    if not html or record.get("classification") in {"sitemap", "robots"}:
        return []
    soup = _sanitized_visible_document(html)
    hints = []
    for anchor in soup.select("a[href]"):
        target = urljoin(source, anchor.get("href", "")).split("#", 1)[0]
        parsed = urlsplit(target)
        if (
            parsed.query or not official_url(target, seed, extra_hosts)
            or not is_catalogue_source(target)
            or target in hints
        ):
            continue
        hints.append(target)
    return hints


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
    strata: dict[int, list[str]] = {index: [] for index in range(-1, 5)}
    identities = set()
    for url in urls:
        identity = programme_identity(url)
        if identity in identities:
            continue
        identities.add(identity)
        rank = _sitemap_candidate_rank(url, titles.get(url, ""))[0]
        # Explicit degree awards are safer template probes than ambiguous
        # professional certificates. This is a sample preference, not an
        # exclusion or a claim that every remaining link is a degree.
        if rank < 4 and re.search(
            r"\b(?:BSc|BEng|BAcc|BA|BS|BBA|BEd|BCom|BTech|BSW|BFA|LLB|"
            r"MSc|MEng|MA|MS|MBA|MRes|MEd|MPhil|MCom|MSW|MFA|LLM|PhD|DPhil|"
            r"PGCert|PGDip|Bachelor|Master|Doctor)\b",
            titles.get(url, "") + " " + urlsplit(url).path.replace("-", " "), re.I,
        ):
            rank = -1
        strata[rank].append(url)

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


def _repair_sample(urls, titles, cap, discovery, patch):
    from app.services.scraper.ai_repair_live import passes
    if patch.get("allow_url_patterns"):
        gained = [url for url in urls if not passes(url, discovery)]
        sample = _sitemap_sample(gained, titles, cap)
        if len(sample) >= 2:
            return sample
    return _sitemap_sample(urls, titles, cap)


def _sample_alternatives(urls, titles, sample):
    identities = {programme_identity(url) for url in sample}
    return _sitemap_sample([url for url in urls if programme_identity(url) not in identities], titles, 2)


async def discover_official_catalogue(evidence) -> dict:
    from app.services.scraper.guards import classify_course_candidate, OBVIOUS_NON_DEGREE, is_blocked_page
    seed = evidence.ctx["scrape_url"]
    sample_cap = 2 if (
        evidence.ctx.get("catalogue_recovery") and not evidence.ctx.get("provider_failure")
    ) else 4
    discovery = evidence.config.discovery.model_dump()
    configured = discovery.get("sitemap_url")
    canonicalizations = discovery.get("sitemap_loc_host_canonicalizations") or []
    try:
        configured_cap = int(discovery.get("max_candidates", _SITEMAP_MAX_CANDIDATES))
    except (TypeError, ValueError):
        configured_cap = _SITEMAP_MAX_CANDIDATES
    max_candidates = min(_SITEMAP_MAX_CANDIDATES, max(0, configured_cap))
    from app.services.scraper.directory_catalogue import (
        collect_directory_catalogue, directory_roots,
    )
    roots = (
        getattr(evidence, "directory_roots", [])
        or directory_roots(discovery.get("seed_urls") or [])
    )
    if roots:
        async def read_directory(url, _config, _timeout):
            record = await evidence.fetch(url)
            body = record.get("_source_html")
            if body:
                # Directory pages are navigation evidence even if a generic
                # inspector sees degree words in several result cards.
                record["classification"] = "robots" if urlsplit(url).path == "/robots.txt" else "listing"
                record["reference_only"] = True
                record["fields"] = []
                record["owned_html"] = ""
            return (body, "" if body else record.get("classification", "source_unavailable"),
                    record.get("reason", ""))
        directory = await collect_directory_catalogue(
            roots, seed, evidence.config, fetch=read_directory,
            max_pages=max(0, evidence.max_pages // 2 - 2),
            max_candidates=max_candidates,
            max_seconds=max(0, evidence.max_seconds / 2),
        )
        urls, titles = [], {}
        for item in directory.candidates:
            url, title = item["url"], item["name"]
            if (
                classify_course_candidate(url, title)[0] != OBVIOUS_NON_DEGREE
                and not is_blocked_page(url)[0]
                and not is_intentionally_excluded_course_url(url)
                and passes(url, discovery)
            ):
                urls.append(url)
                titles[url] = title
        accepted = directory.complete and len(urls) >= 2
        preferred_samples = [
            url for url in urls
            if directory.international_sample_hints.get(url.rstrip("/"))
        ]
        result = {
            "source": roots[0] if accepted else None,
            "candidates": urls if accepted else [],
            "sample": _sitemap_sample(
                preferred_samples if len(preferred_samples) >= 2 else urls,
                titles, min(2, sample_cap),
            ),
            "coverage_scope": directory.scope,
            "university_coverage": "unverified",
            "directory_pages": directory.pages,
        }
        sample_pool = preferred_samples if len(preferred_samples) >= 2 else urls
        alternative_pool = [
            url for url in _sitemap_sample(sample_pool, titles, 4)
            if url not in result["sample"]
        ]
        result["sample_alternatives"] = _sitemap_sample(alternative_pool, titles, 2)
        if result["source"]:
            result["filter_patch"] = {
                "seed_urls": roots,
                "bfs_page_budget": max(
                    len(directory.pages) + len({urlsplit(url).netloc for url in roots}),
                    discovery.get("bfs_page_budget") or 25,
                ),
            }
        else:
            result.update(
                partial_candidates=urls,
                partial_sources=[{"url": row["url"], "reason": row["reason"],
                                  "candidate_count": len(urls)}
                                 for row in directory.failures],
                remaining_sources=directory.remaining,
            )
        return result
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
        if evidence.ctx.get("catalogue_recovery"):
            filter_patch.update(catalogue_allow_proposal(sitemap_urls, {**discovery, **filter_patch}))
        sitemap_urls = [
            url for url in sitemap_urls if passes(url, {**discovery, **filter_patch})
        ]
        sitemap_urls = _bounded_sitemap_urls(sitemap_urls, sitemap_titles, max_candidates)
        if max_candidates > 0 and len(sitemap_urls) >= min(2, max_candidates):
            result = {
                "source": configured,
                "candidates": sitemap_urls,
                "sample": _repair_sample(
                    sitemap_urls, sitemap_titles,
                    max(0, min(sample_cap, (evidence.max_pages - 2) // 2)),
                    discovery, filter_patch,
                ),
            }
            if filter_patch:
                result["filter_patch"] = filter_patch
            result["sample_alternatives"] = _sample_alternatives(sitemap_urls, sitemap_titles, result["sample"])
            return result

    sources = list(dict.fromkeys(filter(None, [
        *(url for url in evidence.ctx.get("requested_course_urls", []) if is_catalogue_source(url)),
        configured, *(discovery.get("seed_urls") or []), urljoin(seed, "/sitemap.xml"), seed,
    ])))[:3]
    partial_sources = []
    partial_candidates = {}
    for source_index, source in enumerate(sources):
        if not official_url(source, seed, evidence.extra_hosts):
            continue
        record = await evidence.fetch(source)
        for hint in _catalogue_hints(record, source, seed, evidence.extra_hosts):
            if len(sources) < 3 and hint not in sources:
                sources.append(hint)
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
        if evidence.ctx.get("catalogue_recovery"):
            filter_patch.update(catalogue_allow_proposal(candidates, {**discovery, **filter_patch}))
        candidates = [url for url in candidates if passes(url, {**discovery, **filter_patch})]
        candidates = candidates[:max_candidates]
        if candidates and record.get("has_pagination"):
            partial_sources.append({"url": source, "candidate_count": len(candidates),
                                    "reason": "Pagination not verified; not a complete catalogue"})
            for link in record.get("links") or []:
                if isinstance(link, dict) and link.get("url") in candidates:
                    partial_candidates.setdefault(link["url"], link.get("name", ""))
        # Never persist an explicitly paginated first page as a whole strategy.
        # Neither a complete sitemap nor an unpaginated listing proves every
        # course is eligible: normal extraction and the floor guard still run.
        if (
            max_candidates > 0
            and len(candidates) >= min(2, max_candidates)
            and not record.get("has_pagination")
        ):
            source_titles = {link["url"]: link.get("name", "") for link in record.get("links", [])
                             if isinstance(link, dict) and link.get("url")}
            result = {"source": source, "candidates": candidates,
                      "sample": _repair_sample(candidates, source_titles,
                          max(0, min(sample_cap, (evidence.max_pages - 2 * (source_index + 1)) // 2)),
                          discovery, filter_patch)}
            if filter_patch:
                result["filter_patch"] = filter_patch
            result["sample_alternatives"] = _sample_alternatives(candidates, source_titles, result["sample"])
            return result
    result = {"source": None, "candidates": [], "sample": []}
    if partial_candidates:
        result.update(
            partial_sources=partial_sources,
            partial_candidates=list(partial_candidates),
            sample=_sitemap_sample(list(partial_candidates), partial_candidates,
                                   min(sample_cap, max(0, (evidence.max_pages - 6) // 2))),
        )
    return result