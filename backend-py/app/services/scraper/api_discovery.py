"""
Auto API Discovery
==================
Opens sample course URLs in a headless Playwright browser, records all
JSON XHR/fetch responses, scores them as potential course-data API
endpoints, and returns ranked candidates together with a ready-made YAML
config snippet.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

# ── Blocklist: analytics / tracking / ad domains ──────────────────────────────
_ANALYTICS_DOMAINS = (
    "google-analytics", "googletagmanager.com", "facebook.com", "fbcdn",
    "clarity.ms", "hotjar.com", "newrelic.com", "nr-data.net", "sentry.io",
    "doubleclick.net", "segment.io", "mixpanel.com", "amplitude.com",
    "intercom.io", "zendesk.com", "pardot.com", "hubspot.com", "mparticle",
    "tealium", "optimizely", "fullstory", "logrocket", "mouseflow",
    "crazyegg.com", "pingdom.com", "appdynamics", "dynatrace", "datadog",
    "rollbar.com", "bugsnag.com", "appinsights", "bat.bing.com",
    "ads.linkedin", "connect.facebook", "chartbeat", "parsely.com",
    "beacon", "pixel", "clickstream", "analytics/",
)

# ── URL path patterns that suggest a course-data API ─────────────────────────
_API_PATH_HINTS = (
    "/api/", "/_api/", "/graphql", "/_headless/", "/_next/data/",
    "/wp-json/", "/rest/", "/courses/", "/course/", "/programme/",
    "/contensis/", "/delivery/", "/cms/", "/v1/", "/v2/", "/v3/",
    "/services/", "/data/", "catalog", "catalogue",
    "/content/", "/search/", "/umbraco/api/", "/sitecore/api/",
    "courseinfo", "courselist", "coursedetail", "/programmes/",
)

# ── JSON keys that strongly suggest course data ───────────────────────────────
_COURSE_JSON_KEYS: set[str] = {
    "courseName", "courseTitle", "title", "name", "programmeTitle", "awardTitle",
    "fees", "internationalFee", "tuitionFee", "fee", "tuition",
    "duration", "studyDuration", "courseLength",
    "studyMode", "modeOfStudy", "deliveryMode",
    "intakeMonths", "startDates", "intakes", "commencementDates",
    "ielts", "englishRequirements", "entryRequirements", "academicEntry",
    "degreeLevel", "awardLevel", "qualificationLevel", "academicLevel",
    "modules", "subjects", "units", "curriculum",
    "campus", "location", "campuses",
}

# ── Free-text keywords that suggest course data ───────────────────────────────
_FIELD_KEYWORDS: dict[str, list[str]] = {
    "fee":      ["fee", "tuition", "cost", "£", "$", "aud", "usd", "gbp", "nzd"],
    "duration": ["duration", " years", " months", "full-time", "part-time"],
    "intake":   ["intake", "start date", "january", "september", "october"],
    "campus":   ["campus", "location", "city", "online", "distance"],
    "award":    ["bachelor", "master", "phd", "degree", "diploma", "certificate",
                 "postgraduate", "undergraduate"],
    "ielts":    ["ielts", "english requirement", "toefl", "pte", "language"],
    "modules":  ["module", "unit", "subject", "curriculum", "syllabus"],
}

# Canterbury-specific CMS path hints (bonus points)
_CONTENSIS_HINTS = ("contensis", "/delivery/", "/_api/", "cms", "headless")


@dataclass
class RawCapture:
    url: str
    method: str
    status: int
    content_type: str
    size: int
    body_sample: str


@dataclass
class ApiCandidate:
    url: str
    method: str = "GET"
    score: int = 0
    confidence: str = "low"
    content_type: str = "application/json"
    size_bytes: int = 0
    fields_found: list[str] = field(default_factory=list)
    page_count: int = 1
    sample_keys: list[str] = field(default_factory=list)
    suggested_yaml: str = ""
    is_paginated: bool = False
    pagination_param: str = ""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_blocked(url: str) -> bool:
    url_l = url.lower()
    return any(dom in url_l for dom in _ANALYTICS_DOMAINS)


def _extract_json_keys(body: str, depth: int = 0, found: set[str] | None = None) -> list[str]:
    if found is None:
        found = set()
    try:
        obj = json.loads(body)
    except Exception:
        return list(found)[:20]
    _walk(obj, depth, found)
    return list(found)[:20]


def _walk(obj: Any, depth: int, found: set[str]) -> None:
    if depth > 3 or len(found) >= 20:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            found.add(k)
            _walk(v, depth + 1, found)
    elif isinstance(obj, list):
        for item in obj[:3]:
            _walk(item, depth + 1, found)


def _score_capture(cap: RawCapture, course_hints: list[str]) -> tuple[int, list[str]]:
    score = 0
    fields_found: list[str] = []
    url_l = cap.url.lower()
    body_l = cap.body_sample.lower()

    # ── API path hints (+12 per hit, max 1) ───────────────────────────────────
    if any(h in url_l for h in _API_PATH_HINTS):
        score += 12

    # ── CMS/Contensis-specific hints (+10) ────────────────────────────────────
    if any(h in url_l for h in _CONTENSIS_HINTS):
        score += 10

    # ── Contains known course title (+25) ─────────────────────────────────────
    if any(h.lower()[:25] in body_l for h in course_hints if h):
        score += 25

    # ── Per-field keyword coverage (+8 each, max 7 fields = 56) ──────────────
    for fname, keywords in _FIELD_KEYWORDS.items():
        if any(kw in body_l for kw in keywords):
            score += 8
            fields_found.append(fname)

    # ── JSON key coverage (+3 each, max 30 keys × 3 = 90) ────────────────────
    try:
        keys = _extract_json_keys(cap.body_sample)
        hits = _COURSE_JSON_KEYS & set(keys)
        score += min(len(hits) * 3, 30)
        # If any key overlap found, merge into fields_found
        if hits and "fee" not in fields_found and any("fee" in k.lower() for k in hits):
            fields_found.append("fee")
    except Exception:
        pass

    # ── Response size sanity (+5) ─────────────────────────────────────────────
    if 500 < cap.size < 5_000_000:
        score += 5

    return score, list(dict.fromkeys(fields_found))  # preserve insertion order, dedupe


def _make_yaml(candidate: ApiCandidate, uni_hostname: str) -> str:
    url_path = urlparse(candidate.url).path
    lines = [
        f"# Auto-discovered API endpoint (score={candidate.score}, confidence={candidate.confidence})",
        f"# Found while intercepting network traffic on {uni_hostname}",
        "# Review and adjust before enabling.",
        "",
    ]
    if "graphql" in candidate.url.lower():
        lines += [
            "extraction:",
            "  api:",
            f"    type: graphql",
            f"    endpoint: \"{candidate.url}\"",
            "    query: |",
            "      # TODO: paste the GraphQL query from DevTools here",
            "",
        ]
    elif candidate.is_paginated:
        lines += [
            "discovery:",
            "  api:",
            f"    endpoint: \"{candidate.url}\"",
            f"    method: {candidate.method}",
            f"    pagination_param: \"{candidate.pagination_param or 'page'}\"",
            "    page_size: 20",
            "    # The response must contain a list field — adjust 'items_key' below",
            "    items_key: \"results\"",
            "",
            "extraction:",
            "  source: api  # skip per-page browser fetch; use API response directly",
            "",
        ]
    else:
        lines += [
            "extraction:",
            "  api:",
            f"    url_template: \"{candidate.url}\"",
            f"    method: {candidate.method}",
            "    # Replace the course-specific segment above with {{course_id}} if needed",
            f"    # e.g. https://{uni_hostname}{url_path.rsplit('/', 1)[0]}/{{course_id}}",
            "",
        ]
    if candidate.fields_found:
        lines += [
            f"  # Fields detected in response: {', '.join(candidate.fields_found)}",
        ]
    return "\n".join(lines)


def _detect_pagination(url: str, body: str) -> tuple[bool, str]:
    params = re.findall(r"[?&](page|offset|start|from|skip|p)=", url.lower())
    if params:
        return True, params[0]
    try:
        obj = json.loads(body)
        if isinstance(obj, dict):
            pagination_keys = {"page", "offset", "totalPages", "total_pages", "nextPage", "next", "hasMore", "hasNextPage"}
            if pagination_keys & set(obj.keys()):
                return True, "page"
    except Exception:
        pass
    return False, ""


# ── Core Playwright capture ───────────────────────────────────────────────────

async def _capture_network_for_url(
    url: str,
    timeout_ms: int = 25_000,
) -> list[RawCapture]:
    try:
        from playwright.async_api import async_playwright  # type: ignore
    except ImportError:
        log.warning("api_discovery: playwright not installed")
        return []

    captured: list[RawCapture] = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                ignore_https_errors=True,
            )
            page = await context.new_page()

            async def on_response(response) -> None:  # type: ignore
                try:
                    resp_url = response.url
                    if _is_blocked(resp_url):
                        return
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    if not (200 <= response.status < 300):
                        return
                    body = await response.body()
                    if len(body) < 200:
                        return
                    captured.append(RawCapture(
                        url=resp_url,
                        method=response.request.method,
                        status=response.status,
                        content_type=ct,
                        size=len(body),
                        body_sample=body[:30_000].decode("utf-8", errors="replace"),
                    ))
                except Exception:
                    pass

            page.on("response", on_response)

            try:
                await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            except Exception:
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                    await asyncio.sleep(2.5)
                except Exception:
                    log.debug("api_discovery: could not load %s", url)

            await context.close()
            await browser.close()

    except Exception as exc:
        log.warning("api_discovery: playwright session failed for %s: %s", url, exc)

    return captured


# ── Public entry point ────────────────────────────────────────────────────────

async def discover_api_endpoints(
    sample_urls: list[str],
    uni_hostname: str = "",
    course_hints: list[str] | None = None,
) -> dict[str, Any]:
    """
    Run API discovery against up to 3 sample URLs.

    Returns a dict::

        {
            "candidates": [...],   # ranked ApiCandidate dicts
            "sample_urls": [...],
            "uni_hostname": "...",
        }
    """
    course_hints = course_hints or []
    urls_to_probe = sample_urls[:3]

    if not urls_to_probe:
        return {"candidates": [], "sample_urls": [], "uni_hostname": uni_hostname}

    log.info("api_discovery: probing %d URL(s) for %s", len(urls_to_probe), uni_hostname)

    # ── Capture network requests for each sample URL ──────────────────────────
    all_captures: list[list[RawCapture]] = []
    for url in urls_to_probe:
        caps = await _capture_network_for_url(url)
        log.info("api_discovery: %s → %d JSON responses captured", url, len(caps))
        all_captures.append(caps)

    # ── Aggregate: group by normalised endpoint URL ───────────────────────────
    # Strip query-string params that vary per course (IDs, tokens) but keep the path
    def _normalise(url: str) -> str:
        p = urlparse(url)
        # Drop per-page dynamic params; keep known stable params
        return f"{p.scheme}://{p.netloc}{p.path}"

    aggregated: dict[str, dict] = {}  # normalised_url → {cap, page_count, score, fields}

    for page_idx, captures in enumerate(all_captures):
        for cap in captures:
            norm = _normalise(cap.url)
            score, fields = _score_capture(cap, course_hints)
            if score < 5:
                continue
            if norm not in aggregated:
                aggregated[norm] = {
                    "cap": cap,
                    "page_count": 0,
                    "score": score,
                    "fields": set(fields),
                }
            agg = aggregated[norm]
            agg["page_count"] += 1
            agg["score"] = max(agg["score"], score)
            agg["fields"].update(fields)

    # ── Bonus: +20 for each additional sample page the endpoint appeared on ───
    for norm, agg in aggregated.items():
        extra = max(0, agg["page_count"] - 1)
        agg["score"] += extra * 20

    # ── Sort by score descending ──────────────────────────────────────────────
    ranked = sorted(aggregated.values(), key=lambda x: x["score"], reverse=True)[:5]

    candidates: list[dict] = []
    for agg in ranked:
        cap: RawCapture = agg["cap"]
        score = agg["score"]
        confidence = "high" if score >= 80 else "medium" if score >= 45 else "low"
        is_paged, pag_param = _detect_pagination(cap.url, cap.body_sample)
        sample_keys = _extract_json_keys(cap.body_sample)

        cand = ApiCandidate(
            url=cap.url,
            method=cap.method,
            score=score,
            confidence=confidence,
            content_type=cap.content_type,
            size_bytes=cap.size,
            fields_found=list(agg["fields"]),
            page_count=agg["page_count"],
            sample_keys=sample_keys,
            is_paginated=is_paged,
            pagination_param=pag_param,
        )
        cand.suggested_yaml = _make_yaml(cand, uni_hostname)
        candidates.append(cand.__dict__)

    log.info(
        "api_discovery: done — %d candidates for %s (top score=%s)",
        len(candidates),
        uni_hostname,
        candidates[0]["score"] if candidates else "n/a",
    )

    return {
        "candidates": candidates,
        "sample_urls": urls_to_probe,
        "uni_hostname": uni_hostname,
    }
