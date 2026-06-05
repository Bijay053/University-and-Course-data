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


# ── Safety check ──────────────────────────────────────────────────────────────

# Paths that indicate the endpoint is NOT a public course-data API
_SAFETY_BLOCKLIST_PATHS = (
    "/auth", "/login", "/sso", "/oauth", "/token", "/session",
    "/student-portal", "/myaccount", "/my.", "/logout",
    "/forgot", "/reset", "/register", "/signup",
    "/cookie", "/privacy", "/terms", "/404", "/error",
    "student.", "my.", "portal.",
)


def safety_check(candidate: dict) -> tuple[bool, str]:
    """
    Returns ``(is_safe, reason_if_not_safe)``.

    Blocks:
    - Analytics/tracking domains (already filtered at capture, re-checked here)
    - Auth / student-portal paths
    - Appeared on fewer than 2 sample pages (too unreliable)
    - Score < 45 (low confidence)
    - No course-like fields detected
    """
    url = candidate.get("url", "").lower()
    score = candidate.get("score", 0)
    page_count = candidate.get("page_count", 0)
    fields = candidate.get("fields_found", [])

    if any(dom in url for dom in _ANALYTICS_DOMAINS):
        return False, "analytics or tracking endpoint"

    for path in _SAFETY_BLOCKLIST_PATHS:
        if path in url:
            return False, f"auth/portal endpoint (contains '{path}')"

    if page_count < 2:
        return False, (
            f"only appeared on {page_count} sample page — "
            "need ≥2 for safety (endpoint must be consistent across course pages)"
        )

    if score < 45:
        return False, f"confidence score too low ({score} — need ≥45)"

    if not fields:
        return False, "no course-like fields detected in the response"

    return True, ""


# ── Smoke-test helpers ────────────────────────────────────────────────────────

_TITLE_KEYS = (
    "courseName", "courseTitle", "title", "name", "programmeTitle",
    "awardTitle", "label", "shortTitle", "course_name", "programme_title",
)
_FEE_KEYS   = ("fees", "internationalFee", "tuitionFee", "fee", "tuition", "cost")
_DUR_KEYS   = ("duration", "studyDuration", "courseLength", "length", "years", "months")
_INTK_KEYS  = ("intakeMonths", "startDates", "intakes", "commencementDates", "start")
_IELTS_KEYS = ("ielts", "englishRequirements", "entryRequirements", "englishEntry")
_LEVEL_KEYS = ("degreeLevel", "awardLevel", "qualificationLevel", "academicLevel", "level", "qualification")


def _get_field(item: dict, keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = item.get(k)
        if v not in (None, "", [], {}):
            return v
    lower = {k.lower(): v for k, v in item.items()}
    for k in keys:
        v = lower.get(k.lower())
        if v not in (None, "", [], {}):
            return v
    return None


def _extract_course_items(data: Any) -> list[dict]:
    """Extract a list of course-like dicts from various response shapes."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in (
            "results", "items", "courses", "data", "records",
            "programmes", "content", "hits", "docs", "entries",
            "response", "collection", "list",
        ):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        # Fallback: any list-of-dicts value
        for v in data.values():
            if isinstance(v, list) and len(v) > 0 and isinstance(v[0], dict):
                return v
    return []


def _extract_titles(items: list[dict]) -> list[str]:
    out = []
    for item in items:
        v = _get_field(item, _TITLE_KEYS)
        if v and isinstance(v, str) and 3 < len(v) < 200:
            out.append(v.strip())
    return out


def _detect_fields_in_items(items: list[dict]) -> list[str]:
    checks = [
        ("fee",      _FEE_KEYS),
        ("duration", _DUR_KEYS),
        ("intake",   _INTK_KEYS),
        ("english/ielts", _IELTS_KEYS),
        ("level",    _LEVEL_KEYS),
    ]
    return [
        fname
        for fname, keys in checks
        if any(_get_field(item, keys) is not None for item in items)
    ]


async def smoke_test_endpoint(endpoint_url: str, max_items: int = 5) -> dict:
    """
    Directly GET the endpoint and report what course data is present.

    Returns::

        {
            ok: bool,
            courses_found: int,
            sample_titles: list[str],
            fields_detected: list[str],
            error: str | None,
        }
    """
    import httpx  # available in requirements
    try:
        async with httpx.AsyncClient(
            timeout=15,
            follow_redirects=True,
            verify=False,
        ) as client:
            resp = await client.get(
                endpoint_url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "application/json, */*",
                },
            )
        if resp.status_code not in range(200, 300):
            return {
                "ok": False, "courses_found": 0,
                "sample_titles": [], "fields_detected": [],
                "error": f"HTTP {resp.status_code} from endpoint",
            }
        try:
            data = resp.json()
        except Exception:
            return {
                "ok": False, "courses_found": 0,
                "sample_titles": [], "fields_detected": [],
                "error": "Response is not valid JSON",
            }
    except Exception as exc:
        return {
            "ok": False, "courses_found": 0,
            "sample_titles": [], "fields_detected": [],
            "error": str(exc)[:200],
        }

    items = _extract_course_items(data)

    if not items:
        # Maybe a single course object rather than a list
        if isinstance(data, dict) and _get_field(data, _TITLE_KEYS):
            return {
                "ok": True, "courses_found": 1,
                "sample_titles": [str(_get_field(data, _TITLE_KEYS))[:120]],
                "fields_detected": _detect_fields_in_items([data]),
                "error": None,
            }
        return {
            "ok": False, "courses_found": 0,
            "sample_titles": [], "fields_detected": [],
            "error": "Response contains no recognisable course items (check items_key in YAML)",
        }

    return {
        "ok": True,
        "courses_found": len(items),
        "sample_titles": _extract_titles(items[:max_items]),
        "fields_detected": _detect_fields_in_items(items[:10]),
        "error": None,
    }


# ── YAML injection ────────────────────────────────────────────────────────────

def _deep_merge(base: dict, patch: dict) -> dict:
    result = dict(base)
    for k, v in patch.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def inject_api_config(
    current_yaml: str,
    candidate: dict,
    uni_hostname: str,
    apply_date: str,
) -> str:
    """
    Merge the discovered API config into the existing YAML for this university.

    Strategy: parse → deep-merge → dump with an explanatory header comment.
    The previous file content is preserved in scraper_config_history via the
    caller; operators can roll back using the config editor history panel.
    """
    import yaml as _yaml

    try:
        existing: dict = _yaml.safe_load(current_yaml) or {} if current_yaml.strip() else {}
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}

    if candidate.get("is_paginated"):
        patch: dict = {
            "discovery": {
                "api": {
                    "endpoint": candidate["url"],
                    "method": candidate.get("method", "GET"),
                    "pagination_param": candidate.get("pagination_param") or "page",
                    "page_size": 20,
                    # Operator must verify this key matches the actual response shape
                    "items_key": "results",
                }
            }
        }
    else:
        patch = {
            "extraction": {
                "api": {
                    "url_template": candidate["url"],
                    "method": candidate.get("method", "GET"),
                }
            }
        }

    merged = _deep_merge(existing, patch)

    header = (
        f"# Auto-discovered API config — applied {apply_date} via portal\n"
        f"# Hostname: {uni_hostname}\n"
        f"# Score: {candidate.get('score', 0)}  Confidence: {candidate.get('confidence', '?')}\n"
        f"# Fields detected: {', '.join(candidate.get('fields_found', []) or [])}\n"
        f"# NOTE: review items_key / pagination_param before triggering a full scrape.\n"
        f"\n"
    )
    body = _yaml.dump(
        merged,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=120,
    )
    return header + body
